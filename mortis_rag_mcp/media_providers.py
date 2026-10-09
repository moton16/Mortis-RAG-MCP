"""Evidence-gated native media adapter framework, with no built-in endpoint."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from .embedding_capabilities import ResolvedEmbeddingProfile, validate_profile, validate_vector
from .providers import ProviderError


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    request_id: str
    modality: str
    data: bytes
    mime_type: str
    media_hash: str


@dataclass(frozen=True, slots=True)
class NativeMediaEvidence:
    model_reference: str
    endpoint_fixture_reference: str
    alignment_reference: str
    license_reference: str
    profile_fingerprint: str
    alignment_space_id: str
    max_input_bytes: int
    max_batch_size: int
    allowed_mime_types: tuple[str, ...]
    #: 可选：显式声明媒体 preprocess / endpoint 版本。提供时必须与 profile 一致，
    #: 缺省不校验（低层构造兼容）；工厂装配路径要求二者非空（见 providers.create_media_provider）。
    preprocess_version: str = ""
    endpoint_revision: str = ""


class MediaTransport(Protocol):
    def __call__(self, inputs: Sequence[EmbeddingInput]) -> Sequence[dict[str, Any]]: ...


def map_media_response(response: Sequence[Any], ids: Sequence[str]) -> dict[str, Any]:
    """把离线/服务响应归一为 `{内部 request_id: embedding}`。

    只接受两种**精确**定位：显式 `request_id`（必须命中输入集合）或 `index`
    （0..N-1，按输入顺序定位）。不要求服务回显 request_id；但数量必须完整、无
    重复、无缺项、无越界——任何缺口都抛错，绝不静默补位。归一接口设计不等于
    批准实际 HTTP 请求体。
    """
    ordered = list(ids)
    mapped: dict[str, Any] = {}
    for item in response:
        if not isinstance(item, Mapping):
            raise ProviderError("native media response items must be mappings")
        key: str | None = None
        if "request_id" in item:
            candidate = item.get("request_id")
            if not isinstance(candidate, str) or candidate not in ordered:
                raise ProviderError("native media response request_id does not match an input")
            key = candidate
        elif "index" in item:
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(ordered):
                raise ProviderError("native media response index is out of range")
            key = ordered[index]
        else:
            raise ProviderError("native media response must map by request_id or index")
        if key in mapped:
            raise ProviderError("duplicate native media response entry")
        mapped[key] = item.get("embedding")
    missing = [item for item in ordered if item not in mapped]
    if missing:
        raise ProviderError("native media response is missing entries")
    return mapped


class NativeMediaProvider:
    """Transport must use the same durable paid-request gate as text POSTs."""

    def __init__(self, profile: ResolvedEmbeddingProfile, evidence: NativeMediaEvidence, transport: MediaTransport) -> None:
        validate_profile(profile)
        refs = (evidence.model_reference, evidence.endpoint_fixture_reference,
                evidence.alignment_reference, evidence.license_reference)
        if not all(refs) or evidence.profile_fingerprint != profile.fingerprint:
            raise ProviderError("NATIVE_MEDIA_EVIDENCE_REQUIRED: exact endpoint evidence missing")
        if not profile.alignment_space_id or evidence.alignment_space_id != profile.alignment_space_id:
            raise ProviderError("NATIVE_MEDIA_ALIGNMENT_UNVERIFIED")
        if not any(modality in profile.modalities for modality in ("image", "audio")):
            raise ProviderError("profile has no verified native media modality")
        limits = (evidence.max_input_bytes, evidence.max_batch_size)
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in limits):
            raise ProviderError("native media limits must be verified positive integers")
        if not evidence.allowed_mime_types:
            raise ProviderError("native media MIME contract required")
        if evidence.preprocess_version and evidence.preprocess_version != profile.preprocess_version:
            raise ProviderError("native media preprocess version mismatch")
        if evidence.endpoint_revision and evidence.endpoint_revision != profile.endpoint_revision:
            raise ProviderError("native media endpoint revision mismatch")
        self.profile = profile
        self.evidence = evidence
        self._transport = transport
        self.request_journal: Any = None
        self.paid_guard: Any = None

    def configure_paid_requests(self, journal: Any, guard: Any, profile_fingerprint: str) -> None:
        if profile_fingerprint != self.profile.fingerprint:
            raise ProviderError("native media authorization profile mismatch")
        self.request_journal = journal
        self.paid_guard = guard

    def ability(self) -> dict[str, Any]:
        """只报告**声明**事实（不是已测能力，也不代表真实端点可用），供 E08 消费。"""
        return {
            "profile_fingerprint": self.profile.fingerprint,
            "modalities": tuple(self.profile.modalities),
            "alignment_space_id": self.profile.alignment_space_id,
            "preprocess_version": self.profile.preprocess_version,
            "endpoint_revision": self.profile.endpoint_revision,
            "allowed_mime_types": tuple(self.evidence.allowed_mime_types),
            "max_input_bytes": self.evidence.max_input_bytes,
            "max_batch_size": self.evidence.max_batch_size,
        }

    def embed_media(self, inputs: Sequence[EmbeddingInput]) -> list[list[float]]:
        items = list(inputs)
        if not items:
            return []
        if len(items) > self.evidence.max_batch_size:
            raise ProviderError("native media batch exceeds verified limit")
        ids = [item.request_id for item in items]
        if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
            raise ProviderError("native media request ids must be unique")
        for item in items:
            if item.modality not in self.profile.modalities or item.modality == "text":
                raise ProviderError("unsupported native media modality")
            if item.mime_type not in self.evidence.allowed_mime_types:
                raise ProviderError("unsupported native media MIME type")
            if not isinstance(item.data, bytes) or not 0 < len(item.data) <= self.evidence.max_input_bytes:
                raise ProviderError("native media input exceeds verified byte limit")
            if hashlib.sha256(item.data).hexdigest() != item.media_hash:
                raise ProviderError("native media input hash mismatch")
        if self.request_journal is None or self.paid_guard is None or not self.paid_guard(self.profile.fingerprint):
            raise ProviderError("EMBEDDING_PENDING_APPROVAL: native media request blocked")
        material = repr([(item.request_id, item.modality, item.mime_type, item.media_hash) for item in items])
        payload_hash = hashlib.sha256(material.encode()).hexdigest()
        request_id = self.request_journal.before_send(payload_hash, self.profile.endpoint, self.profile.fingerprint)
        try:
            response = list(self._transport(items))
            self.request_journal.mark_success(request_id)
        except Exception as exc:
            self.request_journal.mark_unknown(request_id, "response_unconfirmed")
            raise ProviderError("SUBMISSION_UNKNOWN: native media outcome unknown") from exc
        # 请求已响应（mark_success 已记账，不重发）；此处只做**产物合同**校验：
        # 数量/定位/向量不合规即抛错，绝不把契约失败的响应当可索引产物返回。
        if len(response) != len(items):
            raise ProviderError("native media response count mismatch")
        mapped = map_media_response(response, ids)
        return [validate_vector(mapped[item_id], self.profile) for item_id in ids]
