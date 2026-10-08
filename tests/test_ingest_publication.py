"""T02（队列部分）+ T08：摄取队列、租约 fencing 与发布 CAS（C94）。

真实 SQLite、不 mock 存储；宿主隔离（显式 registered_vaults=[]，不读真实注册表）。
覆盖计划里的精确反例（§20.7E）：
- 跨（进程等价的）两 writer 抢同一 job：只有一个 owner；
- A 领取后暂停到租约过期、B 重新领取 → A 的 stage/commit/terminal 全部 0 行；
- 旧 job 晚到不得覆盖新 request_seq；
- job.done 与 revision 切换**同事务**（失败不得留下半套状态）；
- `done` ≠ embedding 已完成（派生层状态独立）；
- 旧 JSON 账本只迁入可靠终态与去重事实。
"""
from __future__ import annotations

import time

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import (
    DocumentStore,
    OwnershipLost,
    QueueFull,
    StoreConflict,
    resolve_storage_layout,
)


def make_config(tmp_path, *, queue_limit: int = 1000) -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    cfg.ingest.queue_limit = queue_limit
    return cfg


def open_store(tmp_path, vault, cfg) -> DocumentStore:
    layout = resolve_storage_layout(cfg, vault, registered_vaults=[])
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    return store


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _stage(store, *, source: str, sha: str = "sha1", job_id: str = "", owner: str = ""):
    return store.stage_revision(
        source=source,
        source_sha256=sha,
        render_sha256="render-" + sha,
        parser_fingerprint="fp1",
        markdown="# hi\n",
        job_id=job_id,
        owner_token=owner,
    )


# ------------------------------------------------------------------ 队列与合并


