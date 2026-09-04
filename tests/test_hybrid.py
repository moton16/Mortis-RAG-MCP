from __future__ import annotations

from pathlib import Path

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def _static_config(tmp_path: Path, **cache_kwargs) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, **cache_kwargs),
    )


class RouteProvider:
    """External embedding provider with controllable vectors.

    - text containing "B标记"  -> the query vector (cosine 1.0 with the query)
    - anything else           -> a deterministic per-text vector (near-orthogonal)
    The query string "星核猎手 银狼" does not contain "B标记", so its vector is
    NOT the query vector; only the B chunk lands on it.
    """

    dimension = 8

    def embed(self, texts):
        result = []
        for text in texts:
            if "B标记" in text:
                result.append([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            else:
                h = hash(text) % 97
                result.append([float((h >> i) & 1) for i in range(self.dimension)])
        return result


def _make_corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    c = tmp_path / "c.md"
    a.write_text("# A\n星核猎手 量子态 独特词 XYZ", encoding="utf-8")
    b.write_text("# B\nB标记 毫无关键词重叠的纯描述段落", encoding="utf-8")
    c.write_text("# C\n银狼的档案记录", encoding="utf-8")
    return a, b, c


def _hybrid_indexer(tmp_path: Path, config: AppConfig, provider=None) -> MarkdownIndexer:
    indexer = MarkdownIndexer(tmp_path, config, embedding_provider=provider)
    indexer.sync()
    return indexer


def test_hybrid_rrf_merges_all_three_routes(tmp_path):
    _make_corpus(tmp_path)
    config = AppConfig(
        embedding=EmbeddingConfig(mode="external", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
    )
    indexer = _hybrid_indexer(tmp_path, config, RouteProvider())

    # "星核猎手" hits A via FTS BM25 (>=3 chars) and bigram lexical;
    # "银狼" (2 chars) hits C via the bigram route only (trigram can't match);
    # B is vector-only (cosine 1.0 to the query, zero keyword overlap).
    results = indexer.search("星核猎手 银狼", top_k=10, use_rerank=False)
    ids = {chunk.id for chunk in results}
    a_id = next(chunk.id for chunk in indexer.all_chunks() if "星核猎手" in chunk.content)
    b_id = next(chunk.id for chunk in indexer.all_chunks() if "B标记" in chunk.content)
    c_id = next(chunk.id for chunk in indexer.all_chunks() if "银狼" in chunk.content)
    assert {a_id, b_id, c_id} <= ids, f"RRF 应合并三路命中, got {ids}"
    # BM25 + bigram double-hit ranks A first.
    assert results[0].id == a_id


def test_hybrid_two_char_cjk_matches_via_bigram(tmp_path):
    (tmp_path / "note.md").write_text("# 银狼\n银狼的编程风格与朋克洛德背景资料", encoding="utf-8")
    indexer = _hybrid_indexer(tmp_path, _static_config(tmp_path))

    results = indexer.search("银狼", top_k=5, use_rerank=False)
    assert results
    assert any("银狼" in chunk.content for chunk in results)


def test_use_hybrid_false_restores_old_behavior(tmp_path):
    (tmp_path / "note.md").write_text("# 星核猎手\n星核猎手 量子态 独特词 XYZ", encoding="utf-8")
    config = _static_config(tmp_path)
    config.use_hybrid = False
    indexer = _hybrid_indexer(tmp_path, config)

    results = indexer.search("星核猎手", top_k=5, use_rerank=False)
    assert results
    assert any("星核猎手" in chunk.content for chunk in results)
    assert indexer.stats()["use_hybrid"] is False


def test_fts_absent_or_failing_falls_back(tmp_path):
    # (a) cache disabled -> no FTS index at all; search still works.
    (tmp_path / "note.md").write_text("# Note\nhello 世界", encoding="utf-8")
    no_cache = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=False),
    )
    indexer = MarkdownIndexer(tmp_path, no_cache)
    indexer.sync()
    assert indexer._fts is None
    assert indexer.stats()["fts_enabled"] is False
    assert indexer.search("世界", top_k=5)

    # (b) simulated FTS failure mid-session -> legacy path still returns hits.
    cached = _hybrid_indexer(tmp_path, _static_config(tmp_path))
    assert cached.stats()["fts_enabled"] is True
    cached._fts = None
    assert cached.search("世界", top_k=5)


def test_incremental_sync_updates_fts(tmp_path):
    file = tmp_path / "note.md"
    file.write_text("# Note\n星核猎手专属内容", encoding="utf-8")
    indexer = _hybrid_indexer(tmp_path, _static_config(tmp_path))
    assert indexer.search("星核猎手", top_k=5, use_rerank=False)

    # Modify: old keyword gone, new keyword searchable (delete-by-source).
    file.write_text("# Note\n完全不同的新内容", encoding="utf-8")
    indexer.sync()
    assert not indexer.search("星核猎手", top_k=5, use_rerank=False)
    assert indexer.search("完全不同", top_k=5, use_rerank=False)

    # Delete: its keyword gone from FTS too.
    file.unlink()
    indexer.sync()
    assert not indexer.search("完全不同", top_k=5, use_rerank=False)


def test_rebuild_and_purge_handle_fts_db(tmp_path):
    (tmp_path / "note.md").write_text("# Note\n星核猎手内容", encoding="utf-8")
    indexer = _hybrid_indexer(tmp_path, _static_config(tmp_path))
    cache_root = tmp_path / "cache"
    assert list(cache_root.rglob("*.sqlite"))

    indexer.rebuild()
    # rebuild repopulates the FTS index.
    assert list(cache_root.rglob("*.sqlite"))
    assert indexer.search("星核猎手", top_k=5, use_rerank=False)

    indexer.purge_cache()
    assert not list(cache_root.rglob("*.sqlite"))


def test_stats_exposes_new_keys(tmp_path):
    (tmp_path / "note.md").write_text("# Note\nhello", encoding="utf-8")
    indexer = _hybrid_indexer(tmp_path, _static_config(tmp_path))
    stats = indexer.stats()
    assert stats["use_hybrid"] is True
    assert stats["fts_enabled"] is True
    assert stats["vector_backend"] == "memory"


def test_warm_cache_upgrade_populates_fts(tmp_path):
    """A second indexer over an unchanged vault (warm .bin cache, no file
    changes) must still populate FTS — the sync-only hook would never fire."""
    (tmp_path / "a.md").write_text("# A\n星核猎手专属内容", encoding="utf-8")
    first = _hybrid_indexer(tmp_path, _static_config(tmp_path))
    assert first._fts is not None
    assert first._fts.count() == len(first.all_chunks())

    second = MarkdownIndexer(tmp_path, _static_config(tmp_path))
    second.sync()
    assert second._fts is not None
    assert second._fts.count() == len(second.all_chunks())
    assert second.search("星核猎手", top_k=5, use_rerank=False)


def test_rrf_per_route_config_roundtrip(tmp_path):
    from mortis_rag_mcp.config import load_config

    path = tmp_path / "app.toml"
    path.write_text(
        """
        [embedding]
        mode = "static"
        dimension = 8
        [index]
        rrf_per_route = 7
        rerank_cap = 11
        """,
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.rrf_per_route == 7
    assert config.rerank_cap == 11

    import pytest
    from mortis_rag_mcp.config import AppConfig

    with pytest.raises(ValueError):
        AppConfig(rrf_per_route=0)
    with pytest.raises(ValueError):
        AppConfig(rerank_cap=0)


def test_rrf_per_route_limits_fusion_pool(tmp_path):
    """With rrf_per_route=2 only the top-2 candidates of each route enter the
    fused pool, so the weakest of three matching files drops out entirely."""
    (tmp_path / "a.md").write_text("# A\n" + "苹果香蕉梨 " * 5, encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n" + "苹果香蕉梨 " * 3, encoding="utf-8")
    (tmp_path / "c.md").write_text("# C\n苹果香蕉梨", encoding="utf-8")

    config = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
        rrf_per_route=2,
    )
    indexer = MarkdownIndexer(tmp_path, config)
    indexer.sync()
    results = indexer.search("苹果", top_k=10, use_rerank=False)
    sources = {chunk.source for chunk in results}
    assert "a.md" in sources and "b.md" in sources
    assert "c.md" not in sources, f"rrf_per_route=2 应把最弱的 C 挡在融合池外, got {sources}"

    # Default width (40) keeps C reachable.
    default_indexer = MarkdownIndexer(
        tmp_path,
        AppConfig(
            embedding=EmbeddingConfig(mode="static", dimension=8),
            cache=CacheConfig(dir=str(tmp_path / "cache2"), enabled=True),
        ),
    )
    default_indexer.sync()
    default_sources = {chunk.source for chunk in default_indexer.search("苹果", top_k=10, use_rerank=False)}
    assert "c.md" in default_sources


def test_rrf_per_route_config_roundtrip(tmp_path):
    from mortis_rag_mcp.config import load_config

    path = tmp_path / "app.toml"
    path.write_text(
        """
        [embedding]
        mode = "static"
        dimension = 8
        [index]
        rrf_per_route = 7
        rerank_cap = 11
        """,
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.rrf_per_route == 7
    assert config.rerank_cap == 11

    import pytest
    from mortis_rag_mcp.config import AppConfig

    with pytest.raises(ValueError):
        AppConfig(rrf_per_route=0)
    with pytest.raises(ValueError):
        AppConfig(rerank_cap=0)


def test_rrf_per_route_limits_fusion_pool(tmp_path):
    """With rrf_per_route=2 only the top-2 candidates of each route enter the
    fused pool, so the weakest of three matching files drops out entirely."""
    (tmp_path / "a.md").write_text("# A\n" + "苹果香蕉梨 " * 5, encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n" + "苹果香蕉梨 " * 3, encoding="utf-8")
    (tmp_path / "c.md").write_text("# C\n苹果香蕉梨", encoding="utf-8")

    config = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
        rrf_per_route=2,
    )
    indexer = MarkdownIndexer(tmp_path, config)
    indexer.sync()
    results = indexer.search("苹果", top_k=10, use_rerank=False)
    sources = {chunk.source for chunk in results}
    assert "a.md" in sources and "b.md" in sources
    assert "c.md" not in sources, f"rrf_per_route=2 应把最弱的 C 挡在融合池外, got {sources}"

    # Default width (40) keeps C reachable.
    default_indexer = MarkdownIndexer(
        tmp_path,
        AppConfig(
            embedding=EmbeddingConfig(mode="static", dimension=8),
            cache=CacheConfig(dir=str(tmp_path / "cache2"), enabled=True),
        ),
    )
    default_indexer.sync()
    default_sources = {chunk.source for chunk in default_indexer.search("苹果", top_k=10, use_rerank=False)}
    assert "c.md" in default_sources
