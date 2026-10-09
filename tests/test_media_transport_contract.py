"""E07：内部媒体 profile/归一/provider 工厂接缝（全部无网络 fixture）。

钉住的合同：
- 媒体能力只由**显式声明**决定：不按 model 名推、不按同维向量推 alignment；
- 工厂在声明不完整时判定「该 route 不可用」，绝不用文本成功冒充；
- 归一接受 request_id 或 index（不强求回显 request_id），但数量必须完整、无重复缺项；
- 响应契约失败**不**回退成 unknown（请求已响应，不重发）。
"""
from __future__ import annotations

import hashlib

import pytest

from mortis_rag_mcp.config import EmbeddingConfig
from mortis_rag_mcp.embedding_capabilities import (
    EmbeddingContractError, resolve_embedding_profile, resolve_media_profile,
)
from mortis_rag_mcp.media_providers import (
    EmbeddingInput, NativeMediaEvidence, NativeMediaProvider, map_media_response,
)
from mortis_rag_mcp.providers import ProviderError, create_embedding_provider, create_media_provider

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def media_config(**changes) -> EmbeddingConfig:
    values = dict(
        mode="external", model="unknown", endpoint="https://example.test/embed",
        dimension=2, send_dimensions=False,
        media_modalities=("image",),
        media_alignment_space_id="verified-space",
        media_preprocess_version="media-image-v1",
        media_endpoint_revision="ep-rev-1",
        media_allowed_mime_types=("image/png",),
        media_max_input_bytes=1024,
        media_max_batch_size=4,
        media_model_reference="model-ref",
        media_endpoint_fixture_reference="fixture-ref",
        media_alignment_reference="alignment-ref",
        media_license_reference="license-ref",
    )
    values.update(changes)
    return EmbeddingConfig(**values)


def input_for(request_id: str, data: bytes = PNG) -> EmbeddingInput:
    return EmbeddingInput(request_id, "image", data, "image/png", hashlib.sha256(data).hexdigest())


def configured(provider: NativeMediaProvider) -> NativeMediaProvider:
    journal = type("J", (), {
        "before_send": lambda self, *a: "req-1",
        "mark_success": lambda self, *a: None,
        "mark_unknown": lambda self, *a: None,
    })()
    provider.configure_paid_requests(journal, lambda fingerprint: True, provider.profile.fingerprint)
    return provider


def test_media_profile_requires_every_explicit_declaration():
    profile = resolve_media_profile(media_config())
    assert profile.modalities == ("text", "image")
    assert profile.alignment_space_id == "verified-space"
    assert profile.preprocess_version == "media-image-v1"
    assert profile.endpoint_revision == "ep-rev-1"
    # 文本 profile 只是共享身份，不因声明媒体而改变文本维度/模板语义。
    assert resolve_embedding_profile(media_config()).native_dim == profile.native_dim

    for changes in (
        {"media_modalities": ()},
        {"media_alignment_space_id": ""},
        {"media_preprocess_version": ""},
        {"media_endpoint_revision": ""},
    ):
        with pytest.raises(EmbeddingContractError):
            resolve_media_profile(media_config(**changes))


def test_modality_and_dimension_never_imply_media_or_alignment():
    # 未声明模态 → 不生成媒体 profile（不按模型名/维度推）。
    with pytest.raises(EmbeddingContractError):
        resolve_media_profile(media_config(media_modalities=()))
    # 文本 profile 不带 alignment：同维不等于已对齐。
    assert resolve_embedding_profile(media_config()).alignment_space_id == "verified-space"
    assert resolve_embedding_profile(EmbeddingConfig(mode="external", dimension=2)).alignment_space_id == ""
    assert resolve_embedding_profile(EmbeddingConfig(mode="external", dimension=2)).modalities == ("text",)


def test_factory_returns_none_without_declaration_and_mounts_with_transport():
    assert create_media_provider(EmbeddingConfig(mode="external", dimension=2)) is None

    sent = []

    def transport(items):
        sent.append(list(items))
        return [{"index": 0, "embedding": [3, 4]}]

    provider = create_media_provider(media_config(), transport=transport)
    assert isinstance(provider, NativeMediaProvider)
    configured(provider)
    assert provider.embed_media([input_for("a")])[0] == pytest.approx([0.6, 0.8])
    assert len(sent) == 1
    ability = provider.ability()
    assert ability["alignment_space_id"] == "verified-space"
    assert ability["allowed_mime_types"] == ("image/png",)
    assert ability["preprocess_version"] == "media-image-v1"


