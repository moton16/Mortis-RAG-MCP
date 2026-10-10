from dataclasses import FrozenInstanceError, replace
import hashlib
import sqlite3
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from mortis_rag_mcp.embedding_capabilities import (
    EmbeddingContractError, ResolvedEmbeddingProfile, embed_with_profile,
    resolve_embedding_profile, validate_vector,
)
from mortis_rag_mcp.media_providers import EmbeddingInput, NativeMediaEvidence, NativeMediaProvider
from mortis_rag_mcp.providers import ExternalEmbeddingProvider, ProviderError
from mortis_rag_mcp.vector import bind_vector_space, VectorSpaceMismatch


def config(**changes):
    values = dict(mode="external", model="unknown", endpoint="https://example.test/embed", dimension=2,
                  send_dimensions=False, capability_profile="", client_slicing=False, adapter="openai")
    values.update(changes)
    return SimpleNamespace(**values)


def profile():
    return resolve_embedding_profile(config())


def test_profile_immutable_and_endpoint_identity():
    p = profile()
    with pytest.raises(FrozenInstanceError):
        p.model = "other"
    assert p.fingerprint != resolve_embedding_profile(config(endpoint="https://else.test/embed")).fingerprint
    assert p.fingerprint == resolve_embedding_profile(config(endpoint="HTTPS://EXAMPLE.TEST/embed/")).fingerprint


def test_fixed_exact_registry_does_not_guess_family():
    assert resolve_embedding_profile(config(model="BAAI/bge-m3", dimension=1024)).native_dim == 1024
    for changes in ({"dimension": 512}, {"send_dimensions": True}, {"client_slicing": True}):
        with pytest.raises(EmbeddingContractError):
            resolve_embedding_profile(config(**{"model": "BAAI/bge-m3", "dimension": 1024, **changes}))
    assert resolve_embedding_profile(config(model="my-bge-m3-fork")).native_dim == 2
    with pytest.raises(EmbeddingContractError):
        resolve_embedding_profile(config(capability_profile="EG2"))
    with pytest.raises(EmbeddingContractError):
        resolve_embedding_profile(config(client_slicing=True))


@pytest.mark.parametrize("vector", [[True, 1], [float("nan"), 1], [float("inf"), 1], [1e39, 1], [0, 0], [1], ["1", 1]])
def test_invalid_vectors(vector):
    with pytest.raises(EmbeddingContractError):
        validate_vector(vector, profile())


def test_mrl_native_validation_before_slice_and_l2():
    p = replace(profile(), native_dim=4, dimensions_protocol="client", verified_mrl_dims=(2,))
    assert validate_vector([3, 4, 2, 1], p) == pytest.approx([0.6, 0.8])
    with pytest.raises(EmbeddingContractError):
        validate_vector([3, 4], p)
    with pytest.raises(EmbeddingContractError):
        validate_vector([0, 0, 2, 1], p)
    with pytest.raises(EmbeddingContractError):
        validate_vector([3, 4, 2, 1], replace(p, verified_mrl_dims=()))


@pytest.mark.parametrize("index", [True, 0.0, "0"])
def test_response_rejects_coerced_indexes(index):
    provider = ExternalEmbeddingProvider("https://example.test", dimension=2, profile=profile())
    provider._post = lambda payload: {"data": [{"index": index, "embedding": [3, 4]}]}
    with pytest.raises(ProviderError):
        provider.embed(["x"])


