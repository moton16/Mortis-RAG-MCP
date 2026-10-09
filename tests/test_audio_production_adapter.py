"""E09：AudioConfig 生产装配、格式分流、崩溃续跑与失败可见性（NEW）。

真实 server 工厂（`VaultMcpServer` + 真实临时 registry/config/cache + 真实 SQLite
DocumentStore）与真实 PCM WAV fixture；远端/解码一律显式 mock 或显式注入，
不联网、不计费、不碰用户媒体。fake decoder 只验证**接缝**，不冒充真实格式通过。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import DocumentStore, OwnershipLost, resolve_storage_layout
from mortis_rag_mcp.ingest.audio import (AudioUnsupported, audio_settings, iter_segments,
                                         parse_audio)
from mortis_rag_mcp.ingest.transcription import AUDIO_ADAPTER_UNAVAILABLE
from mortis_rag_mcp.ingest.worker import StoreMediaSink, VirtualIngestWorker
from mortis_rag_mcp.server import VaultMcpServer

AUDIO_CFG_KWARGS = {"segment_seconds": 2, "overlap_seconds": 1}


@pytest.fixture()
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def wav(path: Path, frames: int = 28001, *, seed: int = 0) -> Path:
    # 各段 PCM 互不相同：全零会让不同片段落成同一个 blob，掩盖重复/复用差异。
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = bytes(((index + seed * 31) * 7 + 3) % 256 for index in range(frames * 2))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(payload)
    return path


def put_bytes(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / "vault" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def build_server(tmp_path: Path, *, audio_toml: str = "", vault_name: str = "Vault"):
    """真实 server 工厂：真实 app.toml（含 [audio]）、真实 registry 与缓存目录。"""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        'mode = "static"\n\n[ingest]\nenabled = true\nstorage = "virtual"\naudio_enabled = true\n'
        + (f"\n[audio]\n{audio_toml}\n" if audio_toml else ""),
        encoding="utf-8",
    )
    server = VaultMcpServer(config_path)
    server.config.cache.dir = str(tmp_path / "cache")
    server.config.cache.enabled = True
    server.config.cache.placement = "home"
    server.registry.add(str(vault), name=vault_name)
    return server, vault


class _RecordingAdapter:
    evidence_reference = "test-fixture-only"
    fingerprint = "fixture-v1"
    local_only = True

    def __init__(self, *, fail_from: int | None = None, accept_timeout: bool = False) -> None:
        self.calls: list[int] = []
        self.timeouts: list[float | None] = []
        self._fail_from = fail_from
        self._accept_timeout = accept_timeout

    def transcribe(self, segment, timeout=None):
        self.calls.append(int(segment.ordinal))
        self.timeouts.append(timeout)
        if self._fail_from is not None and int(segment.ordinal) >= self._fail_from:
            raise RuntimeError("injected crash after confirmed segments")
        return {"text": f"words-{segment.ordinal}"}


class _FixturePcmDecoder:
    """显式注入的解码组件 fixture：只证明接缝与分流，不代表真实 ffmpeg/mp3 通过。"""

    name = "fixture-pcm"
    fingerprint = "fixture-v1"
    supported_formats = frozenset({"mp3", "flac", "m4a"})

    def __init__(self, *, sample_rate: int = 8000, sample_width: int = 2, channels: int = 1,
                 frames: int = 8000) -> None:
        self._rate = sample_rate
        self._width = sample_width
        self._channels = channels
        self._frames = frames

    def probe(self, path: Path) -> dict[str, int]:
        return {"channels": self._channels, "sample_width": self._width,
                "sample_rate": self._rate, "frames": self._frames}

    def read_frames(self, path: Path, start_frame: int, count: int) -> bytes:
        block = self._frames * self._channels * self._width
        start = start_frame * self._channels * self._width
        data = bytes((index * 5 + 1) % 256 for index in range(block))
        return data[start:start + count * self._channels * self._width]


def _audio_config(**overrides):
    from mortis_rag_mcp.config import AudioConfig

    return AudioConfig(**{**AUDIO_CFG_KWARGS, **overrides})


def _ingest_ns(**overrides):
    """virtual worker 的最小 ingest 配置（音频参数走真实 AudioConfig，不读 flat）。"""
    base = {"enabled": True, "audio_enabled": True, "storage": "virtual",
            "network_policy": "configured", "routing": "auto", "auto_watch": False,
            "output_dirname": ".mortis-parsed"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _acfg(**overrides):
    """直接喂给 parse_audio/iter_segments 的合成视图（含 ingest 开关字段）。"""
    return audio_settings(_ingest_ns(), _audio_config(**overrides))


def _store(tmp_path: Path, name: str = "vault") -> DocumentStore:
    vault = tmp_path / name
    vault.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / f"cache-{name}"), enabled=True),
    )
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)
    return store


def _expire_lease(store: DocumentStore, job_id: str) -> None:
    conn = sqlite3.connect(str(store._store_path(store.generation_id)))
    try:
        conn.execute("UPDATE ingest_jobs SET lease_until = ? WHERE job_id = ?",
                     (time.time() - 1.0, job_id))
        conn.commit()
    finally:
        conn.close()


def _occurrences(store: DocumentStore, source: str) -> list[dict]:
    active = store.get_active(source)
    assert active is not None
    return list(store.iter_media(source, revision_id=active.revision.revision_id))


def _blob_count(store: DocumentStore) -> int:
    conn = store._open_conn(store._ensure_read_generation())
    try:
        return int(conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------- 配置真正生效（A）

def test_server_factory_wires_audioconfig_chunker_and_worker(isolated_registry, tmp_path):
    """AudioConfig/chunker 指纹/worker 接缝沿 server→factory→worker 全部到位。"""
    server, vault = build_server(tmp_path)
    assert server.config.audio.segment_seconds == 30  # 默认 AudioConfig 已加载

    manager = server._ingest_manager_for(str(vault))
    from mortis_rag_mcp.ingest.worker import VirtualIngestWorker

    assert isinstance(manager, VirtualIngestWorker)
    assert manager.audio_config is server.config.audio
    assert manager._chunker_fingerprint() != "chunker-fingerprint-unwired", \
        "生产装配必须注入真实 chunker 指纹 provider"
    assert manager.audio_decoder is None  # 未注入解码组件：非 PCM 明确 blocked
    assert manager._audio_settings().segment_seconds == 30


def test_non_default_audio_config_governs_segments_and_job_reuse(isolated_registry, tmp_path):
    """非默认分段/重叠确实被消费；不同音频 profile 不复用旧任务。"""
    wav(vault_path := tmp_path / "vault" / "sound.wav")
    server, vault = build_server(tmp_path, audio_toml="segment_seconds = 2\noverlap_seconds = 1")
    result = server.call_tool("kb_ingest", {"action": "submit", "sources": ["sound.wav"]})
    data = json.loads(result["content"][0]["text"])
    assert data["submitted"] == 1
    manager = server._ingest_manager_for(str(vault))
    manager._worker.join(timeout=10.0)
    status = manager.status()["jobs"][0]
    assert status["state"] == "done", status

    store = manager._store()
    active = store.get_active("sound.wav")
    caps = active.revision.capabilities
    assert caps["audio_metadata"]["segments"] == 3, "非默认 2s/1s 配置必须生效（默认 30s 只有 1 段）"
    assert len(caps["coverage_ms"]) == 3
    assert [occ["t_start_ms"] for occ in _occurrences(store, "sound.wav")] == [0, 1000, 2000]

    # 改音频 profile → job 指纹变化 → 不与旧任务合并（不误复用旧片段/旧任务）。
    server.config.audio.segment_seconds = 1
    server.config.audio.overlap_seconds = 0
    fingerprint_a = manager._parser_fingerprint("sound.wav")
    fingerprint_b = manager._parser_fingerprint("sound.wav")
    assert fingerprint_a == fingerprint_b  # 同 profile 稳定
    server.config.audio.overlap_seconds = 1
    assert manager._parser_fingerprint("sound.wav") != fingerprint_a
    # 文档源不受音频 profile 影响（避免已入库文档被判成新 profile 而重复入队）。
    assert manager._parser_fingerprint("doc.pdf") == manager._parser_fingerprint("other.pdf")


# ------------------------------------------------------- adapter/timeout/分流（B）

def test_declared_adapter_without_implementation_blocks_visibility(isolated_registry, tmp_path):
    """声明了转录 adapter 但没有可注入实现：入队前明确拒绝，不静默降级 metadata_only。"""
    wav(tmp_path / "vault" / "sound.wav")
    server, vault = build_server(tmp_path, audio_toml='adapter = "transcript"')

    assert server._transcription_adapter() is None  # 工厂不编造实现
    with pytest.raises(ValueError) as exc_info:
        server.call_tool("kb_ingest", {"action": "submit", "sources": ["sound.wav"]})
    assert AUDIO_ADAPTER_UNAVAILABLE in str(exc_info.value)
    manager = server._ingest_manager_for(str(vault))
    assert manager._store().queue_depth() == 0, "缺 adapter 必须入队前拒绝"


def test_transcription_timeout_is_consumed_by_adapter(isolated_registry, tmp_path):
    """`audio.transcription_timeout` 必须真正透传给 adapter（不是摆设字段）。"""
    wav(tmp_path / "vault" / "sound.wav")
    server, vault = build_server(tmp_path, audio_toml="transcription_timeout = 7.5")
    manager = server._ingest_manager_for(str(vault))
    adapter = _RecordingAdapter()
    manager.audio_adapter = adapter
    result = server.call_tool("kb_ingest", {"action": "submit", "sources": ["sound.wav"]})
    data = json.loads(result["content"][0]["text"])
    assert data["submitted"] == 1
    manager._worker.join(timeout=10.0)
    assert manager.status()["jobs"][0]["state"] == "done"
    assert adapter.calls == [1]
    assert adapter.timeouts == [7.5]


def test_transcript_success_is_not_native_audio(isolated_registry, tmp_path):
    """有转录也只是 transcript 出口：semantic_audio 永远为 False，不冒 native。"""
    wav(tmp_path / "vault" / "sound.wav")
    server, vault = build_server(tmp_path)
    manager = server._ingest_manager_for(str(vault))
    manager.audio_adapter = _RecordingAdapter()
    server.call_tool("kb_ingest", {"action": "submit", "sources": ["sound.wav"]})
    manager._worker.join(timeout=10.0)
    store = manager._store()
    caps = store.get_active("sound.wav").revision.capabilities
    assert caps["transcript"] is True
    assert caps["semantic_audio"] is False
    assert caps["status"] == "transcript"
    assert caps["line_basis"] == "transcript"


@pytest.mark.parametrize("name,payload", [
    ("sound.mp3", b"ID3\x03\x00\x00" + b"\x00" * 64),
    ("sound.flac", b"fLaC" + b"\x00" * 64),
    ("sound.m4a", b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 64),
    ("sound.wav", b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x02\x00" + b"\x00" * 20),
])
def test_non_pcm_without_decoder_blocks_before_enqueue(isolated_registry, tmp_path, name, payload):
    """非 PCM/非 PCM-WAV 缺解码能力：按实际格式分流拒绝（0 job、0 MinerU 调用）。"""
    put_bytes(tmp_path, name, payload)
    server, vault = build_server(tmp_path)
    calls: list[str] = []

    class _NoMineru:
        def __init__(self, *args, **kwargs):
            pass

        def parse_structured(self, *args, **kwargs):
            calls.append("mineru")
            raise AssertionError("audio must never reach the document channel")

    import mortis_rag_mcp.ingest.worker as worker_mod

    original = worker_mod.MineruClient
    worker_mod.MineruClient = _NoMineru
    try:
        with pytest.raises(ValueError, match="decoder|blocked|unrecognized"):
            server.call_tool("kb_ingest", {"action": "submit", "sources": [name]})
    finally:
        worker_mod.MineruClient = original
    assert calls == []
    manager = server._ingest_manager_for(str(vault))
    assert manager._store().queue_depth() == 0


def test_declared_ffmpeg_without_component_blocks(isolated_registry, tmp_path):
    """声明了 ffmpeg 却没有注入组件：明确 blocked（不隐式 shell、不静默 core_pcm）。"""
    put_bytes(tmp_path, "sound.mp3", b"ID3\x03\x00\x00" + b"\x00" * 64)
    server, _ = build_server(tmp_path, audio_toml='decoder = "ffmpeg"')
    with pytest.raises(ValueError, match="no verified decoder component injected"):
        server.call_tool("kb_ingest", {"action": "submit", "sources": ["sound.mp3"]})


def test_injected_decoder_drives_non_pcm_dispatch(isolated_registry, tmp_path):
    """显式注入声明 mp3 能力的解码组件：按格式分流走解码段调度（fixture 级验证）。"""
    put_bytes(tmp_path, "sound.mp3", b"ID3\x03\x00\x00" + b"\x00" * 64)
    server, vault = build_server(tmp_path, audio_toml='decoder = "ffmpeg"')
    manager = server._ingest_manager_for(str(vault))
    manager.audio_decoder = _FixturePcmDecoder()
    result = server.call_tool("kb_ingest", {"action": "submit", "sources": ["sound.mp3"]})
    data = json.loads(result["content"][0]["text"])
    assert data["submitted"] == 1
    manager._worker.join(timeout=10.0)
    status = manager.status()["jobs"][0]
    assert status["state"] == "done", status
    store = manager._store()
    caps = store.get_active("sound.mp3").revision.capabilities
    assert caps["source_format"] == "mp3"
    assert caps["decoder"] == "fixture-pcm:fixture-v1"
    assert caps["audio_metadata"]["segments"] == 1  # 8000 帧 @ 30s 段
    occurrences = _occurrences(store, "sound.mp3")
    assert len(occurrences) == 1
    assert occurrences[0]["kind"] == "audio"
    meta = occurrences[0].get("metadata") or {}
    assert meta.get("source_format") == "mp3", "段媒体必须记录真实来源格式"


def test_decoder_capability_mismatch_refuses(isolated_registry, tmp_path):
    """注入组件未声明该格式能力：明确拒绝，不猜。"""
    put_bytes(tmp_path, "sound.flac", b"fLaC" + b"\x00" * 64)
    server, vault = build_server(tmp_path, audio_toml='decoder = "ffmpeg"')
    manager = server._ingest_manager_for(str(vault))
    manager.audio_decoder = _FixturePcmDecoder()
    manager.audio_decoder.supported_formats = frozenset({"mp3"})  # 不声明 flac
    with pytest.raises(ValueError, match="does not declare flac capability"):
        server.call_tool("kb_ingest", {"action": "submit", "sources": ["sound.flac"]})
    assert manager._store().queue_depth() == 0


# ------------------------------------------------- 崩溃续跑 / 租约 / unknown（B）

def test_crash_resume_reuses_confirmed_segments_and_media(isolated_registry, tmp_path):
    """崩溃续跑（真实 store + 真实 WAV）：已确认段零重复转录/sink，媒体引用完整复挂。"""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    path = wav(vault / "sound.wav")
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    store = _store(tmp_path, "vault")
    try:
        cfg = _ingest_ns()
        job, _ = store.enqueue_job(source="sound.wav", source_sha256=source_sha,
                                   parser_fingerprint="audio-fixture")
        claimed = store.claim_job("A")
        assert claimed.job_id == job.job_id

        adapter1 = _RecordingAdapter(fail_from=2)
        worker1 = VirtualIngestWorker(vault, cfg, lambda: store,
                                      audio_config=_audio_config(), audio_adapter=adapter1)
        with pytest.raises(RuntimeError):
            worker1._run_job(store, claimed, "A")  # 第 2 段注入崩溃
        assert adapter1.calls == [1, 2]
        done = [row for row in store.list_subjobs(job.job_id) if row.state == "done"]
        assert [row.ordinal for row in done] == [1], "只有已确认的段进 checkpoint"

        # 崩溃：租约过期 → recover/GC 不得吃掉被 checkpoint 引用的 blob → 同 job 重领
        _expire_lease(store, job.job_id)
        assert store.gc_unreferenced() == 0
        reclaimed = store.claim_job("B")
        assert reclaimed is not None and reclaimed.job_id == job.job_id

        adapter2 = _RecordingAdapter()
        worker2 = VirtualIngestWorker(vault, cfg, lambda: store,
                                      audio_config=_audio_config(), audio_adapter=adapter2)
        worker2._run_job(store, reclaimed, "B")
        assert adapter2.calls == [2, 3], "已确认段不得重复转录（重复计费）"

        active = store.get_active("sound.wav")
        assert active is not None
        occurrences = list(store.iter_media("sound.wav", revision_id=active.revision.revision_id))
        assert [occ["ordinal"] for occ in occurrences] == [1, 2, 3], "恢复媒体必须完整复挂"
        assert _blob_count(store) == 3, "已确认段不重复 sink（blob 总数就是 3 段）"
        caps = active.revision.capabilities
        assert caps["resumed_ordinals"] == [1]
        assert len(caps["coverage_ms"]) == 3
    finally:
        store.close()


def test_unknown_submission_phase_is_never_auto_retried(isolated_registry, tmp_path):
    """结果未知的远端任务：retry 拒绝、保持状态与 next action，不自动 queued/重发。"""
    store = _store(tmp_path, "vault-unknown")
    try:
        job, _ = store.enqueue_job(source="a.wav", source_sha256="s" * 64,
                                   parser_fingerprint="audio-fixture")
        store.claim_job("A")
        store.report_phase(job.job_id, "A", phase="submission_unknown")
        store.fail_job(job.job_id, "A", error_code="SUBMISSION_UNKNOWN",
                       error_summary="expired remote submission requires review")
        worker = VirtualIngestWorker(tmp_path / "vault-unknown", _ingest_ns(),
                                     lambda: store)
        view = worker.retry(job.job_id)
        assert view["retried"] is False
        assert view["state"] == "failed" and view["phase"] == "submission_unknown"
        assert "next_action" in view and view["next_action"]
        assert store.job_status(job.job_id).state == "failed"
    finally:
        store.close()


def test_lease_lost_owner_cannot_write_checkpoints(tmp_path):
    """租约丢失后的旧 owner：checkpoint 写入被真实拒绝，且不得被静默吞掉。"""
    store = _store(tmp_path, "vault-lease")
    try:
        path = tmp_path / "lease.wav"
        wav(path, frames=24000)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        job, _ = store.enqueue_job(source="lease.wav", source_sha256=sha,
                                   parser_fingerprint="audio-fixture")
        store.claim_job("A")
        _expire_lease(store, job.job_id)
        store.claim_job("B")  # 新 owner 接管
        segment = next(iter(iter_segments(path, _acfg())))
        worker = VirtualIngestWorker(tmp_path / "vault-lease", _ingest_ns(),
                                     lambda: store, audio_config=_audio_config())
        with pytest.raises(OwnershipLost):
            worker._audio_checkpoint(store, job, "A", segment, "late words")
    finally:
        store.close()


def test_same_transcript_different_audio_never_reuses_segments(tmp_path):
    """相同转录文本、不同音频：片段 SHA 不同 → 必须重算，绝不用 A 的转录冒 B 的段。"""
    store = _store(tmp_path, "vault-reuse")
    try:
        path_a = tmp_path / "a.wav"
        path_b = tmp_path / "b.wav"
        wav(path_a, frames=28001)
        wav(path_b, frames=28001, seed=1)  # 时长相同、内容不同：转录文本相同但段 SHA 不同
        cfg = _acfg()
        adapter = _RecordingAdapter()
        # sink 写 blob 走真实租约 fencing：需要两个真实已领取的 job。
        job_a, _ = store.enqueue_job(source="a.wav", source_sha256="a" * 64,
                                     parser_fingerprint="audio-fixture")
        job_b, _ = store.enqueue_job(source="b.wav", source_sha256="b" * 64,
                                     parser_fingerprint="audio-fixture")
        store.claim_job("A")
        store.claim_job("B")
        sink_a = StoreMediaSink(store, job_id=job_a.job_id, owner_token="A")
        sink_b = StoreMediaSink(store, job_id=job_b.job_id, owner_token="B")
        result_a = parse_audio(path_a, cfg, adapter=adapter, sink=sink_a)
        assert adapter.calls == [1, 2, 3]
        # 模拟「按 ordinal 复用 A 的已完成事实」的错误恢复：SHA 校验必须拒绝。
        first = {ordinal: {"ordinal": ordinal, "sha256": seg.input_sha256,
                           "t_start_ms": int(seg.t_start_ms), "t_end_ms": int(seg.t_end_ms),
                           "text": f"words-{ordinal}"}
                 for ordinal, seg in enumerate(iter_segments(path_a, cfg), start=1)}
        adapter_b = _RecordingAdapter()
        result_b = parse_audio(path_b, cfg, adapter=adapter_b, sink=sink_b, resume=first)
        assert adapter_b.calls == [1, 2, 3], "不同音频的同 ordinal 段 SHA 必然不同：必须重算"
        assert result_b.capabilities["resumed_ordinals"] == []
        assert result_b.capabilities["profile_fingerprint"] == result_a.capabilities["profile_fingerprint"]
        # 相同转录文本（正文只有标题文件名不同）：
        lines_a = [line for line in result_a.markdown.splitlines() if line.startswith("[")]
        lines_b = [line for line in result_b.markdown.splitlines() if line.startswith("[")]
        assert lines_a == lines_b == ["[0-2000 ms] words-1", "[1000-3000 ms] words-2",
                                      "[2000-3500 ms] words-3"]
        assert (sink_a.items[0].metadata["segment_sha256"]
                != sink_b.items[0].metadata["segment_sha256"])
    finally:
        store.close()
