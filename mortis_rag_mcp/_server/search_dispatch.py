"""检索路由分发与参数规范化（v0.8.0 自 server.py 提取）。

职责：
- 集中处理 kb_search 的入参规范化（top_k, use_rerank, group_by_vault, dedupe, preview, filters）；
- 目标库解析与判定：单库直通、默认单库、Scoped 定向多库或全局盲搜；
- 守护冷启动状态：单库初始化状态机检查（try_sync_with_guard 友好响应）；
- 活实例规则：接收 live 的 VaultMcpServer 实例。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from .fanout import fanout_search, apply_budget

if TYPE_CHECKING:
    from mortis_rag_mcp.server import VaultMcpServer


def _parse_budget_bytes(value: Any) -> int | None:
    """防御式解析 budget_bytes：夹取到 [500, 100000]，非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        fval = float(value)
        import math
        if math.isnan(fval) or math.isinf(fval):
            return None
        val = int(fval)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(500, min(100000, val))


def _parse_exact_terms(value: Any) -> list[str] | None:
    """防御式解析 exact_terms：去空去重、上限 8 条、每条 ≤100 字符。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return None
    terms: list[str] = []
    seen: set[str] = set()
    for item in items:
        # bool 在 Python 中是 int 子类，必须显式排除布尔值与 None
        if item is None or isinstance(item, bool) or not isinstance(item, (str, int, float)):
            continue
        s = str(item).strip()
        if not s:
            continue
        s = s[:100]
        s_lower = s.lower()
        if s_lower not in seen:
            seen.add(s_lower)
            terms.append(s)
        if len(terms) >= 8:
            break
    return terms or None


def _search_single_vault(
    server: VaultMcpServer,
    indexer: Any,
    query: str,
    top_k: int,
    use_rerank: bool,
    search_filters: Any,
    dedupe: bool,
    exact_terms: list[str] | None,
    preview: bool,
    query_tokens: list[str] | None,
    budget_bytes: int | None,
) -> dict[str, Any]:
    if not Path(indexer.vault_path).is_dir():
        with indexer._cache_lock:
            indexer._chunks = {}
            indexer._signatures = {}
        return {"chunks": []}
    indexer.request_refresh()
    r_status = indexer.refresh_status()
    is_cold = (
        indexer.last_sync is None
        and not getattr(indexer, "_chunks_cache_loaded", False)
        and len(indexer._chunks) == 0
    )
    if is_cold:
        return {
            "status": "indexing",
            "message": "知识库正在后台进行首次初始化构建与嵌入计算，请稍候...",
            "progress": r_status["indexing_progress"],
            "retry_after": 3,
            "chunks": [],
        }

    results = indexer.search(
        query,
        top_k,
        bool(use_rerank),
        filters=search_filters,
        dedupe=bool(dedupe),
        exact_terms=exact_terms,
    )
    res_dict: dict[str, Any] = {"chunks": [chunk.to_dict(preview=preview, query_tokens=query_tokens) for chunk in results]}
    if r_status["indexing_in_progress"]:
        res_dict["indexing_in_progress"] = True
        res_dict["indexing_progress"] = r_status["indexing_progress"]
    if r_status.get("refresh_error"):
        res_dict["indexing_error"] = r_status["refresh_error"]
    if budget_bytes is not None:
        res_dict = apply_budget(res_dict, budget_bytes, orig_offset=search_filters.offset, preview=preview)
    return res_dict


def dispatch_search(server: VaultMcpServer, arguments: dict[str, Any]) -> dict[str, Any]:
    """处理 kb_search 请求分发。"""
    from ..server import _parse_top_k, _search_filter, _tokenize_query

    targets = server._parse_vault_targets(arguments)
    query = str(arguments.get("query", ""))
    top_k = _parse_top_k(arguments.get("top_k", 10), server.config.max_top_k)
    use_rerank = arguments.get("use_rerank", True)
    if isinstance(use_rerank, str):
        use_rerank = use_rerank.strip().lower() in {"1", "true", "yes", "on"}
    group_by_vault = arguments.get("group_by_vault", False)
    if isinstance(group_by_vault, str):
        group_by_vault = group_by_vault.strip().lower() in {"1", "true", "yes", "on"}
    dedupe = arguments.get("dedupe", True)
    if isinstance(dedupe, str):
        dedupe = dedupe.strip().lower() not in {"0", "false", "no", "off"}
    preview_val = arguments.get("preview", False)
    if isinstance(preview_val, str):
        preview = preview_val.strip().lower() in {"1", "true", "yes", "on"}
    else:
        preview = bool(preview_val)
    if arguments.get("mode") == "preview":
        preview = True

    search_filters = _search_filter(arguments, server.config.max_top_k)
    query_tokens = _tokenize_query(query)
    budget_bytes = _parse_budget_bytes(arguments.get("budget_bytes"))
    exact_terms = _parse_exact_terms(arguments.get("exact_terms"))

    # 单库检索通道（传入单个目标）
    if len(targets) == 1:
        resolved_single = server._resolve_vault_path(targets[0])
        indexer = server._indexer_for({"vault_path": resolved_single})
        return _search_single_vault(
            server,
            indexer,
            query=query,
            top_k=top_k,
            use_rerank=use_rerank,
            search_filters=search_filters,
            dedupe=dedupe,
            exact_terms=exact_terms,
            preview=preview,
            query_tokens=query_tokens,
            budget_bytes=budget_bytes,
        )

    # 未指定目标时的默认单库处理
    if not targets:
        entries = server.registry.load()
        if len(entries) == 1 and entries[0].solo:
            raise ValueError(
                f"vault '{entries[0].name}' is solo (excluded from global search); "
                "pass an explicit vault_path to search it"
            )
        if len(entries) == 1:
            indexer = server._indexer_for({"vault_path": entries[0].path})
            return _search_single_vault(
                server,
                indexer,
                query=query,
                top_k=top_k,
                use_rerank=use_rerank,
                search_filters=search_filters,
                dedupe=dedupe,
                exact_terms=exact_terms,
                preview=preview,
                query_tokens=query_tokens,
                budget_bytes=budget_bytes,
            )

    # 跨库检索（Scoped 定向多库 或 全局盲搜）
    return fanout_search(
        server,
        query,
        top_k,
        bool(use_rerank),
        bool(group_by_vault),
        search_filters,
        bool(dedupe),
        target_vaults=targets if targets else None,
        preview=preview,
        query_tokens=query_tokens,
        budget_bytes=budget_bytes,
        exact_terms=exact_terms,
    )
