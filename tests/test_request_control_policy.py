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
            "legacy-request-1", kind="embed", payload_hash="deadbeef",
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
