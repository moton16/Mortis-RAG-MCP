"""F07: confirmed response failures and unresolved sends survive text success/restart."""
import io
import json
import errno
import sqlite3
from urllib.error import HTTPError, URLError

import pytest

from test_e20_media_connection import (
    close, commit_media, make_indexer, media_config, native, offline, FakeText,
)


def derived(idx):
    return idx.document_store().get_derived_generation(idx._derived_profile_key())


def media_intents(idx):
    return [i for i in idx._paid_control_store().list_request_intents() if i["kind"] == "media"]


def valid(items):
    return [{"index": i, "embedding": [0, 1]} for i, _ in enumerate(items)]


@pytest.mark.parametrize("restart", [False, True])
def test_confirmed_contract_failure_preserved_then_recovered(tmp_path, restart):
    calls = []
    def malformed(items):
        calls.append("malformed")
        return [{"index": i, "embedding": [1]} for i, _ in enumerate(items)]
    cfg = media_config()
    idx = make_indexer(tmp_path, cfg, malformed)
    commit_media(idx)
    try:
        idx.sync()
        assert idx.failed_files["doc.pdf"].startswith("media_native:")
        assert derived(idx).status == "failed"
        assert "media_native:" in derived(idx).last_error
        assert not native(idx)
        assert any(c.embedding for c in idx._chunks["doc.pdf"] if c.metadata.get("kind") != "media_native")
        assert [i["state"] for i in media_intents(idx)] == ["abandoned"]
        assert media_intents(idx)[0]["reason"] == "media_response_contract_failed"
        assert idx._fts.search("before", 5), "text lexical layer remains searchable"
        if restart:
            close(idx)
            idx = make_indexer(tmp_path, cfg, valid)
            assert idx.failed_files["doc.pdf"].startswith("media_native:")
        else:
            idx.media_provider._transport = valid
        idx.sync()
        assert native(idx) and list(native(idx)[0].embedding) == [0.0, 1.0]
        assert idx.failed_files == {}
        assert derived(idx).status == "ready"
        assert derived(idx).last_error == ""
        assert [i["state"] for i in media_intents(idx)] == ["abandoned", "success"]
        assert [i["attempt"] for i in media_intents(idx)] == [1, 2]
    finally:
        close(idx)


@pytest.mark.parametrize("fault", ["count", "mapping", "dimension", "nan"])
def test_journal_success_only_after_complete_response_contract(tmp_path, fault):
    responses = {
        "count": [],
        "mapping": [{"index": 1, "embedding": [1, 0]}],
        "dimension": [{"index": 0, "embedding": [1]}],
        "nan": [{"index": 0, "embedding": [float("nan"), 1]}],
    }
    idx = make_indexer(tmp_path, media_config(), lambda items: responses[fault])
    commit_media(idx)
    try:
        idx.sync()
        assert [i["state"] for i in media_intents(idx)] == ["abandoned"]
        assert media_intents(idx)[0]["reason"] == "media_response_contract_failed"
        assert derived(idx).status == "failed"
        assert idx.failed_files["doc.pdf"].startswith("media_native:")
    finally:
        close(idx)


@pytest.mark.parametrize("adapter", ["openai_vl", "gemini"])
@pytest.mark.parametrize("fault", ["json", "structure", "401", "500", "429", "network"])
def test_http_outcomes_and_unknown_never_auto_resend(tmp_path, monkeypatch, adapter, fault):
    sends = []
    cfg = media_config(media_adapter=adapter, media_endpoint="https://googleapis.com/v1beta")
    def http(req, **kwargs):
        sends.append(req.full_url)
        if fault in ("401", "500", "429"):
            raise HTTPError(req.full_url, int(fault), "offline status", {},
                            io.BytesIO(b'{"error":{"message":"fixture rejection"}}'))
        if fault == "network":
            raise URLError("offline lost response")
        return io.BytesIO(b"not json" if fault == "json" else b'{"unexpected":[]}')
    monkeypatch.setattr("mortis_rag_mcp.media_providers.urlopen", http)
    idx = make_indexer(tmp_path, cfg)
    commit_media(idx)
    unknown = fault in ("500", "429", "network")
    try:
        idx.sync()
        expected = "submission_unknown" if unknown else "abandoned"
        assert [i["state"] for i in media_intents(idx)] == [expected]
        assert derived(idx).status == "failed"
        assert idx.failed_files["doc.pdf"].startswith("media_native:")
        close(idx)
        idx = make_indexer(tmp_path, cfg)
        if not unknown:
            idx.media_provider._transport = valid
        idx.sync()
        if unknown:
            assert sends == [cfg.media_endpoint]
            assert not native(idx)
            assert derived(idx).status == "failed"
            assert len(media_intents(idx)) == 1
            assert idx.failed_files["doc.pdf"].startswith("media_native:")
        else:
            assert native(idx)
            assert derived(idx).status == "ready"
    finally:
        close(idx)


