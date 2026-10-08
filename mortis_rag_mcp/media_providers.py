"""Evidence-gated native media adapter framework, with no built-in endpoint."""
from __future__ import annotations

import hashlib
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


class MediaTransport(Protocol):
    def __call__(self, inputs: Sequence[EmbeddingInput]) -> Sequence[dict[str, Any]]: ...


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
        if len(response) != len(items):
            raise ProviderError("native media response count mismatch")
        mapped: dict[str, Any] = {}
        for result in response:
            if not isinstance(result, dict) or result.get("request_id") not in ids:
                raise ProviderError("native media response must map exact request ids")
            request_id = result["request_id"]
            if request_id in mapped:
                raise ProviderError("duplicate native media response request id")
            mapped[request_id] = result.get("embedding")
        return [validate_vector(mapped[request_id], self.profile) for request_id in ids]
