"""Immutable embedding contracts; model names never imply media capability."""
from __future__ import annotations

import hashlib
import json
import math
from string import Formatter
from dataclasses import asdict, dataclass, replace
from struct import pack, unpack
from types import MappingProxyType
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


class EmbeddingContractError(ValueError):
    pass


def normalize_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint.strip())
    if parts.username or parts.password:
        raise EmbeddingContractError("endpoint credentials must use api_key")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


@dataclass(frozen=True, slots=True)
class ResolvedEmbeddingProfile:
    profile_name: str
    adapter: str
    model: str
    endpoint: str
    model_revision: str
    endpoint_revision: str
    native_dim: int
    effective_dim: int
    request_dim: int | None
    dimensions_protocol: str
    verified_mrl_dims: tuple[int, ...] = ()
    modalities: tuple[str, ...] = ("text",)
    alignment_space_id: str = ""
    max_context: int | None = None
    query_template: str = "{text}"
    document_template: str = "{text}"
    normalization: str = "l2-float32-v1"
    preprocess_version: str = "text-v1"
    evidence_reference: str = "configured-unverified-text"

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @property
    def space_id(self) -> str:
        return self.fingerprint

    def metadata(self) -> dict[str, Any]:
        return {**asdict(self), "fingerprint": self.fingerprint}


# Only exact, established fixed-text contracts are built in. No EG2 candidate.
EXACT_MODEL_REGISTRY = MappingProxyType({
    "BAAI/bge-m3": (1024, 8192),
    "bge-m3": (1024, 8192),
    "text-embedding-ada-002": (1536, 8192),
    "sentence-transformers/all-MiniLM-L6-v2": (384, 256),
})
CAPABILITY_PROFILES = MappingProxyType({
    "bge-m3": ("BAAI/bge-m3", 1024, 8192),
})


def validate_text_template(template: str) -> None:
    """One intact text slot; escaped literal braces and !s are supported."""
    if not isinstance(template, str):
        raise EmbeddingContractError("embedding template must be a string")
    try:
        slots = [(field, spec, conversion) for _, field, spec, conversion in Formatter().parse(template)
                 if field is not None]
    except ValueError as exc:
        raise EmbeddingContractError("invalid embedding template format") from exc
    if len(slots) != 1 or slots[0][0] != "text" or slots[0][1] or slots[0][2] not in (None, "s"):
        raise EmbeddingContractError("embedding template requires exactly one intact {text} slot")


def resolve_embedding_profile(config: Any) -> ResolvedEmbeddingProfile:
    query_template = getattr(config, "query_template", "{text}")
    document_template = getattr(config, "document_template", "{text}")
    validate_text_template(query_template)
    validate_text_template(document_template)
    dim = config.dimension
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise EmbeddingContractError("embedding dimension must be a positive integer")
    name = getattr(config, "capability_profile", "")
    slicing = getattr(config, "client_slicing", False)
    model = config.model
    native, context = dim, None
    evidence = "configured-unverified-text"
    if config.mode == "static":
        if slicing:
            raise EmbeddingContractError("static profile does not support client slicing")
        return ResolvedEmbeddingProfile("static", "static", "sha256", "", "v1", "", dim, dim, None, "unsupported",
                                        query_template=query_template, document_template=document_template,
                                        evidence_reference="local-deterministic")
    if name:
        if name not in CAPABILITY_PROFILES:
            raise EmbeddingContractError("unsupported capability_profile; provide verified endpoint evidence")
        exact, native, context = CAPABILITY_PROFILES[name]
        if model not in (exact, "bge-m3"):
            raise EmbeddingContractError("capability_profile does not match exact model")
    if model in EXACT_MODEL_REGISTRY:
        native, context = EXACT_MODEL_REGISTRY[model]
        evidence = "exact-fixed-text-contract"
        if dim != native or config.send_dimensions or slicing:
            raise EmbeddingContractError(f"{model} requires dimension={native}, send_dimensions=false, client_slicing=false")
    elif slicing:
        raise EmbeddingContractError("unverified models cannot use client slicing")
    return ResolvedEmbeddingProfile(
        name or "configured-text", getattr(config, "adapter", "openai"), model,
        normalize_endpoint(config.endpoint), getattr(config, "model_revision", ""),
        getattr(config, "endpoint_revision", ""), native, dim,
        dim if config.send_dimensions else None,
        "server" if config.send_dimensions else "unsupported",
        query_template=query_template, document_template=document_template,
        preprocess_version=getattr(config, "preprocess_version", "text-v1"),
        max_context=context, evidence_reference=evidence,
    )


