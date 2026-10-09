"""E20 F01/F02/F03: real SQLite facts, deterministic offline scheduling seams."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import (
    ControlStore, DocumentStore, OwnershipLost, StoreConflict, StoreCorrupt,
    resolve_storage_layout,
)
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp._indexer.cache_codec import _CacheCodec
from mortis_rag_mcp._indexer import snapshot as snapshot_mod


def config(root):
    return AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8),
                     cache=CacheConfig(dir=str(root / "cache"), enabled=True))


def make_store(root):
    vault = root / "vault"
    vault.mkdir()
    cfg = config(root)
    store = DocumentStore(resolve_storage_layout(cfg, vault, registered_vaults=[]), cfg)
    store.open(write=True)
    return store, cfg


def stage(store, body="original", source="paper.pdf", **kwargs):
    physical = store.layout.vault_path / source
    sha = hashlib.sha256(physical.read_bytes()).hexdigest() if physical.exists() else "a" * 64
    return store.stage_revision(
        source=source, source_sha256=sha,
        render_sha256=hashlib.sha256(body.encode()).hexdigest(),
        parser_fingerprint="fixture", markdown=body, **kwargs).revision_id


@pytest.mark.parametrize("zero", [False, True], ids=["missing", "zero"])
@pytest.mark.parametrize("write", [False, True], ids=["reader", "writer"])
def test_active_database_loss_fails_without_recreation(tmp_path, zero, write):
    store, cfg = make_store(tmp_path)
    store.commit_revision(stage(store))
    meta = store.store_meta()
    path = store.layout.generations_dir / store.generation_id / "docstore.sqlite"
    store.close()
    preserved = path.with_name("preserved.sqlite")
    path.rename(preserved)
    if zero:
        path.write_bytes(b"")
    reader = DocumentStore(store.layout, cfg)
    try:
        with pytest.raises(StoreCorrupt) as error:
            reader.open(create=True, write=write)
        assert error.value.code == "STORE_CORRUPT"
        assert path.exists() is zero
        assert not zero or path.read_bytes() == b""
        assert preserved.stat().st_size > 0
        with ControlStore(store.layout) as ctrl:
            ctrl.open(create=False)
            assert ctrl.state().active_document_generation == meta.generation_id
            assert ctrl.state().epoch == meta.vault_epoch
    finally:
        reader.close()
    # Restoring original facts, not initializing a replacement, recovers exact identity.
    if zero:
        path.unlink()
    preserved.rename(path)
    reader.open(create=False)
    assert reader.store_meta() == meta
    assert reader.get_active("paper.pdf").revision.parsed_markdown == "original"
    reader.close()


def test_first_initialization_and_read_only_no_generation(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = config(tmp_path)
    layout = resolve_storage_layout(cfg, vault, registered_vaults=[])
    reader = DocumentStore(layout, cfg)
    reader.open(create=False)
    assert reader.get_active("paper.pdf") is None
    assert not layout.control_path.exists()
    reader.open(write=True)
    rid = stage(reader)
    assert reader.commit_revision(rid) == 1
    meta = reader.store_meta()
    reader.close()
    reader.open(create=False)
    assert reader.store_meta() == meta
    assert reader.get_active("paper.pdf").active_revision == rid
    reader.close()


def test_delete_advances_cas_and_invalidates_late_candidate(tmp_path):
    store, cfg = make_store(tmp_path)
    original = stage(store)
    store.commit_revision(original)
    job, _ = store.enqueue_job(source="paper.pdf", source_sha256="a" * 64,
                               parser_fingerprint="fixture")
    store.claim_job("worker")
    expected = store.change_seq()
    source_token = store.source_seq("paper.pdf")
    candidate = stage(store, "late", job_id=job.job_id, owner_token="worker")
    store.mark_derived_generation("fixture", change_seq=expected,
                                  chunker_fingerprint="c", space_fingerprint="s", status="ready")
    store.mark_deleted("paper.pdf")
    assert store.change_seq() == expected + 1
    assert store.source_seq("paper.pdf") != source_token
    assert store.get_derived_generation("fixture").status == "stale"
    assert store.get_active("paper.pdf", include_hidden=True) is None
    with pytest.raises(StoreConflict, match="deleted source"):
        store.commit_revision(candidate)
    with pytest.raises(StoreConflict):
        store.commit_revision(candidate, expected_change_seq=expected)
    with pytest.raises(StoreConflict, match="expected_source_seq"):
        store.commit_revision(candidate, expected_source_seq=source_token)
    with pytest.raises(OwnershipLost):
        store.commit_job_revision(job.job_id, "worker", candidate)
    assert store.job_status(job.job_id).state == "superseded"
    assert store.job_status(job.job_id).result_revision == ""
    store.mark_deleted("paper.pdf")
    assert store.change_seq() == expected + 1, "idempotent tombstone must not churn seq"
    store.close()
    store.open(create=False)
    assert store.change_seq() == expected + 1
    assert store.get_active("paper.pdf") is None
    assert store.job_status(job.job_id).state == "superseded"
    # A fresh deliberate reingest remains possible without changing epoch/schema.
    fresh, created = store.enqueue_job(source="paper.pdf", source_sha256="a" * 64,
                                       parser_fingerprint="fixture")
    assert created
    claimed = store.claim_job("fresh-worker")
    assert claimed.job_id == fresh.job_id
    seq = store.change_seq()
    token = store.source_seq("paper.pdf")
    rid = stage(store, "fresh", job_id=fresh.job_id, owner_token="fresh-worker")
    assert store.source_seq("paper.pdf") == token, "staging cannot advance publication token"
    store.commit_revision(stage(store, "unrelated", source="other.pdf"))
    assert store.change_seq() != seq
    store.commit_job_revision(fresh.job_id, "fresh-worker", rid, expected_source_seq=token)
    assert store.get_active("paper.pdf").revision.parsed_markdown == "fresh"
    store.close()


def test_pin_protects_revision_retention_until_release(tmp_path):
    store, cfg = make_store(tmp_path)
    old = stage(store, "old")
    store.commit_revision(old)
    pin = store.capture_generation_pin()
    writer = DocumentStore(store.layout, cfg)
    writer.open(write=True)
    try:
        writer.commit_revision(stage(writer, "new"))
        writer.commit_revision(stage(writer, "newest"))
        conn = sqlite3.connect(str(store.layout.generations_dir / store.generation_id / "docstore.sqlite"))
        try:
            assert conn.execute("SELECT 1 FROM document_revisions WHERE revision_id=?", (old,)).fetchone()
        finally:
            conn.close()
        assert store.validate_generation_pin(pin) is False, "same-generation writes invalidate captured seq"
        assert store.release_generation_pin(pin)
        writer.commit_revision(stage(writer, "after-release"))
        assert writer._load_revision(writer._conn(), old) is None
        assert len(list(writer._conn().execute("SELECT revision_id FROM document_revisions"))) == 2
    finally:
        writer.close()
        store.close()


def test_delete_before_first_stage_fences_pending_job(tmp_path):
    store, cfg = make_store(tmp_path)
    job, _ = store.enqueue_job(source="first.pdf", source_sha256="a" * 64,
                               parser_fingerprint="fixture")
    store.claim_job("old-worker")
    token = store.source_seq("first.pdf")
    assert store.list_documents() == []
    assert store.pending_ingest_sources() == ["first.pdf"]
    seq = store.change_seq()
    assert store.mark_deleted("first.pdf") == seq + 1
    assert store.source_seq("first.pdf") != token
    assert store.pending_ingest_sources() == []
    with pytest.raises(OwnershipLost):
        stage(store, source="first.pdf", job_id=job.job_id, owner_token="old-worker")
    assert store.get_active("first.pdf", include_hidden=True) is None
    assert store.list_documents("deleted")[0].source == "first.pdf"
    store.close()
    store.open(create=False)
    assert store.job_status(job.job_id).state == "superseded"
    assert store.source_seq("first.pdf") != token
    store.close()


def indexed_store(root):
    vault = root / "vault"
    vault.mkdir()
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 synthetic")
    owner = MarkdownIndexer(vault, config(root))
    store = owner.document_store(write=True)
    rid = store.stage_revision(source="paper.pdf",
        source_sha256=hashlib.sha256((vault / "paper.pdf").read_bytes()).hexdigest(),
        render_sha256=hashlib.sha256(b"original").hexdigest(),
        parser_fingerprint="fixture", markdown="original").revision_id
    store.commit_revision(rid)
    owner.sync()
    return owner, store, rid


def test_export_interleaved_commits_do_not_publish_mixed_snapshot(tmp_path, monkeypatch):
    owner, store, old = indexed_store(tmp_path)
    out = tmp_path / "snapshot.zip"
    out.write_bytes(b"existing-output")
    backup = store.backup_to
    writer = DocumentStore(store.layout, owner.config)
    writer.open(write=True)
    def interleave(path, **kwargs):
        writer.commit_revision(stage(writer, "new"))
        writer.commit_revision(stage(writer, "newest"))
        return backup(path, **kwargs)
    monkeypatch.setattr(store, "backup_to", interleave)
    try:
        with pytest.raises(StoreConflict):
            owner.export_snapshot(out)
        assert out.read_bytes() == b"existing-output"
        assert writer._load_revision(writer._conn(), old) is not None
        monkeypatch.setattr(store, "backup_to", backup)
        owner.sync()
        assert owner.export_snapshot(out)["exported"]
        with zipfile.ZipFile(out) as archive:
            chunks = tmp_path / "chunks.bin"
            chunks.write_bytes(archive.read("chunks.bin"))
            database = tmp_path / "backup.sqlite"
            database.write_bytes(archive.read("docstore.sqlite"))
        references = {c.metadata["revision_id"] for _, cs in _CacheCodec.load(chunks)[1].values() for c in cs}
        with sqlite3.connect(str(database)) as conn:
            retained = {row[0] for row in conn.execute("SELECT revision_id FROM document_revisions")}
        assert references <= retained
    finally:
        writer.close()
        owner.close_document_store()


def test_export_rejects_chunks_with_absent_revision(tmp_path):
    owner, store, old = indexed_store(tmp_path)
    try:
        # Real successive publications prune the previously indexed fact before capture.
        store.commit_revision(stage(store, "new"))
        store.commit_revision(stage(store, "newest"))
        out = tmp_path / "snapshot.zip"
        with pytest.raises(StoreConflict, match="revision"):
            owner.export_snapshot(out)
        assert not out.exists()
    finally:
        owner.close_document_store()


@pytest.mark.parametrize("seam", ["validation", "hash"])
def test_export_freezes_chunks_before_other_client_cache_replace(tmp_path, monkeypatch, seam):
    owner, store, old = indexed_store(tmp_path)
    vault = owner.vault_path
    note = vault / "note.md"
    note.write_text("# ORIGINAL\n\noriginal captured text", encoding="utf-8")
    cfg = owner.config
    owner.sync()
    original_ids = {c.id for cs in owner._chunks.values() for c in cs}
    other = MarkdownIndexer(vault, cfg)
    replaced = False

    def other_client():
        nonlocal replaced
        if replaced:
            return
        replaced = True
        note.write_text("# REPLACED\n\nother client's newer text", encoding="utf-8")
        other.sync()  # Real second client, same cache key, its own sync lock.
        assert {c.id for cs in other._chunks.values() for c in cs} != original_ids

    if seam == "validation":
        validate = snapshot_mod._require_export_revisions
        def interleave(*args):
            validate(*args)
            other_client()
        monkeypatch.setattr(snapshot_mod, "_require_export_revisions", interleave)
    else:
        digest = snapshot_mod._sha_file
        def interleave(path):
            value = digest(path)
            if path == owner._chunks_cache_path or path.name == "chunks.bin":
                other_client()
            return value
        monkeypatch.setattr(snapshot_mod, "_sha_file", interleave)
    out = tmp_path / "export.zip"
    try:
        assert owner.export_snapshot(out)["exported"]
        assert replaced, "the independent cache writer must actually run"
        with zipfile.ZipFile(out) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            payload = archive.read("chunks.bin")
            assert hashlib.sha256(payload).hexdigest() == manifest["members"]["chunks.bin"]["sha256"]
            captured = tmp_path / "captured.bin"
            captured.write_bytes(payload)
            files = _CacheCodec.load(captured)[1]
            assert {c.id for _, cs in files.values() for c in cs} == original_ids
            assert manifest["stats"]["chunks"] == len(original_ids)
            database = tmp_path / "captured.sqlite"
            database.write_bytes(archive.read("docstore.sqlite"))
            with sqlite3.connect(str(database)) as conn:
                rids = {r[0] for r in conn.execute("SELECT revision_id FROM document_revisions")}
            references = {c.metadata["revision_id"] for _, cs in files.values() for c in cs
                          if c.metadata.get("revision_id")}
            assert old in references
            assert references <= rids
    finally:
        owner.close_document_store()
        other.close_document_store()
