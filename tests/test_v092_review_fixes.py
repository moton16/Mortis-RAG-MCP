"""v0.9.2 二次评审（`784f2c5` / `1feb2ce`）修复的回归测试。

钉住这批修复的**行为契约**（全部离线：不联网、不启动监听、不安排刷新）：

- P0 `doctor` 探活的付费 guard：`paid_request_guard` 返回 bool，不得再被当函数调用；
  探活指纹与运行时同口径（`normalize_endpoint`），且必须尊重显式撤销；
- P1 HTTP 4xx「明确拒绝」必须终态化，不得永久占用未决集合（旧闸门下会冻死整条路由）；
- P1 成果核验器不可用 ≠ 成果丢失：不得据此下发 `held_*`（会冻结整个 profile）；
- P1 磁盘后端的 RAM 记账集合未重建时，严格迁移必须**报错**而不是报「没有向量」；
- P2 `resume_route` 命中配置暂停时不得先把控制库里的显式撤销清掉；
- P2 `prune_audit` 不得吞掉未处置的旧记录台账（默认 retention=30 天，属可达路径）；
- P2 声明只读打开时读路径不得落 DDL；
- P2 MCP `inputSchema` 的 action enum 必须与 `REQUEST_CONTROL_ACTIONS` 一致；
- P2 `release` 的免确认口径不得套用到快照之后已推进的批次（CAS 在事务内）；
- P2 2xx 只证明传输成功：产物合同不通过不得在账上留 `success`；
- P2 迁移台账读失败必须可见，不得伪装成「已全部处置」。
"""
from __future__ import annotations

import hashlib
import json
import time
from io import BytesIO
from urllib.error import HTTPError

import pytest

from mortis_rag_mcp import providers
from mortis_rag_mcp._indexer import embedding_resume
from mortis_rag_mcp._indexer import sync_engine as _sync_engine
from mortis_rag_mcp.config import (
    AppConfig, CacheConfig, EmbeddingConfig, RequestControlConfig,
)
from mortis_rag_mcp.doc_store import ControlStore, resolve_storage_layout
from mortis_rag_mcp.embedding_capabilities import normalize_endpoint
from mortis_rag_mcp.paid_requests import PaidRequestJournal
from mortis_rag_mcp.providers import (
    ExternalEmbeddingProvider, PaidRequestRefused, ProviderError,
)
from mortis_rag_mcp.request_lifecycle import (
    OUTCOME_SUCCEEDED, PURPOSE_BACKGROUND_DOCUMENT, RequestContext, component_key,
)
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
    """把 server 的配置解析钉到临时库，避免读到宿主/隔离配置。"""
    monkeypatch.setattr("mortis_rag_mcp.server.load_config", lambda *a, **k: _config(tmp_path))


def _control(tmp_path, vault=None) -> ControlStore:
    control = ControlStore(resolve_storage_layout(_config(tmp_path), vault or _vault(tmp_path)))
    control.open(create=True, write=True)
    return control


def _component(index: int = 1) -> dict:
    embedding_key = f"ek-{index}"
    return {
        "component_key": component_key(purpose_family_name="document",
                                       profile_fingerprint="fp",
                                       embedding_key=embedding_key,
                                       exact_input_sha256=f"sha-{index}"),
        "embedding_key": embedding_key, "input_sha256": f"sha-{index}", "source": "a.md",
        "source_kind": "document", "source_sha256": "s", "revision_id": "", "chunk_id": f"c{index}",
    }


def _seed_batch(control: ControlStore, batch_id="batch-1", owner="owner-1", state="reserved"):
    component = _component()
    embedding_resume.reserve_embedding_batch(control, {
        "batch_id": batch_id, "operation_id": "op-1", "purpose_family": "document",
        "profile_fingerprint": "fp", "endpoint": ENDPOINT, "components": [component],
    }, owner)
    if state in {"sending", "complete"}:
        embedding_resume.begin_embedding_send(control, batch_id, owner)
    if state == "complete":
        embedding_resume.record_embedding_outcome(control, batch_id, owner, OUTCOME_SUCCEEDED)
        embedding_resume.confirm_embedding_checkpoint(
            control, batch_id, owner, [component["embedding_key"]])
    return component


