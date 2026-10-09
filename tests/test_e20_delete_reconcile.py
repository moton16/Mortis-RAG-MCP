"""Production sync must fence a deleted first ingest, not exclusions/unknown IO."""
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp._indexer import sync_engine


@pytest.mark.parametrize("condition", ["deleted", "ignored", "incomplete", "permission"])
def test_first_ingest_reconcile_fences_only_confirmed_deletion(tmp_path, monkeypatch, condition):
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "paper.pdf"
    source.write_bytes(b"synthetic document")
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8),
                    cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")))
    indexer = MarkdownIndexer(vault, cfg)
    store = indexer.document_store(write=True)
    job, created = store.enqueue_job(source="paper.pdf",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), parser_fingerprint="fixture")
    assert created
    assert store.claim_job("worker").job_id == job.job_id
    token = store.source_seq("paper.pdf")
    sequence = store.change_seq()
    assert store.list_documents() == [], "first ingest must not rely on an existing active revision"
    if condition != "permission":
        source.unlink()
    if condition == "ignored":
        (vault / cfg.ignore_file).write_text("paper.pdf\n", encoding="utf-8")
    elif condition == "incomplete":
        original = sync_engine.scan_indexable_files
        monkeypatch.setattr(sync_engine, "scan_indexable_files",
            lambda *args, **kwargs: replace(original(*args, **kwargs), complete=False))
    elif condition == "permission":
        original = Path.stat

        def denied(path, *args, **kwargs):
            if path == source:
                raise PermissionError("synthetic source state unknown")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)
    try:
        indexer.sync()
        if condition == "deleted":
            assert store.job_status(job.job_id).state == "superseded"
            assert store.change_seq() == sequence + 1
            assert store.source_seq("paper.pdf") != token
            assert store.list_documents()[0].visibility == "deleted"
            assert store.get_active("paper.pdf") is None
        else:
            assert store.job_status(job.job_id).state == "parsing"
            assert store.change_seq() == sequence
            assert store.source_seq("paper.pdf") == token
            assert store.list_documents() == []
    finally:
        indexer.close_document_store()
