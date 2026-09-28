"""单库检索引擎 / RRF 混合重排 / 词法与向量候选召回（v0.8.0 自 indexer.py 提取）。

模块边界：
- 独立的单库检索核心，接收只读配置与运行时实例引用；
- 运行时零反向依赖 ``mortis_rag_mcp.indexer`` Facade（仅 TYPE_CHECKING 导入）；
- ``rerank_chunks`` 与 ``_to_emb`` 随检索引擎一并迁入，Facade 经 re-export 保持导入兼容；
- numpy 保持惰性按需导入，可选依赖缺失时安全降级为标量余弦。
"""
from __future__ import annotations

from array import array
from dataclasses import replace
import re
from typing import Any, Iterable, TYPE_CHECKING

from .models import Chunk, SearchFilter, _EMB_DTYPE, dedupe_by_content_hash

if TYPE_CHECKING:
    from mortis_rag_mcp.indexer import MarkdownIndexer

_RRF_K = 60

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_ASCII_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_SHORT_STOPWORDS = frozenset({
    "a", "an", "at", "be", "by", "do", "go", "he", "if", "in", "is", "it",
    "me", "my", "no", "of", "on", "or", "so", "to", "up", "we", "us", "am"
})


def _to_emb(vectors: Iterable[float]) -> array:
    return array(_EMB_DTYPE, vectors)


def _lexical_haystack(chunk: Chunk) -> str:
    """为词法路检索构建匹配文本：正文 + aliases 别名追加（别名命中权重大于普通词）。"""
    aliases = chunk.metadata.get("aliases")
    if not aliases:
        return chunk.content.lower()
    clean_parts = [
        str(a).lower().strip()
        for a in aliases
        if a is not None and not isinstance(a, bool) and str(a).strip()
    ]
    if not clean_parts:
        return chunk.content.lower()
    return f"{chunk.content.lower()} {' '.join(clean_parts)}"


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


def cosine(left: Any, right: Any) -> float:
    """计算两个向量的余弦相似度。维度不一致时返回 0.0。"""
    if len(left) != len(right):
        return 0.0
    size = len(left)
    if not size:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for index in range(size):
        lv = left[index]
        rv = right[index]
        dot += lv * rv
        left_norm += lv * lv
        right_norm += rv * rv
    return dot / ((left_norm ** 0.5) * (right_norm ** 0.5)) if left_norm and right_norm else 0.0


def query_tokens(query: str) -> list[str]:
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


