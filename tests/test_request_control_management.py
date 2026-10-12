"""MCP/CLI 管理入口验收（v0.9.2 §9/§10）：精确恢复、迁移边界与回滚演练。

使用临时 vault 与控制库；不联网、不安排刷新、不启动监听。
"""
from __future__ import annotations

import json

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig, RequestControlConfig
from mortis_rag_mcp._indexer import embedding_resume
from mortis_rag_mcp.doc_store import ControlStore, resolve_storage_layout
from mortis_rag_mcp.server import REQUEST_CONTROL_ACTIONS, request_control_action

ENDPOINT = "https://embedding.test/v1/embeddings"


def _vault(tmp_path, name="vault"):
    target = tmp_path / name
    target.mkdir(exist_ok=True)
    (target / "a.md").write_text("A BODY", encoding="utf-8")
    return target


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", endpoint=ENDPOINT, model="m",
                                  dimension=1, send_dimensions=False, batch_size=1),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, embedding_max_workers=1),
        request_control=RequestControlConfig(mode="bounded_resume"),
    )


@pytest.fixture(autouse=True)
def pinned_server_config(tmp_path, monkeypatch):
    """把 server 的配置解析钉到本测试的临时库，避免读到宿主/隔离配置。"""
    monkeypatch.setattr("mortis_rag_mcp.server.load_config", lambda *a, **k: _config(tmp_path))


def _control(tmp_path, vault) -> ControlStore:
    control = ControlStore(resolve_storage_layout(_config(tmp_path), vault))
    control.open(create=True, write=True)
    return control


def _seed_batch(control, batch_id="batch-1", owner="owner-1", state="reserved", index=1):
    from mortis_rag_mcp.request_lifecycle import component_key
    embedding_key = f"ek-{index}"
    component = {
        "component_key": component_key(purpose_family_name="document",
                                       profile_fingerprint="fp", embedding_key=embedding_key,
                                       exact_input_sha256=f"sha-{index}"),
        "embedding_key": embedding_key, "input_sha256": f"sha-{index}", "source": "a.md",
        "source_kind": "document", "source_sha256": "s", "revision_id": "",
        "chunk_id": f"c{index}",
    }
    control.reserve_embedding_batch(batch_id, operation_id="op-1", purpose_family="document",
                                    profile_fingerprint="fp", endpoint=ENDPOINT,
                                    owner_token=owner, components=[component])
    if state == "sending":
        control.begin_embedding_send(batch_id, owner)
    return component


def test_vault_path_is_mandatory(tmp_path):
    result = request_control_action(vault_path="", action="list")
    assert result["applied"] is False
    assert result["error"] == "VAULT_PATH_REQUIRED"


def test_unsupported_action_is_rejected(tmp_path):
    vault = _vault(tmp_path)
    result = request_control_action(vault_path=vault, action="delete_everything")
    assert result["applied"] is False
    assert result["error"] == "UNSUPPORTED_ACTION"
    assert "delete_everything" not in REQUEST_CONTROL_ACTIONS


def test_list_is_read_only_and_reports_legacy_pending(tmp_path):
    """T31：只读动作返回当前工作与旧迁移边界，不安排刷新、不启动监听、不联网。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        _seed_batch(control)
        control.record_send_intent("legacy-1", writer="", kind="embed", payload_hash="ph",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        result = request_control_action(vault_path=vault, action="list", config_path=None)
        assert result["applied"] is False and result["read_only"] is True
        assert [item["batch_id"] for item in result["work"]] == ["batch-1"]
        # 旧记录没有可信组件映射 → 进入一次性迁移待处置集合。
        assert [item["request_id"] for item in result["legacy_migration_pending"]] == ["legacy-1"]
    finally:
        control.close()


def test_abandon_moves_batch_to_manual_only(tmp_path):
    """T27：abandon 只转 manual_only，保留占用 → watch 不会自动重发。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        component = _seed_batch(control, state="sending")
        preview = request_control_action(vault_path=vault, action="abandon", batch_id="batch-1")
        assert preview["applied"] is False and preview.get("preview") is True
        applied = request_control_action(vault_path=vault, action="abandon",
                                         batch_id="batch-1", dry_run=False)
        assert applied["applied"] is True
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "manual_only"}
    finally:
        control.close()


