"""C100：付费请求闸门（持久 send_intent journal + profile 授权）合同。

钉住：
- 外部付费 POST 发送前必须先落持久意图，结果只允许 success / submission_unknown；
- 控制面不可用（cache 关闭）时 fail closed，不得静默放行；
- 升级/pending 审批期间外部请求数必须为 0（embed_missing 只是暂停，不谎报失败）；
- `--approve-reembedding` 只授权精确 fingerprint，配置漂移后自动失效。
"""
from __future__ import annotations

import json

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.paid_requests import PaidRequestJournal, paid_request_guard
from mortis_rag_mcp.providers import ProviderError
from mortis_rag_mcp.server import approve_reembedding


def make_config(tmp_path, *, enabled: bool = True, model: str = "bge-m3") -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = enabled
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    cfg.embedding.mode = "external"
    cfg.embedding.endpoint = "https://example.invalid/v1/embeddings"
    cfg.embedding.model = model
    cfg.embedding.dimension = 1024
    cfg.embedding.api_key = "test-key"
    cfg.embedding.send_dimensions = False
    return cfg


def _open_control(tmp_path, cfg):
    from mortis_rag_mcp.paid_requests import open_paid_control
    layout = resolve_storage_layout(cfg, tmp_path / "vault")
    return open_paid_control(layout)


