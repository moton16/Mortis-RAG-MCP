"""续算层验收（v0.9.2 §5/§7）：原子占用、持久 hold、检查点确认与重启恢复。

全部使用临时 SQLite 与 fake transport；不连接任何真实端点。
"""
from __future__ import annotations

import json
import time

import pytest

from mortis_rag_mcp._indexer import embedding_resume
from mortis_rag_mcp.doc_store import ControlStore, ResumeConflict, resolve_storage_layout
from mortis_rag_mcp.request_lifecycle import (
    OUTCOME_SUCCEEDED, OUTCOME_TRANSPORT_UNKNOWN, RequestOutcome, component_key,
)


def _control(tmp_path, name="ctrl") -> ControlStore:
    from mortis_rag_mcp.config import AppConfig, CacheConfig
    cfg = AppConfig(cache=CacheConfig(dir=str(tmp_path / name), enabled=True))
    control = ControlStore(resolve_storage_layout(cfg, tmp_path / "vault"))
    control.open(create=True, write=True)
    return control


def _component(index: int, *, source: str = "a.md") -> dict:
    embedding_key = f"ek-{index}"
    return {
        "component_key": component_key(purpose_family_name="document",
                                       profile_fingerprint="fp-test",
                                       embedding_key=embedding_key,
                                       exact_input_sha256=f"sha-{index}"),
        "embedding_key": embedding_key,
        "input_sha256": f"sha-{index}",
        "source": source,
        "source_kind": "document",
        "source_sha256": "src-sha",
        "revision_id": "rev-1",
        "chunk_id": f"chunk-{index}",
    }


def _plan(batch_id: str, components: list[dict], operation_id: str = "op-1") -> dict:
    return {
        "batch_id": batch_id,
        "operation_id": operation_id,
        "purpose_family": "document",
        "profile_fingerprint": "fp-test",
        "endpoint": "https://embedding.test/v1/embeddings",
        "components": components,
    }


def test_two_connections_cannot_claim_same_component(tmp_path):
    """T07：两个连接抢同一个稳定输入身份，只能有一个占用成功，零重复 POST。"""
    # 同一控制库、两个独立连接（模拟两个进程）。
    control_a = _control(tmp_path, "ctrl")
    control_b = _control(tmp_path, "ctrl")
    component = _component(1)
    first = embedding_resume.reserve_embedding_batch(
        control_a, _plan("batch-1", [component]), "owner-a")
    second = embedding_resume.reserve_embedding_batch(
        control_b, _plan("batch-2", [component]), "owner-b")
    try:
        assert first.ok is True
        assert second.ok is False
        assert second.states.get(component["component_key"]) == "reserved"
        # 冲突后新表必须完整存在（DDL 是显式事务，不能只建一半）。
        work = embedding_resume.list_embedding_work(control_a)
        assert len(work) == 1
    finally:
        control_a.close()
        control_b.close()


def test_independent_components_are_not_blocked_by_each_other(tmp_path):
    """T08：独立输入身份互不影响，不存在 profile 级连带暂停。"""
    control = _control(tmp_path)
    try:
        one = embedding_resume.reserve_embedding_batch(
            control, _plan("batch-1", [_component(1)]), "owner-a")
        two = embedding_resume.reserve_embedding_batch(
            control, _plan("batch-2", [_component(2)]), "owner-b")
        assert one.ok and two.ok
        # 一个批次进入 sending，另一个仍然可以独立推进。
        assert embedding_resume.begin_embedding_send(control, "batch-1", "owner-a").allowed
        assert embedding_resume.begin_embedding_send(control, "batch-2", "owner-b").allowed
    finally:
        control.close()


def test_success_prefix_then_unknown_holds_only_that_batch(tmp_path):
    """T09：A 成功可复用、B 持久 hold、C 仍能正常完成。"""
    control = _control(tmp_path)
    try:
        a = _component(1)
        b = _component(2)
        c = _component(3)
        owner = "owner-1"
        for name, component in (("batch-a", a), ("batch-b", b), ("batch-c", c)):
            reservation = embedding_resume.reserve_embedding_batch(
                control, _plan(name, [component]), owner)
            assert reservation.ok
            assert embedding_resume.begin_embedding_send(control, name, owner).allowed
        # A 成功且检查点确认 → complete
        embedding_resume.record_embedding_outcome(control, "batch-a", owner, OUTCOME_SUCCEEDED)
        result = embedding_resume.confirm_embedding_checkpoint(
            control, "batch-a", owner, [a["embedding_key"]])
        assert result["state"] == "complete"
        # B 未知 → held_unknown，占用保留
        embedding_resume.record_embedding_outcome(
            control, "batch-b", owner,
            RequestOutcome(outcome=OUTCOME_TRANSPORT_UNKNOWN, response_received=False,
                           remote_effect_unknown=True, http_status=None, detail="reset"))
        # C 正常完成
        embedding_resume.record_embedding_outcome(control, "batch-c", owner, OUTCOME_SUCCEEDED)
        embedding_resume.confirm_embedding_checkpoint(control, "batch-c", owner, [c["embedding_key"]])

        states = control.embedding_component_states(
            [a["component_key"], b["component_key"], c["component_key"]])
        assert states[a["component_key"]] == "complete"
        assert states[b["component_key"]] == "held_unknown"
        assert states[c["component_key"]] == "complete"
        # 重新计划时只有 B 被排除：A/C 不会重新付费。
        assert embedding_resume.active_component_states(control, [a["component_key"]]) == \
            {a["component_key"]: "complete"}
    finally:
        control.close()


