"""E15：真实服务合同适配器回归（媒体 embeddings / 转录 / MinerU 结构媒体）。

覆盖三个子出口的 mock-transport 隔离回归：
1. HttpMediaTransport：请求形态、base64 dataURI 编码、响应索引对齐、429/网络异常未知停、限额与模态拒绝、普通工厂装配；
2. OpenAiTranscriptionAdapter：multipart 请求编码、verbose_json 与 json 响应解析、细粒度分段时间戳、timeout 透传、429/网络异常未知停、缺 endpoint 显式拒绝、parse_audio 链路贯通；
3. MinerU 结构媒体映射：content_list.json 真实字段（caption/OCR/page/bbox）映射到 MediaOccurrenceSpec、正文 Markdown 字符锚点 [start, end) 检索定位、无引用不猜、尺寸绝不冒充 anchor、StoreMediaSink 真实落库回归。
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import wave
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from mortis_rag_mcp.config import AppConfig, AudioConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout
from mortis_rag_mcp.embedding_capabilities import resolve_media_profile
from mortis_rag_mcp.ingest.audio import (
    AudioSegment,
    audio_settings,
    iter_segments,
    parse_audio,
)
from mortis_rag_mcp.ingest.mineru import MineruClient, MineruError, _safe_extract_zip
from mortis_rag_mcp.ingest.models import DictMediaSink, ResourceLimits
from mortis_rag_mcp.ingest.transcription import (
    AUDIO_ADAPTER_UNAVAILABLE,
    TRANSCRIPT_CONTRACT_UNVERIFIED,
    OpenAiTranscriptionAdapter,
    TranscriptionContractUnverified,
    TranscriptionError,
    create_transcription_adapter,
)
from mortis_rag_mcp.ingest.worker import StoreMediaSink
from mortis_rag_mcp.media_providers import (
    EmbeddingInput,
    GeminiMediaTransport,
    HttpMediaTransport,
    NativeMediaEvidence,
    NativeMediaProvider,
)
from mortis_rag_mcp.providers import ProviderError, create_media_provider

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
SAMPLE_VEC = [0.1, 0.2, 0.3, 0.4]


# =====================================================================
# 1. 媒体 Embeddings Transport 回归
# =====================================================================


def _media_config(**changes) -> EmbeddingConfig:
    values = dict(
        mode="external",
        model="Qwen/Qwen3-VL-Embedding-8B",
        endpoint="https://api.siliconflow.cn/v1/embeddings",
        adapter="siliconflow_vl",
        dimension=4,
        send_dimensions=True,
        media_modalities=("image",),
        media_alignment_space_id="vl-space-v1",
        media_preprocess_version="vl-image-v1",
        media_endpoint_revision="vl-ep-1",
        media_allowed_mime_types=("image/png", "image/jpeg", "image/webp"),
        media_max_input_bytes=1024 * 1024,
        media_max_batch_size=4,
        media_model_reference="qwen3-vl-ref",
        media_endpoint_fixture_reference="qwen3-vl-fixture",
        media_alignment_reference="qwen3-vl-align",
        media_license_reference="qwen3-vl-license",
    )
    values.update(changes)
    return EmbeddingConfig(**values)


def _input(req_id: str, data: bytes = PNG_BYTES, modality: str = "image", mime: str = "image/png") -> EmbeddingInput:
    return EmbeddingInput(
        request_id=req_id,
        modality=modality,
        data=data,
        mime_type=mime,
        media_hash=hashlib.sha256(data).hexdigest(),
    )


class _MockJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self._counter = 0

    def before_send(self, payload_hash: str, endpoint: str, profile: str) -> str:
        self._counter += 1
        req_id = f"req-{self._counter}"
        self.events.append(("before_send", req_id))
        return req_id

    def mark_success(self, req_id: str) -> None:
        self.events.append(("mark_success", req_id))

    def mark_unknown(self, req_id: str, reason: str) -> None:
        self.events.append(("mark_unknown", req_id))


def test_http_media_transport_request_shape_and_response_parsing():
    cfg = _media_config()
    transport = HttpMediaTransport(
        endpoint=cfg.endpoint,
        model=cfg.model,
        api_key="test-key",
        dimension=cfg.dimension,
        send_dimensions=True,
        allowed_mime_types=cfg.media_allowed_mime_types,
        max_input_bytes=cfg.media_max_input_bytes,
        max_batch_size=cfg.media_max_batch_size,
    )

    captured_request = {}

    def fake_urlopen(req, timeout=30.0):
        captured_request["url"] = req.full_url
        captured_request["headers"] = dict(req.headers)
        captured_request["body"] = json.loads(req.data.decode("utf-8"))
        resp_data = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": 0, "embedding": SAMPLE_VEC},
            ],
            "model": cfg.model,
        }
        return io.BytesIO(json.dumps(resp_data).encode("utf-8"))

    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=fake_urlopen):
        result = transport([_input("img-1")])

    assert len(result) == 1
    assert result[0]["index"] == 0
    assert result[0]["embedding"] == SAMPLE_VEC
    assert captured_request["url"] == "https://api.siliconflow.cn/v1/embeddings"
    assert captured_request["headers"]["Authorization"] == "Bearer test-key"
    assert captured_request["headers"]["Content-type"] == "application/json"
    body = captured_request["body"]
    assert body["model"] == "Qwen/Qwen3-VL-Embedding-8B"
    assert body["dimensions"] == 4
    assert len(body["input"]) == 1
    assert body["input"][0]["image"].startswith("data:image/png;base64,")


def test_http_media_transport_error_handling():
    transport = HttpMediaTransport(
        endpoint="https://api.siliconflow.cn/v1/embeddings",
        model="Qwen/Qwen3-VL-Embedding-8B",
    )

    # 1. 429 Rate limited with error json body
    err_body = io.BytesIO(json.dumps({"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}).encode("utf-8"))
    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=HTTPError("https://api.test", 429, "Too Many Requests", {}, err_body)):
        with pytest.raises(ProviderError, match="media embedding HTTP failure: HTTP Error 429.*Rate limit reached"):
            transport([_input("1")])

    # 2. 502 Bad Gateway with HTML error page
    html_body = io.BytesIO(b"<html><head><title>502 Bad Gateway</title></head><body>502 Bad Gateway</body></html>")
    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=HTTPError("https://api.test", 502, "Bad Gateway", {}, html_body)):
        with pytest.raises(ProviderError, match="media embedding HTTP failure: HTTP Error 502: Bad Gateway"):
            transport([_input("1")])

    # 3. Network error (URLError)
    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=URLError("Connection refused")):
        with pytest.raises(ProviderError, match="media embedding network error"):
            transport([_input("1")])

    # 4. HTTP 200 but response has error dict
    with patch("mortis_rag_mcp.media_providers.urlopen", return_value=io.BytesIO(json.dumps({"error": {"message": "Model not found"}}).encode("utf-8"))):
        with pytest.raises(ProviderError, match="media embedding API error: Model not found"):
            transport([_input("1")])


def test_http_media_transport_enforces_limits_and_modality():
    transport = HttpMediaTransport(
        endpoint="https://api.test/embed",
        model="m",
        max_batch_size=1,
        max_input_bytes=10,
        allowed_mime_types=("image/png",),
    )

    # 批大小超限
    with pytest.raises(ProviderError, match="batch size"):
        transport([_input("1"), _input("2")])

    # 音频模态被 VL transport 显式拒绝
    with pytest.raises(ProviderError, match="modality 'audio' unsupported"):
        transport([_input("1", modality="audio")])

    # MIME 类型不在白名单
    with pytest.raises(ProviderError, match="not in allowed types"):
        transport([_input("1", mime="image/gif")])

    # 字节超限
    with pytest.raises(ProviderError, match="data size"):
        transport([_input("1", data=b"x" * 20)])


def test_create_media_provider_defaults_to_http_transport_for_vl_adapter():
    cfg = _media_config()
    provider = create_media_provider(cfg)  # transport=None
    assert isinstance(provider, NativeMediaProvider)
    assert isinstance(provider._transport, HttpMediaTransport)
    assert provider._transport.endpoint == cfg.endpoint
    assert provider._transport.model == cfg.model


def test_media_provider_end_to_end_with_journal_and_failure_stops():
    cfg = _media_config()
    journal = _MockJournal()

    def fake_transport(items):
        raise HTTPError("https://api.test/embed", 429, "Rate Limited", {"Retry-After": "5"}, io.BytesIO(b"{}"))

    provider = create_media_provider(cfg, transport=fake_transport)
    provider.configure_paid_requests(journal, lambda fp: True, provider.profile.fingerprint)

    with pytest.raises(ProviderError, match="SUBMISSION_UNKNOWN"):
        provider.embed_media([_input("item-1")])

    # 验证 journal 闭环：before_send 后立即 mark_unknown，绝不静默重发
    assert [ev[0] for ev in journal.events] == ["before_send", "mark_unknown"]


def _gemini_media_config(**changes) -> EmbeddingConfig:
    values = dict(
        mode="external",
        model="gemini-embedding-2",
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        adapter="gemini",
        dimension=4,
        send_dimensions=True,
        media_modalities=("audio",),
        media_alignment_space_id="gemini-audio-space-v1",
        media_preprocess_version="gemini-audio-v1",
        media_endpoint_revision="gemini-ep-1",
        media_allowed_mime_types=("audio/mp3", "audio/mpeg", "audio/wav", "image/png"),
        media_max_input_bytes=1024 * 1024,
        media_max_batch_size=4,
        media_model_reference="gemini-embedding-2-ref",
        media_endpoint_fixture_reference="gemini-audio-fixture",
        media_alignment_reference="gemini-audio-align",
        media_license_reference="gemini-license",
    )
    values.update(changes)
    return EmbeddingConfig(**values)


def test_gemini_media_transport_batch_audio_and_image_request():
    captured_request = {}

    def fake_urlopen(req, timeout=30.0):
        captured_request["url"] = req.full_url
        captured_request["headers"] = dict(req.headers)
        captured_request["body"] = json.loads(req.data.decode("utf-8"))
        resp_data = {
            "embeddings": [
                {"values": SAMPLE_VEC},
                {"values": [0.5, 0.6, 0.7, 0.8]},
            ]
        }
        return io.BytesIO(json.dumps(resp_data).encode("utf-8"))

    transport = GeminiMediaTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        model="gemini-embedding-2",
        api_key="test-api-key",
        dimension=4,
        send_dimensions=True,
    )

    items = [
        _input("aud-1", data=WAV_BYTES, modality="audio", mime="audio/wav"),
        _input("img-1", data=PNG_BYTES, modality="image", mime="image/png"),
    ]

    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=fake_urlopen):
        result = transport(items)

    assert len(result) == 2
    assert result[0]["index"] == 0
    assert result[0]["embedding"] == SAMPLE_VEC
    assert result[1]["index"] == 1
    assert result[1]["embedding"] == [0.5, 0.6, 0.7, 0.8]

    assert captured_request["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
    assert captured_request["headers"]["X-goog-api-key"] == "test-api-key"
    assert captured_request["headers"]["Content-type"] == "application/json"

    body = captured_request["body"]
    assert "requests" in body
    assert len(body["requests"]) == 2
    req0 = body["requests"][0]
    assert req0["model"] == "models/gemini-embedding-2"
    assert req0["output_dimensionality"] == 4
    assert req0["content"]["parts"][0]["inline_data"]["mime_type"] == "audio/wav"

    req1 = body["requests"][1]
    assert req1["model"] == "models/gemini-embedding-2"
    assert req1["output_dimensionality"] == 4
    assert req1["content"]["parts"][0]["inline_data"]["mime_type"] == "image/png"


def test_gemini_media_transport_single_embed_content_endpoint():
    captured_request = {}

    def fake_urlopen(req, timeout=30.0):
        captured_request["body"] = json.loads(req.data.decode("utf-8"))
        resp_data = {"embedding": {"values": SAMPLE_VEC}}
        return io.BytesIO(json.dumps(resp_data).encode("utf-8"))

    transport = GeminiMediaTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent",
        model="gemini-embedding-2",
        api_key="Bearer token-123",
    )

    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=fake_urlopen):
        result = transport([_input("aud-1", data=WAV_BYTES, modality="audio", mime="audio/wav")])

    assert len(result) == 1
    assert result[0]["index"] == 0
    assert result[0]["embedding"] == SAMPLE_VEC
    assert captured_request["body"]["content"]["parts"][0]["inline_data"]["mime_type"] == "audio/wav"


def test_gemini_media_transport_error_handling():
    transport = GeminiMediaTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        model="gemini-embedding-2",
    )

    # 1. 429 Rate limited
    err_body = io.BytesIO(json.dumps({"error": {"code": 429, "message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}}).encode("utf-8"))
    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=HTTPError("https://api.test", 429, "Too Many Requests", {}, err_body)):
        with pytest.raises(ProviderError, match="Gemini media embedding HTTP failure: HTTP Error 429.*RESOURCE_EXHAUSTED"):
            transport([_input("1", data=WAV_BYTES, modality="audio", mime="audio/wav")])

    # 2. Network error (URLError)
    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=URLError("Connection refused")):
        with pytest.raises(ProviderError, match="Gemini media embedding network error"):
            transport([_input("1", data=WAV_BYTES, modality="audio", mime="audio/wav")])

    # 3. HTTP 200 with API error
    with patch("mortis_rag_mcp.media_providers.urlopen", return_value=io.BytesIO(json.dumps({"error": {"message": "Invalid model"}}).encode("utf-8"))):
        with pytest.raises(ProviderError, match="Gemini media embedding API error: Invalid model"):
            transport([_input("1", data=WAV_BYTES, modality="audio", mime="audio/wav")])

    # 4. Malformed response JSON missing embeddings
    with patch("mortis_rag_mcp.media_providers.urlopen", return_value=io.BytesIO(json.dumps({"wrong": 1}).encode("utf-8"))):
        with pytest.raises(ProviderError, match="must contain 'embedding' or 'embeddings'"):
            transport([_input("1", data=WAV_BYTES, modality="audio", mime="audio/wav")])


def test_gemini_media_transport_enforces_limits_and_modality():
    transport = GeminiMediaTransport(
        endpoint="https://api.test/embed",
        model="gemini-embedding-2",
        max_batch_size=1,
        max_input_bytes=10,
        allowed_mime_types=("audio/wav",),
    )

    # 批大小超限
    with pytest.raises(ProviderError, match="batch size"):
        transport([
            _input("1", data=WAV_BYTES, modality="audio", mime="audio/wav"),
            _input("2", data=WAV_BYTES, modality="audio", mime="audio/wav"),
        ])

    # 不支持的模态（如视频）拒绝
    with pytest.raises(ProviderError, match="modality 'video' unsupported"):
        transport([_input("1", modality="video")])

    # 不在白名单的 MIME 类型
    with pytest.raises(ProviderError, match="not in allowed types"):
        transport([_input("1", data=WAV_BYTES, modality="audio", mime="audio/mp3")])

    # 字节超限
    with pytest.raises(ProviderError, match="data size"):
        transport([_input("1", data=b"x" * 20, modality="audio", mime="audio/wav")])


def test_create_media_provider_for_gemini_adapter():
    cfg = _gemini_media_config()
    provider = create_media_provider(cfg)  # transport=None
    assert isinstance(provider, NativeMediaProvider)
    assert isinstance(provider._transport, GeminiMediaTransport)
    assert provider._transport.endpoint == cfg.endpoint
    assert provider._transport.model == cfg.model

    journal = _MockJournal()
    provider.configure_paid_requests(journal, lambda fp: True, provider.profile.fingerprint)

    fake_response = {"embeddings": [{"values": [1.0, 0.0, 0.0, 0.0]}]}
    with patch("mortis_rag_mcp.media_providers.urlopen", return_value=io.BytesIO(json.dumps(fake_response).encode("utf-8"))):
        vecs = provider.embed_media([_input("aud-1", data=WAV_BYTES, modality="audio", mime="audio/wav")])
    assert vecs == [[1.0, 0.0, 0.0, 0.0]]
    assert [ev[0] for ev in journal.events] == ["before_send", "mark_success"]


def test_gemini_media_transport_rejects_batch_on_embed_content_endpoint():
    transport = GeminiMediaTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent",
        model="gemini-embedding-2",
    )
    with pytest.raises(ProviderError, match="single :embedContent endpoint does not accept batch"):
        transport([
            _input("aud-1", data=WAV_BYTES, modality="audio", mime="audio/wav"),
            _input("aud-2", data=WAV_BYTES, modality="audio", mime="audio/wav"),
        ])


def test_gemini_media_transport_accepts_single_embedding_without_embed_content_suffix():
    def fake_urlopen(req, timeout=30.0):
        resp_data = {"embedding": {"values": SAMPLE_VEC}}
        return io.BytesIO(json.dumps(resp_data).encode("utf-8"))

    transport = GeminiMediaTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:customEndpoint",
        model="gemini-embedding-2",
    )
    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=fake_urlopen):
        result = transport([_input("aud-1", data=WAV_BYTES, modality="audio", mime="audio/wav")])
    assert len(result) == 1
    assert result[0]["embedding"] == SAMPLE_VEC


def test_gemini_media_transport_rejects_single_embedding_for_multiple_items():
    def fake_urlopen(req, timeout=30.0):
        resp_data = {"embedding": {"values": SAMPLE_VEC}}
        return io.BytesIO(json.dumps(resp_data).encode("utf-8"))

    transport = GeminiMediaTransport(
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        model="gemini-embedding-2",
    )
    with patch("mortis_rag_mcp.media_providers.urlopen", side_effect=fake_urlopen):
        with pytest.raises(ProviderError, match="single embedding response received but request had 2 items"):
            transport([
                _input("aud-1", data=WAV_BYTES, modality="audio", mime="audio/wav"),
                _input("aud-2", data=WAV_BYTES, modality="audio", mime="audio/wav"),
            ])


def test_create_media_provider_for_embeddinggemma_adapter():
    # 1. Google Cloud endpoint -> routes to GeminiMediaTransport
    cfg_cloud = _gemini_media_config(
        adapter="embeddinggemma2",
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
    )
    provider_cloud = create_media_provider(cfg_cloud)
    assert isinstance(provider_cloud, NativeMediaProvider)
    assert isinstance(provider_cloud._transport, GeminiMediaTransport)

    # 2. Local loopback endpoint -> raises informative ProviderError
    cfg_local = _gemini_media_config(
        adapter="embeddinggemma2",
        endpoint="http://127.0.0.1:8080/v1/embeddings",
    )
    with pytest.raises(ProviderError, match="local inference server.*lacks a verified HTTP REST audio embedding schema"):
        create_media_provider(cfg_local)


def test_multimodal_audio_contract_fixtures_integrity():
    fixtures_dir = Path(__file__).parent / "fixtures"
    gemini_fixture = fixtures_dir / "gemini_multimodal_embedding_contract.json"
    gemma_fixture = fixtures_dir / "embeddinggemma2_model_card_contract.json"

    assert gemini_fixture.is_file()
    assert gemma_fixture.is_file()

    with open(gemini_fixture, "r", encoding="utf-8") as f:
        gemini_data = json.load(f)
    assert gemini_data["contract_name"] == "gemini_multimodal_embeddings_v1"
    assert "audio" in gemini_data["supported_modalities"]
    assert "audio/mp3" in gemini_data["allowed_mime_types"]
    assert "audio/wav" in gemini_data["allowed_mime_types"]
    assert gemini_data["audio_specifications"]["max_duration_seconds"] == 180

    with open(gemma_fixture, "r", encoding="utf-8") as f:
        gemma_data = json.load(f)
    assert gemma_data["model_identifier"] == "google/embeddinggemma-2"
    assert gemma_data["parameters"]["total"] == "740M"
    assert gemma_data["vector_space"]["native_dimension"] == 768
    assert "audio" in gemma_data["supported_modalities"]
    assert gemma_data["audio_specifications"]["sample_rate_hz"] == 16000
    assert gemma_data["audio_specifications"]["placeholder_token"] == "<|audio|>"



# =====================================================================
# 2. 转录 Adapter 回归
# =====================================================================


def _wav_segment(ordinal: int = 1, frames: int = 8000) -> AudioSegment:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * frames)
    data = bio.getvalue()
    return AudioSegment(
        ordinal=ordinal,
        start_frame=0,
        end_frame=frames,
        t_start_ms=0,
        t_end_ms=1000,
        input_sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def test_transcription_adapter_multipart_request_and_verbose_json():
    seg = _wav_segment(1)
    captured = {}

    def fake_urlopen(req, timeout=30.0):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        captured["content_type"] = req.headers.get("Content-type")
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = req.data
        resp = {
            "task": "transcribe",
            "language": "zh",
            "duration": 1.0,
            "text": "测试音频转录",
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.0, "text": "测试音频转录"},
            ],
        }
        return io.BytesIO(json.dumps(resp).encode("utf-8"))

    journal = _MockJournal()
    adapter = OpenAiTranscriptionAdapter(
        endpoint="https://api.openai.com/v1/audio/transcriptions",
        model="whisper-1",
        api_key="sk-test",
        timeout=15.0,
        journal=journal,
    )

    with patch("mortis_rag_mcp.ingest.transcription.urlopen", side_effect=fake_urlopen):
        result = adapter.transcribe(seg, timeout=12.5)

    assert result["text"] == "测试音频转录"
    assert len(result["segments"]) == 1
    assert captured["timeout"] == 12.5
    assert "multipart/form-data" in captured["content_type"]
    assert captured["auth"] == "Bearer sk-test"
    assert b"whisper-1" in captured["body"]
    assert b"segment-1.wav" in captured["body"]
    assert [ev[0] for ev in journal.events] == ["before_send", "mark_success"]


def test_transcription_adapter_error_marks_unknown_and_no_retry():
    seg = _wav_segment(1)
    journal = _MockJournal()

    def fake_urlopen(req, timeout=30.0):
        raise HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"error": "rate_limit"}'))

    adapter = OpenAiTranscriptionAdapter(
        endpoint="https://api.openai.com/v1/audio/transcriptions",
        journal=journal,
    )

    with patch("mortis_rag_mcp.ingest.transcription.urlopen", side_effect=fake_urlopen):
        with pytest.raises(TranscriptionError, match="SUBMISSION_UNKNOWN"):
            adapter.transcribe(seg)

    assert [ev[0] for ev in journal.events] == ["before_send", "mark_unknown"]


def test_create_transcription_adapter_validation():
    # 空配置 -> None
    assert create_transcription_adapter(AudioConfig(adapter="")) is None

    # 未受支持的 adapter 标识 -> TranscriptionContractUnverified
    with pytest.raises(TranscriptionContractUnverified, match="未受支持"):
        create_transcription_adapter(AudioConfig(adapter="unknown_engine"))

    # 声明了 adapter 但缺少 endpoint -> TranscriptionContractUnverified
    with pytest.raises(TranscriptionContractUnverified, match="transcription_endpoint"):
        create_transcription_adapter(AudioConfig(adapter="whisper", transcription_endpoint=""))

    # 正常装配
    cfg = AudioConfig(
        adapter="openai_whisper",
        transcription_endpoint="https://api.test/v1/audio/transcriptions",
        transcription_model="whisper-1",
    )
    adapter = create_transcription_adapter(cfg)
    assert isinstance(adapter, OpenAiTranscriptionAdapter)
    assert adapter.endpoint == "https://api.test/v1/audio/transcriptions"
    assert adapter.model == "whisper-1"


def test_transcription_adapter_error_details_and_quota_diagnostics():
    seg = _wav_segment(1)
    journal = _MockJournal()
    adapter = OpenAiTranscriptionAdapter(
        endpoint="https://api.openai.com/v1/audio/transcriptions",
        journal=journal,
    )

    # 1. 429 insufficient quota payload preserved in TranscriptionError
    err_body = io.BytesIO(json.dumps({
        "error": {
            "message": "You exceeded your current quota, please check your plan and billing details.",
            "type": "insufficient_quota",
            "code": "insufficient_quota",
        }
    }).encode("utf-8"))
    with patch("mortis_rag_mcp.ingest.transcription.urlopen", side_effect=HTTPError("https://api.test", 429, "Too Many Requests", {}, err_body)):
        with pytest.raises(TranscriptionError) as exc_info:
            adapter.transcribe(seg)
        assert "insufficient_quota" in str(exc_info.value)
        assert "exceeded your current quota" in str(exc_info.value)
        assert "SUBMISSION_UNKNOWN" in str(exc_info.value)
    assert [ev[0] for ev in journal.events] == ["before_send", "mark_unknown"]

    # 2. 500 HTML gateway failure preserved
    journal.events.clear()
    html_body = io.BytesIO(b"<html><body>500 Internal Server Error</body></html>")
    with patch("mortis_rag_mcp.ingest.transcription.urlopen", side_effect=HTTPError("https://api.test", 500, "Internal Server Error", {}, html_body)):
        with pytest.raises(TranscriptionError) as exc_info:
            adapter.transcribe(seg)
        assert "500" in str(exc_info.value)
        assert "Internal Server Error" in str(exc_info.value)
    assert [ev[0] for ev in journal.events] == ["before_send", "mark_unknown"]

    # 3. HTTP 200 with error payload
    journal.events.clear()
    with patch("mortis_rag_mcp.ingest.transcription.urlopen", return_value=io.BytesIO(json.dumps({"error": {"message": "Invalid audio data"}}).encode("utf-8"))):
        with pytest.raises(TranscriptionError) as exc_info:
            adapter.transcribe(seg)
        assert "Invalid audio data" in str(exc_info.value)


def test_create_transcription_adapter_direct_keys_and_options(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MY_WHISPER_KEY", "sk-env-key")
    cfg = AudioConfig(
        adapter="openai_whisper",
        transcription_endpoint="https://api.test/v1/audio/transcriptions",
        transcription_model="whisper-large-v3",
        transcription_api_key_env="MY_WHISPER_KEY",
    )
    adapter = create_transcription_adapter(cfg)
    assert isinstance(adapter, OpenAiTranscriptionAdapter)
    assert adapter.api_key == "sk-env-key"
    assert adapter.model == "whisper-large-v3"

    # 兼容通过 SimpleNamespace 传入直接 api_key / language / response_format 的配置对象
    from types import SimpleNamespace
    duck_cfg = SimpleNamespace(
        adapter="whisper",
        transcription_endpoint="https://api.test/v1/audio/transcriptions",
        transcription_model="whisper-base",
        transcription_api_key="sk-direct-key",
        transcription_response_format="verbose_json",
        transcription_language="zh",
    )
    adapter_duck = create_transcription_adapter(duck_cfg)
    assert adapter_duck.api_key == "sk-direct-key"
    assert adapter_duck.response_format == "verbose_json"
    assert adapter_duck.language == "zh"
    assert adapter_duck.model == "whisper-base"


def test_transcription_adapter_pipeline_with_parse_audio(tmp_path: Path):
    path = tmp_path / "sample.wav"
    seg = _wav_segment(1, frames=8000)
    path.write_bytes(seg.data)

    adapter = OpenAiTranscriptionAdapter(
        endpoint="https://api.test/v1/audio/transcriptions",
        transport=lambda segment, timeout=None: {"text": f"transcript-of-seg-{segment.ordinal}"},
        local_only=True,
    )

    from types import SimpleNamespace

    sink = DictMediaSink()
    audio_cfg = AudioConfig(adapter="whisper", transcription_endpoint="https://api.test", segment_seconds=2, overlap_seconds=1)
    cfg = audio_settings(SimpleNamespace(audio_enabled=True), audio_cfg)
    result = parse_audio(path, cfg, adapter=adapter, sink=sink)


    assert result.capabilities["transcript"] is True
    assert "transcript-of-seg-1" in result.markdown
    assert len(sink.occurrences) == 1
    assert sink.occurrences[0].caption == "transcript-of-seg-1"


# =====================================================================
# 3. MinerU 结构媒体映射回归
# =====================================================================


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_mineru_zip_structure_mapping_caption_ocr_page_and_anchor():
    markdown = (
        "# 论文标题\n\n"
        "正文开头段落，说明实验背景。\n\n"
        "![图1：分布曲线图](images/fig_1.png)\n\n"
        "中间段落，介绍表格内容。\n\n"
        "![表1：性能指标](images/table_1.png)\n\n"
        "未在正文中引用的散图：\n\n"
        "结尾结论。\n"
    )
    anchor_1_start = markdown.index("![图1：分布曲线图]")
    anchor_1_end = anchor_1_start + len("![图1：分布曲线图](images/fig_1.png)")
    anchor_2_start = markdown.index("![表1：性能指标]")
    anchor_2_end = anchor_2_start + len("![表1：性能指标](images/table_1.png)")

    content_list = [
        {
            "type": "text",
            "text": "正文开头段落，说明实验背景。",
            "page_idx": 0,
            "bbox": [50, 100, 950, 300],
        },
        {
            "type": "image",
            "img_path": "images/fig_1.png",
            "image_caption": ["图1：分布曲线图（1989–2000）"],
            "ocr": "OCR Text inside Fig 1",
            "page_idx": 1,
            "bbox": [62, 480, 946, 904],
        },
        {
            "type": "table",
            "img_path": "images/table_1.png",
            "table_caption": ["表1：性能指标全览"],
            "page_idx": 2,
            "bbox": [100, 200, 900, 800],
        },
    ]

    zip_bytes = _make_zip({
        "full.md": markdown.encode("utf-8"),
        "content_list.json": json.dumps(content_list).encode("utf-8"),
        "images/fig_1.png": PNG_BYTES,
        "images/table_1.png": PNG_BYTES,
        "images/unreferenced.png": PNG_BYTES,
    })

    sink = DictMediaSink()
    limits = ResourceLimits(
        memory_budget_bytes=64 * 1024 * 1024,
        extracted_max_bytes=32 * 1024 * 1024,
        markdown_max_bytes=1024 * 1024,
        media_max_bytes=1024 * 1024,
        json_max_bytes=1024 * 1024,
        max_members=50,
        max_media=20,
        max_image_pixels=25000000,
    )

    outcome = _safe_extract_zip(zip_bytes, limits=limits, sink=sink)
    assert outcome.partial is False
    assert len(outcome.media) == 3

    # 图 1 验证：caption, ocr, page (1-based), bbox, anchor
    m1 = next(m for m in outcome.media if m.name == "images/fig_1.png")
    assert m1.page == 2  # page_idx 1 -> page 2
    assert m1.caption == "图1：分布曲线图（1989–2000）"
    assert m1.ocr == "OCR Text inside Fig 1"
    assert m1.bbox == (62.0, 480.0, 946.0, 904.0)
    assert m1.anchor_start == anchor_1_start
    assert m1.anchor_end == anchor_1_end
    assert markdown[m1.anchor_start : m1.anchor_end] == "![图1：分布曲线图](images/fig_1.png)"

    # 表 1 验证：table_caption 透传，page 3
    m2 = next(m for m in outcome.media if m.name == "images/table_1.png")
    assert m2.page == 3  # page_idx 2 -> page 3
    assert m2.caption == "表1：性能指标全览"
    assert m2.ocr == ""
    assert m2.bbox == (100.0, 200.0, 900.0, 800.0)
    assert m2.anchor_start == anchor_2_start
    assert m2.anchor_end == anchor_2_end

    # 未引用的图：无 markdown 证据 -> anchor 必须为 None，不可用尺寸冒充！
    m3 = next(m for m in outcome.media if m.name == "images/unreferenced.png")
    assert m3.anchor_start is None
    assert m3.anchor_end is None
    assert m3.page is None
    assert m3.caption == ""


def test_mineru_store_media_sink_persists_anchor_and_dimensions_separately(tmp_path: Path):
    """验证真实 SQLite DocumentStore 落盘后，anchor 与尺寸完全隔离。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
    )
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)

    markdown = "# 报告\n\n![图示](images/demo.png)\n"
    anchor_start = markdown.index("![图示]")
    anchor_end = anchor_start + len("![图示](images/demo.png)")

    try:
        sink = StoreMediaSink(store)
        sink.add(
            name="images/demo.png",
            data=PNG_BYTES,
            kind="image",
            ordinal=1,
            mime_type="image/png",
            page=2,
            width=640,
            height=480,
            caption="说明文字",
            ocr="识别文字",
            anchor_start=anchor_start,
            anchor_end=anchor_end,
        )

        staged = store.stage_revision(
            source="paper.pdf",
            source_sha256=hashlib.sha256(b"raw").hexdigest(),
            render_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
            parser_fingerprint="mineru-v4:test",
            markdown=markdown,
        )
        store.attach_occurrences(staged.revision_id, sink.items)
        store.commit_revision(staged.revision_id, source_sha256=hashlib.sha256(b"raw").hexdigest())

        # 用第二 SQLite 连接读回验证落盘列
        db_path = tmp_path / "backup.sqlite"
        store.backup_to(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT anchor_start, anchor_end, page, caption, ocr, metadata_json FROM media_occurrences WHERE occurrence_id = 'occ-00001'"
            ).fetchone()
            assert row is not None
            assert row[0] == anchor_start
            assert row[1] == anchor_end
            assert row[0] != 640 and row[1] != 480, "尺寸绝不可写进 anchor 列"
            assert row[2] == 2  # page
            assert row[3] == "说明文字"
            assert row[4] == "识别文字"
            meta = json.loads(row[5])
            assert meta.get("archive_member") == "images/demo.png"
        finally:
            conn.close()
    finally:
        store.close()


