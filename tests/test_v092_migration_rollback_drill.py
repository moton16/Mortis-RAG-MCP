"""v0.9.2 迁移与回滚演练（离线、可重复执行，作为交付证据）。

演练四件事，全部用 fake transport 与临时控制库，零真实网络：

A. 旧 `success` 记账 + 成果缺失 → `complete` 降级为持久 hold，不自动重算。
B. 旧 `revoked` 授权 → 新旧模式都零 POST；显式 resume_route 才解除。
C. 旧 `prepared` / `submission_unknown` 孤立记录 → 不冻结新工作；可显式处置。
D. 回滚到 `legacy_profile` → 新表 hold 不被裸撤；hold 清除后自动续算恢复。
"""
from __future__ import annotations

import json

import pytest

from mortis_rag_mcp._indexer import embedding_resume
from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig, RequestControlConfig
from mortis_rag_mcp.doc_store import ControlStore, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.request_lifecycle import OUTCOME_SUCCEEDED
from mortis_rag_mcp.server import request_control_action

ENDPOINT = "https://embedding.test/v1/embeddings"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _transport(calls):
    def fake(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append(list(payload["input"]))
        return _Resp({"data": [{"embedding": [1.0]} for _ in payload["input"]]})

    return fake


def _config(tmp_path, *, mode="bounded_resume") -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", endpoint=ENDPOINT, model="m",
                                  dimension=1, send_dimensions=False, batch_size=1),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, embedding_max_workers=1),
        request_control=RequestControlConfig(mode=mode),
    )


def _vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("A BODY", encoding="utf-8")
    return vault


@pytest.fixture(autouse=True)
def pinned_server_config(tmp_path, monkeypatch):
    """把管理入口的配置解析钉到本测试的临时库（否则读到宿主隔离配置）。"""
    monkeypatch.setattr("mortis_rag_mcp.server.load_config",
                        lambda *a, **k: _config(tmp_path))


def _seed_old_success_without_artifact(tmp_path, vault, indexer, batch_id="batch-old"):
    """旧程序：POST 成功进 journal=success，但成果没落盘（crash 在 checkpoint 前）。"""
    control = indexer._paid_control_store()
    plan = {"batch_id": batch_id, "operation_id": "op-old", "purpose_family": "document",
            "profile_fingerprint": indexer._embedding_profile.fingerprint,
            "endpoint": ENDPOINT,
            "components": [{"component_key": "ck-old", "embedding_key": "ek-old",
                            "input_sha256": "sha-old", "source": "a.md",
                            "source_kind": "document", "source_sha256": "s",
                            "revision_id": "", "chunk_id": "c-old"}]}
    assert embedding_resume.reserve_embedding_batch(control, plan, "owner-old").ok
    embedding_resume.begin_embedding_send(control, batch_id, "owner-old")
    embedding_resume.record_embedding_outcome(control, batch_id, "owner-old", OUTCOME_SUCCEEDED)
    # 旧路径把 journal 记成 success，但组件停在 result_pending（没进 complete）。
    control.record_send_intent("legacy-success", writer="", kind="embed", payload_hash="ph",
                               endpoint=ENDPOINT,
                               profile_fingerprint=indexer._embedding_profile.fingerprint,
                               attempt=1)
    control.mark_intent("legacy-success", "success", "settled by old version")
    return control


def test_drill_a_old_success_without_artifact_is_held_not_recomputed(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _transport(calls))
    indexer = MarkdownIndexer(vault, _config(tmp_path))
    try:
        control = _seed_old_success_without_artifact(tmp_path, vault, indexer)
        # 审计式恢复：complete/result_pending 但没有可恢复成果 → 持久 hold。
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="",
            owner_predicate=lambda token: token == "owner-old")
        assert summary["missing_result"] + summary["held_complete"] >= 1
        states = control.embedding_component_states(["ck-old"])
        assert states["ck-old"] in {"held_result_missing", "held_unknown"}
        # 该旧输入身份永久 hold，且在观测面可见。
        assert embedding_resume.held_summary(control)["held_batches"] >= 1
        # 当前磁盘内容仍有自己的身份：它会正常补嵌一次（旧记录不得冒充它的成果），
        # 但第二次同步必须复用检查点，零新增 POST。
        indexer.sync()
        assert len(calls) == 1
        indexer.sync()
        assert len(calls) == 1, "旧 success 记录不得让同一输入被反复重算"
        # 旧身份从未被自动重放。
        assert control.embedding_component_states(["ck-old"]) == states
    finally:
        indexer.close_document_store()