def test_retry_requires_confirmation_and_refuses_active_sending(tmp_path):
    """T28：无确认、或批次仍在 sending，一律拒绝。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        _seed_batch(control, state="sending")
        no_confirm = request_control_action(vault_path=vault, action="retry",
                                            batch_id="batch-1", dry_run=False)
        assert no_confirm["error"] == "RESUME_RETRY_NEEDS_CONFIRMATION"
        active = request_control_action(vault_path=vault, action="retry", batch_id="batch-1",
                                        confirm_repeat_cost=True, dry_run=False)
        assert active["error"] == "RESUME_BATCH_STILL_SENDING"
    finally:
        control.close()


def test_retry_releases_only_the_named_batch(tmp_path):
    """T29：确认后只释放指定批次，不影响其他记录。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        one = _seed_batch(control, batch_id="batch-1", owner="owner-1", index=1)
        two = _seed_batch(control, batch_id="batch-2", owner="owner-2", index=2)
        result = request_control_action(vault_path=vault, action="retry", batch_id="batch-1",
                                        confirm_repeat_cost=True, dry_run=False)
        assert result["applied"] is True
        states = control.embedding_component_states(
            [one["component_key"], two["component_key"]])
        assert states[one["component_key"]] == "released"
        # 另一批次未被牵连。
        assert states[two["component_key"]] == "reserved"
    finally:
        control.close()


