from __future__ import annotations

import hashlib
import json
import math
import random
import time
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import EmbeddingConfig, RerankerConfig
from .embedding_capabilities import (
    EmbeddingContractError, ResolvedEmbeddingProfile,
    resolve_embedding_profile, validate_vector,
)

# 测试注入点：monkeypatch `_sleep` 即可断言退避序列，不必真的等待。
_sleep = time.sleep
# 指数退避的上限，避免网络长时间不可用时把调用方挂死。
_MAX_BACKOFF = 30.0
# 退避抖动幅度（向上浮动 0~50%）：多个 worker 拿到同一个 Retry-After 时
# 会同时醒来、同时重发，再次撞上限流（thundering herd）。
_JITTER_RATIO = 0.5


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


def _retry_after_seconds(headers: Any) -> float | None:
    """解析 Retry-After 响应头（只认数字秒数；HTTP-date 形式返回 None）。"""
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # inf / 1e999 这类值必须挡掉：_backoff_seconds 用 max() 取较大者，
    # 服务端只要回一个 `Retry-After: inf`，time.sleep(inf) 会把线程永久挂死。
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, _MAX_BACKOFF)


class _JsonHttpProvider:
    def __init__(self, endpoint: str, api_key: str = "", timeout: float = 30.0, max_retries: int = 3, retry_backoff: float = 1.0) -> None:
        if not endpoint:
            raise ValueError("external provider endpoint is required")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.request_journal: Any = None
        self.paid_guard: Any = None
        self.request_profile = ""

    def configure_paid_requests(self, journal: Any, guard: Any, profile_fingerprint: str) -> None:
        self.request_journal = journal
        self.paid_guard = guard
        self.request_profile = profile_fingerprint

    def _post(self, payload: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # Request 对象可在重试循环里复用，无需每次重建。
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        journal = self.request_journal
        if journal is None or self.paid_guard is None or not self.paid_guard(self.request_profile):
            raise ProviderError("PAID_REQUEST_CONTROL_UNAVAILABLE: durable journal required; profile may be revoked")
        payload_hash = hashlib.sha256(request.data).hexdigest()
        request_id = journal.before_send(payload_hash, self.endpoint, self.request_profile)
        try:
            # Non-idempotent POST: even 429/5xx may already have incurred cost.
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            journal.mark_success(request_id)
            return result
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            journal.mark_unknown(request_id, "response_unconfirmed")
            raise ProviderError("SUBMISSION_UNKNOWN: request outcome unknown; automatic retry disabled") from exc

    def _backoff_seconds(self, attempt: int, exc: BaseException) -> float | None:
        """返回本次失败应等待的秒数；None 表示不可重试，立即失败。"""
        retry_after = 0.0
        if isinstance(exc, HTTPError):
            code = getattr(exc, "code", 0) or 0
            if code == 429:
                # 服务端给出的限流等待时间优先于本地估算的退避。
                retry_after = _retry_after_seconds(getattr(exc, "hdrs", None)) or 0.0
            elif code < 500:
                # 其余 4xx（以及 3xx 等非成功响应）重试无意义，保持旧的立即失败语义。
                return None
        elif not isinstance(exc, (URLError, OSError)):
            # ValueError / JSONDecodeError：响应体本身损坏，重试无益。
            return None
        # 本地估算与服务端建议取较大者，再统一封顶 —— 封顶必须放在 max() 之外，
        # 否则服务端给的超大 Retry-After 会绕过 _MAX_BACKOFF。
        delay = max(min(self.retry_backoff * (2 ** attempt), _MAX_BACKOFF), retry_after)
        jittered = delay * (1.0 + random.random() * _JITTER_RATIO)
        return min(jittered, _MAX_BACKOFF)


class ExternalEmbeddingProvider(_JsonHttpProvider):
    def __init__(self, endpoint: str, model: str = "", api_key: str = "", timeout: float = 30.0, dimension: int | None = None, send_dimensions: bool = True, max_retries: int = 3, retry_backoff: float = 1.0, batch_size: int = 32, *, profile: ResolvedEmbeddingProfile | None = None) -> None:
        super().__init__(endpoint, api_key, timeout, max_retries, retry_backoff)
        self.model = model
        self.dimension = dimension
        self.send_dimensions = send_dimensions
        self.batch_size = batch_size
        self.profile = profile
        if profile is not None:
            self.dimension = profile.effective_dim
            self.send_dimensions = profile.request_dim is not None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        items = list(texts)
        if self.batch_size <= 0 or len(items) <= self.batch_size:
            return self._embed_batch(items)
        # 长文件按 batch_size 切片：单请求过大既容易被限流，也容易触发服务端长度上限。
        vectors: list[list[float]] = []
        for start in range(0, len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            # 条数校验在 _embed_batch 内统一做，单批与多批路径都不会漏。
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch}
        if self.dimension and self.send_dimensions:
            payload["dimensions"] = self.dimension
        response = self._post(payload)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            raise ProviderError("embedding response must contain a data list")
        # 少返回向量会让后续 chunk 的向量整体错位（静默污染整个向量库），
        # 这里拦住。默认 batch_size=32，多数文件走的就是这条单批路径。
        if len(data) != len(batch):
            raise ProviderError(
                f"embedding response returned {len(data)} vectors for {len(batch)} inputs"
            )
        # OpenAI 兼容接口不保证返回顺序，data[i].index 才是权威位置。
        indexes = []
        indexed = [isinstance(item, dict) and "index" in item for item in data]
        if any(indexed) and not all(indexed):
            raise ProviderError("embedding response mixes indexed and unindexed items")
        for position, item in enumerate(data):
            index = item.get("index", position) if isinstance(item, dict) else None
            if isinstance(index, bool) or not isinstance(index, int):
                raise ProviderError("embedding response contains invalid indexes")
            indexes.append(index)
        if sorted(indexes) != list(range(len(batch))):
            raise ProviderError(
                f"embedding response indexes {sorted(indexes)} do not match {len(batch)} inputs"
            )
        ordered = [item for _, item in sorted(zip(indexes, data), key=lambda pair: pair[0])]
        try:
            vectors = [list(item["embedding"]) for item in ordered]
            profile = self.profile
            if profile is None:
                dim = self.dimension or (len(vectors[0]) if vectors else 0)
                profile = ResolvedEmbeddingProfile("legacy-text", "openai", self.model, self.endpoint, "", "", dim, dim, None, "unsupported")
            return [validate_vector(vector, profile) for vector in vectors]
        except (KeyError, TypeError, EmbeddingContractError) as exc:
            raise ProviderError("embedding response contains invalid vectors") from exc


class ExternalRerankerProvider(_JsonHttpProvider):
    def __init__(self, endpoint: str, model: str = "", api_key: str = "", timeout: float = 30.0, max_retries: int = 3, retry_backoff: float = 1.0) -> None:
        super().__init__(endpoint, api_key, timeout, max_retries, retry_backoff)
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
    profile = resolve_embedding_profile(config)
    if config.mode == "static":
        provider = StaticEmbeddingProvider(config.dimension)
        provider.profile = profile
        return provider
    if config.mode == "external":
        return ExternalEmbeddingProvider(
            config.endpoint,
            config.model,
            config.api_key,
            config.timeout,
            config.dimension,
            config.send_dimensions,
            config.max_retries,
            config.retry_backoff,
            config.batch_size,
            profile=profile,
        )
    raise ValueError(f"unsupported embedding mode: {config.mode}")


def create_media_provider(config: EmbeddingConfig, *, transport: Any = None) -> Any:
    """受控原生媒体 provider 工厂（E07）。

    - 配置未声明任何媒体模态 → 返回 `None`（该能力不存在，不是错误；文本路径不受影响）。
    - 声明不完整（modality / alignment_space_id / media preprocess / endpoint revision /
      MIME / 限额 / 证据引用）→ 抛 `ProviderError`：明确「该 route 不可用」，绝不用
      文本成功、同维向量或模型名冒充，也绝不自动试端点。
    - `transport` 必须由调用方显式注入；真实 HTTP 边界（Q09 合同）缺失时该能力同样不可用。
    """
    declared = tuple(getattr(config, "media_modalities", ()) or ())
    if not declared:
        return None
    from .embedding_capabilities import EmbeddingContractError, resolve_media_profile
    from .media_providers import NativeMediaEvidence, NativeMediaProvider
    try:
        profile = resolve_media_profile(config)
    except EmbeddingContractError as exc:
        raise ProviderError(f"native media capability unavailable: {exc}") from exc
    mime = tuple(getattr(config, "media_allowed_mime_types", ()) or ())
    max_bytes = int(getattr(config, "media_max_input_bytes", 0) or 0)
    max_batch = int(getattr(config, "media_max_batch_size", 0) or 0)
    references = tuple(str(getattr(config, name, "") or "") for name in (
        "media_model_reference", "media_endpoint_fixture_reference",
        "media_alignment_reference", "media_license_reference"))
    if not mime:
        raise ProviderError("native media capability unavailable: allowed MIME types are not declared")
    if max_bytes <= 0 or max_batch <= 0:
        raise ProviderError("native media capability unavailable: byte/batch limits are not declared")
    if not all(references):
        raise ProviderError("native media capability unavailable: endpoint evidence references are incomplete")
    if transport is None:
        adapter_name = str(getattr(config, "adapter", "") or "").strip().lower()
        if adapter_name in {"openai_vl", "siliconflow_vl", "openai_multimodal", "vl_multimodal", "media_http"}:
            from .media_providers import HttpMediaTransport
            dim = getattr(config, "dimension", None)
            send_dim = bool(getattr(config, "send_dimensions", False))
            transport = HttpMediaTransport(
                endpoint=config.endpoint,
                model=config.model,
                api_key=getattr(config, "api_key", ""),
                timeout=getattr(config, "timeout", 30.0),
                dimension=dim,
                send_dimensions=send_dim,
                allowed_mime_types=mime,
                max_input_bytes=max_bytes,
                max_batch_size=max_batch,
            )
        else:
            raise ProviderError("native media capability unavailable: no declared transport (Q09 protocol missing)")

    evidence = NativeMediaEvidence(
        references[0], references[1], references[2], references[3],
        profile.fingerprint, profile.alignment_space_id, max_bytes, max_batch, mime,
        preprocess_version=profile.preprocess_version, endpoint_revision=profile.endpoint_revision,
    )
    return NativeMediaProvider(profile, evidence, transport)


def create_reranker_provider(config: RerankerConfig) -> ExternalRerankerProvider | None:
    if not config.enabled:
        return None
    return ExternalRerankerProvider(
        config.endpoint, config.model, config.api_key, config.timeout,
        max_retries=config.max_retries, retry_backoff=config.retry_backoff,
    )
