from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import EmbeddingConfig, RerankerConfig


class ProviderError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class RerankerProvider(Protocol):
    def rerank(self, query: str, documents: Sequence[str]) -> list[dict[str, Any]]: ...


class StaticEmbeddingProvider:
    """Deterministic local hash embeddings; it never performs network/LLM calls."""

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        for index in range(self.dimension):
            byte = digest[index % len(digest)]
            values[index] = (byte / 127.5) - 1.0
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]


class _JsonHttpProvider:
    def __init__(self, endpoint: str, api_key: str = "", timeout: float = 30.0) -> None:
        if not endpoint:
            raise ValueError("external provider endpoint is required")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, payload: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"provider request failed: {exc}") from exc


class ExternalEmbeddingProvider(_JsonHttpProvider):
    def __init__(self, endpoint: str, model: str = "", api_key: str = "", timeout: float = 30.0, dimension: int | None = None, send_dimensions: bool = True) -> None:
        super().__init__(endpoint, api_key, timeout)
        self.model = model
        self.dimension = dimension
        self.send_dimensions = send_dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.model, "input": list(texts)}
        if self.dimension and self.send_dimensions:
            payload["dimensions"] = self.dimension
        response = self._post(payload)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            raise ProviderError("embedding response must contain a data list")
        try:
            return [list(item["embedding"]) for item in data]
        except (KeyError, TypeError) as exc:
            raise ProviderError("embedding response contains invalid vectors") from exc


class ExternalRerankerProvider(_JsonHttpProvider):
    def __init__(self, endpoint: str, model: str = "", api_key: str = "", timeout: float = 30.0) -> None:
        super().__init__(endpoint, api_key, timeout)
        self.model = model

    def rerank(self, query: str, documents: Sequence[str]) -> list[dict[str, Any]]:
        if not documents:
            return []
        payload = {"model": self.model, "query": query, "documents": list(documents)}
        response = self._post(payload)
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            raise ProviderError("reranker response must contain a results list")
        return results

    def rerank_or_none(self, query: str, documents: Sequence[str]) -> list[dict[str, Any]] | None:
        try:
            return self.rerank(query, documents)
        except Exception:
            return None


def create_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    if config.mode == "static":
        return StaticEmbeddingProvider(config.dimension)
    if config.mode == "external":
        return ExternalEmbeddingProvider(config.endpoint, config.model, config.api_key, config.timeout, config.dimension, config.send_dimensions)
    raise ValueError(f"unsupported embedding mode: {config.mode}")


def create_reranker_provider(config: RerankerConfig) -> ExternalRerankerProvider | None:
    if not config.enabled:
        return None
    return ExternalRerankerProvider(config.endpoint, config.model, config.api_key, config.timeout)