def test_response_order_and_old_unindexed_adapter():
    provider = ExternalEmbeddingProvider("https://example.test", dimension=2, profile=profile())
    provider._post = lambda payload: {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
    assert provider.embed(["x", "y"]) == [[1, 0], [0, 1]]
    provider._post = lambda payload: {"data": [{"embedding": [3, 4]}]}
    assert provider.embed(["x"])[0] == pytest.approx([0.6, 0.8])
    assert provider.embed([]) == []


def test_query_document_fake_compatibility():
    calls = []
    fake = SimpleNamespace(embed=lambda texts: calls.append(texts) or [[3, 4]])
    p = replace(profile(), query_template="q:{text}", document_template="d:{text}")
    assert embed_with_profile(fake, ["x"], p, query=True) == embed_with_profile(fake, ["x"], p)
    assert calls == [["q:x"], ["d:x"]]


def test_post_gate_and_single_send_unknown(monkeypatch):
    provider = ExternalEmbeddingProvider("https://example.test", max_retries=99)
    sends = []
    def send(*args, **kwargs):
        sends.append(1)
        raise URLError("lost response")
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", send)
    with pytest.raises(ProviderError, match="CONTROL_UNAVAILABLE"):
        provider.embed(["x"])
    assert sends == []
    events = []
    journal = SimpleNamespace(before_send=lambda *args: events.append("intent") or "id",
                              mark_unknown=lambda *args: events.append("unknown"),
                              mark_success=lambda *args: events.append("success"))
    provider.configure_paid_requests(journal, lambda p: True, "approved")
    with pytest.raises(ProviderError, match="SUBMISSION_UNKNOWN"):
        provider.embed(["x"])
    assert sends == [1]
    assert events == ["intent", "unknown"]


def test_persistent_space_meta_restart_and_legacy(tmp_path):
    path = tmp_path / "vectors.sqlite"
    with sqlite3.connect(path) as conn:
        bind_vector_space(conn, profile().fingerprint)
    with sqlite3.connect(path) as conn:
        bind_vector_space(conn, profile().fingerprint)
        with pytest.raises(VectorSpaceMismatch):
            bind_vector_space(conn, "other")
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE vec_ids (chunk_id TEXT)")
        conn.execute("INSERT INTO vec_ids VALUES ('old')")
        with pytest.raises(VectorSpaceMismatch):
            bind_vector_space(conn, "new")
        assert conn.execute("SELECT chunk_id FROM vec_ids").fetchone() == ("old",)


def test_native_media_evidence_and_request_id_mapping():
    p = replace(profile(), modalities=("text", "image"), alignment_space_id="verified-space")
    evidence = NativeMediaEvidence("model", "fixture", "alignment", "license", p.fingerprint, "verified-space", 10, 2, ("image/png",))
    media = EmbeddingInput("a", "image", b"image", "image/png", hashlib.sha256(b"image").hexdigest())
    native = NativeMediaProvider(p, evidence, lambda items: [{"request_id": "a", "embedding": [3, 4]}])
    with pytest.raises(ProviderError, match="PENDING_APPROVAL"):
        native.embed_media([media])
    journal = SimpleNamespace(before_send=lambda *args: "id", mark_success=lambda *args: None, mark_unknown=lambda *args: None)
    native.configure_paid_requests(journal, lambda fingerprint: True, p.fingerprint)
    assert native.embed_media([media])[0] == pytest.approx([0.6, 0.8])
    with pytest.raises(ProviderError):
        NativeMediaProvider(p, replace(evidence, alignment_reference=""), lambda items: [])
    with pytest.raises(ProviderError):
        native.embed_media([replace(media, media_hash="wrong")])


def test_embeddinggemma2_profile_resolution():
    eg2 = resolve_embedding_profile(config(model="embeddinggemma2", dimension=768, capability_profile="embeddinggemma2"))
    assert eg2.native_dim == 768
    assert eg2.max_context == 8192
    assert eg2.effective_dim == 768
    assert eg2.evidence_reference == "exact-fixed-text-contract"
    eg2_alias = resolve_embedding_profile(config(model="google/embeddinggemma-2", dimension=768, capability_profile="embeddinggemma2"))
    assert eg2_alias.native_dim == 768
    with pytest.raises(EmbeddingContractError):
        resolve_embedding_profile(config(model="embeddinggemma2", dimension=1024, capability_profile="embeddinggemma2"))
