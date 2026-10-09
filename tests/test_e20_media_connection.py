"""F05/F06: real factories and journal, synthetic HTTP only."""
from dataclasses import replace
import hashlib
import io
import json
import socket

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig, load_config
from mortis_rag_mcp.doc_store import MediaOccurrenceSpec, resolve_storage_layout
from mortis_rag_mcp.embedding_capabilities import resolve_media_profile
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.media_providers import EmbeddingInput
from mortis_rag_mcp.paid_requests import PaidRequestJournal
from mortis_rag_mcp.providers import create_media_provider
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("E20 fixture attempted a real network connection")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", deny)
    monkeypatch.setattr("mortis_rag_mcp.media_providers.urlopen", deny)
    for name in ("MORTIS_RAG_API_KEY", "VAULT_MCP_API_KEY", "E20_MEDIA_KEY"):
        monkeypatch.delenv(name, raising=False)


def media_config(**changes):
    cfg = EmbeddingConfig(
        mode="external", endpoint="https://text.invalid/embed", model="fixture-text",
        dimension=2, send_dimensions=False, media_adapter="openai_vl",
        media_endpoint="https://media-a.invalid/embed", media_model="fixture-media",
        media_dimension=2, media_api_key="synthetic-key",
        media_modalities=("image",), media_alignment_space_id="fixture-shared-space",
        media_preprocess_version="fixture-media-v1", media_endpoint_revision="fixture-ep-v1",
        media_allowed_mime_types=("image/png",), media_max_input_bytes=4096,
        media_max_batch_size=4, media_model_reference="fixture-model-ref",
        media_endpoint_fixture_reference="fixture-endpoint-ref",
        media_alignment_reference="fixture-alignment-ref", media_license_reference="fixture-license-ref")
    return replace(cfg, **changes)


class FakeText:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def make_indexer(folder, cfg, transport=None):
    folder.mkdir(exist_ok=True)
    vault = folder / "vault"
    vault.mkdir(exist_ok=True)
    config = AppConfig(embedding=cfg, cache=CacheConfig(
        enabled=True, dir=str(folder / "cache"), embedding_max_workers=1))
    idx = MarkdownIndexer(vault, config, embedding_provider=FakeText(),
                          media_provider=create_media_provider(cfg, transport=transport))
    idx._storage_layout = resolve_storage_layout(config, vault, registered_vaults=[])
    return idx


def commit_media(idx, count=1):
    source = idx.vault_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.4 E20 synthetic fixture")
    body = "# Fixture\n\nbefore media after\n"
    store = idx.document_store(write=True)
    rev = store.stage_revision(
        source="doc.pdf", source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        render_sha256=hashlib.sha256(body.encode()).hexdigest(),
        parser_fingerprint="fixture-parser", markdown=body)
    specs = []
    for ordinal in range(count):
        blob = store.put_media_blob(data=b"\x89PNG\r\n\x1a\n" + bytes([ordinal + 1]) * 16,
                                    mime_type="image/png", width=8, height=8)
        start = body.index("media")
        specs.append(MediaOccurrenceSpec(
            occurrence_id=f"fixture-occ-{ordinal}", blob_id=blob, kind="image", ordinal=ordinal,
            mime_type="image/png", caption="fixture media", page=1,
            anchor_start=start, anchor_end=start + 5))
    store.attach_occurrences(rev.revision_id, specs)
    store.commit_revision(rev.revision_id)
    return rev.revision_id


def native(idx):
    return [c for c in idx._chunks.get("doc.pdf", ()) if c.metadata.get("kind") == "media_native"]


def close(idx):
    idx.stop_watching()
    idx.close_document_store()
    if idx._fts is not None:
        idx._fts.close()


