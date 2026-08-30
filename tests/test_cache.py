from __future__ import annotations

from pathlib import Path

from vault_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from vault_mcp.indexer import MarkdownIndexer


class CountingProvider:
    """Embedding provider that counts how many times it was called."""

    def __init__(self) -> None:
        self.calls = 0
        self.dimension = 8

    def embed(self, texts):
        self.calls += 1
        return [[float(index % 7) / 7.0 for index in range(self.dimension)] for _ in texts]


def _config(tmp_path: Path, **cache_kwargs) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, **cache_kwargs),
    )


def test_cache_reuses_unchanged_files_without_re_embedding(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\nsecond file", encoding="utf-8")

    provider = CountingProvider()
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider)
    indexer.sync()
    assert provider.calls >= 2  # both files embedded once
    assert len(indexer.all_chunks()) >= 2

    # A fresh indexer over the same vault must load from cache: no provider calls.
    provider2 = CountingProvider()
    indexer2 = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider2)
    indexer2.sync()
    assert provider2.calls == 0
    assert len(indexer2.all_chunks()) == len(indexer.all_chunks())


def test_cache_invalidated_on_file_change(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    provider = CountingProvider()
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider)
    indexer.sync()
    first = len(indexer.all_chunks())

    # Change the file: the next sync must re-embed only this file.
    (tmp_path / "a.md").write_text("# A\nhello world changed", encoding="utf-8")
    provider2 = CountingProvider()
    indexer2 = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider2)
    indexer2.sync()
    assert provider2.calls == 1
    assert any("changed" in chunk.content for chunk in indexer2.all_chunks())
    assert len(indexer2.all_chunks()) == first


def test_cache_misses_are_reembedded_and_deleted_files_removed(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    assert indexer.all_chunks()

    (tmp_path / "b.md").write_text("# B\nbrand new", encoding="utf-8")
    indexer.sync()
    assert any(chunk.source == "b.md" for chunk in indexer.all_chunks())

    (tmp_path / "a.md").unlink()
    indexer.sync()
    assert not any(chunk.source == "a.md" for chunk in indexer.all_chunks())


def test_concurrent_external_embedding_calls_all_files(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\nworld", encoding="utf-8")
    (tmp_path / "c.md").write_text("# C\nfoo", encoding="utf-8")

    cfg = _config(tmp_path, embedding_max_workers=3)
    cfg.embedding.mode = "external"
    cfg.embedding.dimension = 4

    class FakeExternal:
        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            import time
            time.sleep(0.05)
            return [[1.0 / (i + 1) for i in range(4)] for _ in texts]

    provider = FakeExternal()
    indexer = MarkdownIndexer(tmp_path, cfg, embedding_provider=provider)
    chunks = indexer.sync()
    assert provider.calls == 3
    assert len(chunks) == 3
    assert all(chunk.embedding is not None and len(chunk.embedding) == 4 for chunk in chunks)


def test_external_embedding_failure_records_failure_keeps_text(tmp_path):
    (tmp_path / "ok.md").write_text("# OK\nfine", encoding="utf-8")
    (tmp_path / "bad.md").write_text("# Bad\nboom", encoding="utf-8")

    class Flaky:
        def embed(self, texts):
            if any("boom" in text for text in texts):
                raise OSError("provider down")
            return [[1.0] * 4 for _ in texts]

    cfg = _config(tmp_path)
    cfg.embedding.mode = "external"
    cfg.embedding.dimension = 4
    indexer = MarkdownIndexer(tmp_path, cfg, embedding_provider=Flaky())
    chunks = indexer.sync()

    sources = [chunk.source for chunk in chunks]
    assert "ok.md" in sources
    # 分层缓存：embedding 失败时文本层保留（词法检索仍可用），失败被记录。
    assert "bad.md" in sources
    assert "bad.md" in indexer.failed_files
    # 语义召回应跳过无向量的 chunk，但词法检索仍能命中 bad.md。
    bad_chunks = [chunk for chunk in indexer.all_chunks() if chunk.source == "bad.md"]
    assert bad_chunks and all(chunk.embedding is None or not len(chunk.embedding) for chunk in bad_chunks)
