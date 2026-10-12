"""请求控制策略验收（v0.9.2 §6/§7/§8）：后台续算、预算、暂停与手动分离。

全部走真实生产 config → MarkdownIndexer → fake transport 路径，不测 helper 本身。
"""
from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from mortis_rag_mcp.config import (
    AppConfig, CacheConfig, EmbeddingConfig, RequestControlConfig, load_config,
)
from mortis_rag_mcp._indexer import embedding_resume
from mortis_rag_mcp.indexer import MarkdownIndexer

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


def _bounded_config(tmp_path, *, limit: int = 32, batch_size: int = 1, **extra) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", endpoint=ENDPOINT, model="m",
                                  dimension=1, send_dimensions=False, batch_size=batch_size,
                                  **extra),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, embedding_max_workers=1),
        request_control=RequestControlConfig(mode="bounded_resume",
                                             background_max_posts_per_round=limit),
    )


def _ok_transport(calls, *, fail_on=()):
    def fake(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        inputs = payload["input"]
        calls.append(list(inputs))
        if any(any(token in text for token in fail_on) for text in inputs):
            raise URLError("connection reset by peer")
        return _Resp({"data": [{"embedding": [1.0]} for _ in inputs]})

    return fake


def test_success_prefix_persists_and_unknown_stays_held(tmp_path, monkeypatch):
    """T09/T10：A/C 成功落检查点可复用；B 未知持久 hold，下一轮零重发。"""
    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    (tmp_path / "b.md").write_text("B BODY", encoding="utf-8")
    (tmp_path / "c.md").write_text("C BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls, fail_on=("B BODY",)))

    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        indexer.sync()
        assert len(calls) == 3, "首轮每批一次 POST，失败批不得自动重发"
        assert indexer.failed_files.get("b.md", "").startswith("AUTOMATIC_REPLAY_HELD")
        assert "a.md" not in indexer.failed_files
        assert "c.md" not in indexer.failed_files
        # 第二轮：a/c 复用已有向量，b 仍 held → 零新 POST。
        indexer.sync()
        assert len(calls) == 3
        assert indexer.failed_files.get("b.md", "").startswith("AUTOMATIC_REPLAY_HELD")
    finally:
        indexer.close_document_store()
        control = indexer._paid_control_store()
        if control is not None:
            control.close()


def test_background_post_budget_defers_without_failure_streak(tmp_path, monkeypatch):
    """T24：一轮预算用尽后明确延期，不计入失败 streak，也不形成即时自循环。"""
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text(f"{name[0]} BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    config = _bounded_config(tmp_path, limit=1)
    indexer = MarkdownIndexer(tmp_path, config)
    try:
        indexer.sync()
        assert len(calls) == 1
        # 延期不是"文件失败"：quarantine streak 不得增长。
        assert indexer._embed_failure_quarantine == {}
    finally:
        indexer.close_document_store()


def test_explicit_revocation_stops_background_posts(tmp_path, monkeypatch):
    """T26：明确 revoked 的路由零 POST；正常启用配置不派生审批。"""
    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        control = indexer._paid_control_store()
        assert control is not None
        control.revoke_paid_authorization(indexer._embedding_profile.fingerprint)
        indexer.sync()
        assert calls == []
        assert indexer._embedding_paused is True
    finally:
        indexer.close_document_store()


def test_old_unresolved_audit_does_not_freeze_the_profile(tmp_path, monkeypatch):
    """T30：旧 profile 的孤立未决审计不再冻结整条路由（新模式取消 profile 锁）。"""
    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        control = indexer._paid_control_store()
        control.record_send_intent(
            "legacy-request-1", writer="", kind="embed", payload_hash="deadbeef",
            endpoint=ENDPOINT, profile_fingerprint=indexer._embedding_profile.fingerprint,
            attempt=None)
        indexer.sync()
        # 旧未决记录不再是暂停判据；新工作按组件占用正常发送。
        assert len(calls) == 1
        assert indexer._embedding_paused is False
    finally:
        indexer.close_document_store()


def test_manual_route_is_not_blocked_by_background_hold(tmp_path, monkeypatch):
    """T21：后台存在 held 组件时，手动 query/rerank 的路由判断仍然独立可用。"""
    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen",
                        _ok_transport(calls, fail_on=("A BODY",)))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        indexer.sync()
        assert indexer.failed_files.get("a.md", "").startswith("AUTOMATIC_REPLAY_HELD")
        # 手动路径的闸门查询不受后台 hold 影响。
        assert indexer.may_use_paid_profile(indexer._embedding_profile.fingerprint) is True
    finally:
        indexer.close_document_store()


def test_cache_disabled_defers_background_without_posts(tmp_path, monkeypatch):
    """T25：续算不可用时后台明确延期，不静默付费。"""
    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    config = _bounded_config(tmp_path)
    config.cache.enabled = False
    indexer = MarkdownIndexer(tmp_path, config)
    try:
        indexer.sync()
        assert calls == []
        assert indexer._embedding_paused is True
    finally:
        indexer.close_document_store()


def test_request_control_config_does_not_move_profile_fingerprint(tmp_path):
    """T38：预算/审计配置不得进入 embedding profile fingerprint（否则触发全库重嵌）。"""
    from mortis_rag_mcp.embedding_capabilities import resolve_embedding_profile
    base = _bounded_config(tmp_path)
    plain = AppConfig(
        embedding=base.embedding,
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
    )
    assert (resolve_embedding_profile(base.embedding).fingerprint
            == resolve_embedding_profile(plain.embedding).fingerprint)


def test_request_control_config_rejects_bad_values():
    """严格拒绝布尔冒充整数、非法枚举与越界范围。"""
    for bad in ({"mode": "nonsense"}, {"background_embedding": "maybe"},
                {"background_max_posts_per_round": True}, {"query_max_posts": 0},
                {"query_max_posts": 9999}, {"audit_retention_days": -1},
                {"paused_profiles": ("",)}):
        with pytest.raises(ValueError):
            RequestControlConfig(**bad)


def test_load_config_reads_request_control(tmp_path):
    path = tmp_path / "app.toml"
    path.write_text(
        """
        [embedding]
        mode = "external"
        [request_control]
        mode = "bounded_resume"
        background_embedding = "manual"
        background_max_posts_per_round = 4
        query_max_posts = 2
        audit_retention_days = 0
        paused_profiles = ["abc123"]
        """,
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.request_control.mode == "bounded_resume"
    assert config.request_control.background_embedding == "manual"
    assert config.request_control.background_max_posts_per_round == 4
    assert config.request_control.query_max_posts == 2
    assert config.request_control.audit_retention_days == 0
    assert config.request_control.paused_profiles == ("abc123",)


def test_manual_background_policy_stops_automatic_posts(tmp_path, monkeypatch):
    """manual 背景策略：后台不自动计算外部向量。"""
    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    config = _bounded_config(tmp_path)
    object.__setattr__(config.request_control, "background_embedding", "manual")
    indexer = MarkdownIndexer(tmp_path, config)
    try:
        indexer.sync()
        assert calls == []
    finally:
        indexer.close_document_store()


def test_component_key_is_stable_across_batch_sizes():
    """T10/T19：改批大小/超时/并发不得改变输入身份，也就不得解除旧 hold。"""
    from mortis_rag_mcp.request_lifecycle import component_key
    kwargs = dict(purpose_family_name="document", profile_fingerprint="fp",
                  embedding_key="ek", exact_input_sha256="sha")
    assert component_key(**kwargs) == component_key(**kwargs)
    assert component_key(**kwargs) != component_key(**{**kwargs, "exact_input_sha256": "other"})
    assert component_key(**kwargs) != component_key(**{**kwargs, "purpose_family_name": "native_media"})


def test_external_rebuild_requires_confirm_cost(tmp_path, monkeypatch):
    """T36/P7：外部全量重算缺确认时只返回预览，且零副作用（不清缓存、不启监听）。"""
    from mortis_rag_mcp.server import VaultMcpServer

    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    server = object.__new__(VaultMcpServer)
    server.config = indexer.config
    monkeypatch.setattr(server, "_indexer_for", lambda arguments, **kw: indexer)
    called: list[bool] = []
    monkeypatch.setattr(indexer, "rebuild", lambda: called.append(True))
    try:
        preview = server._kb_rebuild({"vault_path": str(tmp_path)})
        assert preview["rebuild"] == "preview"
        assert preview["applied"] is False
        assert preview["requires"] == "confirm_cost=true"
        assert "estimated_posts" in preview
        assert called == [], "预览阶段绝不能真的重建"
        confirmed = server._kb_rebuild({"vault_path": str(tmp_path), "confirm_cost": True})
        assert called == [True], "确认后必须真正重建"
        assert "rebuild" not in confirmed or confirmed.get("rebuild") != "preview"
    finally:
        indexer.stop_watching()
        indexer.close_document_store()


def test_budget_deferral_is_not_reported_as_unknown_outcome(tmp_path, monkeypatch):
    """R3 反例 4：预算延期是良性上限，不得冒充 AUTOMATIC_REPLAY_HELD。"""
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(f"{name[0]} BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path, limit=1))
    try:
        indexer.sync()
        assert len(calls) == 1
        values = list(indexer.failed_files.values())
        assert any(value.startswith("BUDGET_DEFERRED") for value in values)
        assert not any(value.startswith("AUTOMATIC_REPLAY_HELD") for value in values)
    finally:
        indexer.close_document_store()


def test_content_change_clears_embedding_quarantine(tmp_path, monkeypatch):
    """R3 反例 3：内容修正后旧退避 streak 必须清零（新身份不该被旧退避误伤）。

    新内容同样打不通，所以「清空」只能来自内容变更分支本身，不可能是成功路径顺带清掉。
    """
    target = tmp_path / "a.md"
    target.write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen",
                        _ok_transport(calls, fail_on=("A BODY",)))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        indexer._embed_failure_quarantine["a.md"] = 3
        target.write_text("A BODY STILL BROKEN", encoding="utf-8")
        indexer.sync()
        assert "a.md" not in indexer._embed_failure_quarantine
        assert calls[-1] == ["A BODY STILL BROKEN"]
    finally:
        indexer.close_document_store()


def test_legacy_mode_cannot_bypass_component_hold(tmp_path, monkeypatch):
    """R2 反例 1：切回 legacy_profile 不得裸撤新表 hold（held_result_missing 会重发）。"""
    from mortis_rag_mcp.doc_store import resolve_storage_layout
    from mortis_rag_mcp.request_lifecycle import OUTCOME_SUCCEEDED

    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    config = _bounded_config(tmp_path)
    object.__setattr__(config.request_control, "mode", "legacy_profile")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    indexer = MarkdownIndexer(tmp_path, config)
    try:
        control = indexer._paid_control_store()
        assert control is not None
        fingerprint = indexer._embedding_profile.fingerprint
        # 造出「POST 已成功、结果却没落盘」的批次：journal=success + 组件 held。
        plan = {"batch_id": "batch-stale", "operation_id": "op-stale",
                "purpose_family": "document", "profile_fingerprint": fingerprint,
                "endpoint": ENDPOINT,
                "components": [{"component_key": "ck-stale", "embedding_key": "ek-stale",
                                "input_sha256": "sha", "source": "a.md",
                                "source_kind": "document", "source_sha256": "s",
                                "revision_id": "", "chunk_id": "c1"}]}
        from mortis_rag_mcp._indexer import embedding_resume
        assert embedding_resume.reserve_embedding_batch(control, plan, "owner-stale").ok
        embedding_resume.begin_embedding_send(control, "batch-stale", "owner-stale")
        embedding_resume.record_embedding_outcome(
            control, "batch-stale", "owner-stale", OUTCOME_SUCCEEDED)
        embedding_resume.confirm_embedding_checkpoint(control, "batch-stale", "owner-stale", [])
        assert control.embedding_component_states(["ck-stale"]) == {"ck-stale": "held_result_missing"}
        # 旧路径必须把这条 hold 当成暂停判据，零 POST。
        indexer.sync()
        assert calls == []
        assert indexer._embedding_paused is True
    finally:
        indexer.close_document_store()


def test_partial_checkpoint_stops_further_paid_batches(tmp_path, monkeypatch):
    """R2 反例 2：部分确认同样停付，不得继续堆后面的付费批次。"""
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text(f"{name[0]} BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path, batch_size=2))
    try:
        original = indexer.checkpoint_embeddings
        monkeypatch.setattr(indexer, "checkpoint_embeddings",
                            lambda bindings: list(original(bindings))[:1])
        indexer.sync()
        assert len(calls) == 1, "首组部分确认后必须停付，不得再发第二组"
        assert len(calls[0]) == 2
        # 未落盘的那一条必须变成持久 hold（可观测、不自动重发）。
        assert any(value.startswith("RESULT_NOT_RECOVERED")
                   for value in indexer.failed_files.values())
    finally:
        indexer.close_document_store()


def test_same_process_stale_reservation_is_cleaned_at_sync_boundary(tmp_path, monkeypatch):
    """R2 反例 4：本进程上一轮遗留的 reserved 必须在同步边界释放，否则永久卡死。"""
    from mortis_rag_mcp._indexer import embedding_resume

    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", _ok_transport(calls))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        from mortis_rag_mcp._indexer.sync_engine import _resume_owner_token

        control = indexer._paid_control_store()
        token = _resume_owner_token(indexer)
        plan = {"batch_id": "batch-leftover", "operation_id": "op-1",
                "purpose_family": "document",
                "profile_fingerprint": indexer._embedding_profile.fingerprint,
                "endpoint": ENDPOINT,
                "components": [{"component_key": "ck-leftover", "embedding_key": "ek-leftover",
                                "input_sha256": "sha", "source": "a.md",
                                "source_kind": "document", "source_sha256": "s",
                                "revision_id": "", "chunk_id": "c1"}]}
        assert embedding_resume.reserve_embedding_batch(control, plan, token).ok
        indexer.sync()
        states = control.embedding_component_states(["ck-leftover"])
        assert states.get("ck-leftover") == "released", "本进程遗留占用必须被释放"
        assert len(calls) == 1
    finally:
        indexer.close_document_store()


def test_held_components_survive_indexer_restart(tmp_path, monkeypatch):
    """跨重启：held 组件仍在，重建 indexer 后零自动 POST。"""
    (tmp_path / "a.md").write_text("A BODY", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen",
                        _ok_transport(calls, fail_on=("A BODY",)))
    indexer = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        indexer.sync()
        assert len(calls) == 1
        work = embedding_resume.list_embedding_work(
            indexer._paid_control_store(), states=("held_unknown",))
        assert len(work) == 1
    finally:
        indexer.close_document_store()
        control = indexer._paid_control_store()
        if control is not None:
            control.close()
    # 新进程/新实例：held 仍在，零 POST。
    fresh = MarkdownIndexer(tmp_path, _bounded_config(tmp_path))
    try:
        fresh._paid_control_store()
        fresh.sync()
        assert len(calls) == 1
    finally:
        fresh.close_document_store()