@pytest.mark.parametrize("changes", [
    {"media_allowed_mime_types": ()},
    {"media_max_input_bytes": 0},
    {"media_max_batch_size": 0},
    {"media_model_reference": ""},
    {"media_license_reference": ""},
])
def test_factory_reports_unavailable_when_declaration_incomplete(changes):
    with pytest.raises(ProviderError):
        create_media_provider(media_config(**changes), transport=lambda items: [])


def test_factory_requires_declared_transport_but_keeps_text_working():
    # Q09 缺协议 → 无 transport → 媒体 route 不可用；文本 provider 照常构造。
    with pytest.raises(ProviderError, match="transport"):
        create_media_provider(media_config())
    assert create_embedding_provider(EmbeddingConfig(mode="static", dimension=8)).embed(["x"])


def test_normalize_maps_by_index_or_request_id_and_rejects_gaps():
    ids = ["a", "b"]
    assert map_media_response([{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}], ids) == {
        "a": [1, 0], "b": [0, 1]}
    assert map_media_response([{"request_id": "b", "embedding": [0, 1]},
                               {"request_id": "a", "embedding": [1, 0]}], ids) == {"a": [1, 0], "b": [0, 1]}
    with pytest.raises(ProviderError):
        map_media_response([{"index": 0, "embedding": [1, 0]}], ids)  # 缺项
    with pytest.raises(ProviderError):
        map_media_response([{"index": 0, "embedding": [1, 0]},
                            {"request_id": "a", "embedding": [1, 0]}], ids)  # 重复
    with pytest.raises(ProviderError):
        map_media_response([{"index": 2, "embedding": [1, 0]}, {"index": 0, "embedding": [1, 0]}], ids)  # 越界
    with pytest.raises(ProviderError):
        map_media_response([{"embedding": [1, 0]}, {"embedding": [0, 1]}], ids)  # 无定位


def test_response_contract_failure_is_not_marked_unknown():
    events = []

    def transport(items):
        return [{"index": 0, "embedding": [1, 0]}]  # 缺一条 → 契约失败

    provider = create_media_provider(media_config(media_max_batch_size=2), transport=transport)
    journal = type("J", (), {
        "before_send": lambda self, *a: events.append("intent") or "req-1",
        "mark_success": lambda self, *a: events.append("success"),
        "mark_unknown": lambda self, *a: events.append("unknown"),
    })()
    provider.configure_paid_requests(journal, lambda fingerprint: True, provider.profile.fingerprint)
    with pytest.raises(ProviderError):
        provider.embed_media([input_for("a"), input_for("b", PNG + b"2")])
    assert events == ["intent", "success"]  # 请求已响应，绝不重发


def test_evidence_version_mismatch_is_rejected():
    profile = resolve_media_profile(media_config())
    evidence = NativeMediaEvidence("m", "f", "a", "l", profile.fingerprint, profile.alignment_space_id,
                                   1024, 4, ("image/png",),
                                   preprocess_version="other", endpoint_revision="ep-rev-1")
    with pytest.raises(ProviderError):
        NativeMediaProvider(profile, evidence, lambda items: [])
    evidence = NativeMediaEvidence("m", "f", "a", "l", profile.fingerprint, profile.alignment_space_id,
                                   1024, 4, ("image/png",), endpoint_revision="other")
    with pytest.raises(ProviderError):
        NativeMediaProvider(profile, evidence, lambda items: [])


def test_config_validates_media_declaration_fields():
    with pytest.raises(ValueError):
        EmbeddingConfig(mode="external", media_max_input_bytes=-1)
    with pytest.raises(ValueError):
        EmbeddingConfig(mode="external", media_modalities=("hologram",))
    with pytest.raises(ValueError):
        EmbeddingConfig(mode="external", media_modalities="image")  # 字符串不是序列
