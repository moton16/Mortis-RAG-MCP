"""Production document/single/fanout paths; fake only the HTTP boundary."""
from dataclasses import replace
import json

import pytest

from mortis_rag_mcp.config import load_config, EmbeddingConfig
from mortis_rag_mcp.embedding_capabilities import resolve_embedding_profile
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.registry import VaultEntry
from mortis_rag_mcp.server import VaultMcpServer
from mortis_rag_mcp._server.fanout import fanout_search
from mortis_rag_mcp._indexer.token_chunking import embedding_key


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.body


@pytest.fixture
def transport(monkeypatch):
    calls = []
    state = {"failure": None}
    def send(request, **kwargs):
        payload = json.loads(request.data)
        calls.append((request.full_url, payload))
        if "input" in payload:
            if state["failure"] == "unknown":
                raise OSError("fixture lost response")
            vector = [1.0, 0.0] if request.full_url.endswith("/a") else [0.0, 1.0]
            if state["failure"] == "bad":
                vector = [0.0, 0.0]
            return Response({"data": [{"index": i, "embedding": vector}
                                     for i in range(len(payload["input"]))]})
        return Response({"results": [{"index": i, "relevance_score": 1.0}
                                     for i in range(len(payload["documents"]))]})
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", send)
    return calls, state


def config_file(tmp_path, *, query="{text}", document="{text}"):
    path = tmp_path / "app.toml"
    path.write_text(
        '[embedding]\nmode="external"\nmodel="synthetic"\ndimension=2\n'
        'endpoint="https://example.invalid/a"\nsend_dimensions=false\n'
        f'query_template={json.dumps(query)}\ndocument_template={json.dumps(document)}\n'
        f'[cache]\ndir="{(tmp_path / "cache").as_posix()}"\nplacement="home"\n',
        encoding="utf-8",
    )
    return path


def new_indexer(tmp_path, cfg, name="vault"):
    vault = tmp_path / name
    vault.mkdir(exist_ok=True)
    (vault / "a.md").write_text("fixture body " + name, encoding="utf-8")
    owner = MarkdownIndexer(vault, cfg)
    owner.sync()
    return owner


@pytest.mark.parametrize("q,d", [("{text}", "{text}"), ("q:{text}", "d:{text}")])
def test_real_document_and_single_query_templates(tmp_path, transport, q, d):
    calls, _ = transport
    owner = new_indexer(tmp_path, load_config(config_file(tmp_path, query=q, document=d)))
    try:
        chunk = owner.all_chunks()[0]
        assert calls[0][1]["input"] == [d.format(text=chunk.content)]
        assert chunk.metadata["embedding_key"] == embedding_key(
            d.format(text=chunk.content), owner._embedding_profile.fingerprint,
            owner._embedding_profile.preprocess_version, chunk.metadata.get("media_hashes", ()))
        calls.clear()
        owner.search(" fixture ", use_rerank=False)
        assert calls[0][1]["input"] == [q.format(text="fixture")]
        assert len(calls) == 1
    finally:
        owner.close_document_store()


@pytest.mark.parametrize("template", ["{other}", "{text.x}", "{text[0]}", "{", "{text:.3}", "{text}{text}", "no slot"])
def test_invalid_template_rejected_before_send(tmp_path, transport, template):
    with pytest.raises(ValueError):
        load_config(config_file(tmp_path, query=template))
    assert transport[0] == []


def test_programmatic_bad_template_type():
    with pytest.raises(ValueError):
        resolve_embedding_profile(EmbeddingConfig(query_template=123))


def setup_fanout(tmp_path, monkeypatch, *, different=False, templates=False):
    registry = tmp_path / "vaults.toml"
    registry.write_text("", encoding="utf-8")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(registry))
    monkeypatch.setattr(VaultMcpServer, "_start_background_index", lambda self: None)
    path = config_file(tmp_path, query="q:{text}" if templates else "{text}",
                       document="d:{text}" if templates else "{text}")
    server = VaultMcpServer(path)
    cfg = load_config(path)
    a = new_indexer(tmp_path, cfg, "A")
    cfg_b = replace(cfg, embedding=replace(cfg.embedding, endpoint="https://example.invalid/b")) if different else cfg
    b = new_indexer(tmp_path, cfg_b, "B")
    entries = [VaultEntry(str(x.vault_path.resolve()), x.vault_path.name, 1.0) for x in (a, b)]
    server.registry.save(entries)
    for owner in (a, b):
        server._indexers[str(owner.vault_path.resolve())] = owner
        monkeypatch.setattr(owner, "request_refresh", lambda **kwargs: None)
    return server, a, b


@pytest.mark.parametrize("different", [False, True])
def test_fanout_vector_reuse_only_same_space(tmp_path, monkeypatch, transport, different):
    calls, _ = transport
    server, a, b = setup_fanout(tmp_path, monkeypatch, different=different, templates=True)
    observed = {}
    for owner in (a, b):
        original = owner._vector_backend.query
        def spy(vector, limit, *, owner=owner, original=original):
            observed[owner.vault_path.name] = list(vector)
            return original(vector, limit)
        monkeypatch.setattr(owner._vector_backend, "query", spy)
    calls.clear()
    try:
        fanout_search(server, " fixture ", 5, False)
        assert len(calls) == (2 if different else 1)
        assert all(p["input"] == ["q:fixture"] for _, p in calls)
        assert observed["A"] == [1.0, 0.0]
        assert observed["B"] == ([0.0, 1.0] if different else [1.0, 0.0])
    finally:
        server.shutdown()


