"""跨库检索 Fan-out 编排（v0.8.0 自 server.py 提取）。

核心契约：
- 单次 Query Embedding：整个 fan-out 跨库流程只对 query 计算一次向量；
- 过滤/去重优先于 rerank：过滤与去重在 rerank 之前执行；
- 库级权重保护：rerank 计算完成后将库级权重乘回 chunk.score；
- 跨库分页：全局模式在整体排序后统一分页；分组模式按库切桶分别分页；
- 活实例规则：接收活的 VaultMcpServer 实例，直读 _indexers 状态。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..registry import VaultEntry, normalize_vault_key
from .._indexer.models import Chunk, SearchFilter, dedupe_by_content_hash
from .._indexer.search import rerank_chunks

if TYPE_CHECKING:
    from mortis_rag_mcp.server import VaultMcpServer


def fanout_search(
    server: VaultMcpServer,
    query: str,
    top_k: int,
    use_rerank: bool,
    group_by_vault: bool = False,
    filters: SearchFilter | None = None,
    dedupe: bool = True,
    target_vaults: list[str] | None = None,
    preview: bool = False,
    query_tokens: list[str] | None = None,
) -> dict[str, Any]:
    """Search across registered vaults (either globally or scoped to target_vaults), merge and rerank once.

    group_by_vault=True 时返回按库分组的结果（每组 top_k 条），否则平铺返回。
    filters 的过滤条件对每个库分别生效，分页（offset/limit）只在最后合并
    排序后的全局结果上做一次——否则各库各翻一页，合并出来的顺序没有意义。
    """
    per_vault_k = max(top_k, 20)
    # 单库检索只吃过滤条件，不吃分页：分页留到全局合并之后。
    per_vault_filters: SearchFilter | None = None
    if filters is not None:
        per_vault_filters = SearchFilter(
            path_prefix=filters.path_prefix,
            tags=filters.tags,
            mtime_after=filters.mtime_after,
            mtime_before=filters.mtime_before,
        )
    all_entries = server.registry.load()
    vault_name_map = {entry.path: entry.name for entry in all_entries}

    target_keys: set[str] | None = None
    if target_vaults is not None and len(target_vaults) > 0:
        target_keys = {normalize_vault_key(server._resolve_vault_path(t)) for t in target_vaults}

    entries: list[VaultEntry] = []
    excluded_solo: list[str] = []

    for entry in all_entries:
        if not Path(entry.path).is_dir():
            continue
        entry_key = normalize_vault_key(entry.path)
        if target_keys is not None:
            # 定向多库检索（Scoped Multi-Vault）：显式包含的 solo 库允许合法参与检索
            if entry_key in target_keys:
                entries.append(entry)
        else:
            # 全局盲搜：跳过所有 solo 库并汇报
            if entry.solo:
                excluded_solo.append(entry.path)
            else:
                entries.append(entry)

    if not entries:
        if target_keys is not None:
            raise ValueError(f"none of the requested vaults could be searched: {target_vaults}")
        if excluded_solo and len(excluded_solo) == len(all_entries):
            raise ValueError(
                "no searchable vaults: all registered vaults are solo (excluded from global search); "
                "pass an explicit vault_path or vault_paths to search"
            )
        raise ValueError("no readable registered vaults; call kb_init first")
    merged: list[tuple[VaultEntry, Chunk]] = []
    searched: list[str] = []
    errors: dict[str, str] = {}

    # Embed the query exactly once for the whole fan-out.
    query_vector = None
    if server.config.embedding.mode == "external":
        try:
            first = server._indexer_for({"vault_path": entries[0].path})
            query_vector = first.embedding_provider.embed([query])[0]
        except Exception as exc:
            errors["_query_embedding"] = str(exc)

    for entry in entries:
        try:
            indexer = server._indexer_for({"vault_path": entry.path})
            sync_ok = indexer.try_sync_with_guard(timeout=1.5)
            if not sync_ok and len(indexer._chunks) == 0:
                errors[entry.path] = "indexing in progress"
                continue
            chunks = indexer.search(
                query,
                per_vault_k,
                False,
                query_vector=query_vector,
                filters=per_vault_filters,
                dedupe=dedupe,
            )
            for chunk in chunks:
                merged.append((entry, chunk))
            searched.append(entry.path)
        except Exception as exc:
            errors[entry.path] = str(exc)

    # 库级权重：分数乘以该库 weight 后再参与全局排序。
    # 使用 replace 生成打分副本，避免污染 indexer 内存常驻对象。
    merged = [
        (entry, replace(chunk, score=chunk.score * entry.weight))
        for entry, chunk in merged
    ]

    merged.sort(key=lambda pair: (-pair[1].score, pair[1].source, pair[1].metadata["chunk_index"]))
    pairs = merged
    # 跨库再去重一次：同一份内容可能躺在两个库里（比如一个库是另一个的备份）。
    if dedupe:
        pairs = dedupe_by_content_hash(pairs, chunk_of=lambda pair: pair[1])
    if use_rerank and merged:
        provider = None
        for indexer in list(server._indexers.values()):
            if indexer.reranker_provider is not None:
                provider = indexer.reranker_provider
                break
        if provider is not None:
            # rerank 的候选池必须来自去重+加权后的 pairs，而不是未去重的
            # merged：否则去重被这条路径整体撤销。
            pool = [chunk for _, chunk in pairs]
            reranked = rerank_chunks(query, pool, provider, cap=server.config.rerank_cap)
            origin_map: dict[str, list[VaultEntry]] = {}
            for entry, chunk in pairs:
                origin_map.setdefault(chunk.id, []).append(entry)
            new_pairs = []
            for chunk in reranked:
                assigned_entries = origin_map.get(chunk.id)
                if assigned_entries:
                    new_pairs.append((assigned_entries.pop(0), chunk))
                elif pairs:
                    new_pairs.append((pairs[0][0], chunk))
            pairs = new_pairs
            # rerank 覆盖了 chunk.score，把库级权重乘回去。
            pairs = [
                (entry, replace(chunk, score=chunk.score * entry.weight))
                for entry, chunk in pairs
            ]

    if filters is not None:
        start, end = filters.page_slice(top_k)
    else:
        start, end = 0, max(0, top_k)

    if query_tokens is None:
        from ..server import _tokenize_query
        tokens = _tokenize_query(query)
    else:
        tokens = query_tokens

    if group_by_vault:
        # 分组模式：保持融合后的组内顺序，按库切桶；组顺序取各组最高分降序。
        # 分页在这里是"每组各翻一页"——全局先切一刀会让低分库整组消失。
        buckets: dict[str, dict[str, Any]] = {}
        for entry, chunk in pairs:
            group = buckets.get(entry.path)
            if group is None:
                group = {"vault": entry.path, "vault_name": entry.name, "chunks": []}
                buckets[entry.path] = group
            data = chunk.to_dict(preview=preview, query_tokens=tokens)
            data["vault"] = entry.path
            data["vault_name"] = entry.name
            group["chunks"].append(data)
        for group in buckets.values():
            group["chunks"] = group["chunks"][start:end]
        # 切片后桶可能空了，直接丢掉（max 不接受空序列）。
        groups = sorted(
            (group for group in buckets.values() if group["chunks"]),
            key=lambda group: -max(chunk["score"] for chunk in group["chunks"]),
        )
        res: dict[str, Any] = {"groups": groups, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}
        if len(searched) > 1 and not target_vaults:
            names = [vault_name_map.get(s, Path(s).name) for s in searched]
            res["hint"] = (
                f"本次检索横跨 {len(searched)} 个库：{names}。若用户问题指向特定库或目录，"
                "下次请传 vault_path 或 path_prefix 定向检索，精度更高、噪音更少。"
            )
        return res

    pairs = pairs[start:end]

    out_chunks = []
    for entry, chunk in pairs:
        data = chunk.to_dict(preview=preview, query_tokens=tokens)
        data["vault"] = entry.path
        data["vault_name"] = entry.name
        out_chunks.append(data)
    res = {"chunks": out_chunks, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}
    if len(searched) > 1 and not target_vaults:
        names = [vault_name_map.get(s, Path(s).name) for s in searched]
        res["hint"] = (
            f"本次检索横跨 {len(searched)} 个库：{names}。若用户问题指向特定库或目录，"
            "下次请传 vault_path 或 path_prefix 定向检索，精度更高、噪音更少。"
        )
    return res