class ResponseStub:
    """urlopen 的最小替身（只提供 `_post` 用到的上下文管理器 + read）。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_guard_semantics(tmp_path):
    cfg = make_config(tmp_path)
    control = _open_control(tmp_path, cfg)
    try:
        assert paid_request_guard(control, "fp-a") is True          # 首次使用：无 pending
        assert paid_request_guard(control, "fp-a", pending_approval=True) is False
        control.authorize_paid_profile("fp-a", scope="reembedding")
        assert paid_request_guard(control, "fp-a", pending_approval=True) is True
        control.revoke_paid_authorization("fp-a")
        assert paid_request_guard(control, "fp-a") is False          # 撤销优先于一切
        assert paid_request_guard(control, "") is False
        assert paid_request_guard(control, "fp-b", pending_approval=False) is True
    finally:
        control.close()


def test_journal_blocks_resend_until_intent_resolved(tmp_path):
    """结果未知的同一载荷禁止自动重发；人工放弃后才允许再发一次（attempt 只用于观测）。"""
    cfg = make_config(tmp_path)
    control = _open_control(tmp_path, cfg)
    try:
        journal = PaidRequestJournal(control, "embed")
        first = journal.before_send("hash-1", "https://example.invalid", "fp-a")
        journal.mark_unknown(first, "response_unconfirmed")
        # 未决意图必须可见（旧实现 pending() 只看 prepared，把 submission_unknown 藏掉了）
        assert [item["request_id"] for item in journal.pending()] == [first]
        # 同一载荷结果未知 → 拒绝重发，且不得落新意图
        with pytest.raises(ProviderError) as excinfo:
            journal.before_send("hash-1", "https://example.invalid", "fp-a")
        assert "PAID_REQUEST_UNRESOLVED" in str(excinfo.value)
        assert len(control.list_request_intents()) == 1
        # 人工放弃（确认不复用该结果）后才允许再次发送
        control.mark_intent(first, "abandoned", "operator reviewed")
        second = journal.before_send("hash-1", "https://example.invalid", "fp-a")
        journal.mark_success(second)
        intents = control.list_request_intents()
        assert [item["state"] for item in intents] == ["abandoned", "success"]
        assert [item["attempt"] for item in intents] == [1, 2]      # 计数只用于观测
        assert intents[0]["payload_hash"] == "hash-1"
        assert journal.pending() == []
        # 不同载荷 / endpoint / profile 不受影响
        assert journal.before_send("hash-2", "https://example.invalid", "fp-a")
        assert journal.before_send("hash-1", "https://other.invalid", "fp-a")
        assert journal.before_send("hash-1", "https://example.invalid", "fp-b")
    finally:
        control.close()


def test_sync_does_not_resend_after_unknown_outcome(tmp_path, monkeypatch):
    """端到端：一次网络错误（结果未知）之后，下一轮 sync 不得再发同一载荷。"""
    cfg = make_config(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\nbody text unknown\n", encoding="utf-8")
    calls: list[dict] = []

    def failing_urlopen(request, timeout=None):
        calls.append(json.loads(request.data.decode("utf-8")))
        raise OSError("simulated network failure")

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", failing_urlopen)
    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer.sync()
        assert len(calls) == 1, "首次应发出一次请求"
        indexer.sync()
        assert len(calls) == 1, "结果未知后不得自动重发"
        intents = indexer._paid_control_store().list_request_intents()
        assert [item["state"] for item in intents] == ["submission_unknown"]
        assert indexer._embedding_paused is True
        assert not any(chunk.embedding for chunks in indexer._chunks.values() for chunk in chunks)
    finally:
        indexer.close_document_store()


def test_multi_batch_unknown_pauses_instead_of_resending_leading_batches(tmp_path, monkeypatch):
    """多批切片下结果未知 → 整体暂停，不得每轮重发已成功的**前导批**（重复计费）。"""
    cfg = make_config(tmp_path)
    cfg.chunk_size = 40
    cfg.chunk_overlap = 0
    cfg.embedding.batch_size = 1
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(
        "# 标题\n\n第一段内容甲甲乙乙。\n\n第二段内容丙丙丁丁。\n\n第三段内容戊戊己己。\n",
        encoding="utf-8",
    )
    sent: list[str] = []

    def flaky_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload["input"][0][:24])
        if len(sent) == 2:          # 第 2 批网络失败 → 结果未知
            raise OSError("simulated network failure")
        vectors = [{"index": i, "embedding": [0.0] * 1023 + [1.0]}
                   for i in range(len(payload["input"]))]
        return ResponseStub(json.dumps({"data": vectors}).encode("utf-8"))

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", flaky_urlopen)
    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer.sync()
        assert len(sent) == 2, "首个文件应分两批发出"
        assert indexer.unresolved_paid_intents(), "未决意图必须可观测"
        assert indexer.has_unresolved_paid_intents(indexer._paid_control_store(), "embed") is True

        indexer.sync()
        assert len(sent) == 2, "未决期间不得重发任何一批（含已成功的前导批）"
        assert indexer._embedding_paused is True
        # 暂停只覆盖索引批量路径：查询期嵌入是单次、载荷唯一、不重放的请求，
        # 不能被一起拦掉（否则一次网络抖动会让语义检索静默退回词法）。
        from mortis_rag_mcp._indexer.sync_engine import _paid_embedding_allowed
        assert _paid_embedding_allowed(indexer) is False, "索引批量路径必须暂停"
        assert indexer.may_use_paid_profile(indexer._embedding_profile.fingerprint) is True, \
            "查询期闸门不得被索引期未决意图阻塞"

        # 人工确认/放弃未决意图之后才恢复发送。
        control = indexer._paid_control_store()
        for item in control.list_request_intents():
            if item["state"] == "submission_unknown":
                control.mark_intent(item["request_id"], "abandoned", "operator reviewed")
        indexer.sync()
        assert len(sent) > 2, "放弃未决意图后应恢复"
    finally:
        indexer.close_document_store()


def test_profile_drift_requires_explicit_approval(tmp_path, monkeypatch):
    """换 model/dimension = 全库重新付费嵌入，必须显式授权，不得按「首次使用」放行。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\nbody text drift\n", encoding="utf-8")
    calls: list[dict] = []

    def ok_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append(payload)
        vectors = [{"index": i, "embedding": [0.0] * 1023 + [1.0]}
                   for i in range(len(payload["input"]))]
        return ResponseStub(json.dumps({"data": vectors}).encode("utf-8"))

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", ok_urlopen)
    first = MarkdownIndexer(vault, make_config(tmp_path, model="bge-m3"))
    try:
        first.sync()
        assert calls, "首次建立向量缓存应当发出请求"
    finally:
        first.close_document_store()

    calls.clear()
    drifted = MarkdownIndexer(vault, make_config(tmp_path, model="another-model"))
    try:
        fingerprint = drifted._embedding_profile.fingerprint
        assert drifted._paid_profile_requires_approval is True, "旧模型的向量文件存在 = 漂移"
        assert drifted.may_use_paid_profile(fingerprint) is False
        drifted.sync()
        assert calls == [], "未授权期间外部请求数必须为 0"
        assert drifted._embedding_paused is True
        assert not drifted.failed_files, "暂停不得把每个文件谎报为失败"
        summary = drifted.reembedding_approval_summary()
        assert summary["profile_fingerprint"] == fingerprint
        # 显式授权精确 fingerprint 后放行
        drifted._paid_control_store().authorize_paid_profile(fingerprint, scope="reembedding")
        assert drifted.may_use_paid_profile(fingerprint) is True
        drifted.sync()
        assert calls, "授权后应恢复付费嵌入"
    finally:
        drifted.close_document_store()