def validate_profile(profile: ResolvedEmbeddingProfile) -> None:
    validate_text_template(profile.query_template)
    validate_text_template(profile.document_template)
    if profile.dimensions_protocol not in {"unsupported", "server", "client"}:
        raise EmbeddingContractError("unsupported dimensions protocol")
    for dim in (profile.native_dim, profile.effective_dim):
        if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
            raise EmbeddingContractError("invalid profile dimension")
    if profile.dimensions_protocol == "client":
        if profile.request_dim is not None or profile.effective_dim not in profile.verified_mrl_dims:
            raise EmbeddingContractError("client slicing requires verified MRL dimensions and no server dimensions")
        if profile.effective_dim > profile.native_dim:
            raise EmbeddingContractError("client dimension exceeds native output")
    elif profile.dimensions_protocol == "server":
        if profile.request_dim != profile.effective_dim:
            raise EmbeddingContractError("server request dimension must match effective dimension")
    elif profile.request_dim is not None or profile.effective_dim != profile.native_dim:
        raise EmbeddingContractError("fixed profile cannot request or slice dimensions")


def validate_vector(values: Iterable[Any], profile: ResolvedEmbeddingProfile) -> list[float]:
    validate_profile(profile)
    expected = profile.native_dim if profile.dimensions_protocol == "client" else profile.effective_dim
    try:
        raw = list(values)
    except TypeError as exc:
        raise EmbeddingContractError("embedding vector must be numeric sequence") from exc
    if len(raw) != expected:
        raise EmbeddingContractError(f"embedding dimension mismatch: expected {expected}, got {len(raw)}")
    numeric: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise EmbeddingContractError("embedding values must be numbers, not bool")
        try:
            number = float(value)
            converted = unpack("f", pack("f", number))[0]
        except (OverflowError, ValueError) as exc:
            raise EmbeddingContractError("embedding value exceeds float32 range") from exc
        if not math.isfinite(number) or not math.isfinite(converted):
            raise EmbeddingContractError("embedding values must be finite float32")
        numeric.append(converted)
    if profile.dimensions_protocol == "client":
        numeric = numeric[:profile.effective_dim]
    norm = math.hypot(*numeric)
    if not norm:
        raise EmbeddingContractError("embedding vector has zero norm")
    normalized = [unpack("f", pack("f", value / norm))[0] for value in numeric]
    if not any(normalized):
        raise EmbeddingContractError("embedding vector underflows float32")
    return normalized


def embedding_key(profile: ResolvedEmbeddingProfile, text: str, modality: str = "text", media_hash: str = "") -> str:
    material = json.dumps([profile.fingerprint, modality, text, media_hash, profile.preprocess_version], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def embed_with_profile(provider: Any, texts: Any, profile: ResolvedEmbeddingProfile, *, query: bool = False) -> list[list[float]]:
    validate_profile(profile)
    items = list(texts)
    if not items:
        return []
    template = profile.query_template if query else profile.document_template
    vectors = provider.embed([template.format(text=text) for text in items])
    if len(vectors) != len(items):
        raise EmbeddingContractError("embedding batch count mismatch")
    # Providers with resolved profiles have already sliced their native outputs.
    effective = replace(profile, native_dim=profile.effective_dim, request_dim=None, dimensions_protocol="unsupported")
    return [validate_vector(vector, effective if getattr(provider, "profile", None) else profile) for vector in vectors]
