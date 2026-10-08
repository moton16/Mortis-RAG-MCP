"""E06：最小管理 CLI（互斥 / --vault / 坏配置退出 2 / list+abandon）与 retry 转发（NEW）。

全部临时 config/cache/registry；list/abandon 只消费本机记录，不发起任何请求。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import ControlStore, DocumentStore, resolve_storage_layout
from mortis_rag_mcp.ingest.worker import VirtualIngestWorker
from mortis_rag_mcp.server import (  # noqa: E402
    SERVER_INSTRUCTIONS,
    _control_for,
    _explicit_config_problem,
    main,
)


def _app_config(tmp_path: Path, name: str) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / f"cache-{name}"), enabled=True),
    )


def _vault(tmp_path: Path, name: str) -> Path:
    vault = tmp_path / f"vault-{name}"
    vault.mkdir(parents=True, exist_ok=True)
    return vault


def _config_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"app-{name}.toml"
    path.write_text("mode = 'static'\n", encoding="utf-8")
    return path


def _layout(tmp_path: Path, name: str):
    """与 CLI 完全同一条解析链（`_control_for`），保证落点与 list/abandon 一致。"""
    return _control_for(_vault(tmp_path, name), _config_file(tmp_path, name))


def _seed_intents(tmp_path: Path, name: str) -> dict[str, str]:
    """在本机控制库里放三条意图：两条 prepared、一条 success。"""
    layout = _layout(tmp_path, name)
    control = ControlStore(layout)
    control.open(create=True, write=True)
    try:
        control.record_send_intent("req-target", kind="embedding", payload_hash="p1",
                                   endpoint="https://example.invalid/embed", attempt=1)
        control.record_send_intent("req-other", kind="embedding", payload_hash="p2",
                                   endpoint="https://example.invalid/embed", attempt=1)
        control.record_send_intent("req-done", kind="rerank", payload_hash="p3",
                                   endpoint="https://example.invalid/rerank", attempt=1)
        control.mark_intent("req-done", "success")
    finally:
        control.close()
    return {"target": "req-target", "other": "req-other", "done": "req-done"}


def test_management_actions_are_mutually_exclusive(tmp_path: Path):
    vault = _vault(tmp_path, "mutex")
    with pytest.raises(SystemExit) as excinfo:
        main(["--doctor", "--migrate-ingest", "--vault", str(vault)])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--list-requests", "--abandon-request", "req-1", "--vault", str(vault)])
    assert excinfo.value.code == 2
    # --apply 只能配 --migrate-ingest
    with pytest.raises(SystemExit) as excinfo:
        main(["--apply", "--vault", str(vault)])
    assert excinfo.value.code == 2


def test_management_action_requires_explicit_vault(tmp_path: Path):
    with pytest.raises(SystemExit) as excinfo:
        main(["--list-requests"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--abandon-request", "req-1"])
    assert excinfo.value.code == 2


def test_invalid_explicit_or_selected_config_exits_two(tmp_path: Path):
    vault = _vault(tmp_path, "cfg")
    missing = tmp_path / "missing.toml"
    assert main(["--list-requests", "--vault", str(vault), "--app-config", str(missing)]) == 2


def test_config_priority_and_unselected_values_are_ignored(tmp_path: Path, monkeypatch):
    good = tmp_path / "good.toml"
    good.write_text("mode = 'static'\n", encoding="utf-8")
    monkeypatch.setenv("MORTIS_RAG_CONFIG", str(good))
    monkeypatch.setenv("VAULT_MCP_CONFIG", str(tmp_path / "does-not-exist.toml"))
    # 新 env 命中且有效 → 不再检查低优先级旧 env
    assert _explicit_config_problem(None) == ""
    monkeypatch.setenv("MORTIS_RAG_CONFIG", str(tmp_path / "nope.toml"))
    assert "MORTIS_RAG_CONFIG" in _explicit_config_problem(None)


def test_list_requests_is_read_only_and_redacted(tmp_path: Path, capsys):
    ids = _seed_intents(tmp_path, "list")
    vault = _vault(tmp_path, "list")
    config = _config_file(tmp_path, "list")
    assert main(["--list-requests", "--vault", str(vault), "--app-config", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["count"] == 3
    states = {item["request_id"]: item["state"] for item in payload["requests"]}
    assert states == {ids["target"]: "prepared", ids["other"]: "prepared", ids["done"]: "success"}
    # 脱敏：只暴露 request_id/kind/state/attempt/next_action
    assert all(set(item) == {"request_id", "kind", "state", "attempt", "next_action"}
               for item in payload["requests"])
    assert all("endpoint" not in item and "payload_hash" not in item
               for item in payload["requests"])
    # 只读：状态没有变化
    control = ControlStore(_layout(tmp_path, "list"))
    control.open(create=False, write=False)
    try:
        assert control.intent_state(ids["target"]) == "prepared"
    finally:
        control.close()


def test_abandon_moves_only_the_target_record(tmp_path: Path, capsys):
    ids = _seed_intents(tmp_path, "abandon")
    vault = _vault(tmp_path, "abandon")
    config = _config_file(tmp_path, "abandon")
    assert main(["--abandon-request", ids["target"], "--vault", str(vault),
                 "--app-config", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["abandoned"] is True and payload["state"] == "abandoned"
    assert "repeat" in payload["warning"]

    control = ControlStore(_layout(tmp_path, "abandon"))
    control.open(create=False, write=False)
    try:
        assert control.intent_state(ids["other"]) == "prepared", "放弃不得波及其它记录"
        assert control.intent_state(ids["done"]) == "success", "终态不得被覆盖"
    finally:
        control.close()

    # 终态记录的 abandon 是明确的 no-op（不报错、不改动）
    assert main(["--abandon-request", ids["done"], "--vault", str(vault),
                 "--app-config", str(config)]) == 0
    second = json.loads(capsys.readouterr().out.strip())
    assert second["abandoned"] is False and second["state"] == "success"


def test_worker_retry_only_for_failed_or_cancelled(tmp_path: Path):
    vault = _vault(tmp_path, "retry")
    cfg = _app_config(tmp_path, "retry")
    layout = resolve_storage_layout(cfg, vault)
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    try:
        job, _ = store.enqueue_job(source="a.wav", source_sha256="s" * 64,
                                   parser_fingerprint="pcm")
        store.claim_job("A")
        store.fail_job(job.job_id, "A", error_code="PARSE_FAILED")
        worker = VirtualIngestWorker(vault, cfg, store_provider=lambda: store)
        retried = worker.retry(job.job_id)
        assert retried["retried"] is True and retried["state"] == "queued"
        assert store.job_status(job.job_id).state == "queued"

        # 活任务不可重试：明确返回原因，不抛到外层
        store.claim_job("B")
        live = worker.retry(job.job_id)
        assert live["retried"] is False and live["state"] == "parsing"
        assert "failed/cancelled" in live["next_action"]

        # unknown 远端结果：保状态/phase/remote_task_id，给 next action
        store.report_phase(job.job_id, "B", phase="submission_unknown",
                           remote_task_id="remote-42")
        store.fail_job(job.job_id, "B", error_code="SUBMISSION_UNKNOWN")
        unknown = worker.retry(job.job_id)
        assert unknown["retried"] is False
        assert unknown["state"] == "failed"
        assert unknown["remote_task_id"] == "remote-42"
        assert unknown["reason"] == "SUBMISSION_UNKNOWN"
        assert "do not resend" in unknown["next_action"]
    finally:
        store.close()


def test_server_instructions_do_not_trigger_auto_doctor():
    assert "若状态为 ❌，仅允许运行一次" not in SERVER_INSTRUCTIONS
    assert "自动运行" in SERVER_INSTRUCTIONS
    assert "用户明确要求" in SERVER_INSTRUCTIONS