def test_pause_and_resume_route_are_explicit(tmp_path):
    """resume_route 只解除明确暂停，不解除 batch hold。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        component = _seed_batch(control, batch_id="batch-1", owner="owner-1")
        paused = request_control_action(vault_path=vault, action="pause_route",
                                        profile_fingerprint="fp", dry_run=False)
        assert paused["applied"] is True
        assert control.paid_authorization_state("fp") == "revoked"
        resumed = request_control_action(vault_path=vault, action="resume_route",
                                         profile_fingerprint="fp", dry_run=False)
        assert resumed["applied"] is True
        assert control.paid_authorization_state("fp") == "authorized"
        # batch 占用不受路由暂停影响。
        assert control.embedding_component_states([component["component_key"]]) == \
            {component["component_key"]: "reserved"}
    finally:
        control.close()


def test_resolve_legacy_only_touches_listed_records(tmp_path):
    """T31：只处置列出的旧记录；拒绝未列出的目标；不作无条件"清全部"。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        control.record_send_intent("legacy-1", writer="", kind="embed", payload_hash="p1",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        control.record_send_intent("legacy-2", writer="", kind="embed", payload_hash="p2",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        without_confirm = request_control_action(vault_path=vault, action="resolve_legacy",
                                                 request_ids=["legacy-1"], dry_run=False)
        assert without_confirm["error"] == "RESOLVE_LEGACY_NEEDS_CONFIRMATION"
        result = request_control_action(vault_path=vault, action="resolve_legacy",
                                        request_ids=["legacy-1", "not-listed"],
                                        confirm_repeat_cost=True, dry_run=False)
        applied = {item["request_id"]: item["applied"] for item in result["dispositions"]}
        assert applied["legacy-1"] is True
        assert applied["not-listed"] is False
        # 未列出的旧记录不得被自动终结。
        assert control.intent_state("legacy-2") == "prepared"
    finally:
        control.close()


def test_resolve_legacy_disposition_is_idempotent_and_truthful(tmp_path):
    """R3 反例 1：success/abandoned 不可改状态，但必须能被处置且不谎报 applied。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        control.record_send_intent("legacy-done", writer="", kind="embed", payload_hash="p1",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=1)
        control.mark_intent("legacy-done", "success", "settled earlier")
        control.record_send_intent("legacy-gone", writer="", kind="embed", payload_hash="p2",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=1)
        control.mark_intent("legacy-gone", "abandoned", "given up earlier")
        pending = request_control_action(vault_path=vault, action="list")
        assert {item["request_id"] for item in pending["legacy_migration_pending"]} == \
            {"legacy-done", "legacy-gone"}
        result = request_control_action(vault_path=vault, action="resolve_legacy",
                                        request_ids=["legacy-done", "legacy-gone"],
                                        confirm_repeat_cost=True, dry_run=False)
        assert result["applied"] is True
        assert all(item["applied"] for item in result["dispositions"])
        # 不可变状态保持不变（历史事实不得被改写）。
        assert control.intent_state("legacy-done") == "success"
        assert control.intent_state("legacy-gone") == "abandoned"
        # 已退出待处置集合，且重复处置不谎报成功。
        after = request_control_action(vault_path=vault, action="list")
        assert after["legacy_migration_pending"] == []
        again = request_control_action(vault_path=vault, action="resolve_legacy",
                                       request_ids=["legacy-done"],
                                       confirm_repeat_cost=True, dry_run=False)
        assert again["applied"] is False
        assert again["dispositions"][0]["error"] == "NOT_IN_PENDING_SET"
    finally:
        control.close()


def test_legacy_mode_hold_blocks_but_abandon_is_not_a_dead_end(tmp_path):
    """R3 反例 2：held_* 冻结旧模式；manual_only（显式 abandon）不得让整库停摆。"""
    from mortis_rag_mcp._indexer import embedding_resume
    from mortis_rag_mcp._indexer.sync_engine import _paid_embedding_allowed
    from mortis_rag_mcp.indexer import MarkdownIndexer

    vault = _vault(tmp_path)
    (vault / "a.md").write_text("A BODY", encoding="utf-8")
    cfg = _config(tmp_path)
    object.__setattr__(cfg.request_control, "mode", "legacy_profile")
    indexer = MarkdownIndexer(vault, cfg)
    try:
        control = indexer._paid_control_store()
        fingerprint = indexer._embedding_profile.fingerprint
        plan = {"batch_id": "batch-1", "operation_id": "op-1", "purpose_family": "document",
                "profile_fingerprint": fingerprint, "endpoint": ENDPOINT,
                "components": [{"component_key": "ck-1", "embedding_key": "ek-1",
                                "input_sha256": "sha", "source": "a.md",
                                "source_kind": "document", "source_sha256": "s",
                                "revision_id": "", "chunk_id": "c1"}]}
        assert embedding_resume.reserve_embedding_batch(control, plan, "owner-1").ok
        embedding_resume.begin_embedding_send(control, "batch-1", "owner-1")
        embedding_resume.record_embedding_outcome(
            control, "batch-1", "owner-1", "transport_unknown")
        assert control.embedding_component_states(["ck-1"]) == {"ck-1": "held_unknown"}
        assert _paid_embedding_allowed(indexer) is False, "held_* 必须在旧模式也冻结"
        # 运维显式 abandon → 不再是全库死结。
        embedding_resume.resolve_embedding_work(control, "batch-1", action="abandon")
        assert control.embedding_component_states(["ck-1"]) == {"ck-1": "manual_only"}
        assert _paid_embedding_allowed(indexer) is True
    finally:
        indexer.close_document_store()


def test_missing_batch_id_is_reported(tmp_path):
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        result = request_control_action(vault_path=vault, action="abandon")
        assert result["error"] == "BATCH_ID_REQUIRED"
        result = request_control_action(vault_path=vault, action="pause_route")
        assert result["error"] == "PROFILE_FINGERPRINT_REQUIRED"
    finally:
        control.close()


def test_rollback_legacy_mode_still_sees_new_holds(tmp_path):
    """T34：切回 legacy_profile 时新表 hold 仍在，不能被忽略。"""
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    try:
        from mortis_rag_mcp.request_lifecycle import RequestOutcome
        component = _seed_batch(control, batch_id="batch-1", owner="owner-1", state="sending")
        embedding_resume.record_embedding_outcome(
            control, "batch-1", "owner-1",
            RequestOutcome(outcome="transport_unknown", response_received=False,
                           remote_effect_unknown=True))
        legacy = ControlStore(resolve_storage_layout(_config(tmp_path), vault))
        legacy.open(create=False, write=False)
        try:
            work = embedding_resume.list_embedding_work(legacy)
            assert [item["batch_id"] for item in work] == ["batch-1"]
            assert work[0]["state"] == "held_unknown"
            # 旧程序不认识的新表不会破坏旧读取合同。
            assert legacy.list_request_intents() == []
        finally:
            legacy.close()
    finally:
        control.close()
