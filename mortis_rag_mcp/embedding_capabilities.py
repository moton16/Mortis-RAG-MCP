"""Immutable embedding contracts; model names never imply media capability."""
from __future__ import annotations

import hashlib
import json
import math
from string import Formatter
from dataclasses import asdict, dataclass, field, replace
from struct import pack, unpack
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


class EmbeddingContractError(ValueError):
    pass


#: 允许声明的模态。`text` 永远存在（正文与媒体共享同一 profile 身份）。
SUPPORTED_MODALITIES = ("text", "image", "audio")
#: 真正的原生媒体模态（`text` 不算媒体能力）。
MEDIA_MODALITIES = ("image", "audio")


def validate_media_modalities(modalities: Any) -> tuple[str, ...]:
    """只接受显式声明的模态序列；字符串不是序列。不按模型名/维度推能力。"""
    if isinstance(modalities, str):
        raise EmbeddingContractError("modalities must be a sequence of strings, not a string")
    try:
        values = tuple(modalities)
    except TypeError as exc:
        raise EmbeddingContractError("modalities must be a sequence of strings") from exc
    if not values:
        raise EmbeddingContractError("modalities must not be empty")
    if "text" not in values:
        raise EmbeddingContractError("text modality is required for the shared space")
    for value in values:
        if value not in SUPPORTED_MODALITIES:
            raise EmbeddingContractError(f"unsupported modality: {value!r}")
    if len(set(values)) != len(values):
        raise EmbeddingContractError("modalities must not repeat")
    return values


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


