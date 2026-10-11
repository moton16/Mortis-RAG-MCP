from __future__ import annotations

import json

import pytest

from mortis_rag_mcp.config import EmbeddingConfig
from mortis_rag_mcp.providers import create_embedding_provider


class _JsonResponse:
    def __init__(self, size: int) -> None:
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps({
            "data": [{"index": index, "embedding": [1.0]} for index in range(self.size)]
        }).encode("utf-8")


@pytest.mark.parametrize(
    ("declaration", "expected_input", "expected_hint"),
    [
        ("none", ["alpha", "beta"], None),
        ("typed", [{"text": "alpha"}, {"text": "beta"}], None),
        ("hint", ["alpha", "beta"], "text"),
    ],
)
def test_embedding_input_modality_serialization(
    monkeypatch, declaration, expected_input, expected_hint
):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data.decode("utf-8")))
        return _JsonResponse(2)

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    provider = create_embedding_provider(EmbeddingConfig(
        mode="external",
        endpoint="http://127.0.0.1:8000/v1/embeddings",
        dimension=1,
        send_dimensions=False,
        input_modality_declaration=declaration,
    ))

    assert provider.embed(["alpha", "beta"]) == [[1.0], [1.0]]
    assert captured["input"] == expected_input
    if expected_hint is None:
        assert "input_modality" not in captured
    else:
        assert captured["input_modality"] == expected_hint


def test_embedding_input_modality_defaults_to_none():
    assert EmbeddingConfig().input_modality_declaration == "none"


def test_embedding_input_modality_rejects_unknown_value():
    with pytest.raises(ValueError, match="input_modality_declaration"):
        EmbeddingConfig(input_modality_declaration="automatic")