@pytest.mark.parametrize("failure", ["unknown", "bad"])
def test_fanout_failed_space_is_attempted_once(tmp_path, monkeypatch, transport, failure):
    calls, state = transport
    server, a, b = setup_fanout(tmp_path, monkeypatch)
    calls.clear()
    state["failure"] = failure
    try:
        result = fanout_search(server, " fixture ", 5, False)
        assert len(calls) == 1
        assert result["chunks"], "词法检索仍可用"
        if failure == "unknown":
            assert len(a.unresolved_paid_intents()) == 1
            assert b.unresolved_paid_intents() == []
    finally:
        server.shutdown()


def test_fanout_rerank_uses_selected_target(tmp_path, monkeypatch, transport):
    from mortis_rag_mcp.providers import create_reranker_provider
    calls, _ = transport
    server, a, b = setup_fanout(tmp_path, monkeypatch)
    c = new_indexer(tmp_path, a.config, "C")
    server._indexers = {str(c.vault_path.resolve()): c, **server._indexers}
    for owner in (c, b):
        owner.config = replace(owner.config, reranker=replace(owner.config.reranker, enabled=True,
                                endpoint=f"https://example.invalid/rerank/{owner.vault_path.name}"))
        owner.reranker_provider = create_reranker_provider(owner.config.reranker)
        owner.configure_paid_provider("rerank", owner.reranker_provider, owner._reranker_profile_fingerprint())
    calls.clear()
    try:
        fanout_search(server, "fixture", 5, True)
        reranks = [(url, p) for url, p in calls if "documents" in p]
        assert len(reranks) == 1 and reranks[0][0].endswith("/B")
        assert any(x["kind"] == "rerank" for x in b._paid_control_store().list_request_intents())
        assert not any(x["kind"] == "rerank" for x in c._paid_control_store().list_request_intents())
    finally:
        server.shutdown()


@pytest.mark.parametrize("field", ["query_template", "document_template"])
def test_template_change_rebuilds_new_space_preserving_old(tmp_path, transport, field):
    calls, _ = transport
    cfg = load_config(config_file(tmp_path))
    first = new_indexer(tmp_path, cfg)
    old_path, old_key = first._vectors_cache_path, first.all_chunks()[0].metadata["embedding_key"]
    old_bytes = old_path.read_bytes()
    first.close_document_store()
    calls.clear()
    cfg = replace(cfg, embedding=replace(cfg.embedding, **{field: "new:{text}"}))
    owner = MarkdownIndexer(tmp_path / "vault", cfg)
    try:
        assert all(c.embedding is None for c in owner.all_chunks())
        owner.sync()
        assert calls and owner._vectors_cache_path != old_path
        assert old_path.read_bytes() == old_bytes
        assert owner.all_chunks()[0].metadata["embedding_key"] != old_key
        assert owner._paid_control_store().list_payment_authorizations() == []
    finally:
        owner.close_document_store()


def test_pre_r2_compatible_cache_path_zero_reembed(tmp_path, transport):
    calls, _ = transport
    cfg = load_config(config_file(tmp_path))
    first = new_indexer(tmp_path, cfg)
    path = first._vectors_cache_path
    legacy = path.with_name(path.name.replace("." + first._embedding_profile.fingerprint, ""))
    first.close_document_store()
    path.rename(legacy)  # Synthetic historical filename, same proven codec/meta.
    calls.clear()
    owner = MarkdownIndexer(tmp_path / "vault", cfg)
    try:
        owner.sync()
        assert calls == [] and owner._vectors_cache_path == legacy
        assert all(c.embedding for c in owner.all_chunks())
    finally:
        owner.close_document_store()


@pytest.mark.parametrize("body", [
    {"data": []},
    {"data": [{"index": 0, "embedding": [1]}]},
    {"data": [{"index": 0, "embedding": [float("nan"), 1]}]},
    {"data": [{"index": 0, "embedding": [float("inf"), 1]}]},
    {"data": [{"index": 0, "embedding": [0, 0]}]},
    {"data": [{"index": 1, "embedding": [1, 0]}]},
])
def test_bad_response_on_real_sync_never_caches_vectors(tmp_path, monkeypatch, transport, body):
    cfg = load_config(config_file(tmp_path))
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", lambda *a, **k: Response(body))
    owner = new_indexer(tmp_path, cfg)
    try:
        assert owner.failed_files and not owner._vectors_cache_path.exists()
        assert all(c.embedding is None for c in owner.all_chunks())
    finally:
        owner.close_document_store()


def test_fanout_empty_or_cold_no_query_posts(tmp_path, monkeypatch, transport):
    calls, _ = transport
    server, a, b = setup_fanout(tmp_path, monkeypatch)
    calls.clear()
    try:
        fanout_search(server, "  ", 5, False)
        assert calls == []
        for owner in (a, b):
            owner.last_sync = None
            owner._chunks_cache_loaded = False
            owner._chunks.clear()
        fanout_search(server, "fixture", 5, False)
        assert calls == []
    finally:
        server.shutdown()


def test_failed_space_does_not_freeze_other_space(tmp_path, monkeypatch, transport):
    calls, _ = transport
    server, a, b = setup_fanout(tmp_path, monkeypatch, different=True)
    original = __import__("mortis_rag_mcp.providers", fromlist=["urlopen"]).urlopen
    def send(request, **kwargs):
        if request.full_url.endswith("/a"):
            calls.append((request.full_url, json.loads(request.data)))
            raise OSError("unknown A only")
        return original(request, **kwargs)
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", send)
    calls.clear()
    try:
        result = fanout_search(server, "fixture", 5, False)
        assert len(calls) == 2 and result["chunks"]
        assert len(a.unresolved_paid_intents()) == 1
        assert b.unresolved_paid_intents() == []
    finally:
        server.shutdown()
