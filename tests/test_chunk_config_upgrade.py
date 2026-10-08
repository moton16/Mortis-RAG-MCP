"""R2 defaults, historical codec compatibility and physical/virtual text routing."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, ChunkingConfig, load_config
from mortis_rag_mcp.indexer import MarkdownIndexer, _CacheCodec
from mortis_rag_mcp._indexer.token_chunking import chunker_fingerprint
from test_text_profile_wiring import transport, config_file, new_indexer


def test_constructor_and_empty_toml_unified(tmp_path):
    path = tmp_path / "empty.toml"
    path.write_text("", encoding="utf-8")
    assert AppConfig().chunking.mode == load_config(path).chunking.mode == "estimated_tokens"


def test_programmatic_old_character_parameters():
    assert AppConfig(chunk_size=800, chunk_overlap=120).chunking.mode == "legacy_chars"
    explicit = ChunkingConfig(mode="estimated_tokens", mode_explicit=True)
    assert AppConfig(chunk_size=800, chunking=explicit).chunking.mode == "estimated_tokens"


def test_mutated_old_parameters_without_cache_stay_legacy(tmp_path):
    cfg = AppConfig()
    cfg.cache.enabled = False
    cfg.chunk_size, cfg.chunk_overlap = 800, 120
    owner = MarkdownIndexer(tmp_path, cfg)
    try:
        assert owner._chunking_config.mode == "legacy_chars"
    finally:
        owner.close_document_store()


def test_old_cache_restores_legacy_parameters_and_golden_identity(tmp_path, transport):
    calls, _ = transport
    cfg = load_config(config_file(tmp_path))
    old_cfg = replace(cfg, chunk_size=40, chunk_overlap=5,
                      chunking=ChunkingConfig(mode="legacy_chars", mode_explicit=True))
    first = new_indexer(tmp_path, old_cfg)
    golden = [(c.id, c.content, c.metadata["start_line"], c.metadata["end_line"])
              for c in first.all_chunks()]
    path = first._chunks_cache_path
    meta, files = _CacheCodec.load(path)
    for key in ("chunking_mode", "target_tokens", "overlap_tokens", "hard_limit_tokens", "estimator_profile"):
        meta.pop(key)
    _CacheCodec.dump(path, meta, files)  # Existing pre-mode codec, not a new registry.
    first.close_document_store()
    calls.clear()
    owner = MarkdownIndexer(tmp_path / "vault", cfg)
    try:
        owner.sync()
        assert owner._chunking_config.mode == "legacy_chars"
        assert owner._chunking_config.legacy_chunk_size == 40
        assert owner._chunking_config.legacy_chunk_overlap == 5
        assert owner._chunks_meta()["chunk_size"] == 40
        assert owner._chunker_fingerprint() == chunker_fingerprint(owner._chunking_config)
        assert [(c.id, c.content, c.metadata["start_line"], c.metadata["end_line"])
                for c in owner.all_chunks()] == golden
        assert calls == []
    finally:
        owner.close_document_store()


def test_explicit_old_chars_override_estimated_cache(tmp_path, transport):
    cfg = load_config(config_file(tmp_path))
    first = new_indexer(tmp_path, cfg)
    first.close_document_store()
    path = tmp_path / "legacy.toml"
    path.write_text(config_file(tmp_path).read_text(encoding="utf-8") +
                    '[index]\nchunk_size=40\nchunk_overlap=0\n', encoding="utf-8")
    owner = MarkdownIndexer(tmp_path / "vault", load_config(path))
    try:
        assert owner._chunking_config.mode == "legacy_chars"
        assert owner._chunking_config.legacy_explicit
        owner.sync()
        assert all("source_spans" not in c.metadata for c in owner.all_chunks())
        assert owner.reembedding_approval_summary() is None
    finally:
        owner.close_document_store()


@pytest.mark.parametrize("suffix", [".md", ".MD", ".markdown", ".MARKDOWN", ".txt", ".TXT"])
@pytest.mark.parametrize("virtual", [False, True])
def test_suffix_sync_read_same_source_branch(tmp_path, suffix, virtual):
    vault = tmp_path / "vault"
    vault.mkdir()
    source = "note" + suffix
    physical = "# Physical\nphysical unique text\n"
    rendered = "# Rendered\nvirtual unique text\n"
    path = vault / source
    path.write_text(physical, encoding="utf-8")
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.placement = "home"
    cfg.cache.enabled = True
    owner = MarkdownIndexer(vault, cfg)
    try:
        if virtual:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            store = owner.document_store(write=True)
            staged = store.stage_revision(source=source, source_sha256=sha,
                render_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
                parser_fingerprint="fixture", markdown=rendered)
            store.commit_revision(staged.revision_id, source_sha256=sha)
        owner.sync()
        assert source in owner._chunks
        body = "\n".join(c.content for c in owner._chunks[source])
        assert ("virtual unique" in body) is virtual
        assert ("physical unique" in body) is not virtual
        assert ("virtual unique" in owner.read(source)) is virtual
        assert all(bool(c.metadata.get("revision_id")) is virtual for c in owner._chunks[source])
    finally:
        owner.close_document_store()


def test_markdown_temp_names_not_indexed(tmp_path):
    owner = new_indexer(tmp_path, AppConfig())
    try:
        for name in ("draft.tmp.markdown", "draft.swp.MARKDOWN", "draft.swo.markdown"):
            (owner.vault_path / name).write_text("temporary", encoding="utf-8")
        owner.sync()
        assert set(owner._chunks) == {"a.md"}
    finally:
        owner.close_document_store()


def test_explicit_rechunk_normal_rebuild_no_approval(tmp_path, transport):
    calls, _ = transport
    cfg = load_config(config_file(tmp_path))
    first = new_indexer(tmp_path, cfg)
    first.close_document_store()
    cfg = replace(cfg, chunking=ChunkingConfig(mode="estimated_tokens", mode_explicit=True,
                     target_tokens=4, overlap_tokens=0, hard_limit_tokens=6))
    calls.clear()
    owner = MarkdownIndexer(tmp_path / "vault", cfg)
    try:
        owner.sync()
        assert calls
        assert owner.reembedding_approval_summary() is None
        assert owner._paid_control_store().list_payment_authorizations() == []
    finally:
        owner.close_document_store()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows filesystem case alias")
def test_case_alias_virtual_wins_even_with_incomplete_scan(tmp_path, monkeypatch):
    from mortis_rag_mcp._indexer import scanning
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    vault = tmp_path / "vault"
    vault.mkdir()
    physical = vault / "Note.MARKDOWN"
    physical.write_text("physical unique", encoding="utf-8")
    owner = MarkdownIndexer(vault, cfg)
    try:
        owner.sync()
        assert "Note.MARKDOWN" in owner._chunks
        body = "virtual unique"
        sha = hashlib.sha256(physical.read_bytes()).hexdigest()
        store = owner.document_store(write=True)
        staged = store.stage_revision(source="note.markdown", source_sha256=sha,
            render_sha256=hashlib.sha256(body.encode()).hexdigest(),
            parser_fingerprint="fixture", markdown=body)
        store.commit_revision(staged.revision_id, source_sha256=sha)
        denied = vault / "denied"
        denied.mkdir()
        original = scanning.os.scandir
        def partial(path):
            if Path(path) == denied:
                raise PermissionError("fixture inaccessible subtree")
            return original(path)
        monkeypatch.setattr(scanning.os, "scandir", partial)
        owner.sync()
        assert set(owner._chunks) == {"note.markdown"}
        assert all("virtual unique" in c.content for c in owner.all_chunks())
        assert owner.read("Note.MARKDOWN") == "virtual unique"
        assert owner.read("note.markdown") == "virtual unique"
    finally:
        owner.close_document_store()