def fts_query(query: str) -> str | None:
    """Build an FTS5 MATCH expression from tokens of length >= 3.

    Returns None when no such token survives (e.g. a 2-char CJK query like
    "银狼"), so the caller skips the BM25 route and the bigram lexical route
    covers the query instead. Trigram cannot match <3-char queries.

    中文特判：FTS5 用的是 trigram 分词器（索引的是 3 字滑窗），而查询侧把
    整段连续 CJK 当一个引号短语的话，必须"原样连续出现"才命中。长度 >= 4 的
    CJK 段切成 3 字滑窗、用 OR 连接，让 BM25 按命中滑窗数量给分级召回。
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


def hybrid_rank(
    query: str,
    all_chunks: list[Chunk],
    lexical: dict[str, float],
    semantic_snapshot: dict[str, float],
    fts: Any,
    rrf_per_route: int,
    path_prefix: str = "",
    *,
    exact_route: list[str] | None = None,
) -> list[Chunk]:
    """Three-route RRF fusion: FTS5 BM25 + vector cosine + bigram lexical.

    chunk.score becomes the RRF value (comparable across vaults), then the
    caller sorts and reranks as usual. Any single route failing or empty is
    simply absent — search never throws.
    """
    by_id = {chunk.id: chunk for chunk in all_chunks}
    routes: list[list[str]] = []

    # Route A: FTS5 BM25 (trigram). Skipped when the query has no >=3-char token.
    fts_sql = fts_query(query)
    if fts_sql is not None and fts is not None:
        try:
            routes.append([chunk_id for chunk_id, _score in fts.search(fts_sql, rrf_per_route, path_prefix)])
        except Exception:
            pass

    # Route B: vector cosine, raw and descending.
    if semantic_snapshot:
        ordered = sorted(semantic_snapshot.items(), key=lambda item: -item[1])
        routes.append([chunk_id for chunk_id, _score in ordered[: rrf_per_route]])

    # Route C: bigram lexical soft scores, descending, score > 0 only.
    lexical_ordered = sorted(
        ((chunk_id, score) for chunk_id, score in lexical.items() if score > 0),
        key=lambda item: -item[1],
    )
    if lexical_ordered:
        routes.append([chunk_id for chunk_id, _score in lexical_ordered[: rrf_per_route]])

    # Route D: exact_terms must route
    if exact_route:
        routes.append(exact_route)

    if not routes:
        return []

    fused: dict[str, float] = {}
    for route in routes:
        for rank, chunk_id in enumerate(route, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)

    ranked = [replace(by_id[chunk_id], score=fused[chunk_id]) for chunk_id in fused if chunk_id in by_id]
    ranked.sort(key=lambda chunk: (-chunk.score, chunk.source, chunk.metadata["chunk_index"]))
    return ranked


def semantic_rank(query_vector: Iterable[float], chunks: list[Chunk]) -> list[Chunk]:
    """Batch cosine similarity. numpy when available, scalar loop fallback."""
    embedded = [chunk for chunk in chunks if chunk.embedding is not None and len(chunk.embedding)]
    if not embedded:
        return []
    try:
        import numpy as np
        matrix = np.asarray([chunk.embedding for chunk in embedded], dtype=np.float32)
        vector = np.asarray(query_vector, dtype=np.float32)
        denominator = np.linalg.norm(matrix, axis=1) * np.linalg.norm(vector)
        if denominator.size == 0 or float(np.linalg.norm(vector)) == 0.0:
            return []
        similarities = (matrix @ vector) / (denominator + 1e-9)
        for chunk, similarity in zip(embedded, similarities.tolist()):
            chunk.score = float(similarity)
        return embedded
    except Exception:
        for chunk in embedded:
            chunk.score = cosine(query_vector, chunk.embedding)
        return embedded


def search_single_vault(
    owner: MarkdownIndexer,
    query: str,
    top_k: int = 10,
    use_rerank: bool = False,
    query_vector: Iterable[float] | None = None,
    filters: SearchFilter | None = None,
    dedupe: bool = True,
    *,
    exact_terms: list[str] | None = None,
) -> list[Chunk]:
    """单库三路混合检索管线主逻辑。"""
    try:
        top_k = max(1, min(int(top_k), owner.config.max_top_k))
    except (TypeError, ValueError):
        top_k = 10
    query = query.strip()
    all_chunks = owner.all_chunks()

    # 防御归一化 exact_terms：去空去重、单项截断 100 字符、最多 8 条
    clean_terms: list[str] = []
    if exact_terms:
        raw_terms = exact_terms if isinstance(exact_terms, (list, tuple)) else str(exact_terms).split(",")
        seen_terms: set[str] = set()
        for t in raw_terms:
            if t is None or isinstance(t, bool) or not isinstance(t, (str, int, float)):
                continue
            s = str(t).strip()[:100].lower()
            if s and s not in seen_terms:
                seen_terms.add(s)
                clean_terms.append(s)
            if len(clean_terms) >= 8:
                break

    if not query:
        ranked = [replace(c, score=0.0) for c in all_chunks]
        if clean_terms:
            ranked = [chunk for chunk in ranked if all(term in _lexical_haystack(chunk) for term in clean_terms)]
        if dedupe:
            ranked = dedupe_by_content_hash(ranked)
        if filters is None:
            return ranked[: max(0, top_k)]
        ranked = [chunk for chunk in ranked if filters.matches(chunk)]
        start, end = filters.page_slice(top_k)
        return ranked[start:end]

    q_tokens = query_tokens(query)

    short_acronym_tokens: list[str] = []
    regular_tokens: list[str] = []
    raw_words = _WORD_RE.findall(query)
    has_substantive_tokens = any(
        t not in _SHORT_STOPWORDS or (t.isascii() and any(w.isupper() and w.lower() == t for w in raw_words))
        for t in q_tokens
    )
    for token in q_tokens:
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

    lexical: dict[str, float] = {}
    must_candidates: list[tuple[str, int, str, int]] = []
    for chunk in all_chunks:
        haystack = _lexical_haystack(chunk)
        token_hits = sum(haystack.count(token) for token in regular_tokens)
        acronym_hits = len(acronym_regex.findall(chunk.content)) if acronym_regex is not None else 0
        exact_boost = 1 if query.lower() in haystack else 0
        lexical[chunk.id] = float(token_hits + acronym_hits * 5.0 + exact_boost * 10)
        if clean_terms and all(term in haystack for term in clean_terms):
            hits = sum(haystack.count(term) for term in clean_terms)
            try:
                c_idx = int(chunk.metadata.get("chunk_index") or 0)
            except (TypeError, ValueError):
                c_idx = 0
            must_candidates.append((chunk.id, hits, chunk.source, c_idx))

    # 第 1 层召回保障：全库扫描构造 must 候选集并作为 route D 传入 hybrid_rank
    must: list[tuple[str, int]] = []
    exact_route: list[str] | None = None
    if must_candidates:
        must_candidates.sort(key=lambda x: (-x[1], x[2], x[3]))
        must_cap = max(owner.config.rrf_per_route, top_k)
        must = [(cid, hits) for cid, hits, _, _ in must_candidates[:must_cap]]
        exact_route = [cid for cid, _ in must]

    semantic_chunks: list[Chunk] = []
    semantic_snapshot: dict[str, float] = {}
    if owner.config.embedding.mode == "external":
        try:
            if query_vector is None:
                query_vector = owner.embedding_provider.embed([query])[0]
            vec_limit = max(top_k, owner.config.rrf_per_route, owner.config.rerank_cap)
            if filters is not None:
                vec_limit = max(vec_limit, min(top_k * 20, vec_limit * 8))
            pairs = owner._vector_backend.query(query_vector, vec_limit)
            semantic_snapshot = dict(pairs)
            for chunk in all_chunks:
                if chunk.id in semantic_snapshot:
                    semantic_chunks.append(replace(chunk, score=semantic_snapshot[chunk.id]))
        except Exception:
            pass

    hybrid = owner.config.use_hybrid and owner._fts is not None and owner._fts.available
    if hybrid:
        prefix = filters.path_prefix if filters is not None else ""
        ranked = hybrid_rank(
            query,
            all_chunks,
            lexical,
            semantic_snapshot,
            owner._fts,
            owner.config.rrf_per_route,
            path_prefix=prefix,
            exact_route=exact_route,
        )
    else:
        ranked = []
    if not ranked:
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
        # 第 2 层召回保障：非 hybrid 降级路径下，为 must 中未进入 ranked 的 chunk 追加固定保底分
        if must:
            by_id = {c.id: c for c in all_chunks}
            existing_ids = {c.id for c in ranked}
            floor_score = 1.0 / (_RRF_K + owner.config.rrf_per_route)
            for chunk_id, _ in must:
                if chunk_id not in existing_ids and chunk_id in by_id:
                    ranked.append(replace(by_id[chunk_id], score=floor_score))

    ranked.sort(key=lambda chunk: (-chunk.score, chunk.source, chunk.metadata["chunk_index"]))

    # 第 3 层召回保障：融合排序后、与 filters.matches 同阶段执行硬过滤；rerank 之后不再过滤
    if clean_terms:
        ranked = [chunk for chunk in ranked if all(term in _lexical_haystack(chunk) for term in clean_terms)]

    if filters is not None:
        ranked = [chunk for chunk in ranked if filters.matches(chunk)]

    if dedupe:
        ranked = dedupe_by_content_hash(ranked)

    if use_rerank and owner.reranker_provider and ranked:
        ranked = rerank_chunks(query, ranked, owner.reranker_provider, cap=owner.config.rerank_cap)

    if filters is None:
        return ranked[: max(0, top_k)]
    start, end = filters.page_slice(top_k)
    return ranked[start:end]


class SearchEngine:
    """只读单库检索引擎组件。"""

    def __init__(self, owner: MarkdownIndexer) -> None:
        self.owner = owner

    def execute(
        self,
        query: str,
        top_k: int = 10,
        use_rerank: bool = False,
        query_vector: Iterable[float] | None = None,
        filters: SearchFilter | None = None,
        dedupe: bool = True,
        *,
        exact_terms: list[str] | None = None,
    ) -> list[Chunk]:
        return search_single_vault(
            self.owner,
            query=query,
            top_k=top_k,
            use_rerank=use_rerank,
            query_vector=query_vector,
            filters=filters,
            dedupe=dedupe,
            exact_terms=exact_terms,
        )

    @classmethod
    def search(
        cls,
        owner: MarkdownIndexer,
        query: str,
        top_k: int = 10,
        use_rerank: bool = False,
        query_vector: Iterable[float] | None = None,
        filters: SearchFilter | None = None,
        dedupe: bool = True,
        *,
        exact_terms: list[str] | None = None,
    ) -> list[Chunk]:
        return search_single_vault(
            owner,
            query=query,
            top_k=top_k,
            use_rerank=use_rerank,
            query_vector=query_vector,
            filters=filters,
            dedupe=dedupe,
            exact_terms=exact_terms,
        )
