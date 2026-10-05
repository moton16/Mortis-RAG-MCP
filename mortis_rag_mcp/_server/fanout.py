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
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..registry import VaultEntry, normalize_vault_key
from .._indexer.models import Chunk, SearchFilter, dedupe_by_content_hash
from .._indexer.search import rerank_chunks

if TYPE_CHECKING:
    from mortis_rag_mcp.server import VaultMcpServer


def _measure_payload_bytes(result: Any) -> int:
    """计量最终响应字符串的 UTF-8 字节数。

    对齐 server.py::_text_content 包装后的最终序列化口径（引用函数名，勿钉行号）：
    json.dumps({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}, ensure_ascii=False)
    """
    inner = json.dumps(result, ensure_ascii=False)
    outer = {"content": [{"type": "text", "text": inner}]}
    return len(json.dumps(outer, ensure_ascii=False).encode("utf-8"))


def _build_overflow_response(
    base_meta: dict[str, Any],
    orig_offset: int,
    budget_bytes: int,
    mode: str = "flat",
    group_next_offsets: dict[str, int] | None = None,
) -> dict[str, Any]:
    overflow_res = dict(base_meta)
    if mode == "flat":
        overflow_res["chunks"] = []
        overflow_res["next_offset"] = orig_offset
    else:
        overflow_res["groups"] = []
        overflow_res["next_offset"] = None
        if group_next_offsets is not None:
            overflow_res["group_next_offsets"] = dict(group_next_offsets)
    overflow_res["truncated"] = True
    overflow_res["returned"] = 0
    overflow_res["budget_exceeded"] = True
    overflow_res["budget_hint"] = "narrow vaults or increase budget_bytes"

    # 迭代稳定 minimum_budget_bytes（最多 3 次），避免格式化字符数变化导致假称 <= budget
    est = budget_bytes
    for _ in range(3):
        overflow_res["minimum_budget_bytes"] = est
        actual = _measure_payload_bytes(overflow_res)
        if actual == est:
            break
        est = actual
    overflow_res["minimum_budget_bytes"] = est
    return overflow_res


