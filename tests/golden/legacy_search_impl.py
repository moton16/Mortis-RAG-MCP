"""v0.7.3（C44 = git 0367acf）单库检索实现逐字冻结快照 —— test_search_oracle 的对照方。

与 tests/golden/legacy_split_table_impl.py 同一模式（v0.8.0 P4 门禁）：
本文件是重构前单体 `mortis_rag_mcp/indexer.py` 中检索主路径的**逐字拷贝**，
除以下两类机械改写外一字未动：

1. `MarkdownIndexer.search / _fts_query / _hybrid_rank / _query_tokens` 原样
   搬入 `LegacySearchOracle` 类；`self.` 的**状态访问**（config / _fts /
   all_chunks() / embedding_provider / _vector_backend / reranker_provider）
   经 `__getattr__` 穿透到被包裹的活实例，冻结方法则原样遮蔽活实例同名方法
   （`__getattr__` 只在类上找不到时才触发，冻结方法优先级更高）；
2. 检索主路径引用的模块级常量与纯函数（`_RRF_K`、`_WORD_RE`、`_ASCII_RE`、
   `_CJK_RE`、`_SHORT_STOPWORDS`、`dedupe_by_content_hash`、`rerank_chunks`）
   随迁冻结，保证对照路径不触碰任何新实现代码。

冻结范围（源行号，git 0367acf:mortis_rag_mcp/indexer.py）：
- 常量块 29 / 63-65 / 75-78
- dedupe_by_content_hash 383-402
- rerank_chunks 631-661
- search 2130-2261、_fts_query 2263-2295、_hybrid_rank 2297-2345、
  _query_tokens 2347-2366

共享输入说明：`SearchFilter.matches / page_slice` 由测试以同一对象传入两侧
（SearchFilter 本体已由审查逐字锁定，Chunk 形态由 golden/codec 测试钉死）；
`dataclasses.replace` 为标准库。
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:  # 仅为类型注解，运行时零依赖新实现
    from mortis_rag_mcp._indexer.models import Chunk, SearchFilter

_RRF_K = 60

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_ASCII_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_SHORT_STOPWORDS = frozenset({
    "a", "an", "at", "be", "by", "do", "go", "he", "if", "in", "is", "it",
    "me", "my", "no", "of", "on", "or", "so", "to", "up", "we", "us", "am"
})


def dedupe_by_content_hash(items: list[Any], chunk_of: Callable[[Any], Chunk] | None = None) -> list[Any]:
    """按 content_hash 保序去重：同一份内容只留排在最前面的那条。

    items 默认就是 Chunk 列表；跨库 fan-out 传的是 (vault_entry, chunk) 元组，
    用 chunk_of 把 chunk 取出来即可。没有 content_hash 的老 chunk 一律保留——
    宁可多返回一条，也不能把内容未知的 chunk 误判成重复删掉。
    """
    extract = chunk_of if chunk_of is not None else (lambda item: item)
    seen: set[str] = set()
    kept: list[Any] = []
    for item in items:
        digest = extract(item).metadata.get("content_hash")
        if digest is None:
            kept.append(item)
            continue
        if digest in seen:
            continue
        seen.add(digest)
        kept.append(item)
    return kept


def rerank_chunks(query: str, ranked: list[Chunk], reranker_provider: Any, cap: int = 60) -> list[Chunk]:
    """Reorder `ranked` with the reranker provider (module-level so the server
    can rerank a merged multi-vault candidate pool with a single API call).

    Returns the reordered list; on provider failure the input order is kept.
    """
    if not ranked:
        return ranked
    try:
        candidates = ranked[:cap]  # cap rerank payload; never send the whole corpus
        if hasattr(reranker_provider, "rerank_or_none"):
            reranked = reranker_provider.rerank_or_none(query, [chunk.content for chunk in candidates])
        else:
            reranked = reranker_provider.rerank(query, [chunk.content for chunk in candidates])
    except Exception:
        return [replace(c) for c in ranked]
    if not reranked:
        return [replace(c) for c in ranked]
    positions = {int(item["index"]): item for item in reranked if "index" in item}
    scored_candidates = list(candidates)
    for index, item in positions.items():
        if 0 <= index < len(scored_candidates) and "relevance_score" in item:
            scored_candidates[index] = replace(scored_candidates[index], score=float(item["relevance_score"]))
        elif 0 <= index < len(scored_candidates):
            scored_candidates[index] = replace(scored_candidates[index])
    for index in range(len(scored_candidates)):
        if index not in positions:
            scored_candidates[index] = replace(scored_candidates[index])
    ordered = [scored_candidates[index] for index in positions if 0 <= index < len(scored_candidates)]
    ordered += [chunk for index, chunk in enumerate(scored_candidates) if index not in positions]
    return ordered + [replace(c) for c in ranked[len(candidates):]]


class LegacySearchOracle:
    """C44（v0.7.3）检索算法逐字冻结；状态经 `__getattr__` 穿透到活实例。"""

    def __init__(self, idx: Any) -> None:
        self._idx = idx

    def __getattr__(self, name: str) -> Any:
        # 仅当类上找不到该属性时触发：状态全部穿透到活实例（_fts / config /
        # _chunks / embedding_provider / _vector_backend / reranker_provider /
        # all_chunks() …），冻结方法则原样遮蔽活实例的同名方法。
        if name == "_idx":
            raise AttributeError(name)
        return getattr(self._idx, name)

    def search(self, query: str, top_k: int = 10, use_rerank: bool = False, query_vector: Iterable[float] | None = None, filters: SearchFilter | None = None, dedupe: bool = True) -> list[Chunk]:
        # 兜底夹取：server 层已夹过一次，这里再夹一道，保证任何调用方都不会
        # 把 10**9 这样的值透传给 sqlite-vec 的 KNN 堆或候选池切片。
        try:
            top_k = max(1, min(int(top_k), self.config.max_top_k))
        except (TypeError, ValueError):
            top_k = 10
        query = query.strip()
        all_chunks = self.all_chunks()
        if not query:
            ranked = [replace(c, score=0.0) for c in all_chunks]
            if dedupe:
                ranked = dedupe_by_content_hash(ranked)
            if filters is None:
                return ranked[: max(0, top_k)]
            ranked = [chunk for chunk in ranked if filters.matches(chunk)]
            start, end = filters.page_slice(top_k)
            return ranked[start:end]

        query_tokens = self._query_tokens(query)

        # 针对 SQLite FTS5 Trigram 分词器丢弃 < 3 字符短英文词（如 RC、AI、OS、IP、Go）的补偿机制：
        # 1. 过滤英文常用停用词（如 to, in, at, is 等），当查询中含有实质词汇时排除纯虚词，避免频次倒挂
        # 2. 将所有待加权短词合并为一个预编译正则，将 O(N_chunks * N_tokens) 降为 O(N_chunks) 单次扫描
        short_acronym_tokens: list[str] = []
        regular_tokens: list[str] = []
        raw_words = _WORD_RE.findall(query)
        has_substantive_tokens = any(
            t not in _SHORT_STOPWORDS or (t.isascii() and any(w.isupper() and w.lower() == t for w in raw_words))
            for t in query_tokens
        )
        for token in query_tokens:
            if token.isascii() and token.isalnum() and len(token) < 3:
                is_raw_upper = any(w.isupper() and w.lower() == token for w in raw_words)
                if token not in _SHORT_STOPWORDS or is_raw_upper:
                    short_acronym_tokens.append(token)
                elif not has_substantive_tokens:
                    regular_tokens.append(token)
            else:
                regular_tokens.append(token)

        acronym_regex: re.Pattern | None = None
        if short_acronym_tokens:
            try:
                acronym_regex = re.compile(
                    r"\b(?:" + "|".join(re.escape(t) for t in set(short_acronym_tokens)) + r")\b",
                    re.IGNORECASE,
                )
            except Exception:
                acronym_regex = None

        # Lexical scores are a soft signal, never a hard gate: every chunk gets a
        # score so semantic recall always has the full corpus to work with.
        lexical: dict[str, float] = {}
        for chunk in all_chunks:
            haystack = chunk.content.lower()
            token_hits = sum(haystack.count(token) for token in regular_tokens)
            acronym_hits = len(acronym_regex.findall(chunk.content)) if acronym_regex is not None else 0
            exact_boost = 1 if query.lower() in haystack else 0
            lexical[chunk.id] = float(token_hits + acronym_hits * 5.0 + exact_boost * 10)

        # Semantic route: raw cosine, snapshotted BEFORE any lexical fusion so
        # both the hybrid (RRF) and legacy paths can use it independently.
        # Queries always go through the vector backend (memory brute-force or
        # disk-backed sqlite-vec KNN).
        semantic_chunks: list[Chunk] = []
        semantic_snapshot: dict[str, float] = {}
        if self.config.embedding.mode == "external":
            try:
                # Callers doing multi-vault fan-out embed the query once and
                # pass it in, so N vaults cost one embed call instead of N.
                if query_vector is None:
                    query_vector = self.embedding_provider.embed([query])[0]
                # Rerank candidate cap + per-route RRF width both come from
                # config so callers can trade recall vs API payload size.
                # 带过滤条件时放大候选池：过滤发生在候选截断之后，窄过滤条件
                # （path_prefix/tags）下 top-vec_limit 全局候选可能全被滤掉而
                # 真匹配在窗口外，返回偏少或空、翻页边界不稳定。
                vec_limit = max(top_k, self.config.rrf_per_route, self.config.rerank_cap)
                if filters is not None:
                    vec_limit = max(vec_limit, min(top_k * 20, vec_limit * 8))
                pairs = self._vector_backend.query(query_vector, vec_limit)
                semantic_snapshot = dict(pairs)
                for chunk in all_chunks:
                    if chunk.id in semantic_snapshot:
                        semantic_chunks.append(replace(chunk, score=semantic_snapshot[chunk.id]))
            except Exception:
                pass

        hybrid = self.config.use_hybrid and self._fts is not None and self._fts.available
        if hybrid:
            # path_prefix 下推到 FTS 的 SQL 层只是减少候选量（source 是 UNINDEXED
            # 列，可直接进 WHERE）；过滤的正确性由下面的统一后过滤保证。
            prefix = filters.path_prefix if filters is not None else ""
            ranked = self._hybrid_rank(query, all_chunks, lexical, semantic_snapshot, path_prefix=prefix)
        else:
            ranked = []
        if not ranked:
            # Legacy path, unchanged: cosine dominates, lexical breaks ties.
            if semantic_chunks:
                lex_max = max(lexical[chunk.id] for chunk in semantic_chunks) or 1.0
                ranked = [
                    replace(chunk, score=chunk.score + (lexical[chunk.id] / lex_max) * 0.2)
                    for chunk in semantic_chunks
                ]
            else:
                ranked = [
                    replace(chunk, score=lexical[chunk.id])
                    for chunk in all_chunks
                    if lexical[chunk.id] > 0
                ]

        ranked.sort(key=lambda chunk: (-chunk.score, chunk.source, chunk.metadata["chunk_index"]))

        # 过滤放在 rerank 之前：rerank 是要花钱/花时间的配额，不能浪费在马上
        # 会被过滤掉的条目上。
        if filters is not None:
            ranked = [chunk for chunk in ranked if filters.matches(chunk)]

        # 去重同样放在 rerank 之前：付费 rerank 的 cap（默认 60）个名额会被
        # 同一段内容的 N 份副本占满，实际收益只剩 1/N。行 1468 的 early dedupe
        # 只覆盖混合模式之前，这里兜底语义路由无词法结果的情况。
        if dedupe:
            ranked = dedupe_by_content_hash(ranked)

        if use_rerank and self.reranker_provider and ranked:
            ranked = rerank_chunks(query, ranked, self.reranker_provider, cap=self.config.rerank_cap)

        if filters is None:
            return ranked[: max(0, top_k)]
        start, end = filters.page_slice(top_k)
        return ranked[start:end]

    def _fts_query(self, query: str) -> str | None:
        """Build an FTS5 MATCH expression from tokens of length >= 3.

        Returns None when no such token survives (e.g. a 2-char CJK query like
        "银狼"), so the caller skips the BM25 route and the bigram lexical route
        covers the query instead. Trigram cannot match <3-char queries.

        中文特判：FTS5 用的是 trigram 分词器（索引的是 3 字滑窗），而查询侧把
        整段连续 CJK 当一个引号短语的话，必须"原样连续出现"才命中——中文没有
        分词，一段自然语言就是 8~10 个字，实测「半导体物理」「如何学习半导体
        物理」这类查询对含相关内容的正文命中 0 条，BM25 路由对中文基本是摆设
        （不报错，另两路兜住，所以看不出问题）。长度 >= 4 的 CJK 段切成 3 字
        滑窗、用 OR 连接，让 BM25 按命中滑窗数量给分级召回。
        """
        terms: list[str] = []
        for piece in _WORD_RE.findall(query):
            for word in _ASCII_RE.findall(piece):
                if len(word) >= 3:
                    terms.append('"' + word.lower().replace('"', '""') + '"')
            for cjk in _CJK_RE.findall(piece):
                if len(cjk) == 3:
                    terms.append('"' + cjk.replace('"', '""') + '"')
                elif len(cjk) >= 4:
                    shingles = [
                        cjk[index : index + 3] for index in range(len(cjk) - 2)
                    ]
                    joined = " OR ".join(
                        '"' + shingle.replace('"', '""') + '"' for shingle in shingles
                    )
                    terms.append(f"({joined})")
        if not terms:
            return None
        return " AND ".join(terms)

    def _hybrid_rank(
        self,
        query: str,
        all_chunks: list[Chunk],
        lexical: dict[str, float],
        semantic_snapshot: dict[str, float],
        path_prefix: str = "",
    ) -> list[Chunk]:
        """Three-route RRF fusion: FTS5 BM25 + vector cosine + bigram lexical.

        chunk.score becomes the RRF value (comparable across vaults), then the
        caller sorts and reranks as usual. Any single route failing or empty is
        simply absent — search never throws.
        """
        by_id = {chunk.id: chunk for chunk in all_chunks}
        routes: list[list[str]] = []

        # Route A: FTS5 BM25 (trigram). Skipped when the query has no >=3-char token.
        fts_sql = self._fts_query(query)
        if fts_sql is not None and self._fts is not None:
            try:
                routes.append([chunk_id for chunk_id, _score in self._fts.search(fts_sql, self.config.rrf_per_route, path_prefix)])
            except Exception:
                pass

        # Route B: vector cosine, raw and descending.
        if semantic_snapshot:
            ordered = sorted(semantic_snapshot.items(), key=lambda item: -item[1])
            routes.append([chunk_id for chunk_id, _score in ordered[: self.config.rrf_per_route]])

        # Route C: bigram lexical soft scores, descending, score > 0 only.
        lexical_ordered = sorted(
            ((chunk_id, score) for chunk_id, score in lexical.items() if score > 0),
            key=lambda item: -item[1],
        )
        if lexical_ordered:
            routes.append([chunk_id for chunk_id, _score in lexical_ordered[: self.config.rrf_per_route]])

        if not routes:
            return []

        fused: dict[str, float] = {}
        for route in routes:
            for rank, chunk_id in enumerate(route, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)

        ranked = [replace(by_id[chunk_id], score=fused[chunk_id]) for chunk_id in fused if chunk_id in by_id]
        ranked.sort(key=lambda chunk: (-chunk.score, chunk.source, chunk.metadata["chunk_index"]))
        return ranked

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        """Tokenize for lexical scoring: ASCII words verbatim, CJK text as bigrams.

        The previous regex grabbed a whole CJK run as one token, which made
        lexical scoring behave like exact-substring matching for Chinese. Bigrams
        keep named entities (卡芙卡 -> 卡芙/芙卡) matchable without any tokenizer
        dependency.
        """
        tokens: list[str] = []
        for piece in _WORD_RE.findall(query):
            for word in _ASCII_RE.findall(piece):
                tokens.append(word.lower())
            for cjk in _CJK_RE.findall(piece):
                chars = list(cjk)
                if len(chars) == 1:
                    tokens.append(chars[0])
                else:
                    tokens.extend("".join(chars[i:i + 2]) for i in range(len(chars) - 1))
        return tokens
