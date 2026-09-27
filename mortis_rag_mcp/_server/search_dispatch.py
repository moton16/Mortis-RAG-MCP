"""检索路由分发与参数规范化（v0.8.0 自 server.py 提取）。

职责：
- 集中处理 kb_search 的入参规范化（top_k, use_rerank, group_by_vault, dedupe, preview, filters）；
- 目标库解析与判定：单库直通、默认单库、Scoped 定向多库或全局盲搜；
- 守护冷启动状态：单库初始化状态机检查（try_sync_with_guard 友好响应）；
- 活实例规则：接收 live 的 VaultMcpServer 实例。
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .fanout import fanout_search

if TYPE_CHECKING:
    from mortis_rag_mcp.server import VaultMcpServer


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

    # 单库检索通道（传入单个目标）
    if len(targets) == 1:
        resolved_single = server._resolve_vault_path(targets[0])
        indexer = server._indexer_for({"vault_path": resolved_single})
        sync_ok = indexer.try_sync_with_guard(timeout=1.5)
        if not sync_ok and len(indexer._chunks) == 0:
            return {
                "status": "indexing",
                "message": "知识库正在后台进行首次初始化构建与嵌入计算，请稍候...",
                "progress": indexer._sync_progress,
                "retry_after": 3,
                "chunks": [],
            }
        results = indexer.search(query, top_k, bool(use_rerank), filters=search_filters, dedupe=bool(dedupe))
        res_dict: dict[str, Any] = {"chunks": [chunk.to_dict(preview=preview, query_tokens=query_tokens) for chunk in results]}
        if not sync_ok:
            res_dict["indexing_in_progress"] = True
            res_dict["indexing_progress"] = indexer._sync_progress
        return res_dict

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
            sync_ok = indexer.try_sync_with_guard(timeout=1.5)
            if not sync_ok and len(indexer._chunks) == 0:
                return {
                    "status": "indexing",
                    "message": "知识库正在后台进行首次初始化构建与嵌入计算，请稍候...",
                    "progress": indexer._sync_progress,
                    "retry_after": 3,
                    "chunks": [],
                }
            results = indexer.search(query, top_k, bool(use_rerank), filters=search_filters, dedupe=bool(dedupe))
            res_dict = {"chunks": [chunk.to_dict(preview=preview, query_tokens=query_tokens) for chunk in results]}
            if not sync_ok:
                res_dict["indexing_in_progress"] = True
                res_dict["indexing_progress"] = indexer._sync_progress
            return res_dict

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
    )