def toml_config(tmp_path, cfg):
    lines = []
    for key in cfg.__dataclass_fields__:
        value = getattr(cfg, key)
        if value is not None:
            lines.append(f"{key} = {json.dumps(list(value) if isinstance(value, tuple) else value)}")
    path = tmp_path / "app.toml"
    path.write_text("[embedding]\n" + "\n".join(lines) +
                    '\n[cache]\nenabled = true\ndir = "' + (tmp_path / "cache").as_posix() + '"\n',
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("adapter", ["openai_vl", "gemini", "embeddinggemma2"])
def test_env_only_factory_and_server_assembly(tmp_path, monkeypatch, adapter):
    monkeypatch.setenv("E20_MEDIA_KEY", "dedicated-env-key")
    cfg = media_config(media_adapter=adapter, media_endpoint="https://googleapis.com/v1beta",
                       media_api_key="", api_key="", media_api_key_env="E20_MEDIA_KEY")
    loaded = load_config(toml_config(tmp_path, cfg))
    provider = create_media_provider(loaded.embedding)
    assert provider._transport.api_key == "dedicated-env-key"
    server = object.__new__(VaultMcpServer)
    server.config = loaded
    server._indexers = {}
    assembled, error = server._media_provider_and_error()
    assert error is None and assembled is not None
    assert assembled.profile.fingerprint == provider.profile.fingerprint
    assert assembled._transport.api_key == "dedicated-env-key"


@pytest.mark.parametrize("route", ["programmatic", "toml"])
@pytest.mark.parametrize("dedicated,general,env,global_key,expected", [
    ("dedicated", "general", "env", "global", "dedicated"),
    ("", "general", "env", "", "general"),
    ("", "", "env", "", "env"),
    ("", "", "", "global", "global"),
])
def test_auth_precedence(tmp_path, monkeypatch, route, dedicated, general, env, global_key, expected):
    monkeypatch.setenv("E20_MEDIA_KEY", env)
    monkeypatch.setenv("MORTIS_RAG_API_KEY", global_key)
    cfg = media_config(media_api_key=dedicated, api_key=general, media_api_key_env="E20_MEDIA_KEY")
    if route == "toml":
        cfg = load_config(toml_config(tmp_path, cfg)).embedding
    assert create_media_provider(cfg)._transport.api_key == expected


@pytest.mark.parametrize("adapter", ["openai_vl", "gemini", "embeddinggemma2"])
def test_resolved_profile_is_transport_and_journal_identity(tmp_path, monkeypatch, adapter):
    cfg = media_config(media_adapter=adapter, media_endpoint="https://googleapis.com/v1beta",
                       media_model="actual-media", media_dimension=3, send_dimensions=True)
    provider = create_media_provider(cfg)
    profile = provider.profile
    assert (profile.adapter, profile.endpoint, profile.model, profile.effective_dim) == (
        adapter, provider._transport.endpoint, provider._transport.model, provider._transport.dimension)
    assert profile.request_dim == 3
    assert resolve_media_profile(cfg) == profile
    idx = make_indexer(tmp_path, cfg)
    try:
        provider.configure_paid_requests(PaidRequestJournal(idx._paid_control_store, "media"),
                                         lambda fp: True, profile.fingerprint)
        sent = []
        def fake_http(req, **kwargs):
            sent.append((req.full_url, json.loads(req.data)))
            body = ({"data": [{"index": 0, "embedding": [1, 0, 0]}]} if adapter == "openai_vl"
                    else {"embeddings": [{"values": [1, 0, 0]}]})
            return io.BytesIO(json.dumps(body).encode())
        monkeypatch.setattr("mortis_rag_mcp.media_providers.urlopen", fake_http)
        data = b"\x89PNG\r\n\x1a\nfixture"
        provider.embed_media([EmbeddingInput("a", "image", data, "image/png", hashlib.sha256(data).hexdigest())])
        intent = idx._paid_control_store().list_request_intents()[0]
        assert intent["endpoint"] == sent[0][0] == profile.endpoint
        assert intent["profile_fingerprint"] == profile.fingerprint
        assert intent["state"] == "success"
        body = sent[0][1] if adapter == "openai_vl" else sent[0][1]["requests"][0]
        assert body["model"].removeprefix("models/") == profile.model
        assert body.get("dimensions", body.get("output_dimensionality")) == profile.request_dim
    finally:
        close(idx)


@pytest.mark.parametrize("change", [
    {"media_endpoint": "https://media-b.invalid/embed"},
    {"media_model": "fixture-media-b"},
    {"media_dimension": 3},
    {"media_adapter": "gemini"},
])
def test_connection_drift_changes_only_media_profile(change):
    from mortis_rag_mcp.embedding_capabilities import resolve_embedding_profile
    cfg = media_config()
    assert resolve_media_profile(cfg).fingerprint != resolve_media_profile(replace(cfg, **change)).fingerprint
    assert resolve_embedding_profile(cfg).fingerprint == resolve_embedding_profile(replace(cfg, **change)).fingerprint


@pytest.mark.parametrize("change", [
    {"media_endpoint": "https://media-b.invalid/embed"},
    {"media_model": "fixture-media-b"},
])
def test_endpoint_or_model_drift_rebuilds_native_after_restart(tmp_path, change):
    calls = []
    def old(items):
        calls.append("old")
        return [{"index": i, "embedding": [1, 0]} for i, _ in enumerate(items)]
    def new(items):
        calls.append("new")
        return [{"index": i, "embedding": [0, 1]} for i, _ in enumerate(items)]
    cfg = media_config()
    one = make_indexer(tmp_path, cfg, old)
    commit_media(one)
    one.sync()
    old_id = native(one)[0].id
    close(one)
    two = make_indexer(tmp_path, replace(cfg, **change), new)
    try:
        two.sync()
        assert calls == ["old", "new"]
        assert native(two)[0].id != old_id
        assert list(native(two)[0].embedding) == [0.0, 1.0]
    finally:
        close(two)