def test_regrouping_does_not_release_the_held_component(tmp_path):
    """T10：改批大小/顺序后，原输入身份仍然 held，不得换个 payload 绕过去。"""
    control = _control(tmp_path)
    try:
        component = _component(1)
        owner = "owner-1"
        assert embedding_resume.reserve_embedding_batch(
            control, _plan("batch-1", [component]), owner).ok
        assert embedding_resume.begin_embedding_send(control, "batch-1", owner).allowed
        embedding_resume.record_embedding_outcome(
            control, "batch-1", owner,
            RequestOutcome(outcome="http_error", response_received=True,
                           remote_effect_unknown=True, http_status=500))
        # 换 operation_id、换批次组合、换 payload_hash 都不能让同一身份重新占用。
        conflict = embedding_resume.reserve_embedding_batch(
            control, _plan("batch-2", [component, _component(9)], operation_id="op-2"), owner)
        assert conflict.ok is False
        assert conflict.states[component["component_key"]] == "held_unknown"
        # 新增输入不受影响。
        fresh = embedding_resume.reserve_embedding_batch(
            control, _plan("batch-3", [_component(9)]), owner)
        assert fresh.ok is True
    finally:
        control.close()


def test_complete_row_is_not_a_permanent_claim_until_audit(tmp_path):
    """T37/T41：complete 行不是永久占用；成果丢失时转 held_result_missing。"""
    control = _control(tmp_path)
    try:
        component = _component(1)
        owner = "owner-1"
        embedding_resume.reserve_embedding_batch(control, _plan("batch-1", [component]), owner)
        embedding_resume.begin_embedding_send(control, "batch-1", owner)
        embedding_resume.record_embedding_outcome(control, "batch-1", owner, OUTCOME_SUCCEEDED)
        embedding_resume.confirm_embedding_checkpoint(control, "batch-1", owner, [component["embedding_key"]])
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "complete"}
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="owner-other",
            verify_recoverable=lambda keys: set(), audit_complete=True)
        assert summary["held_complete"] == 1
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "held_result_missing"}
    finally:
        control.close()


def test_crash_after_reserve_releases_without_sending(tmp_path):
    """T11：reserved 状态证明此前没有发送，恢复后释放，零自动重发。"""
    control = _control(tmp_path)
    try:
        component = _component(1)
        assert embedding_resume.reserve_embedding_batch(
            control, _plan("batch-1", [component]), "owner-dead").ok
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="owner-live")
        assert summary["released_reserved"] == 1
        # 释放后不再占用（released 不在 active 集合内），可以重新计划。
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "released"}
        assert embedding_resume.reserve_embedding_batch(
            control, _plan("batch-2", [component]), "owner-live").ok
    finally:
        control.close()


def test_crash_after_begin_send_holds_unknown(tmp_path):
    """T12：begin_send 之后崩溃 → held_unknown，重开零 POST（不能用超时推断）。"""
    control = _control(tmp_path)
    try:
        component = _component(1)
        embedding_resume.reserve_embedding_batch(control, _plan("batch-1", [component]), "owner-dead")
        assert embedding_resume.begin_embedding_send(control, "batch-1", "owner-dead").allowed
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="owner-live",
            verify_recoverable=lambda keys: set())
        assert summary["held_sending"] == 1
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "held_unknown"}
    finally:
        control.close()


def test_result_pending_recovers_from_confirmed_vectors(tmp_path):
    """T13/T14：有可恢复成果 → complete；没有 → held_result_missing。"""
    control = _control(tmp_path)
    try:
        saved = _component(1)
        lost = _component(2)
        owner = "owner-1"
        for name, component in (("batch-saved", saved), ("batch-lost", lost)):
            embedding_resume.reserve_embedding_batch(control, _plan(name, [component]), owner)
            embedding_resume.begin_embedding_send(control, name, owner)
            embedding_resume.record_embedding_outcome(control, name, owner, OUTCOME_SUCCEEDED)
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="owner-new",
            verify_recoverable=lambda keys: {saved["embedding_key"]})
        assert control.embedding_component_states([saved["component_key"]]) == \
            {saved["component_key"]: "complete"}
        assert control.embedding_component_states([lost["component_key"]]) == \
            {lost["component_key"]: "held_result_missing"}
        assert summary["completed_pending"] == 1
        assert summary["missing_result"] == 1
    finally:
        control.close()