class _Response:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakePaidProvider:
    """探活装配用的最小 provider 替身（只记录被装上的 guard / profile）。"""

    is_local_free = False

    def __init__(self) -> None:
        self.guard = None
        self.profile = None
        self.journal = None

    def configure_paid_requests(self, journal, guard, profile_fingerprint, *, mode=""):
        self.journal = journal
        self.guard = guard
        self.profile = profile_fingerprint


# ---------------------------------------------------------------- P0：doctor 探活 guard

def test_doctor_probe_guard_is_a_boolean_not_a_callable(tmp_path):
    """修复前 `bool(db_allowed(profile))` 必抛 TypeError，被吞成「探测失败」。"""
    from mortis_rag_mcp import doctor

    cfg = _config(tmp_path)
    object.__setattr__(cfg, "vault_path", str(_vault(tmp_path)))
    provider = _FakePaidProvider()
    assert doctor._configure_probe_paid(provider, cfg, kind="embed", fingerprint="fp-1") is True
    assert provider.profile == "fp-1"
    assert provider.guard("fp-1") is True      # 常规 profile：放行，且不得抛 TypeError
    assert provider.guard("fp-other") is False


def test_doctor_probe_respects_explicit_revocation(tmp_path):
    """探活必须尊重显式撤销：此前 `pause_route` 之后仍会发一次真实付费请求。"""
    from mortis_rag_mcp import doctor
    from mortis_rag_mcp.paid_requests import open_paid_control

    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    object.__setattr__(cfg, "vault_path", str(vault))
    control = open_paid_control(resolve_storage_layout(cfg, vault))
    try:
        control.revoke_paid_authorization("fp-1")
    finally:
        control.close()
    provider = _FakePaidProvider()
    assert doctor._configure_probe_paid(provider, cfg, kind="embed", fingerprint="fp-1") is True
    assert provider.guard("fp-1") is False


def test_doctor_reranker_fingerprint_matches_runtime_normalization(tmp_path, monkeypatch):
    """指纹材质必须与 `indexer._reranker_profile_fingerprint` 同口径。"""
    from mortis_rag_mcp import doctor

    cfg = _config(tmp_path)
    object.__setattr__(cfg, "vault_path", str(_vault(tmp_path)))
    cfg.reranker.enabled = True
    cfg.reranker.endpoint = "HTTPS://Rerank.Test/v1/rerank/"
    cfg.reranker.model = "m"
    provider = _FakePaidProvider()
    provider.rerank = lambda query, documents: [{"index": 0, "score": 1.0}]
    # `probe_reranker` 在函数内部 `from .providers import create_reranker_provider`：
    # 钉住 providers 模块上的名字即可。
    monkeypatch.setattr(providers, "create_reranker_provider", lambda _rr: provider)
    doctor.probe_reranker(cfg)
    expected = hashlib.sha256("|".join((
        "rerank", str(getattr(cfg.reranker, "adapter", "openai")), "m",
        normalize_endpoint(cfg.reranker.endpoint),
    )).encode("utf-8")).hexdigest()
    assert provider.profile == expected


# ---------------------------------------------------------------- P1：4xx 终态 / 核验器

def test_rejected_http_is_terminal_and_does_not_freeze_the_route(tmp_path, monkeypatch):
    """4xx 明确拒绝（远端未处理）必须终态化，否则永久未决会冻死整条付费路由。"""
    control = _control(tmp_path)
    try:
        journal = PaidRequestJournal(control, "embed")
        provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", dimension=1,
                                            send_dimensions=False)
        provider.configure_paid_requests(journal, lambda _p: True, "fp", mode="bounded_resume")

        def rejected(request, timeout=None):
            raise HTTPError(request.full_url, 401, "unauthorized", {}, BytesIO(b"{}"))

        monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", rejected)
        with pytest.raises(ProviderError) as excinfo:
            provider.embed(["hello"])
        assert excinfo.value.outcome == "rejected"
        assert [item["state"] for item in control.list_request_intents()] == ["abandoned"]
        assert journal.pending() == []
    finally:
        control.close()


