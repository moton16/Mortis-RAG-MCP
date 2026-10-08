"""C99/C104 接缝合同测试：legacy 音频拒绝、队列指纹与共享预算、音频 checkpoint/resume。

无真实端点：不加载转录/native 能力，只用本地 PCM WAV 与注入 fixture。
"""
from __future__ import annotations

import json
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mortis_rag_mcp.ingest.audio import AudioSegment
from mortis_rag_mcp.ingest.worker import (
    AUDIO_ROUTE_UNSUPPORTED,
    DEFAULT_PARSE_BUDGET,
    IngestConfig,
    IngestManager,
    VirtualIngestWorker,
)


def _wav(path: Path, frames: int = 8000) -> Path:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * frames)
    return path


# --------------------------------------------------------------------- legacy 音频拒绝

def test_legacy_explicit_audio_submit_rejects_without_cloud(tmp_path, monkeypatch):
    """legacy 显式提交音频：同步拒绝（稳定错误码），0 云提交，0 job 入队。"""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, audio_enabled=True))
    _wav(tmp_path / "sound.wav")
    parse = MagicMock()
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru.MineruClient.parse", parse)

    with pytest.raises(ValueError, match=AUDIO_ROUTE_UNSUPPORTED):
        mgr.submit(["sound.wav"])

    assert parse.call_count == 0
    assert mgr.status()["jobs"] == []


@pytest.mark.parametrize("suffix", [".wav", ".mp3"])
def test_legacy_scanned_audio_job_fails_without_cloud(tmp_path, monkeypatch, suffix):
    """legacy 扫描到音频（audio_enabled=true）：job 明确失败，绝不落 MinerU。"""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, audio_enabled=True))
    name = f"sound{suffix}"
    if suffix == ".wav":
        _wav(tmp_path / name)
    else:
        (tmp_path / name).write_bytes(b"ID3\x03\x00\x00")

    parse = MagicMock()
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru.MineruClient.parse", parse)

    with patch.object(mgr, "_worker_loop"):
        res = mgr.submit(None)
    assert res["submitted"] == 1
    job_id = res["jobs"][0]["job_id"]

    mgr._worker_loop()

    job = mgr.status(job_id)["job"]
    assert job["state"] == "failed"
    assert AUDIO_ROUTE_UNSUPPORTED in job["error"]
    assert parse.call_count == 0


def test_legacy_audio_not_scanned_when_disabled(tmp_path):
    """audio_enabled=false 时音频不进扫描面（默认关闭）。"""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, audio_enabled=False))
    _wav(tmp_path / "sound.wav")
    assert mgr.scan_pending() == []


# ------------------------------------------------------------- 队列指纹与共享预算

def _virtual_worker(**cfg_kwargs) -> VirtualIngestWorker:
    base = {"routing": "auto", "network_policy": "configured", "storage": "virtual"}
    base.update(cfg_kwargs)
    return VirtualIngestWorker(Path("."), SimpleNamespace(**base), lambda: object())


def test_virtual_worker_defaults_to_process_shared_budget():
    """worker 默认注入模块级共享 ParseBudget，不得每库新建冒充全局。"""
    worker = _virtual_worker()
    assert worker.parse_budget is DEFAULT_PARSE_BUDGET


def test_job_fingerprint_distinguishes_routing_profile():
    """同内容不同 routing/network profile 不得误合并。"""
    auto = _virtual_worker(routing="auto")
    mineru = _virtual_worker(routing="mineru")
    auto2 = _virtual_worker(routing="auto")
    assert auto._parser_fingerprint() != mineru._parser_fingerprint()
    assert auto._parser_fingerprint() == auto2._parser_fingerprint()


def test_job_fingerprint_includes_chunker_fingerprint():
    base = _virtual_worker()
    provider_a = VirtualIngestWorker(Path("."), base.config, lambda: object(),
                                     chunker_fingerprint_provider=lambda: "chunker-A")
    provider_b = VirtualIngestWorker(Path("."), base.config, lambda: object(),
                                     chunker_fingerprint_provider=lambda: "chunker-B")
    assert provider_a._parser_fingerprint() != provider_b._parser_fingerprint()
    assert provider_a._parser_fingerprint() != base._parser_fingerprint()


# ------------------------------------------------------------- 音频 checkpoint/resume

class _FakeStore:
    def __init__(self) -> None:
        self.phases: list[dict] = []
        self.rows: list = []

    def report_phase(self, job_id, owner, *, phase, remote_task_id="", checkpoint="", error=""):
        self.phases.append({"phase": phase, "checkpoint": checkpoint})

    def list_subjobs(self, job_id):
        return self.rows


def test_audio_checkpoint_persist_and_resume_roundtrip():
    """逐片段 checkpoint 用现成 report_phase 持久化；有读回接口时 resume 可还原。"""
    worker = _virtual_worker()
    store = _FakeStore()
    job = SimpleNamespace(job_id="j1", phase="prepared", source="a.wav")
    segment = AudioSegment(1, 0, 16000, 0, 2000, "sha-1", b"data")

    worker._audio_checkpoint(store, job, "owner", segment, "hello")

    assert store.phases and store.phases[-1]["phase"] == "prepared"
    payload = json.loads(store.phases[-1]["checkpoint"])
    assert payload["completed"][0]["ordinal"] == 1
    assert payload["completed"][0]["text"] == "hello"

    store.rows = [SimpleNamespace(checkpoint=store.phases[-1]["checkpoint"])]
    resumed = worker._audio_resume(store, job)
    assert resumed[1]["text"] == "hello"
    assert resumed[1]["sha256"] == "sha-1"


def test_audio_resume_without_reader_is_empty():
    """没有 subjob 读回 API 时不假装能恢复（绝不猜）。"""
    worker = _virtual_worker()

    class _NoReader:
        def report_phase(self, *args, **kwargs):
            pass

    job = SimpleNamespace(job_id="j1", phase="prepared", source="a.wav")
    assert worker._audio_resume(_NoReader(), job) == {}
