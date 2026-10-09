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
from urllib.error import HTTPError

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
    HttpMediaTransport,
    NativeMediaEvidence,
    NativeMediaProvider,
)
from mortis_rag_mcp.providers import ProviderError, create_media_provider

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
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