def test_verifier_failure_does_not_dispose_a_healthy_complete_batch(tmp_path):
    """核验器抛错 ≠ 成果丢失：只能跳过，绝不能推进成 `held_result_missing`。"""
    control = _control(tmp_path)
    try:
        _seed_batch(control, state="complete")

        def broken(_components):
            raise RuntimeError("disk store unreadable")

        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="", live_owners=(),
            verify_recoverable=broken, audit_complete=True, states=("complete",),
            owner_predicate=lambda _token: True)
        assert any(str(item).startswith("verify_failed:") for item in summary["errors"])
        assert summary["held_complete"] == 0
        work = embedding_resume.list_embedding_work(control, states=("complete",))
        assert [batch["batch_id"] for batch in work] == ["batch-1"]
    finally:
        control.close()


def test_empty_verifier_result_still_holds_complete_batches(tmp_path):
    """反向钉住：核验器**正常返回空集**时仍是成果丢失（不得被上面的修复放宽）。"""
    control = _control(tmp_path)
    try:
        _seed_batch(control, state="complete")
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="", live_owners=(),
            verify_recoverable=lambda _components: set(), audit_complete=True,
            states=("complete",), owner_predicate=lambda _token: True)
        assert summary["held_complete"] == 1
    finally:
        control.close()


def test_strict_disk_vector_migration_raises_instead_of_reporting_empty():
    """严格迁移：后端读不出 id 时必须抛错（「读不到」≠「不存在」）。"""
    class _Backend:
        def list_ids(self):
            raise RuntimeError("vec0 unavailable")

    class _Owner:
        _vectors_on_disk = True
        _vector_backend = _Backend()

        def __init__(self) -> None:
            self._disk_vectors: set[str] = set()
            self._chunks: dict = {}
            self._vectors_cache_path = None

    lenient = _Owner()
    _sync_engine.ensure_disk_vectors_migrated(lenient)          # 吞掉：保持原语义
    assert lenient._disk_vectors == set()
    strict = _Owner()
    with pytest.raises(RuntimeError, match="vec0 unavailable"):
        _sync_engine.ensure_disk_vectors_migrated(strict, strict=True)


# ---------------------------------------------------------------- P2：逐条行为契约

def test_resume_route_paused_by_config_does_not_clear_revocation(tmp_path, monkeypatch):
    """命中配置暂停必须先返回：此前会先把控制库里的显式撤销清掉。"""
    vault = _vault(tmp_path)
    fingerprint = "fp-resume"
    control = _control(tmp_path, vault)
    try:
        control.revoke_paid_authorization(fingerprint)
        cfg = _config(tmp_path)
        object.__setattr__(cfg, "request_control",
                           RequestControlConfig(mode="bounded_resume",
                                                paused_profiles=(fingerprint,)))
        monkeypatch.setattr("mortis_rag_mcp.server.load_config", lambda *a, **k: cfg)
        result = request_control_action(vault_path=vault, action="resume_route",
                                        profile_fingerprint=fingerprint, dry_run=False)
        assert result["applied"] is False
        assert result["error"] == "ROUTE_PAUSED_BY_CONFIG"
        assert control.paid_authorization_state(fingerprint) == "revoked"
    finally:
        control.close()


