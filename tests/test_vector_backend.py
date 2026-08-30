from __future__ import annotations

import pytest

from vault_mcp.config import AppConfig, CacheConfig, EmbeddingConfig, VectorConfig
from vault_mcp.indexer import MarkdownIndexer

pytest.importorskip("sqlite_vec", reason="pip install sqlite-vec to test the disk backend")

TARGET = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class TargetProvider:
    """Embedding provider: text containing "目标" maps to the target vector."""

    dimension = 8

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            if "目标" in text:
                out.append(list(TARGET))
            else:
                h = hash(text) % 97
                out.append([float((h >> i) & 1) for i in range(self.dimension)])
        return out


def _disk_config(tmp_path, provider_calls: list = None) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", dimension=8),
        vector=VectorConfig(backend="sqlite_vec"),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
    )


def test_disk_backend_keeps_vectors_off_ram(tmp_path):
    (tmp_path / "a.md").write_text("# A\n目标 语义匹配内容 标记", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n无关内容二", encoding="utf-8")
    provider = TargetProvider()
    indexer = MarkdownIndexer(tmp_path, _disk_config(tmp_path), embedding_provider=provider)
    indexer.sync()

    assert indexer.stats()["vector_backend"] == "sqlite_vec"
    # RAM freed: no chunk retains an embedding.
    assert all(chunk.embedding is None for chunk in indexer.all_chunks())
    # Disk store populated.
    assert indexer._vector_backend.count() == len(indexer.all_chunks())
    assert indexer._vector_backend.path.exists()
    # Search still returns hits (vector route reads from disk).
    results = indexer.search("目标", top_k=5, use_rerank=False)
    assert results
    assert any("目标" in chunk.content for chunk in results)


def test_disk_backend_migrates_from_bin_without_reembed(tmp_path):
    (tmp_path / "a.md").write_text("# A\n目标 内容", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n其他内容", encoding="utf-8")

    # Build the legacy .bin cache with the default memory backend.
    mem_config = AppConfig(
        embedding=EmbeddingConfig(mode="external", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
    )
    provider1 = TargetProvider()
    MarkdownIndexer(tmp_path, mem_config, embedding_provider=provider1).sync()
    assert provider1.calls > 0

    # Switch to the disk backend: migration must reuse .bin, zero re-embeds.
    provider2 = TargetProvider()
    indexer = MarkdownIndexer(tmp_path, _disk_config(tmp_path), embedding_provider=provider2)
    indexer.sync()
    assert provider2.calls == 0
    assert indexer._vector_backend.count() == len(indexer.all_chunks())
    assert indexer.search("目标", top_k=5, use_rerank=False)


def test_disk_backend_incremental_sync(tmp_path):
    file = tmp_path / "a.md"
    file.write_text("# A\n目标 内容一", encoding="utf-8")
    provider = TargetProvider()
    indexer = MarkdownIndexer(tmp_path, _disk_config(tmp_path), embedding_provider=provider)
    indexer.sync()
    backend = indexer._vector_backend
    before_ids = set(backend.list_ids())

    # Modify: old chunk ids replaced by new ones (no orphans accumulate).
    file.write_text("# A\n目标 内容一 新增内容二", encoding="utf-8")
    indexer.sync()
    after_ids = set(backend.list_ids())
    assert after_ids & before_ids == set() or len(after_ids) == len(indexer.all_chunks())
    assert backend.count() == len(indexer.all_chunks())
    assert len(provider.embed([""]))  # provider still usable

    # Delete: vectors for the removed source are gone too.
    file.unlink()
    indexer.sync()
    assert backend.count() == 0
    assert indexer._vector_backend.list_ids() == []


def test_disk_backend_purge_removes_file(tmp_path):
    (tmp_path / "a.md").write_text("# A\n目标 内容", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, _disk_config(tmp_path), embedding_provider=TargetProvider())
    indexer.sync()
    db_path = indexer._vector_backend.path
    assert db_path.exists()

    indexer.purge_cache()
    assert not db_path.exists()


def test_disk_backend_restart_reuses_store(tmp_path):
    (tmp_path / "a.md").write_text("# A\n目标 内容", encoding="utf-8")
    config = _disk_config(tmp_path)
    MarkdownIndexer(tmp_path, config, embedding_provider=TargetProvider()).sync()

    # Second instance over the same vault: no re-embed, store reused.
    provider = TargetProvider()
    indexer = MarkdownIndexer(tmp_path, config, embedding_provider=provider)
    indexer.sync()
    assert provider.calls == 0
    assert indexer._vector_backend.count() == len(indexer.all_chunks())