def apply_budget(
    result: dict[str, Any],
    budget_bytes: int | None,
    orig_offset: int = 0,
    preview: bool = False,
    group_orig_offsets: dict[str, int] | None = None,
) -> dict[str, Any]:
    """根据 budget_bytes 预算对检索结果进行有界截断（完整 chunk 粒度，不切半个 chunk）。"""
    if budget_bytes is None:
        return result

    # 1. 平铺模式 (chunks)
    if "chunks" in result:
        all_chunks = list(result["chunks"])
        N = len(all_chunks)

        base_meta = {
            k: v for k, v in result.items()
            if k not in (
                "chunks", "truncated", "returned", "next_offset",
                "budget_hint", "budget_exceeded", "minimum_budget_bytes",
            )
        }

        # 1. 先测全量 N（truncated=False，无 hint）
        cand_all = dict(base_meta)
        cand_all["chunks"] = all_chunks
        cand_all["truncated"] = False
        cand_all["returned"] = N
        cand_all["next_offset"] = orig_offset + N

        if _measure_payload_bytes(cand_all) <= budget_bytes:
            return cand_all

        # 2. 对正整数前缀 1..N-1 进行二分查找（truncated=True，无 hint）
        best_k = 0
        if N > 1:
            low = 1
            high = N - 1
            while low <= high:
                mid = (low + high) // 2
                cand_mid = dict(base_meta)
                cand_mid["chunks"] = all_chunks[:mid]
                cand_mid["truncated"] = True
                cand_mid["returned"] = mid
                cand_mid["next_offset"] = orig_offset + mid
                if _measure_payload_bytes(cand_mid) <= budget_bytes:
                    best_k = mid
                    low = mid + 1
                else:
                    high = mid - 1

        if best_k >= 1:
            res_kept = dict(base_meta)
            res_kept["chunks"] = all_chunks[:best_k]
            res_kept["truncated"] = True
            res_kept["returned"] = best_k
            res_kept["next_offset"] = orig_offset + best_k
            return res_kept

        # 3. best_k == 0：正前缀一个也不满足（或 N == 0）
        if N == 0:
            cand_zero = dict(base_meta)
            cand_zero["chunks"] = []
            cand_zero["truncated"] = False
            cand_zero["returned"] = 0
            cand_zero["next_offset"] = orig_offset
            if _measure_payload_bytes(cand_zero) <= budget_bytes:
                return cand_zero
            return _build_overflow_response(base_meta, orig_offset, budget_bytes, mode="flat")

        # N > 0 且首条放不下：测试带 hint 的空包络
        zero_res = dict(base_meta)
        zero_res["chunks"] = []
        zero_res["truncated"] = True
        zero_res["returned"] = 0
        zero_res["next_offset"] = orig_offset
        zero_res["budget_hint"] = "use compact or increase budget_bytes"

        if _measure_payload_bytes(zero_res) <= budget_bytes:
            return zero_res

        # 空包络亦超出预算：显式 metadata 溢出声明
        return _build_overflow_response(base_meta, orig_offset, budget_bytes, mode="flat")

    # 2. 分组模式 (groups)
    if "groups" in result:
        all_groups = list(result["groups"])
        base_meta = {
            k: v for k, v in result.items()
            if k not in (
                "groups", "truncated", "returned", "next_offset",
                "budget_hint", "budget_exceeded", "minimum_budget_bytes",
                "group_next_offsets",
            )
        }

        vault_orig_map: dict[str, int] = {}
        if group_orig_offsets is not None:
            vault_orig_map.update(group_orig_offsets)
        for g in all_groups:
            v_path = g.get("vault", "")
            if v_path and v_path not in vault_orig_map:
                vault_orig_map[v_path] = orig_offset

        total_chunks = sum(len(g.get("chunks", [])) for g in all_groups)

        def _construct_grouped_candidate(k: int, truncated: bool) -> dict[str, Any]:
            cand = dict(base_meta)
            cand_groups: list[dict[str, Any]] = []
            cand_next_offsets = dict(vault_orig_map)
            k_rem = k

            for g in all_groups:
                v_path = g.get("vault", "")
                v_orig = vault_orig_map.get(v_path, orig_offset)
                g_chunks = g.get("chunks", [])
                r = min(k_rem, len(g_chunks))
                k_rem -= r
                cand_next_offsets[v_path] = v_orig + r

                if r > 0:
                    cand_g = dict(g)
                    cand_g["chunks"] = list(g_chunks[:r])
                    cand_g["returned"] = r
                    cand_g["truncated"] = (r < len(g_chunks))
                    cand_g["next_offset"] = v_orig + r
                    cand_groups.append(cand_g)

            cand["groups"] = cand_groups
            cand["returned"] = k
            cand["truncated"] = truncated
            cand["next_offset"] = None
            cand["group_next_offsets"] = cand_next_offsets
            return cand

        # 1. 先测全量 total_chunks
        cand_all = _construct_grouped_candidate(total_chunks, truncated=False)
        if _measure_payload_bytes(cand_all) <= budget_bytes:
            return cand_all

        # 2. 二分查找正整数前缀 1..total_chunks-1
        best_k = 0
        if total_chunks > 1:
            low = 1
            high = total_chunks - 1
            while low <= high:
                mid = (low + high) // 2
                cand_mid = _construct_grouped_candidate(mid, truncated=True)
                if _measure_payload_bytes(cand_mid) <= budget_bytes:
                    best_k = mid
                    low = mid + 1
                else:
                    high = mid - 1

        if best_k >= 1:
            return _construct_grouped_candidate(best_k, truncated=True)

        # 3. best_k == 0
        if total_chunks == 0:
            cand_zero = _construct_grouped_candidate(0, truncated=False)
            if _measure_payload_bytes(cand_zero) <= budget_bytes:
                return cand_zero
            return _build_overflow_response(
                base_meta, orig_offset, budget_bytes, mode="grouped",
                group_next_offsets=vault_orig_map,
            )

        # total_chunks > 0 且连一条也放不下：测试带 hint 的空包络
        cand_zero_hint = _construct_grouped_candidate(0, truncated=True)
        cand_zero_hint["budget_hint"] = "use compact or increase budget_bytes"
        if _measure_payload_bytes(cand_zero_hint) <= budget_bytes:
            return cand_zero_hint

        # 空包络溢出
        return _build_overflow_response(
            base_meta, orig_offset, budget_bytes, mode="grouped",
            group_next_offsets=vault_orig_map,
        )

    return result


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
    *,
    budget_bytes: int | None = None,
    exact_terms: list[str] | None = None,
    compact: bool = False,
    group_offsets: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Search across registered vaults (either globally or scoped to target_vaults), merge and rerank once.

    group_by_vault=True 时返回按库分组的结果（每组 top_k 条），否则平铺返回。
    filters 的过滤条件对每个库分别生效，分页（offset/limit）只在最后合并
    排序后的全局结果上做一次——否则各库各翻一页，合并出来的顺序没有意义。
    """
    if group_by_vault:
        per_vault_k = min(server.config.max_top_k, max(top_k, 20))
    else:
        req_depth = (filters.offset + (filters.limit or top_k)) if filters is not None else top_k
        per_vault_k = min(server.config.max_top_k, max(top_k, req_depth, 20))
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
    indexing_vaults: dict[str, Any] = {}

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
            indexer.request_refresh()
            r_status = indexer.refresh_status()
            is_cold = (
                indexer.last_sync is None
                and not getattr(indexer, "_chunks_cache_loaded", False)
                and len(indexer._chunks) == 0
            )
            if is_cold:
                errors[entry.path] = "indexing in progress"
                indexing_vaults[entry.path] = {
                    "indexing_in_progress": True,
                    "indexing_progress": r_status["indexing_progress"],
                }
                continue
            if r_status["indexing_in_progress"] or r_status.get("refresh_error"):
                v_info: dict[str, Any] = {
                    "indexing_in_progress": r_status["indexing_in_progress"],
                    "indexing_progress": r_status["indexing_progress"],
                }
                if r_status.get("refresh_error"):
                    v_info["indexing_error"] = r_status["refresh_error"]
                indexing_vaults[entry.path] = v_info
            chunks = indexer.search(
                query,
                per_vault_k,
                False,
                query_vector=query_vector,
                filters=per_vault_filters,
                dedupe=dedupe,
                exact_terms=exact_terms,
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

    merged.sort(key=lambda pair: (-pair[1].score, pair[1].source, int(pair[1].metadata.get("chunk_index") or 0)))
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

    effective_preview = True if compact else preview

    if group_by_vault:
        # 分组模式：在 pairs 层按库切桶，组顺序取各组最高分降序。
        # 分页在这里是"每组各翻一页"——全局先切一刀会让低分库整组消失。
        # 排序在 pairs/Chunk 层完成，投影后不可依赖 chunk["score"]（compact 无此键）。
        buckets: dict[str, dict[str, Any]] = {}
        for entry, chunk in pairs:
            group = buckets.get(entry.path)
            if group is None:
                group = {
                    "entry": entry,
                    "pairs": [],
                    "max_score": chunk.score,
                }
                buckets[entry.path] = group
            else:
                if chunk.score > group["max_score"]:
                    group["max_score"] = chunk.score
            group["pairs"].append((entry, chunk))

        # group_offsets 与分页切片
        page_size = filters.limit if (filters and filters.limit) else top_k
        group_orig_offsets: dict[str, int] = {}
        for entry in entries:
            v_start = group_offsets.get(entry.path, start) if group_offsets else start
            group_orig_offsets[entry.path] = v_start

        for group in buckets.values():
            e_path = group["entry"].path
            v_start = group_orig_offsets.get(e_path, start)
            v_end = v_start + page_size
            group["pairs"] = group["pairs"][v_start:v_end]

        sorted_buckets = sorted(
            (group for group in buckets.values() if group["pairs"]),
            key=lambda group: -group["max_score"],
        )

        groups = []
        for grp in sorted_buckets:
            entry = grp["entry"]
            grp_chunks = []
            for _, chunk in grp["pairs"]:
                data = chunk.to_dict(preview=effective_preview, query_tokens=tokens, compact=compact)
                if not compact:
                    data["vault"] = entry.path
                    data["vault_name"] = entry.name
                grp_chunks.append(data)
            groups.append({
                "vault": entry.path,
                "vault_name": entry.name,
                "chunks": grp_chunks,
            })
        res: dict[str, Any] = {"groups": groups, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}
        if indexing_vaults:
            res["indexing_vaults"] = indexing_vaults
            res["indexing_in_progress"] = True
        if len(searched) > 1 and not target_vaults:
            names = [vault_name_map.get(s, Path(s).name) for s in searched]
            res["hint"] = (
                f"本次检索横跨 {len(searched)} 个库：{names}。若用户问题指向特定库或目录，"
                "下次请传 vault_path 或 path_prefix 定向检索，精度更高、噪音更少。"
            )
        if budget_bytes is not None:
            res = apply_budget(
                res,
                budget_bytes,
                orig_offset=start,
                preview=effective_preview,
                group_orig_offsets=group_orig_offsets,
            )
        return res

    pairs = pairs[start:end]

    out_chunks = []
    for entry, chunk in pairs:
        data = chunk.to_dict(preview=effective_preview, query_tokens=tokens, compact=compact)
        data["vault"] = entry.path
        if not compact:
            data["vault_name"] = entry.name
        out_chunks.append(data)
    res = {"chunks": out_chunks, "searched": searched, "errors": errors, "excluded_solo": excluded_solo}
    if indexing_vaults:
        res["indexing_vaults"] = indexing_vaults
        res["indexing_in_progress"] = True
    if len(searched) > 1 and not target_vaults:
        names = [vault_name_map.get(s, Path(s).name) for s in searched]
        res["hint"] = (
            f"本次检索横跨 {len(searched)} 个库：{names}。若用户问题指向特定库或目录，"
            "下次请传 vault_path 或 path_prefix 定向检索，精度更高、噪音更少。"
        )
    if budget_bytes is not None:
        res = apply_budget(res, budget_bytes, orig_offset=start, preview=effective_preview)
    return res