@pytest.mark.parametrize("unknown", [False, True])
def test_multi_batch_keeps_successful_prefix_and_does_not_pay_twice(tmp_path, unknown):
    calls = []
    def transport(items):
        calls.append([i.request_id for i in items])
        if len(calls) == 2:
            if unknown:
                raise URLError("lost response")
            return [{"index": 0, "embedding": [1]}]
        return valid(items)
    cfg = media_config(media_max_batch_size=1)
    idx = make_indexer(tmp_path, cfg, transport)
    commit_media(idx, count=2)
    try:
        idx.sync()
        assert len(calls) == 2
        assert len(native(idx)) == 1, "successful native batch is durable even if a later batch fails"
        assert idx.failed_files["doc.pdf"].startswith("media_native:")
        assert derived(idx).status == "failed"
        prefix_id = native(idx)[0].id
        close(idx)
        idx = make_indexer(tmp_path, cfg, transport)
        idx.sync()
        assert any(c.id == prefix_id for c in native(idx))
        if unknown:
            assert len(calls) == 2
            assert len(native(idx)) == 1
            assert [i["state"] for i in media_intents(idx)] == ["success", "submission_unknown"]
            assert derived(idx).status == "failed"
        else:
            assert len(calls) == 3
            assert calls[2] == calls[1] and calls[2] != calls[0]
            assert len(native(idx)) == 2
            assert derived(idx).status == "ready"
            assert [i["state"] for i in media_intents(idx)] == ["success", "abandoned", "success"]
    finally:
        close(idx)


def test_proxy_failure_is_not_erased_by_successful_text_embedding(tmp_path, monkeypatch):
    from mortis_rag_mcp._indexer import sync_engine
    original = sync_engine.media_proxy_for_revision
    def broken(*args, **kwargs):
        raise RuntimeError("fixture proxy build failed")
    monkeypatch.setattr(sync_engine, "media_proxy_for_revision", broken)
    idx = make_indexer(tmp_path, media_config(), valid)
    commit_media(idx)
    try:
        idx.sync()
        assert idx.failed_files["doc.pdf"].startswith("media_proxy:")
        assert derived(idx).status == "failed"
        assert idx._chunks["doc.pdf"][0].embedding
        monkeypatch.setattr(sync_engine, "media_proxy_for_revision", original)
        idx.sync()
        assert native(idx)
        assert not idx.failed_files
        assert derived(idx).status == "ready"
    finally:
        close(idx)


def test_media_retry_reuses_text_vectors_without_another_text_charge(tmp_path):
    idx = make_indexer(tmp_path, media_config(),
                       lambda items: [{"index": i, "embedding": [1]} for i, _ in enumerate(items)])
    commit_media(idx)
    text_calls = []
    def text_embed(texts):
        text_calls.append(list(texts))
        return [[1, 0] for _ in texts]
    idx.embedding_provider.embed = text_embed
    try:
        idx.sync()
        assert len(text_calls) == 1
        idx.media_provider._transport = valid
        idx.sync()
        assert len(text_calls) == 1
        assert derived(idx).status == "ready"
    finally:
        close(idx)


def test_failed_derived_outcome_recovers_when_failure_cache_is_missing(tmp_path):
    cfg = media_config()
    idx = make_indexer(tmp_path, cfg, lambda items: [{"index": 0, "embedding": [1]}])
    commit_media(idx)
    idx.sync()
    failure_path = idx._failed_cache_path
    close(idx)
    failure_path.unlink()
    idx = make_indexer(tmp_path, cfg, valid)
    try:
        idx.sync()
        assert native(idx), "durable derived failure must not depend solely on a best-effort cache"
        assert derived(idx).status == "ready"
        assert not idx.failed_files
    finally:
        close(idx)


def test_supported_media_blob_read_failure_is_a_failed_stage(tmp_path, monkeypatch):
    idx = make_indexer(tmp_path, media_config(), valid)
    commit_media(idx)
    store = idx.document_store(write=True)
    original = store.read_media
    def broken(*args, **kwargs):
        raise RuntimeError("fixture missing supported media blob")
    monkeypatch.setattr(store, "read_media", broken)
    try:
        idx.sync()
        assert idx.failed_files["doc.pdf"].startswith("media_native:")
        assert derived(idx).status == "failed"
        assert not media_intents(idx), "local input failure must not be recorded as a paid send"
        monkeypatch.setattr(store, "read_media", original)
        idx.sync()
        assert native(idx) and derived(idx).status == "ready"
    finally:
        close(idx)