def test_drill_b_old_revocation_blocks_both_modes(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _transport(calls))
    for mode in ("bounded_resume", "legacy_profile"):
        cfg = _config(tmp_path, mode=mode)
        indexer = MarkdownIndexer(vault, cfg)
        try:
            control = indexer._paid_control_store()
            control.revoke_paid_authorization(indexer._embedding_profile.fingerprint)
            indexer.sync()
            assert calls == [], f"{mode} 下旧 revoked 必须零 POST"
            assert indexer._embedding_paused is True
            # 显式恢复才解除（且只解除明确暂停，不动 batch hold）。
            resumed = request_control_action(
                vault_path=vault, action="resume_route",
                profile_fingerprint=indexer._embedding_profile.fingerprint, dry_run=False)
            assert resumed["applied"] is True
        finally:
            indexer.close_document_store()


def test_drill_c_isolated_old_records_do_not_freeze_new_work(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _transport(calls))
    indexer = MarkdownIndexer(vault, _config(tmp_path))
    try:
        control = indexer._paid_control_store()
        fingerprint = indexer._embedding_profile.fingerprint
        control.record_send_intent("legacy-prepared", writer="", kind="embed", payload_hash="p1",
                                   endpoint=ENDPOINT, profile_fingerprint=fingerprint,
                                   attempt=None)
        control.record_send_intent("legacy-unknown", writer="", kind="embed", payload_hash="p2",
                                   endpoint=ENDPOINT, profile_fingerprint=fingerprint,
                                   attempt=None)
        control.mark_intent("legacy-unknown", "submission_unknown", "old crash")
        # 新工作照常发送：旧孤立审计不再冻结整条路由。
        indexer.sync()
        assert len(calls) == 1
        assert indexer._embedding_paused is False
        listed = request_control_action(vault_path=vault, action="list",
                                        config_path=None)
        pending = {item["request_id"] for item in listed["legacy_migration_pending"]}
        assert pending == {"legacy-prepared", "legacy-unknown"}
    finally:
        indexer.close_document_store()


def test_drill_d_rollback_never_replays_hold_and_recovers_after_clear(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _transport(calls))
    bounded = MarkdownIndexer(vault, _config(tmp_path))
    try:
        control = bounded._paid_control_store()
        plan = {"batch_id": "batch-hold", "operation_id": "op-hold",
                "purpose_family": "document",
                "profile_fingerprint": bounded._embedding_profile.fingerprint,
                "endpoint": ENDPOINT,
                "components": [{"component_key": "ck-hold", "embedding_key": "ek-hold",
                                "input_sha256": "sha-hold", "source": "a.md",
                                "source_kind": "document", "source_sha256": "s",
                                "revision_id": "", "chunk_id": "c-hold"}]}
        assert embedding_resume.reserve_embedding_batch(control, plan, "owner-h").ok
        embedding_resume.begin_embedding_send(control, "batch-hold", "owner-h")
        embedding_resume.record_embedding_outcome(
            control, "batch-hold", "owner-h", "transport_unknown")
        assert control.embedding_component_states(["ck-hold"]) == {"ck-hold": "held_unknown"}
    finally:
        bounded.close_document_store()

    # 回滚到旧模式：hold 仍在 → 零自动 POST（不裸撤锁）。
    legacy = MarkdownIndexer(vault, _config(tmp_path, mode="legacy_profile"))
    try:
        legacy.sync()
        assert calls == [], "回滚后不得自动重放旧 unknown"
        assert legacy._embedding_paused is True
    finally:
        legacy.close_document_store()

    # 操作员显式放弃该批次 → 旧模式解除暂停（abandon 不是死路）。
    admin = ControlStore(resolve_storage_layout(_config(tmp_path), vault))
    admin.open(create=False, write=True)
    try:
        assert embedding_resume.resolve_embedding_work(
            admin, "batch-hold", action="abandon")["applied"] is True
    finally:
        admin.close()
    legacy2 = MarkdownIndexer(vault, _config(tmp_path, mode="legacy_profile"))
    try:
        legacy2.sync()
        assert len(calls) == 1, "放弃后旧模式恢复发送"
    finally:
        legacy2.close_document_store()

    # 再切回目标模式：新内容照常续算，且零重复计费。
    modern = MarkdownIndexer(vault, _config(tmp_path))
    try:
        modern.sync()
        assert len(calls) == 1, "已成功落盘的检查点必须复用，不得重发"
    finally:
        modern.close_document_store()