def test_enqueue_merges_same_source_sha_parser(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job_a, created_a = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    job_b, created_b = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    assert created_a is True and created_b is False
    assert job_a.job_id == job_b.job_id
    assert store.queue_depth() == 1
    store.close()


def test_enqueue_force_creates_newer_request_seq_and_supersedes_old(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    old, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    new, created = store.enqueue_job(
        source="a.pdf", source_sha256="s1", parser_fingerprint="fp", force=True
    )
    assert created is True
    assert new.request_seq > old.request_seq
    assert store.job_status(old.job_id).state == "superseded"
    # 旧任务不能覆盖新任务：stage 直接 OWNERSHIP_LOST
    with pytest.raises(OwnershipLost):
        _stage(store, source="a.pdf", sha="s1", job_id=old.job_id, owner="tok-old")
    store.close()


def test_queue_limit_rejects_instead_of_dropping(vault, tmp_path):
    cfg = make_config(tmp_path, queue_limit=1)
    store = open_store(tmp_path, vault, cfg)
    store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    with pytest.raises(QueueFull):
        store.enqueue_job(source="b.pdf", source_sha256="s2", parser_fingerprint="fp")
    assert store.queue_depth() == 1
    store.close()


# ------------------------------------------------------------------ 领取与租约 fencing


def test_two_writers_only_one_claims_the_job(vault, tmp_path):
    cfg = make_config(tmp_path)
    store_a = open_store(tmp_path, vault, cfg)
    store_a.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    claimed = store_a.claim_job("owner-A")
    assert claimed is not None and claimed.attempts == 1

    # 「第二个进程」：同一库布局的另一个连接/实例
    store_b = DocumentStore(store_a.layout, cfg)
    store_b.open(write=True)
    assert store_b.claim_job("owner-B") is None      # 租约仍有效，不许抢
    store_b.close()
    store_a.close()


def test_expired_lease_is_reclaimable_but_old_owner_writes_zero_rows(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    first = store.claim_job("owner-A", lease_seconds=1.0, now=0.0)
    assert first is not None

    second = store.claim_job("owner-B", lease_seconds=60.0, now=time.time() + 5.0)
    assert second is not None and second.owner_token == "owner-B"
    assert second.attempts == 2

    # 旧 owner 的一切写入都必须 0 行（§20.7E：A 恢复后 stage/commit/terminal 均不生效）
    with pytest.raises(OwnershipLost):
        _stage(store, source="a.pdf", sha="s1", job_id=job.job_id, owner="owner-A")
    with pytest.raises(OwnershipLost):
        store.report_phase(job.job_id, "owner-A", phase="polling")
    with pytest.raises(OwnershipLost):
        store.fail_job(job.job_id, "owner-A", error_code="X")
    after = store.job_status(job.job_id)
    assert after.state == "parsing" and after.owner_token == "owner-B"
    store.close()


def test_renew_lease_after_expiry_is_ownership_lost(vault, tmp_path):
    """租约到期后**不可复活**（§12.5）：续租 CAS 影响 0 行 → OWNERSHIP_LOST。

    `claim_job` 对租约时长有 1 秒下限（拒绝 0/负数把租约关掉），所以这里用
    `now=0.0` 造出「已过期的租约」，而不是靠 sleep 猜时钟。
    """
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    store.claim_job("owner-A", lease_seconds=1.0, now=0.0)
    with pytest.raises(OwnershipLost):
        store.renew_lease(job.job_id, "owner-A", lease_seconds=60.0)
    store.close()


def test_report_phase_persists_remote_task_id_in_subjob(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    store.claim_job("owner-A")
    store.report_phase(job.job_id, "owner-A", phase="submitted", remote_task_id="batch-9")
    row = store._open_conn(store._ensure_read_generation()).execute(
        "SELECT state, remote_task_id FROM ingest_subjobs WHERE job_id = ? AND ordinal = 0",
        (job.job_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "submitted" and row[1] == "batch-9"
    store.close()


# ------------------------------------------------------------------ 发布 CAS 与同事务


def test_done_and_revision_switch_happen_in_one_transaction(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    store.claim_job("owner-A")
    staged = _stage(store, source="a.pdf", sha="s1", job_id=job.job_id, owner="owner-A")

    seq = store.commit_job_revision(job.job_id, "owner-A", staged.revision_id, source_sha256="s1")
    assert seq == 1
    finished = store.job_status(job.job_id)
    assert finished.state == "done" and finished.result_revision == staged.revision_id
    assert store.get_active("a.pdf") is not None
    store.close()


def test_failed_commit_leaves_no_half_state(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    store.claim_job("owner-A")
    staged = _stage(store, source="a.pdf", sha="s1", job_id=job.job_id, owner="owner-A")

    with pytest.raises(StoreConflict):
        store.commit_job_revision(
            job.job_id, "owner-A", staged.revision_id, source_sha256="s1", expected_change_seq=99
        )
    # 整批回滚：任务还是 parsing，revision 还是 staged，active 未发布
    after = store.job_status(job.job_id)
    assert after.state == "parsing" and after.result_revision == ""
    assert store.get_active("a.pdf") is None
    store.close()


def test_commit_rejects_when_source_sha_changed(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    store.claim_job("owner-A")
    staged = _stage(store, source="a.pdf", sha="s1", job_id=job.job_id, owner="owner-A")
    with pytest.raises(StoreConflict):
        store.commit_job_revision(job.job_id, "owner-A", staged.revision_id, source_sha256="s2")
    assert store.job_status(job.job_id).state == "parsing"
    store.close()


def test_job_done_does_not_imply_embedding_ready(vault, tmp_path):
    """`done` 只代表解析事实已发布；派生（chunk/embedding）状态独立可查（§12.5）。"""
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    store.claim_job("owner-A")
    staged = _stage(store, source="a.pdf", sha="s1", job_id=job.job_id, owner="owner-A")
    store.commit_job_revision(job.job_id, "owner-A", staged.revision_id, source_sha256="s1")

    assert store.job_status(job.job_id).state == "done"
    assert store.get_derived_generation("profile-x") is None      # 尚未构建
    store.mark_derived_generation("profile-x", change_seq=store.change_seq(),
                                  chunker_fingerprint="c1", space_fingerprint="s1",
                                  status="pending")
    derived = store.get_derived_generation("profile-x")
    assert derived.status == "pending" and derived.change_seq == 1
    store.close()


# ------------------------------------------------------------------ 取消 / 重试


def test_cancel_is_cooperative_and_terminal_state_not_cancellable(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    assert store.cancel_job(job.job_id) is True
    assert store.job_status(job.job_id).state == "cancelled"
    assert store.claim_job("owner-A") is None
    assert store.cancel_job(job.job_id) is False
    store.close()


def test_retryable_failure_stays_failed_until_explicit_retry(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    job, _ = store.enqueue_job(source="a.pdf", source_sha256="s1", parser_fingerprint="fp")
    store.claim_job("owner-A")
    failed = store.fail_job(job.job_id, "owner-A", error_code="SUBMISSION_UNKNOWN",
                            error_summary="outcome unknown", retryable=True, retry_after=30.0)
    assert failed.state == "failed" and failed.retry_after == 30.0
    assert store.claim_job("owner-B") is None       # 绝不自动重传
    retried = store.retry_job(job.job_id)
    assert retried.state == "queued" and retried.owner_token == ""
    assert store.claim_job("owner-B") is not None
    store.close()


# ------------------------------------------------------------------ 恢复与账本迁移


def test_recover_keeps_staged_candidates_of_live_lease(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    live, _ = store.enqueue_job(source="live.pdf", source_sha256="s1", parser_fingerprint="fp")
    dead, _ = store.enqueue_job(source="dead.pdf", source_sha256="s2", parser_fingerprint="fp")
    store.claim_job("owner-A", lease_seconds=600.0)
    _stage(store, source="live.pdf", sha="s1", job_id=live.job_id, owner="owner-A")
    _stage(store, source="dead.pdf", sha="s2")
    assert store.recover() == 1                     # 只清掉没有活跃租约的候选
    assert store._open_conn(store._ensure_read_generation()).execute(
        "SELECT COUNT(*) FROM document_revisions WHERE state = 'staged'"
    ).fetchone()[0] == 1
    assert dead.job_id  # 保留引用避免未使用告警
    store.close()


def test_legacy_ledger_migration_only_restores_terminal_facts(vault, tmp_path):
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    payload = {
        "version": 1,
        "jobs": {
            "j1": {"job_id": "j1", "source": "done.pdf", "sha256": "s1", "state": "done",
                   "submitted_at": 1.0, "finished_at": 2.0},
            "j2": {"job_id": "j2", "source": "bad.pdf", "sha256": "s2", "state": "failed",
                   "error": "boom", "submitted_at": 3.0},
            "j3": {"job_id": "j3", "source": "mid.pdf", "sha256": "s3", "state": "parsing",
                   "submitted_at": 4.0},
        },
        "auto_seen": {"done.pdf": {"sha256": "s1", "last_job_id": "j1", "state": "done"}},
    }
    result = store.migrate_legacy_ledger(payload)
    assert result["restored_jobs"] == 2
    assert result["skipped_active_states"] == 1
    assert store.job_status("j3") is None            # 中间态不复活、不自动重传
    assert store.job_status("j1").state == "done"
    assert store.job_status("j2").state == "failed"
    # 幂等：重复迁移不重复插入
    again = store.migrate_legacy_ledger(payload)
    assert again["restored_jobs"] == 0 and again["restored_auto_seen"] == 0
    seen = list(store.iter_auto_seen())
    assert any(item["source"] == "done.pdf" and item["source_sha256"] == "s1" for item in seen)
    store.close()