def test_unknown_settled_source_does_not_rewrite_cache_on_every_sync(tmp_path, monkeypatch):
    def lost(items):
        raise URLError("lost response")
    idx = make_indexer(tmp_path, media_config(), lost)
    commit_media(idx)
    try:
        idx.sync()
        assert derived(idx).status == "failed"
        saves = []
        monkeypatch.setattr(idx, "_save_cache", lambda: saves.append("save"))
        idx.sync()
        idx.sync()
        assert saves == []
        assert len(media_intents(idx)) == 1
    finally:
        close(idx)


def test_declared_media_factory_failure_is_not_false_ready(tmp_path):
    from mortis_rag_mcp.config import AppConfig, CacheConfig
    from mortis_rag_mcp.doc_store import resolve_storage_layout
    from mortis_rag_mcp.indexer import MarkdownIndexer
    from mortis_rag_mcp.providers import create_media_provider
    cfg = media_config(media_model_reference="")
    config = AppConfig(embedding=cfg, cache=CacheConfig(
        enabled=True, dir=str(tmp_path / "cache"), embedding_max_workers=1))
    vault = tmp_path / "vault"
    vault.mkdir()
    idx = MarkdownIndexer(vault, config, embedding_provider=FakeText())
    idx._storage_layout = resolve_storage_layout(config, vault, registered_vaults=[])
    assert idx.media_provider is None and idx._media_capability_error
    commit_media(idx)
    try:
        idx.sync()
        assert idx.failed_files["doc.pdf"].startswith("media_native:")
        assert derived(idx).status == "failed"
        assert idx.index_state()["index_state"] != "ready"
        assert "media derivation failed" in idx.index_state()["next_action"]
        assert idx._fts.search("before", 5)
        assert not media_intents(idx)
        repaired_cfg = media_config()
        idx.media_provider = create_media_provider(repaired_cfg, transport=valid)
        idx._media_capability_error = None
        idx.configure_paid_provider("media", idx.media_provider, idx.media_provider.profile.fingerprint)
        idx.sync()
        assert native(idx)
        assert derived(idx).status == "ready"
        assert idx.index_state()["index_state"] == "ready"
    finally:
        close(idx)


def _native_vectors(idx):
    return {c.id: list(idx._vector_backend.get_vectors([c.id])[c.id])
            for c in native(idx)}


def _real_store_reopen_evidence(idx):
    """An independent SQLite connection observes the production committed facts."""
    store = idx.document_store()
    path = store.layout.generations_dir / store.generation_id / "docstore.sqlite"
    assert path.is_file()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        return conn.execute("SELECT COUNT(*) FROM media_occurrences").fetchone()[0]


@pytest.mark.parametrize("backend", ["memory", "sqlite_vec"])
@pytest.mark.parametrize("stage", ["blob", "proxy"])
def test_review_c02_local_blob_retry_preserves_verified_prefix_across_restarts(tmp_path, monkeypatch, backend, stage):
    from mortis_rag_mcp._indexer import sync_engine
    sends = []
    def transport(items):
        sends.extend(item.request_id.rsplit("\0", 1)[-1] for item in items)
        if len(sends) == 2:
            return [{"index": 0, "embedding": [1]}]
        return valid(items)
    cfg = media_config(media_max_batch_size=1)
    idx = make_indexer(tmp_path, cfg, transport, backend=backend)
    text_calls = []
    def text_embed(texts):
        text_calls.append(list(texts))
        return [[1, 0] for _ in texts]
    idx.embedding_provider.embed = text_embed
    revision_id = commit_media(idx, count=2)
    try:
        idx.sync()
        assert _real_store_reopen_evidence(idx) == 2
        assert sends == ["fixture-occ-0", "fixture-occ-1"]
        original_prefix = _native_vectors(idx)
        assert len(original_prefix) == 1
        assert derived(idx).status == "failed"
        # Two separate restarts/local failure rounds must retain the same first batch.
        for _ in range(2):
            close(idx)
            idx = make_indexer(tmp_path, cfg, transport, backend=backend)
            idx.embedding_provider.embed = text_embed
            store = idx.document_store(write=True)
            original_read = store.read_media
            def temporarily_missing(source, **kwargs):
                if kwargs["occurrence_id"] == "fixture-occ-1":
                    raise OSError(errno.EIO, "fixture second blob temporarily unavailable")
                return original_read(source, **kwargs)
            with monkeypatch.context() as patch:
                if stage == "blob":
                    patch.setattr(store, "read_media", temporarily_missing)
                else:
                    def broken_proxy(*args, **kwargs):
                        raise OSError(errno.EIO, "fixture proxy stage temporarily unavailable")
                    patch.setattr(sync_engine, "media_proxy_for_revision", broken_proxy)
                idx.sync()
            assert _native_vectors(idx) == original_prefix
            assert sends == ["fixture-occ-0", "fixture-occ-1"]
            assert derived(idx).status == "failed"
            assert idx.failed_files["doc.pdf"].startswith("media_native:" if stage == "blob" else "media_proxy:")
            assert idx._fts.search("before", 5)
            assert idx.document_store().get_active("doc.pdf").revision.revision_id == revision_id
            assert [i["state"] for i in media_intents(idx)] == ["success", "abandoned"]
            assert len(text_calls) == 1
        close(idx)
        idx = make_indexer(tmp_path, cfg, transport, backend=backend)
        idx.embedding_provider.embed = text_embed
        idx.sync()
        assert sends == ["fixture-occ-0", "fixture-occ-1", "fixture-occ-1"]
        assert all(_native_vectors(idx)[key] == value for key, value in original_prefix.items())
        assert len(native(idx)) == 2
        assert derived(idx).status == "ready"
        assert idx.failed_files == {}
        assert [i["state"] for i in media_intents(idx)] == ["success", "abandoned", "success"]
        assert sends.count("fixture-occ-0") == 1
        assert len(text_calls) == 1
    finally:
        close(idx)


