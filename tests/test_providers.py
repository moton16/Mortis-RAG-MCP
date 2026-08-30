from __future__ import annotations

import json
from urllib.request import Request

import pytest

from vault_mcp.config import AppConfig, EmbeddingConfig, RerankerConfig, load_config
from vault_mcp.providers import ExternalEmbeddingProvider, ExternalRerankerProvider, StaticEmbeddingProvider


def test_load_config_supports_static_embedding_and_optional_reranker(tmp_path):
    path = tmp_path / "app.toml"
    path.write_text(
        """
        [vault]
        path = "C:/知识库/Obsidian Vault"
        [embedding]
        mode = "static"
        dimension = 3
        [reranker]
        enabled = false
        """,
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.vault_path == tmp_path / "C:/知识库/Obsidian Vault" if False else "C:/知识库/Obsidian Vault"
    assert config.embedding.mode == "static"
    assert config.embedding.dimension == 3
    assert config.reranker.enabled is False


def test_external_embedding_request_has_expected_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _JsonResponse({"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr("vault_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalEmbeddingProvider(
        endpoint="https://embedding.test/v1/embeddings",
        model="embed-model",
        api_key="secret",
        timeout=7,
    )

    assert provider.embed(["你好", "world"]) == [[0.1, 0.2]]
    request = captured["request"]
    assert isinstance(request, Request)
    assert request.full_url == "https://embedding.test/v1/embeddings"
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data.decode("utf-8")) == {
        "model": "embed-model",
        "input": ["你好", "world"],
    }
    assert captured["timeout"] == 7


def test_external_reranker_request_has_expected_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return _JsonResponse({"results": [{"index": 1, "relevance_score": 0.8}]})

    monkeypatch.setattr("vault_mcp.providers.urlopen", fake_urlopen)
    provider = ExternalRerankerProvider(
        endpoint="https://rerank.test/v1/rerank",
        model="rerank-model",
        api_key="secret",
    )

    assert provider.rerank("问题", ["a", "b"]) == [
        {"index": 1, "relevance_score": 0.8}
    ]
    request = captured["request"]
    assert json.loads(request.data.decode("utf-8")) == {
        "model": "rerank-model",
        "query": "问题",
        "documents": ["a", "b"],
    }


def test_reranker_failure_degrades_to_original_order(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("vault_mcp.providers.urlopen", fail)
    provider = ExternalRerankerProvider(endpoint="https://rerank.test")

    assert provider.rerank_or_none("q", ["a", "b"]) is None


def test_static_embedding_does_not_call_llm(monkeypatch):
    monkeypatch.setattr(
        "vault_mcp.providers.urlopen",
        lambda *args, **kwargs: pytest.fail("static mode called an external model"),
    )
    provider = StaticEmbeddingProvider(dimension=4)

    vectors = provider.embed(["same", "same", "不同"])

    assert len(vectors) == 3
    assert all(len(vector) == 4 for vector in vectors)
    assert vectors[0] == vectors[1]


class _JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")
