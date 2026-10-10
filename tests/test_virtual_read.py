from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import SourcePathError, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp._indexer import reading


@pytest.fixture
def indexer(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    config.cache.dir = str(tmp_path / "cache")
    config.cache.enabled = True
    idx = MarkdownIndexer(vault, config)
    idx._storage_layout = resolve_storage_layout(config, vault, registered_vaults=[])
    yield idx
    idx.close_document_store()


def publish(idx, body="# Heading\n\nrendered text\n", raw=b"binary source", source="a.pdf"):
    (idx.vault_path / source).write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    store = idx.document_store(write=True)
    staged = store.stage_revision(
        source=source, source_sha256=sha,
        render_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        parser_fingerprint="test", markdown=body,
        capabilities={"coverage": {"pages": 1}},
    )
    store.commit_revision(staged.revision_id, source_sha256=sha)
    return store.get_active(source).revision


def test_virtual_metadata_and_physical_binary_denial(indexer):
    revision = publish(indexer)
    result = reading.read_virtual_result(indexer, "a.pdf", heading="Heading")
    assert result.content == "# Heading\n\nrendered text"
    assert result.source_kind == "virtual"
    assert result.source_sha256 == revision.render_sha256
    assert result.original_sha256 == revision.source_sha256
    assert result.line_basis == "rendered_markdown"
    assert result.coverage == {"pages": 1}
    with pytest.raises(ValueError, match="Markdown or plain-text"):
        indexer._safe_path("a.pdf")


@pytest.mark.parametrize("text", ["", "a\r\nb\r\n", "\ufeff# Heading\nlast", "a\n\nlast"])
def test_shared_slice_equals_physical(tmp_path, text):
    path = tmp_path / "a.md"
    path.write_bytes(text.encode("utf-8"))
    raw = path.read_bytes()
    physical = reading.read_file_result(path, "a.md", max_chars=3)
    shared = reading.slice_text_result(raw.decode("utf-8-sig"), "a.md", hashlib.sha256(raw).hexdigest(), max_chars=3)
    assert physical == shared


def test_virtual_cursor_and_heading_rules(indexer):
    publish(indexer, "# A\nabcdef\n```\n# fake\n```\n# B\nlast")
    first = reading.read_virtual_result(indexer, "a.pdf", heading="A", max_chars=5)
    assert first.content == "# A\na"
    assert (first.next_start_line, first.next_start_char) == (2, 1)
    rest = reading.read_virtual_result(indexer, "a.pdf", first.next_start_line, 5, start_char=first.next_start_char)
    assert first.content + rest.content == "# A\nabcdef\n```\n# fake\n```"
    with pytest.raises(ValueError, match="heading"):
        reading.read_virtual_result(indexer, "a.pdf", heading="fake")
    with pytest.raises(ValueError, match="start_line"):
        reading.read_virtual_result(indexer, "a.pdf", 99)


def test_source_changed_and_chunk_stale(indexer):
    revision = publish(indexer)
    (indexer.vault_path / "a.pdf").write_bytes(b"changed")
    with pytest.raises(reading.VirtualReadError, match="SOURCE_CHANGED"):
        reading.read_virtual_result(indexer, "a.pdf")
    result = reading.read_virtual_result(indexer, "a.pdf", allow_stale=True)
    assert result.source_changed and result.original_sha256 == revision.source_sha256
    with pytest.raises(reading.VirtualReadError, match="STALE"):
        reading.read_virtual_result(indexer, "a.pdf", allow_stale=True,
            expected_revision_id=revision.revision_id, expected_sha256=revision.source_sha256,
            expected_render_sha256=revision.render_sha256, chunk_id_hint="old")


def test_new_revision_does_not_resurrect_old_chunk(indexer):
    old = publish(indexer)
    publish(indexer)
    with pytest.raises(reading.VirtualReadError, match="STALE"):
        reading.read_virtual_result(indexer, "a.pdf", expected_revision_id=old.revision_id,
            expected_sha256=old.source_sha256, expected_render_sha256=old.render_sha256)


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_allow_stale_is_strict_boolean(indexer, value):
    with pytest.raises(ValueError, match="boolean"):
        reading.read_virtual_result(indexer, "a.pdf", allow_stale=value)


@pytest.mark.parametrize("visibility", ["unverified", "deleted", "exempt"])
def test_hidden_never_reads_even_allow_stale(indexer, visibility):
    publish(indexer)
    indexer.document_store(write=True).set_visibility("a.pdf", visibility)
    with pytest.raises(reading.VirtualReadError, match="UNAVAILABLE"):
        reading.read_virtual_result(indexer, "a.pdf", allow_stale=True)


def test_ignore_missing_and_source_budget(indexer):
    publish(indexer)
    indexer.config.exclude_patterns.append("a.pdf")
    with pytest.raises(reading.VirtualReadError, match="UNAVAILABLE"):
        reading.read_virtual_result(indexer, "a.pdf")
    indexer.config.exclude_patterns.remove("a.pdf")
    (indexer.vault_path / "a.pdf").unlink()
    with pytest.raises(reading.VirtualReadError, match="UNAVAILABLE"):
        reading.read_virtual_result(indexer, "a.pdf", allow_stale=True)
    publish(indexer, raw=b"a" * (1024 * 1024 + 1))
    indexer.config.ingest.max_file_size_mb = 1
    with pytest.raises(reading.VirtualReadError, match="RESOURCE_LIMIT"):
        reading.read_virtual_result(indexer, "a.pdf", allow_stale=True)


def test_deadline_no_stale_bypass(indexer, monkeypatch):
    publish(indexer)
    monkeypatch.setattr(reading, "time", type("Clock", (), {"monotonic": staticmethod(iter([0.0, 2.0]).__next__)})())
    with pytest.raises(reading.VirtualReadError, match="VERIFICATION_PENDING"):
        reading.read_virtual_result(indexer, "a.pdf", allow_stale=True)


@pytest.mark.parametrize("source", ["../a.pdf", "/a.pdf", "C:/a.pdf", "a/../b.pdf"])
def test_source_path_rejected(indexer, source, monkeypatch):
    store_accesses = []
    existing_store = indexer._existing_document_store

    def spy_existing_store():
        store_accesses.append("existing_document_store")
        return existing_store()

    monkeypatch.setattr(indexer, "_existing_document_store", spy_existing_store)
    with pytest.raises(SourcePathError) as caught:
        reading.read_virtual_result(indexer, source)
    assert caught.value.code == "SOURCE_PATH_INVALID"
    assert store_accesses == [], "非法 source 必须在访问任何 store 前拒绝"


def test_unreadable_ignore_fails_closed(indexer, monkeypatch):
    publish(indexer)
    ignore = indexer.vault_path / indexer.config.ignore_file
    ignore.write_text("a.pdf\n", encoding="utf-8")
    original = Path.read_text
    def unreadable(path, *args, **kwargs):
        if path == ignore:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(reading.VirtualReadError, match="UNAVAILABLE"):
        reading.read_virtual_result(indexer, "a.pdf", allow_stale=True)


def test_publication_during_read_fails_closed(indexer, monkeypatch):
    publish(indexer)
    original = reading.slice_text_result
    def publish_after_slice(*args, **kwargs):
        result = original(*args, **kwargs)
        publish(indexer, "new revision")
        return result
    monkeypatch.setattr(reading, "slice_text_result", publish_after_slice)
    with pytest.raises(reading.VirtualReadError, match="SOURCE_CHANGED"):
        reading.read_virtual_result(indexer, "a.pdf")


def test_duplicate_heading_and_empty_virtual(indexer):
    publish(indexer, "# A\nfirst\n# A\nsecond")
    with pytest.raises(ValueError, match="同名标题"):
        reading.read_virtual_result(indexer, "a.pdf", heading="A")
    publish(indexer, "")
    result = reading.read_virtual_result(indexer, "a.pdf")
    assert result.content == "" and result.total_lines == 0
    assert result.effective_start_line is None