# Only exact, established fixed-text contracts are built in.
EXACT_MODEL_REGISTRY = MappingProxyType({
    "BAAI/bge-m3": (1024, 8192),
    "bge-m3": (1024, 8192),
    "text-embedding-ada-002": (1536, 8192),
    "sentence-transformers/all-MiniLM-L6-v2": (384, 256),
    "embeddinggemma2": (768, 8192),
    "embeddinggemma-2": (768, 8192),
    "google/embeddinggemma-2": (768, 8192),
})
CAPABILITY_PROFILES = MappingProxyType({
    "bge-m3": ("BAAI/bge-m3", 1024, 8192),
    "embeddinggemma2": ("embeddinggemma2", 768, 8192),
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
    # E07：模态与 alignment 只来自显式声明；未声明时保持文本默认（指纹不变）。
    declared_media = tuple(getattr(config, "media_modalities", ()) or ())
    modalities = validate_media_modalities(("text", *declared_media)) if declared_media else ("text",)
    alignment_space_id = str(getattr(config, "media_alignment_space_id", "") or "")
    dim = config.dimension
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise EmbeddingContractError("embedding dimension must be a positive integer")
    name = getattr(config, "capability_profile", "")
    slicing = getattr(config, "client_slicing", False)
    model = config.model
    native, context = dim, None
    evidence = "configured-unverified-text"
    if config.mode == "static":
        if declared_media:
            raise EmbeddingContractError("static profile cannot declare native media modalities")
        if slicing:
            raise EmbeddingContractError("static profile does not support client slicing")
        return ResolvedEmbeddingProfile("static", "static", "sha256", "", "v1", "", dim, dim, None, "unsupported",
                                        modalities=("text",), alignment_space_id="",
                                        query_template=query_template, document_template=document_template,
                                        evidence_reference="local-deterministic")
    if name:
        if name not in CAPABILITY_PROFILES:
            raise EmbeddingContractError("unsupported capability_profile; provide verified endpoint evidence")
        exact, native, context = CAPABILITY_PROFILES[name]
        valid_models = {exact, name}
        if name == "bge-m3":
            valid_models.add("bge-m3")
        elif name == "embeddinggemma2":
            valid_models.update({"embeddinggemma-2", "google/embeddinggemma-2"})
        if model not in valid_models:
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
        modalities=modalities, alignment_space_id=alignment_space_id,
        query_template=query_template, document_template=document_template,
        preprocess_version=getattr(config, "preprocess_version", "text-v1"),
        max_context=context, evidence_reference=evidence,
    )


@dataclass(frozen=True, slots=True)
class ResolvedMediaConnection:
    """Media wire and profile share one connection; credentials are not identity."""
    adapter: str
    endpoint: str
    model: str
    dimension: int
    send_dimensions: bool
    api_key: str = field(repr=False, compare=False)


def resolve_media_connection(config: Any) -> ResolvedMediaConnection:
    adapter = str(getattr(config, "media_adapter", "") or getattr(config, "media_provider", "")
                  or getattr(config, "adapter", "") or "").strip().lower()
    if adapter in {"siliconflow_vl", "openai_multimodal", "vl_multimodal", "media_http"}:
        adapter = "openai_vl"
    elif adapter in {"gemini_multimodal", "gemini_embedding", "google_gemini", "gemini-embedding-2"}:
        adapter = "gemini"
    elif adapter in {"embeddinggemma", "embeddinggemma_2", "embedding-gemma"}:
        adapter = "embeddinggemma2"
    model = getattr(config, "media_model", "") or config.model
    if not model and adapter in {"gemini", "embeddinggemma2"}:
        model = "gemini-embedding-2"
    dimension = getattr(config, "media_dimension", None)
    if dimension is None:
        dimension = config.dimension
    from .config import resolve_media_auth
    key = resolve_media_auth(getattr(config, "media_api_key", ""), getattr(config, "api_key", ""),
                             getattr(config, "media_api_key_env", ""))
    return ResolvedMediaConnection(
        adapter, normalize_endpoint(getattr(config, "media_endpoint", "") or config.endpoint),
        model, dimension, bool(config.send_dimensions), key)


def resolve_media_profile(config: Any, *,
                          connection: ResolvedMediaConnection | None = None) -> ResolvedEmbeddingProfile:
    """装配原生媒体 profile（E07）：只消费**显式**声明，绝不按模型名/维度推断。

    缺任一必需声明（modality / alignment_space_id / media preprocess_version /
    media endpoint_revision）即拒绝——调用方据此判定「该 route 不可用」，不得
    用文本成功、同维向量或模型名冒充媒体能力/对齐。
    """
    # Text and media fixed-model contracts are independent.
    resolve_embedding_profile(config)
    connection = connection or resolve_media_connection(config)
    media_config = SimpleNamespace(
        mode=config.mode, adapter=connection.adapter, endpoint=connection.endpoint,
        model=connection.model, dimension=connection.dimension, send_dimensions=connection.send_dimensions,
        capability_profile="", client_slicing=getattr(config, "client_slicing", False),
        query_template=getattr(config, "query_template", "{text}"),
        document_template=getattr(config, "document_template", "{text}"),
        model_revision=getattr(config, "model_revision", ""),
    )
    base = resolve_embedding_profile(media_config)
    declared = tuple(getattr(config, "media_modalities", ()) or ())
    modalities = validate_media_modalities(("text", *declared))
    if not any(item in MEDIA_MODALITIES for item in modalities):
        raise EmbeddingContractError("native media profile requires an explicit image/audio modality")
    alignment = str(getattr(config, "media_alignment_space_id", "") or "").strip()
    if not alignment:
        raise EmbeddingContractError("native media profile requires an explicit alignment_space_id")
    preprocess = str(getattr(config, "media_preprocess_version", "") or "").strip()
    if not preprocess:
        raise EmbeddingContractError("native media profile requires an explicit media preprocess_version")
    endpoint_revision = str(getattr(config, "media_endpoint_revision", "") or "").strip()
    if not endpoint_revision:
        raise EmbeddingContractError("native media profile requires an explicit media endpoint_revision")
    profile = replace(
        base, modalities=modalities, alignment_space_id=alignment,
        preprocess_version=preprocess, endpoint_revision=endpoint_revision,
    )
    validate_profile(profile)
    return profile


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