def test_mineru_auth_failure_does_not_silently_fallback_to_agent():
    """v4 认证失败绝不自动降级到 Agent 免登通道。"""
    client = MineruClient(api_key="invalid-token")

    def fake_post(*a, **kw):
        raise MineruError("v4 apply upload url failed: unauthorized", code=-60001, http_status=401)

    with patch.object(client, "_paid_post", side_effect=fake_post):
        with pytest.raises(MineruError) as exc_info:
            client._parse_v4(Path("doc.pdf"), poll_interval=1, sink=DictMediaSink(), deadline=100, intent_recorder=None, request_id="r1")
        assert "unauthorized" in str(exc_info.value)


def test_mineru_anchor_advanced_formatting_and_page_mapping():
    markdown = (
        "# 测试文档\n\n"
        "![图1：标题属性](images/fig_title.png \"Figure 1 With Title\")\n\n"
        "![图2：尖括号语法](<images/fig_angle.png>)\n\n"
        "![图3：URL编码](images/fig%20space.png)\n\n"
        "![图4：多行\n换行标注](images/fig_multi.png)\n\n"
        "结尾文本。\n"
    )
    content_list = [
        {
            "type": "image",
            "img_path": "images/fig_title.png",
            "page": 5,  # 1-based page without page_idx
            "caption": [{"text": "结构化嵌套 Caption"}],
        },
        {
            "type": "image",
            "img_path": "images/fig_angle.png",
            "page_idx": 5,
            "caption": "尖括号图",
        },
        {
            "type": "image",
            "img_path": "images/fig space.png",
            "page_idx": 6,
            "caption": "空格图",
        },
        {
            "type": "image",
            "img_path": "images/fig_multi.png",
            "page_idx": 7,
            "caption": "多行说明图",
        },
    ]

    zip_bytes = _make_zip({
        "full.md": markdown.encode("utf-8"),
        "content_list.json": json.dumps(content_list).encode("utf-8"),
        "images/fig_title.png": PNG_BYTES,
        "images/fig_angle.png": PNG_BYTES,
        "images/fig space.png": PNG_BYTES,
        "images/fig_multi.png": PNG_BYTES,
    })

    sink = DictMediaSink()
    limits = ResourceLimits()
    outcome = _safe_extract_zip(zip_bytes, limits=limits, sink=sink)
    assert len(outcome.media) == 4

    # 1. 验证带 Title 的 Markdown 图片被准确识别为 anchor，且 page 字段生效（page=5）
    m_title = next(m for m in outcome.media if m.name == "images/fig_title.png")
    assert m_title.page == 5
    assert m_title.caption == "结构化嵌套 Caption"
    assert m_title.anchor_start is not None
    assert markdown[m_title.anchor_start : m_title.anchor_end].startswith("![图1：标题属性](images/fig_title.png")

    # 2. 验证尖括号 destination
    m_angle = next(m for m in outcome.media if m.name == "images/fig_angle.png")
    assert m_angle.anchor_start is not None
    assert markdown[m_angle.anchor_start : m_angle.anchor_end] == "![图2：尖括号语法](<images/fig_angle.png>)"

    # 3. 验证 URL 编码文件名
    m_space = next(m for m in outcome.media if m.name == "images/fig space.png")
    assert m_space.anchor_start is not None
    assert markdown[m_space.anchor_start : m_space.anchor_end] == "![图3：URL编码](images/fig%20space.png)"

    # 4. 验证多行 alt 文本
    m_multi = next(m for m in outcome.media if m.name == "images/fig_multi.png")
    assert m_multi.anchor_start is not None
    assert markdown[m_multi.anchor_start : m_multi.anchor_end] == "![图4：多行\n换行标注](images/fig_multi.png)"

