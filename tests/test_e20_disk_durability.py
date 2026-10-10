"""OPS durability: real disk backend, synthetic write failure, real reopen."""
import errno

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig, VectorConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


@pytest.mark.parametrize("failure", ["raise", "empty", "swallowed"])
def test_disk_vector_write_failure_is_not_persistence_success(tmp_path, monkeypatch, failure):
    pytest.importorskip("sqlite_vec")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# Durable\nkeyword RAM recovery", encoding="utf-8")
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8),
                    cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
                    vector=VectorConfig(backend="sqlite_vec"))
    idx = MarkdownIndexer(vault, cfg)
    assert idx._vectors_on_disk
    backend = idx._vector_backend
    upsert, serialize = backend.upsert_vectors, backend._serialize

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic vector disk full")

    if failure == "swallowed":
        monkeypatch.setattr(backend, "_serialize", full)
    else:
        monkeypatch.setattr(backend, "upsert_vectors", full if failure == "raise" else lambda vectors: set())
    idx.sync()
    ids = {chunk.id for chunk in idx.all_chunks()}
    assert ids
    assert idx.search("keyword")
    assert any(chunk.embedding is not None for chunk in idx.all_chunks())
    assert not idx._disk_vectors
    assert not backend.list_ids()
    assert idx.persistence_status["state"] == "failed"
    error = idx.persistence_status["errors"]["disk_vectors"]
    assert error["path"] == str(idx._vectors_db_path)
    assert error["errno"] == (None if failure == "empty" else errno.ENOSPC)
    assert "disk_vectors" in idx.index_state()["next_action"]
    monkeypatch.setattr(backend, "upsert_vectors", upsert)
    monkeypatch.setattr(backend, "_serialize", serialize)
    idx.sync()
    assert set(backend.list_ids()) == ids
    assert idx.persistence_status["state"] == "ready"
    assert idx.persistence_status["errors"] == {}
    assert all(chunk.embedding is None for chunk in idx.all_chunks())
    reopened = MarkdownIndexer(vault, cfg)
    try:
        assert set(reopened._vector_backend.list_ids()) == ids
        assert set(reopened._vector_backend.get_vectors(ids)) == ids
        assert reopened.search("keyword")
    finally:
        reopened._vector_backend.close()
        reopened.close_document_store()
        backend.close()
        idx.close_document_store()
