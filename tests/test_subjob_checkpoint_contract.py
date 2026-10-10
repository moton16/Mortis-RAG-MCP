"""DocumentStore 段级 subjob / retry / checkpoint 合同测试。

E17：原 `test_audio_checkpoint_restart.py` 中依赖 `ingest.audio` 的音频崩溃
续跑用例随 ffmpeg 解码 / Whisper 转录链路物理清除移除；本文件保留与音频无关
（不导入 `ingest.audio`）的 store 合同用例，覆盖 ordinal 契约、写前超限拒绝、
retry 语义与 cancel/GC/retry 的引用释放。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import (
    DocumentStore,
    StoreConflict,
    StoreContractError,
    resolve_storage_layout,
)


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


def test_cancel_gc_retry_does_not_reuse_released_checkpoint(tmp_path):
    store = _store(tmp_path, "cancel-gc")
    try:
        job, _ = store.enqueue_job(source="a.wav", source_sha256="s" * 64, parser_fingerprint="pcm-v1")
        store.claim_job("A")
        blob = store.put_media_blob(data=b"segment", mime_type="audio/wav")
        store.record_subjob(job.job_id, "A", ordinal=1, input_hash="s" * 64,
                            range={"kind": "audio_ms", "start": 0, "end": 1000},
                            state="done", checkpoint={"blob_id": blob, "ordinal": 1})
        store.cancel_job(job.job_id)
        assert store.gc_unreferenced() == 1
        store.retry_job(job.job_id)
        assert store.list_subjobs(job.job_id) == [], "cancelled retry reused released media references"
    finally:
        store.close()