def test_journal_fails_closed_without_control():
    journal = PaidRequestJournal(lambda: None, "embed")
    with pytest.raises(ProviderError) as excinfo:
        journal.before_send("hash", "https://example.invalid", "fp")
    assert "PAID_REQUEST_JOURNAL_UNAVAILABLE" in str(excinfo.value)


def test_external_embedding_sends_with_journal(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\nbody text one\n", encoding="utf-8")
    calls: list[dict] = []

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append(payload)
        vectors = [{"index": i, "embedding": [0.0] * 1023 + [1.0]}
                   for i in range(len(payload["input"]))]
        return _Response(json.dumps({"data": vectors}).encode("utf-8"))

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer.sync()
        assert calls, "无 pending 审批时外部嵌入应正常发出"
        intents = indexer._paid_control_store().list_request_intents()
        assert intents and all(item["state"] == "success" for item in intents)
        assert intents[0]["kind"] == "embed"
        assert any(chunk.embedding for chunks in indexer._chunks.values() for chunk in chunks)
    finally:
        indexer.close_document_store()


def test_pending_approval_blocks_external_requests(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\nbody text two\n", encoding="utf-8")
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append(json.loads(request.data.decode("utf-8")))
        raise AssertionError("pending 审批期间不得发出任何外部请求")

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer._paid_profile_requires_approval = True
        assert indexer.may_use_paid_profile(indexer._embedding_profile.fingerprint) is False
        indexer.sync()
        assert calls == [], "pending 审批期间外部请求数必须为 0"
        assert indexer._embedding_paused is True
        assert not indexer.failed_files, "暂停不得把每个文件谎报为失败"
        # 文本层仍然可用：暂停只影响付费向量，不影响词法检索
        assert indexer._chunks, "暂停付费嵌入不该阻止文本层建索引"
    finally:
        indexer.close_document_store()


def test_reembedding_approval_summary_is_honest(tmp_path):
    from mortis_rag_mcp._indexer.models import Chunk
    cfg = make_config(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, cfg)
    try:
        assert indexer.reembedding_approval_summary() is None
        indexer._paid_profile_requires_approval = True
        indexer._chunks["a.md"] = [
            Chunk("id-1", "hello", "a.md", "t", {"chunk_index": 0, "start_line": 1, "end_line": 1}),
            Chunk("id-2", "x" * 9, "a.md", "t", {"chunk_index": 1, "start_line": 2, "end_line": 2,
                                                "embedding_disabled": True}),
        ]
        summary = indexer.reembedding_approval_summary()
        assert summary["chunk_count"] == 1
        assert summary["skipped_embedding_disabled"] == 1
        assert summary["estimated_embed_chars"] == 5
        # 估算器未校准：不得伪造费用数字
        assert summary["estimated_cost"] is None
        assert summary["cost_basis"] == "unknown"
        assert summary["profile_fingerprint"] == indexer._embedding_profile.fingerprint
    finally:
        indexer.close_document_store()


def test_approve_reembedding_authorizes_exact_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "vault"
    vault.mkdir()
    config_path = tmp_path / "app.toml"
    cache_dir = str(tmp_path / "cache").replace("\\", "/")
    config_path.write_text(
        'mode = "external"\nendpoint = "https://example.invalid/v1/embeddings"\n'
        'model = "bge-m3"\ndimension = 1024\napi_key = "k"\nsend_dimensions = false\n'
        f'[cache]\ndir = "{cache_dir}"\nenabled = true\nplacement = "home"\n',
        encoding="utf-8",
    )
    result = approve_reembedding("fp-exact", config_path=config_path, vault_path=vault)
    assert result["approved"] is True
    cfg = make_config(tmp_path)
    control = _open_control(tmp_path, cfg)
    try:
        assert control.paid_profile_authorized("fp-exact") is True
        assert control.paid_profile_authorized("fp-other") is False
        assert paid_request_guard(control, "fp-exact", pending_approval=True) is True
    finally:
        control.close()
