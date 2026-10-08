"""E02：段级 checkpoint / 同 job 崩溃续跑 / 引用保护 / retry（NEW）。

真实 DocumentStore（SQLite）落盘；崩溃与租约过期用受控注入（不 sleep 猜时序）。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import (
    DocumentStore,
    OwnershipLost,
    StoreConflict,
    StoreContractError,
    resolve_storage_layout,
)
from mortis_rag_mcp.ingest.audio import iter_segments, parse_audio
from mortis_rag_mcp.ingest.worker import StoreMediaSink, VirtualIngestWorker


def audio_config(**kwargs):
    return SimpleNamespace(audio_enabled=True, audio_segment_seconds=2,
                           audio_overlap_seconds=1, **kwargs)


def wav(path: Path, frames: int = 24000) -> Path:
    # 各段 PCM 必须互不相同：全零会让不同片段落成同一个 blob，掩盖回收/保护差异。
    payload = bytes((index * 7 + 3) % 256 for index in range(frames * 2))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(payload)
    return path


def _store(tmp_path: Path, name: str = "vault") -> DocumentStore:
    vault = tmp_path / f"vault-{name}"
    vault.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / f"cache-{name}"), enabled=True),
    )
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)
    return store


def _expire_lease(store: DocumentStore, job_id: str) -> None:
    """受控注入：把租约直接拨到过去（模拟进程崩溃后租约过期）。"""
    conn = sqlite3.connect(str(store._store_path(store.generation_id)))
    try:
        conn.execute("UPDATE ingest_jobs SET lease_until = ? WHERE job_id = ?",
                     (time.time() - 1.0, job_id))
        conn.commit()
    finally:
        conn.close()


def _corrupt_checkpoint(store: DocumentStore, job_id: str, ordinal: int) -> None:
    conn = sqlite3.connect(str(store._store_path(store.generation_id)))
    try:
        conn.execute("UPDATE ingest_subjobs SET checkpoint = ? WHERE job_id = ? AND ordinal = ?",
                     ("{not-json", job_id, ordinal))
        conn.commit()
    finally:
        conn.close()


class _Adapter:
    evidence_reference = "test-fixture-only"
    fingerprint = "fixture-v1"
    local_only = True

    def __init__(self) -> None:
        self.calls: list[int] = []

    def transcribe(self, segment):
        self.calls.append(int(segment.ordinal))
        return {"text": f"transcribed-{segment.ordinal}"}


class _Sink:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, **kwargs):
        self.items.append(kwargs)
        return f"occ-{int(kwargs['ordinal']):05d}"


def _checkpoint_payload(segment, *, parent_sha: str, blob_id: str, occurrence_id: str) -> dict:
    return {
        "ordinal": int(segment.ordinal),
        "sha256": segment.input_sha256,
        "t_start_ms": int(segment.t_start_ms),
        "t_end_ms": int(segment.t_end_ms),
        "text": f"transcribed-{segment.ordinal}",
        "parent_source_sha256": parent_sha,
        "blob_id": blob_id,
        "media": {"occurrence_id": occurrence_id, "blob_id": blob_id, "kind": "audio",
                  "mime_type": "audio/wav", "t_start_ms": int(segment.t_start_ms),
                  "t_end_ms": int(segment.t_end_ms)},
    }


def test_segments_persisted_then_crash_gc_and_same_job_resume(tmp_path: Path):
    """段落盘 → 崩溃 → 租约过期 → recover/GC → 同 job 恢复：转录与 sink 零重复。"""
    store = _store(tmp_path, "restart")
    try:
        path = wav(tmp_path / "sound.wav", 28001)  # 3 段
        acfg = audio_config()
        segments = list(iter_segments(path, acfg))
        assert len(segments) == 3
        parent_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        job, _ = store.enqueue_job(source="sound.wav", source_sha256=parent_sha,
                                   parser_fingerprint="pcm-v1")
        store.claim_job("A")
        sink = StoreMediaSink(store, job_id=job.job_id, owner_token="A")
        for segment in segments[:2]:  # 前两段处理完即崩溃
            occurrence_id = sink.add(name=f"segment-{segment.ordinal}.wav", data=segment.data,
                                     kind="audio", ordinal=int(segment.ordinal),
                                     mime_type="audio/wav",
                                     t_start_ms=int(segment.t_start_ms),
                                     t_end_ms=int(segment.t_end_ms))
            blob_id = sink.items[-1].blob_id
            store.record_subjob(job.job_id, "A", ordinal=int(segment.ordinal),
                                input_hash=segment.input_sha256,
                                range={"kind": "audio_ms", "start": int(segment.t_start_ms),
                                       "end": int(segment.t_end_ms)},
                                state="done",
                                checkpoint=_checkpoint_payload(
                                    segment, parent_sha=parent_sha, blob_id=blob_id,
                                    occurrence_id=occurrence_id))

        # 崩溃：租约过期；旧 owner 之后不得再写（0 行）
        _expire_lease(store, job.job_id)
        with pytest.raises(OwnershipLost):
            store.record_subjob(job.job_id, "A", ordinal=3, state="done",
                                checkpoint={"ordinal": 3, "sha256": "x" * 64})

        # recover / GC 都不得吃掉尚未 attach 的可恢复 blob
        store.recover()
        assert store.gc_unreferenced() == 0, "未 attach 但被 checkpoint 引用的 blob 被提前回收"

        # 同 job 过期 reclaim（同一个 job_id）
        reclaimed = store.claim_job("B")
        assert reclaimed is not None and reclaimed.job_id == job.job_id

        rows = store.list_subjobs(job.job_id)
        done = {row.ordinal: row for row in rows if row.state == "done"}
        assert sorted(done) == [1, 2]
        assert done[1].range_kind == "audio_ms" and done[1].range_end is not None
        resume_state = {}
        for ordinal, row in done.items():
            import json as _json

            resume_state[ordinal] = _json.loads(row.checkpoint)

        adapter, second_sink = _Adapter(), _Sink()
        result = parse_audio(path, acfg, adapter=adapter, sink=second_sink, resume=resume_state)
        # 已确认段：零重复转录、零重复 sink
        assert adapter.calls == [3], f"已确认段被重复转录：{adapter.calls}"
        assert [item["ordinal"] for item in second_sink.items] == [3]
        assert result.capabilities["resumed_ordinals"] == [1, 2]
        assert len(result.capabilities["coverage_ms"]) == 3, "恢复后 coverage 仍覆盖全部片段"

        # 已确认段的媒体必须重新挂回（不重新 sink）
        specs = VirtualIngestWorker._resumed_occurrences(resume_state, second_sink)
        assert [spec.ordinal for spec in specs] == [1, 2]
        staged = store.stage_revision(source="sound.wav", source_sha256=parent_sha,
                                      render_sha256=hashlib.sha256(b"md").hexdigest(),
                                      parser_fingerprint="pcm-v1", markdown="# audio")
        assert store.attach_occurrences(staged.revision_id, specs, job_id=job.job_id,
                                        owner_token="B") == 2
    finally:
        store.close()


def test_corrupt_checkpoint_does_not_pollute_gc_protection(tmp_path: Path):
    """坏 checkpoint 不进保护集合；有效 checkpoint 引用的 blob 仍然保住。"""
    store = _store(tmp_path, "corrupt")
    try:
        path = wav(tmp_path / "sound.wav", 28001)
        segments = list(iter_segments(path, audio_config()))
        parent_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        job, _ = store.enqueue_job(source="sound.wav", source_sha256=parent_sha,
                                   parser_fingerprint="pcm-v1")
        store.claim_job("A")
        sink = StoreMediaSink(store, job_id=job.job_id, owner_token="A")
        blobs = []
        for segment in segments[:2]:
            occurrence_id = sink.add(name=f"s-{segment.ordinal}.wav", data=segment.data,
                                     kind="audio", ordinal=int(segment.ordinal),
                                     mime_type="audio/wav")
            blob_id = sink.items[-1].blob_id
            blobs.append(blob_id)
            store.record_subjob(job.job_id, "A", ordinal=int(segment.ordinal),
                                input_hash=segment.input_sha256,
                                range={"kind": "audio_ms", "start": 0, "end": 1000},
                                state="done",
                                checkpoint=_checkpoint_payload(
                                    segment, parent_sha=parent_sha, blob_id=blob_id,
                                    occurrence_id=occurrence_id))
        _expire_lease(store, job.job_id)
        _corrupt_checkpoint(store, job.job_id, 2)
        assert store.gc_unreferenced() == 1, "坏 checkpoint 仍被当成有效引用保护"

        store.claim_job("B")
        staged = store.stage_revision(source="sound.wav", source_sha256=parent_sha,
                                      render_sha256=hashlib.sha256(b"md").hexdigest(),
                                      parser_fingerprint="pcm-v1", markdown="# audio")
        from mortis_rag_mcp.doc_store import MediaOccurrenceSpec

        assert store.attach_occurrences(staged.revision_id, [MediaOccurrenceSpec(
            occurrence_id="occ-00001", blob_id=blobs[0], kind="audio", ordinal=1,
            mime_type="audio/wav")], job_id=job.job_id, owner_token="B") == 1
        with pytest.raises(StoreContractError):
            store.attach_occurrences(staged.revision_id, [MediaOccurrenceSpec(
                occurrence_id="occ-00002", blob_id=blobs[1], kind="audio", ordinal=2,
                mime_type="audio/wav")], job_id=job.job_id, owner_token="B")
    finally:
        store.close()


def test_subjob_contract_is_strict(tmp_path: Path):
    store = _store(tmp_path, "contract")
    try:
        job, _ = store.enqueue_job(source="a.wav", source_sha256="s" * 64,
                                   parser_fingerprint="pcm-v1")
        store.claim_job("A")
        # ordinal 0 只能写父任务的远端 submission 阶段
        with pytest.raises(StoreContractError):
            store.record_subjob(job.job_id, "A", ordinal=0, state="parsing")
        assert store.record_subjob(job.job_id, "A", ordinal=0, state="prepared") is True
        # ordinal >= 1 不能用父阶段状态
        with pytest.raises(StoreContractError):
            store.record_subjob(job.job_id, "A", ordinal=1, state="prepared")
        # range 必须带 kind，且不得把页范围当毫秒
        with pytest.raises(StoreContractError):
            store.record_subjob(job.job_id, "A", ordinal=1, state="done",
                                range={"start": 0, "end": 10})
        assert store.record_subjob(job.job_id, "A", ordinal=1, state="done",
                                   range={"kind": "document_pages", "start": 0, "end": 10}) is True
        # 超限 checkpoint：写前拒绝，不截断
        huge = {"blob": "x" * (2 << 20)}
        with pytest.raises(StoreContractError):
            store.record_subjob(job.job_id, "A", ordinal=2, state="done", checkpoint=huge)
        assert [row.ordinal for row in store.list_subjobs(job.job_id)] == [0, 1]
    finally:
        store.close()


def test_retry_job_only_for_failed_or_cancelled(tmp_path: Path):
    """E02-b：仅 failed/cancelled → queued；活任务与 unknown 不得被假重试。"""
    store = _store(tmp_path, "retry")
    try:
        job, _ = store.enqueue_job(source="a.wav", source_sha256="s" * 64,
                                   parser_fingerprint="pcm-v1")
        claimed = store.claim_job("A")
        # 活任务不可重试
        with pytest.raises(StoreContractError):
            store.retry_job(job.job_id)
        store.fail_job(job.job_id, "A", error_code="PARSE_FAILED", error_summary="boom")
        retried = store.retry_job(job.job_id)
        assert retried.state == "queued" and retried.phase == "prepared"
        # 已经是 queued：不能重复回退
        with pytest.raises(StoreContractError):
            store.retry_job(job.job_id)
        # unknown 远端结果不得被假重试
        store.claim_job("B")
        store.report_phase(job.job_id, "B", phase="submission_unknown")
        store.fail_job(job.job_id, "B", error_code="SUBMISSION_UNKNOWN",
                       error_summary="expired remote submission requires review")
        with pytest.raises(StoreConflict):
            store.retry_job(job.job_id)
        assert store.job_status(job.job_id).state == "failed"
        assert store.job_status(job.job_id).phase == "submission_unknown"
        # 显式取消也**不**清除 unknown：仍需人工确认，不能被 retry 假重置
        store.cancel_job(job.job_id)
        with pytest.raises(StoreConflict):
            store.retry_job(job.job_id)

        # 普通 cancelled（无 unknown）可以显式重试
        other, _ = store.enqueue_job(source="b.wav", source_sha256="t" * 64,
                                     parser_fingerprint="pcm-v1")
        store.claim_job("C")
        store.fail_job(other.job_id, "C", error_code="PARSE_FAILED")
        store.cancel_job(other.job_id)
        assert store.retry_job(other.job_id).state == "queued"
        assert claimed.job_id == job.job_id
    finally:
        store.close()