def test_partial_checkpoint_confirmation(tmp_path):
    """T15：只确认真正持久化的键；未确认的转 held_result_missing。"""
    control = _control(tmp_path)
    try:
        components = [_component(1), _component(2)]
        owner = "owner-1"
        embedding_resume.reserve_embedding_batch(control, _plan("batch-1", components), owner)
        embedding_resume.begin_embedding_send(control, "batch-1", owner)
        embedding_resume.record_embedding_outcome(control, "batch-1", owner, OUTCOME_SUCCEEDED)
        result = embedding_resume.confirm_embedding_checkpoint(
            control, "batch-1", owner, [components[0]["embedding_key"]])
        assert result["confirmed"] == 1 and result["total"] == 2
        assert result["state"] == "held_result_missing"
        states = control.embedding_component_states([c["component_key"] for c in components])
        assert states[components[0]["component_key"]] == "complete"
        assert states[components[1]["component_key"]] == "held_result_missing"
    finally:
        control.close()


def test_recovery_owner_predicate_never_touches_unproven_owners(tmp_path):
    """R2：没有存活证明的他人 token 不得被恢复/释放（跨进程纪律）。"""
    control = _control(tmp_path)
    try:
        mine = _component(1)
        other = _component(2)
        embedding_resume.reserve_embedding_batch(control, _plan("batch-mine", [mine]), "owner-mine")
        embedding_resume.reserve_embedding_batch(control, _plan("batch-other", [other]), "owner-other")
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="",
            owner_predicate=lambda token: token == "owner-mine")
        assert summary["released_reserved"] == 1
        assert summary["skipped_unproven"] == 1
        states = control.embedding_component_states(
            [mine["component_key"], other["component_key"]])
        assert states[mine["component_key"]] == "released"
        # 他人批次原封不动：仍在 active 占用里，不会被静默解除。
        assert states[other["component_key"]] == "reserved"
    finally:
        control.close()


def test_recovery_marks_interrupted_sending_as_held(tmp_path):
    """R2：本进程中断在 sending 的批次转持久 hold，绝不自动重发。"""
    control = _control(tmp_path)
    try:
        component = _component(1)
        owner = "pid1:dispatch:abc"
        embedding_resume.reserve_embedding_batch(control, _plan("batch-1", [component]), owner)
        embedding_resume.begin_embedding_send(control, "batch-1", owner)
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="",
            owner_predicate=lambda token: token == owner)
        assert summary["held_sending"] == 1
        states = control.embedding_component_states([component["component_key"]])
        assert states[component["component_key"]] == "held_unknown"
    finally:
        control.close()


def test_abandon_keeps_claim_and_retry_requires_confirmation(tmp_path):
    """T27/T28：abandon 只退出自动队列；retry 必须显式确认且不解除其他记录。"""
    control = _control(tmp_path)
    try:
        component = _component(1)
        owner = "owner-1"
        embedding_resume.reserve_embedding_batch(control, _plan("batch-1", [component]), owner)
        embedding_resume.begin_embedding_send(control, "batch-1", owner)
        embedding_resume.record_embedding_outcome(
            control, "batch-1", owner,
            RequestOutcome(outcome=OUTCOME_TRANSPORT_UNKNOWN, response_received=False,
                           remote_effect_unknown=True))

        refused = embedding_resume.resolve_embedding_work(control, "batch-1", action="retry")
        assert refused["applied"] is False
        assert refused["error"] == "RESUME_RETRY_NEEDS_CONFIRMATION"

        abandoned = embedding_resume.resolve_embedding_work(control, "batch-1", action="abandon")
        assert abandoned["applied"] is True
        # manual_only 仍占用：watch 不会自动重发。
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "manual_only"}

        retried = embedding_resume.resolve_embedding_work(
            control, "batch-1", action="retry", confirm_repeat_cost=True, note="operator approved")
        assert retried["applied"] is True
        # released 不再是 active 占用：显式确认后允许新的一次尝试。
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "released"}
    finally:
        control.close()


def test_retry_rejects_active_sending_batch(tmp_path):
    """T28：仍活跃的 sending 批次不得被 retry 释放。"""
    control = _control(tmp_path)
    try:
        embedding_resume.reserve_embedding_batch(control, _plan("batch-1", [_component(1)]), "owner-1")
        embedding_resume.begin_embedding_send(control, "batch-1", "owner-1")
        result = embedding_resume.resolve_embedding_work(
            control, "batch-1", action="retry", confirm_repeat_cost=True)
        assert result["applied"] is False
        assert result["error"] == "RESUME_BATCH_STILL_SENDING"
    finally:
        control.close()


def test_schema_version_is_unchanged_by_resume_tables(tmp_path):
    """续算表只加不删：不得改写 SCHEMA_VERSION，也不破坏旧读取合同。"""
    from mortis_rag_mcp.doc_store import SCHEMA_VERSION
    control = _control(tmp_path)
    try:
        embedding_resume.reserve_embedding_batch(control, _plan("batch-1", [_component(1)]), "owner-1")
        assert control.state() is not None
        tables = {row[0] for row in control._require_conn().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        assert {"embedding_batches", "embedding_components"} <= tables
        assert SCHEMA_VERSION == 1
    finally:
        control.close()