@pytest.mark.parametrize("backend", ["memory", "sqlite_vec"])
@pytest.mark.parametrize("drift", ["revision", "profile"])
def test_review_c02_drift_never_preserves_obsolete_native_prefix(tmp_path, monkeypatch, backend, drift):
    from dataclasses import replace
    sends = []
    def transport(items):
        sends.extend(item.request_id for item in items)
        return [{"index": 0, "embedding": [1]}] if len(sends) == 2 else valid(items)
    cfg = media_config(media_max_batch_size=1)
    idx = make_indexer(tmp_path, cfg, transport, backend=backend)
    first_revision = commit_media(idx, count=2)
    try:
        idx.sync()
        old_prefix_id = native(idx)[0].id
        close(idx)
        if drift == "profile":
            cfg = replace(cfg, media_model="fixture-media-new-profile")
        idx = make_indexer(tmp_path, cfg, transport, backend=backend)
        if drift == "revision":
            assert commit_media(idx, count=2) != first_revision
        store = idx.document_store(write=True)
        original = store.read_media
        def unavailable(source, **kwargs):
            if kwargs["occurrence_id"] == "fixture-occ-1":
                raise OSError(errno.EIO, "fixture second blob temporarily unavailable")
            return original(source, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(store, "read_media", unavailable)
            idx.sync()
        assert native(idx) == []
        assert idx._vector_backend.get_vectors([old_prefix_id]) == {}
        assert len(sends) == 2
        assert derived(idx).status == "failed"
        close(idx)
        idx = make_indexer(tmp_path, cfg, transport, backend=backend)
        idx.sync()
        assert len(sends) == 4
        assert len(native(idx)) == 2
        assert all(c.id != old_prefix_id for c in native(idx))
        assert derived(idx).status == "ready"
    finally:
        close(idx)


@pytest.mark.parametrize("field", ["source", "revision", "profile", "occurrence", "blob"])
def test_review_c02_prefix_requires_all_five_cached_identity_fields(tmp_path, monkeypatch, field):
    sends = []
    def transport(items):
        sends.extend(item.request_id for item in items)
        return [{"index": 0, "embedding": [1]}] if len(sends) == 2 else valid(items)
    idx = make_indexer(tmp_path, media_config(media_max_batch_size=1), transport)
    commit_media(idx, count=2)
    try:
        idx.sync()
        cached = native(idx)[0]
        if field == "source":
            cached.source = "other.pdf"
        else:
            key = {"revision": "revision_id", "profile": "profile_key",
                   "occurrence": "occurrence_id", "blob": "blob_sha256"}[field]
            cached.metadata[key] = "foreign-identity"
        store = idx.document_store(write=True)
        original = store.read_media
        def unavailable(source, **kwargs):
            if kwargs["occurrence_id"] == "fixture-occ-1":
                raise OSError(errno.EIO, "fixture second blob temporarily unavailable")
            return original(source, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(store, "read_media", unavailable)
            idx.sync()
        assert not native(idx)
        assert len(sends) == 2
        assert derived(idx).status == "failed"
    finally:
        close(idx)
