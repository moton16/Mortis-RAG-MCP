"""F03/OPS seam: exported vectors and staged chunks must describe one snapshot."""
import errno
import zipfile

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig
from mortis_rag_mcp.doc_store import StoreConflict
from mortis_rag_mcp.indexer import MarkdownIndexer, _CacheCodec, _VectorsCodec
from mortis_rag_mcp._indexer import snapshot


def build(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "a.md"
    note.write_text("# Original\noriginal keyword", encoding="utf-8")
    cfg = AppConfig(cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")))
    idx = MarkdownIndexer(vault, cfg)
    idx.sync()
    return idx, note, cfg


def test_cache_replaced_before_capture_exports_importable_vector_subset(tmp_path, monkeypatch):
    idx, note, cfg = build(tmp_path)
    other = MarkdownIndexer(idx.vault_path, cfg)
    original = snapshot.shutil.copyfile

    def replace_before_capture(source, target, *args, **kwargs):
        if source == idx._chunks_cache_path:
            note.write_text("# Replacement\nreplacement keyword distinct", encoding="utf-8")
            other.sync()
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(snapshot.shutil, "copyfile", replace_before_capture)
    out = tmp_path / "export.zip"
    idx.export_snapshot(out)
    with zipfile.ZipFile(out) as archive:
        chunks_path = tmp_path / "captured.bin"
        chunks_path.write_bytes(archive.read("chunks.bin"))
        captured_ids = {c.id for _, chunks in _CacheCodec.load(chunks_path)[1].values() for c in chunks}
        if "vectors.bin" in archive.namelist():
            vectors_path = tmp_path / "captured-vectors.bin"
            vectors_path.write_bytes(archive.read("vectors.bin"))
            assert set(_VectorsCodec.load(vectors_path)[1]) <= captured_ids
    monkeypatch.setattr(snapshot.shutil, "copyfile", original)
    assert idx.import_snapshot(out)["imported"] is True
    idx.close_document_store()
    other.close_document_store()


def test_failed_chunks_persistence_cannot_publish_old_cache_as_current(tmp_path, monkeypatch):
    idx, note, _ = build(tmp_path)
    out = tmp_path / "export.zip"
    idx.export_snapshot(out)
    before = out.read_bytes()

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic chunks disk full")

    monkeypatch.setattr(_CacheCodec, "dump", full)
    note.write_text("# New\nnew searchable in memory", encoding="utf-8")
    idx.sync()
    assert idx.search("searchable")
    assert idx.persistence_status["state"] == "failed"
    with pytest.raises(StoreConflict, match="chunks persistence failed"):
        idx.export_snapshot(out)
    assert out.read_bytes() == before
    assert idx.search("searchable")
    idx.close_document_store()