def test_prune_audit_keeps_undisposed_legacy_rows(tmp_path):
    """保留期清理只清**已处置**的旧行；未处置的旧台账是人工对账凭据。"""
    control = _control(tmp_path, _vault(tmp_path))
    try:
        _seed_batch(control)  # 建出续算表（走有表的清理路径）
        control.record_send_intent("legacy-1", writer="", kind="embed", payload_hash="ph",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        control.mark_intent("legacy-1", "success", expected_states=("prepared",))
        control.record_send_intent("fresh-1", kind="embed", payload_hash="ph2",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        control.mark_intent("fresh-1", "success", expected_states=("prepared",))
        out = control.prune_audit(30, now=time.time() + 400 * 86400)
        assert out["request_intents"] == 1
        assert out["skipped_legacy_pending"] == 1
        assert {item["request_id"] for item in control.list_request_intents()} == {"legacy-1"}
    finally:
        control.close()


def test_read_only_open_does_not_create_payment_tables(tmp_path):
    """声明只读时读路径不得落 DDL；写声明仍可惰性补齐。"""
    control = ControlStore(resolve_storage_layout(_config(tmp_path), _vault(tmp_path)))
    try:
        control.open(create=True, write=True)
        assert control.payment_schema_present() is False
        control.open(create=False, write=False)
        assert control.list_request_intents() == []
        assert control.paid_authorization_state("fp") == "absent"
        assert control.payment_schema_present() is False
        control.open(create=False, write=True)
        assert control.list_request_intents() == []
        assert control.payment_schema_present() is True
    finally:
        control.close()


def test_request_control_schema_lists_every_supported_action():
    """MCP 工具 schema 的 enum 必须与 `REQUEST_CONTROL_ACTIONS` 一致。"""
    from mortis_rag_mcp.server import _tool_definitions

    tool = next(item for item in _tool_definitions() if item["name"] == "kb_request_control")
    enum = tool["inputSchema"]["properties"]["action"]["enum"]
    assert set(enum) == set(REQUEST_CONTROL_ACTIONS)
    assert {"release", "audit_complete", "prune_audit"} <= set(enum)


def test_release_rejects_a_batch_that_moved_after_the_snapshot(tmp_path):
    """快照后状态被推进 → 拒绝，不把「reserved 免确认」套用到可能已发出的批次。"""
    control = _control(tmp_path, _vault(tmp_path))
    try:
        _seed_batch(control, state="sending")
        result = embedding_resume.resolve_embedding_work(
            control, "batch-1", action="release", confirm_repeat_cost=True,
            expected_state="reserved")
        assert result["applied"] is False
        assert result["error"] == "RESUME_BATCH_STATE_CHANGED"
        assert result["batch_state"] == "sending"
    finally:
        control.close()


def test_release_needs_confirmation_for_a_non_reserved_batch(tmp_path):
    """确认门槛下沉到 store 层，按本事务内刚读到的状态判定。"""
    control = _control(tmp_path, _vault(tmp_path))
    try:
        _seed_batch(control, state="sending")
        result = embedding_resume.resolve_embedding_work(
            control, "batch-1", action="release", confirm_repeat_cost=False,
            expected_state="sending")
        assert result["applied"] is False
        assert result["error"] == "RESUME_RELEASE_NEEDS_CONFIRMATION"
    finally:
        control.close()


def test_shape_invalid_2xx_is_not_recorded_as_success(tmp_path, monkeypatch):
    """2xx 只证明传输成功；产物合同不通过时账上不得留 `success`。"""
    control = _control(tmp_path)
    try:
        journal = PaidRequestJournal(control, "embed")
        provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", dimension=1,
                                            send_dimensions=False)
        provider.configure_paid_requests(journal, lambda _p: True, "fp", mode="bounded_resume")

        def bad_shape(request, timeout=None):
            return _Response(json.dumps({"data": [{"index": 0}]}).encode("utf-8"))

        monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", bad_shape)
        with pytest.raises(ProviderError) as excinfo:
            provider.embed(["hello"])
        assert excinfo.value.outcome == "response_invalid"
        assert [item["state"] for item in control.list_request_intents()] == ["submission_unknown"]
    finally:
        control.close()


def test_paid_post_does_not_follow_redirects_or_forward_the_key(tmp_path):
    """付费 POST 不跟随 3xx：既不外泄 `Authorization`，也不把结果归因到别的主机。

    修复前实机探针（`E:/coding/.mortis-runs/v092/probe-gaierror/probe4.py`）：
    目标主机收到 `Bearer <api_key>`，且 302 + 目标 DNS 失败会被误判成 `not_sent`。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[tuple[str, str]] = []

    class _Target(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            hits.append(("GET", self.headers.get("Authorization", "")))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:
            return

    class _Redirector(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.target_port}/login")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args) -> None:
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    redirector = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    redirector.target_port = target.server_address[1]
    for server in (target, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    control = _control(tmp_path)
    try:
        provider = ExternalEmbeddingProvider(
            endpoint=f"http://127.0.0.1:{redirector.server_address[1]}/v1/embeddings",
            model="m", api_key="sk-secret", dimension=1, send_dimensions=False)
        provider.configure_paid_requests(PaidRequestJournal(control, "embed"),
                                        lambda _p: True, "fp", mode="bounded_resume")
        with pytest.raises(ProviderError) as excinfo:
            provider.embed(["hello"])
        assert hits == [], "3xx 目标不得被访问（key 会被转发、结果会被误归因）"
        assert excinfo.value.outcome == "http_error"
        assert excinfo.value.remote_effect_unknown is True, "不得宣称「未发出」"
    finally:
        for server in (target, redirector):
            server.shutdown()
        control.close()


def test_gate_refusal_is_never_swallowed_outside_the_ledger(tmp_path, monkeypatch):
    """闸门拒绝必须上抛：非账本路径不得把同一载荷第二次物理 POST。"""
    control = _control(tmp_path)
    try:
        provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", dimension=1,
                                            send_dimensions=False)
        provider.configure_paid_requests(PaidRequestJournal(control, "embed"),
                                        lambda _p: True, "fp", mode="bounded_resume")
        posts: list[dict] = []

        def unknown(request, timeout=None):
            posts.append(json.loads(request.data.decode("utf-8")))
            raise OSError("simulated network failure")

        monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", unknown)
        with pytest.raises(ProviderError) as first:
            provider.embed(["hello"])
        assert first.value.outcome == "transport_unknown"
        assert len(posts) == 1
        with pytest.raises(PaidRequestRefused):
            provider.embed(["hello"])
        assert len(posts) == 1, "同一载荷不得第二次物理 POST"
    finally:
        control.close()


def test_ledger_managed_send_is_not_blocked_by_the_intent_gate(tmp_path, monkeypatch):
    """账本路径的重发只可能来自操作者确认，意图级闸门不得把它拦回去。"""
    control = _control(tmp_path)
    try:
        provider = ExternalEmbeddingProvider(endpoint=ENDPOINT, model="m", dimension=1,
                                            send_dimensions=False)
        provider.configure_paid_requests(PaidRequestJournal(control, "embed"),
                                        lambda _p: True, "fp", mode="bounded_resume")
        posts: list[int] = []

        def unknown(request, timeout=None):
            posts.append(1)
            raise OSError("simulated network failure")

        monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", unknown)
        context = RequestContext("op-1", PURPOSE_BACKGROUND_DOCUMENT, "batch-1",
                                 ledger_managed=True)
        for _ in range(2):
            with pytest.raises(ProviderError):
                provider.embed_batch_once(["hello"], request_context=context)
        assert len(posts) == 2
    finally:
        control.close()


def test_legacy_pending_read_error_is_surfaced(tmp_path, monkeypatch):
    """控制库读失败不得被上报成「没有待处置旧记录」。"""
    vault = _vault(tmp_path)
    bootstrap = _control(tmp_path, vault)
    bootstrap.close()

    def broken(self, **_kwargs):
        raise RuntimeError("control store unreadable")

    monkeypatch.setattr(ControlStore, "list_request_intents", broken)
    result = request_control_action(vault_path=vault, action="list")
    assert result["legacy_migration_pending"] == []
    assert any(str(item).startswith("intents_unreadable:")
               for item in result["legacy_read_error"])


# ------------------------------------------------- 负路径补漏（v0.9.2 第四窗口）

def test_held_summary_reports_unavailable_without_resume_tables(tmp_path):
    """「没有台账」≠「零 hold」：续算表未建时必须报 `available=False`。"""
    control = _control(tmp_path, _vault(tmp_path))
    try:
        assert control.resume_schema_present() is False
        summary = embedding_resume.held_summary(control)
        assert summary["available"] is False
        assert summary["held_batches"] == 0
        assert summary["active_batches"] == 0
    finally:
        control.close()


def test_release_refuses_a_complete_batch_even_with_confirmation(tmp_path):
    """`complete` 批次不可 release：先要确认，给了确认也因终态不可变被拒。"""
    control = _control(tmp_path, _vault(tmp_path))
    try:
        _seed_batch(control, state="complete")
        plain = embedding_resume.resolve_embedding_work(control, "batch-1", action="release")
        assert plain["applied"] is False
        assert plain["error"] == "RESUME_RELEASE_NEEDS_CONFIRMATION"
        forced = embedding_resume.resolve_embedding_work(
            control, "batch-1", action="release", confirm_repeat_cost=True)
        assert forced["applied"] is False
        assert forced["error"] == "RESUME_BATCH_IMMUTABLE"
        work = embedding_resume.list_embedding_work(control, batch_id="batch-1")
        assert work[0]["state"] == "complete"
    finally:
        control.close()


def test_prune_audit_without_resume_tables_only_touches_intents(tmp_path):
    """没有续算表时清理只走 intents 分支，且不得顺手把续算表建出来。"""
    control = _control(tmp_path, _vault(tmp_path))
    try:
        control.record_send_intent("old-1", writer="", kind="embed", payload_hash="ph1",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        control.mark_intent("old-1", "success", expected_states=("prepared",))
        control.record_send_intent("new-1", kind="embed", payload_hash="ph2",
                                   endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        control.mark_intent("new-1", "success", expected_states=("prepared",))
        out = control.prune_audit(30, now=time.time() + 400 * 86400)
        assert out["request_intents"] == 1
        assert out["batches"] == 0 and out["components"] == 0
        assert out["skipped_legacy_pending"] == 1
        assert control.resume_schema_present() is False
    finally:
        control.close()


def test_verifier_failure_skips_a_sending_batch_without_disposing_it(tmp_path):
    """核验器不可用 ≠ 成果不存在：中断态批次保持 `sending`，且不得计入已处置。"""
    control = _control(tmp_path, _vault(tmp_path))
    try:
        _seed_batch(control, state="sending")

        def broken(_components):
            raise RuntimeError("vector store unreadable")

        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="", live_owners=(),
            verify_recoverable=broken, states=("sending",),
            owner_predicate=lambda _token: True)
        assert any(str(item).startswith("verify_failed:") for item in summary["errors"])
        assert summary["held_sending"] == 0
        work = embedding_resume.list_embedding_work(control, batch_id="batch-1")
        assert work[0]["state"] == "sending"
    finally:
        control.close()


def test_legacy_migration_pending_skips_v092_written_intents(tmp_path):
    """新版本写入的意图带 `writer='v092'` 标记：不属于「无法映射」的待处置对象。"""
    vault = _vault(tmp_path)
    bootstrap = _control(tmp_path, vault)
    try:
        bootstrap.record_send_intent("old-1", writer="", kind="embed", payload_hash="ph1",
                                     endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        bootstrap.mark_intent("old-1", "submission_unknown")
        bootstrap.record_send_intent("new-1", writer="v092", kind="embed", payload_hash="ph2",
                                     endpoint=ENDPOINT, profile_fingerprint="fp", attempt=None)
        bootstrap.mark_intent("new-1", "submission_unknown")
    finally:
        bootstrap.close()
    result = request_control_action(vault_path=vault, action="list")
    pending = {item["request_id"] for item in result["legacy_migration_pending"]}
    assert pending == {"old-1"}


def test_query_max_posts_caps_paid_query_embeddings(tmp_path, monkeypatch):
    """跨库检索里付费查询嵌入受 `query_max_posts` 约束（按 profile 去重计数）。"""
    import types

    from mortis_rag_mcp._server import fanout as fanout_mod
    from mortis_rag_mcp.registry import VaultEntry

    class _FakeIndexer:
        reranker_provider = None
        embedding_provider = object()

        def __init__(self, config: AppConfig, fingerprint: str) -> None:
            self.config = config
            self._embedding_profile = types.SimpleNamespace(fingerprint=fingerprint)
            self.last_sync = 1.0
            self._chunks_cache_loaded = True
            self._chunks: dict = {}

        def request_refresh(self, **_kwargs) -> None:
            return None

        def refresh_status(self) -> dict:
            return {"indexing_in_progress": False, "indexing_progress": None}

        def index_state(self) -> dict:
            return {"index_state": "ready"}

        def _vector_route_allowed(self) -> bool:
            return True

        def search(self, _query, _k, _rerank, **_kwargs) -> list:
            return []

    cfg = _config(tmp_path)
    object.__setattr__(cfg, "request_control",
                       RequestControlConfig(mode="bounded_resume", query_max_posts=2))
    entries: list[VaultEntry] = []
    indexers: dict[str, _FakeIndexer] = {}
    for index in range(3):
        path = tmp_path / f"vault{index}"
        path.mkdir()
        entries.append(VaultEntry(path=str(path), name=f"v{index}", registered_at=0.0))
        indexers[str(path)] = _FakeIndexer(cfg, f"fp-{index}")

    fake_server = types.SimpleNamespace(
        config=cfg,
        registry=types.SimpleNamespace(load=lambda: entries),
        _indexer_for=lambda arguments: indexers[arguments["vault_path"]],
        _resolve_vault_path=lambda value: value,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(fanout_mod, "embed_with_profile",
                        lambda _provider, texts, _profile, query=False: (calls.append(list(texts))
                                                                        or [[0.1]]))
    result = fanout_mod.fanout_search(fake_server, "hello", 5, False)
    assert len(calls) == 2, "上限之外的库不得再发付费查询嵌入"
    assert any("query_max_posts=2 reached" in str(item)
               for item in result["errors"].values())


# --------------------------------------------- 恢复扫描：快照之后的推进不得被误处置

def test_recovery_does_not_count_a_batch_that_moved_after_the_snapshot(tmp_path):
    """快照是 `reserved`、处置时已是 `sending` → 拒绝且**不计数**。

    修复前 `released_reserved` 无条件自增，控制库实际什么都没做，运维看到的
    「已释放 N 批」是假的。
    """
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    other = _control(tmp_path, vault)
    try:
        _seed_batch(control, owner="owner-old")
        original = control.list_embedding_work

        def snapshot_then_advance(*args, **kwargs):
            out = original(*args, **kwargs)
            other.begin_embedding_send("batch-1", "owner-old")  # 旧 owner 其实还活着
            return out

        control.list_embedding_work = snapshot_then_advance
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="owner-a",
            verify_recoverable=lambda _components: set(),
            owner_predicate=lambda _token: True)
        assert summary["released_reserved"] == 0
        assert any("RESUME_BATCH_STATE_CHANGED" in str(item) for item in summary["errors"])
        work = embedding_resume.list_embedding_work(control, batch_id="batch-1")
        assert work[0]["state"] == "sending"
    finally:
        other.close()
        control.close()


def test_recovery_does_not_demote_a_batch_that_already_got_its_result(tmp_path):
    """快照是 `sending`、处置时旧 owner 已落 `result_pending` → 不得降级成 hold。

    降级会把「已收到成功响应」误判成「结果未知」，而 `held_*` 会冻结整条路由。
    """
    vault = _vault(tmp_path)
    control = _control(tmp_path, vault)
    other = _control(tmp_path, vault)
    try:
        _seed_batch(control, owner="owner-old", state="sending")
        original = control.list_embedding_work

        def snapshot_then_advance(*args, **kwargs):
            out = original(*args, **kwargs)
            other.record_embedding_outcome("batch-1", "owner-old", OUTCOME_SUCCEEDED,
                                           reason="old owner got 2xx")
            return out

        control.list_embedding_work = snapshot_then_advance
        summary = embedding_resume.recover_interrupted_batches(
            control, layout=control.layout, owner_token="owner-a",
            verify_recoverable=lambda _components: set(),
            owner_predicate=lambda _token: True)
        assert summary["held_sending"] == 0
        assert any("RESUME_HELD_REFUSED" in str(item) for item in summary["errors"])
        work = embedding_resume.list_embedding_work(control, batch_id="batch-1")
        assert work[0]["state"] == "result_pending"
    finally:
        other.close()
        control.close()
