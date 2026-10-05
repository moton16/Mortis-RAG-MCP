from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig, load_config
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.registry import VaultRegistry
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def test_foreground_search_returns_immediately_while_sync_blocked(tmp_path: Path):
    """C66: 当后台 sync 人为缓慢时，前台 search 必须直接返回已有索引结果，不等待 sync 完成。"""
    vault = tmp_path / "vault_slow_sync"
    vault.mkdir()
    (vault / "note.md").write_text("# Target\nfast search content\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="SlowVault")

    idx = server._indexer_for({"vault_path": "SlowVault"})
    idx.sync()  # 先预热好索引
    assert len(idx.all_chunks()) > 0

    # 用 Event 卡住 _sync_locked
    sync_blocker = threading.Event()
    sync_started = threading.Event()
    orig_sync_locked = idx._sync_locked

    def slow_sync_locked(*args, **kwargs):
        sync_started.set()
        sync_blocker.wait(timeout=5)
        return orig_sync_locked(*args, **kwargs)

    idx._sync_locked = slow_sync_locked

    try:
        # 触发一次后台刷新
        idx.request_refresh(immediate=True)
        assert sync_started.wait(timeout=2), "后台 sync 未按时启动"

        # 前台立即执行 search：必须在 sync_blocker 释放前快速返回
        t0 = time.perf_counter()
        res = server.call_tool("kb_search", {"query": "Target", "vault_path": "SlowVault"})
        elapsed = time.perf_counter() - t0

        assert elapsed < 1.0, f"前台 search 被阻塞，耗时: {elapsed:.3f}s"
        content = json.loads(res["content"][0]["text"])
        assert len(content["chunks"]) > 0
        assert content["chunks"][0]["source"] == "note.md"
        assert content.get("indexing_in_progress") is True
    finally:
        sync_blocker.set()
        server.shutdown()


def test_foreground_read_returns_immediately_while_sync_lock_held(tmp_path: Path):
    """C66: 当 _sync_lock 被占有时，kb_read 原文必须直接读取磁盘，不等待锁。"""
    vault = tmp_path / "vault_read_lock"
    vault.mkdir()
    (vault / "doc.md").write_text("# Doc\nline 1\nline 2\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ReadVault")
    idx = server._indexer_for({"vault_path": "ReadVault"})
    idx.sync()

    # 人为持有 _sync_lock
    assert idx._sync_lock.acquire(timeout=1.0)
    try:
        t0 = time.perf_counter()
        res = server.call_tool("kb_read", {"source": "doc.md", "vault_path": "ReadVault"})
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, f"kb_read 耗时过长: {elapsed:.3f}s"
        content = json.loads(res["content"][0]["text"])
        assert "line 1" in content["content"]
    finally:
        idx._sync_lock.release()
        server.shutdown()


def test_cold_search_returns_indexing_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C66: 首次无缓存冷启动时，返回 status=indexing 与 retry_after=3。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    vault = tmp_path / "vault_cold"
    vault.mkdir()
    (vault / "note.md").write_text("# Cold\ncontent\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ColdVault")

    res = server.call_tool("kb_search", {"query": "Cold", "vault_path": "ColdVault"})
    content = json.loads(res["content"][0]["text"])
    assert content.get("status") == "indexing"
    assert content.get("retry_after") == 3
    assert content.get("chunks") == []
    server.shutdown()


def test_request_refresh_coalesces_bursts(tmp_path: Path):
    """C66: 高频调用 request_refresh 仅复用常驻调度线程，并在单次 sync 期间合并请求。"""
    vault = tmp_path / "vault_coalesce"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\ncontent\n", encoding="utf-8")

    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)
    indexer.sync()

    before_threads = {t.ident for t in threading.enumerate() if t.name == "vault-fs-debounce"}

    # 连续调用 100 次 request_refresh
    for _ in range(100):
        ok = indexer.request_refresh()
        assert ok is True

    # 调度线程应当只有一个存活
    new_threads = [t for t in threading.enumerate() if t.name == "vault-fs-debounce" and t.ident not in before_threads]
    assert len(new_threads) == 1
    assert indexer._fs_scheduler_thread is not None
    assert indexer._fs_scheduler_thread.is_alive()
    indexer.stop_watching()


def test_stale_chunk_id_signature_mismatch_fails_visible(tmp_path: Path):
    """C66: 物理文件修改后若 chunk_id 的 sha256 签名与磁盘不符，fail-visible 提示重新 kb_search。"""
    vault = tmp_path / "vault_stale_chunk"
    vault.mkdir()
    note = vault / "stale.md"
    note.write_text("# Original\noriginal content here\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="StaleVault")

    idx = server._indexer_for({"vault_path": "StaleVault"})
    idx.sync()
    chunks = idx.all_chunks()
    assert len(chunks) > 0
    cid = chunks[0].id

    # 篡改磁盘文件内容，模拟文件更新而索引尚未刷新的窗口期
    note.write_text("# Modified\ncompletely different text line 1\nline 2\n", encoding="utf-8")

    try:
        with pytest.raises(ValueError) as exc_info:
            server._kb_read({"chunk_id": cid, "vault_path": "StaleVault"})
        msg = str(exc_info.value)
        assert "stale" in msg
        assert "签名不一致" in msg or "已修改" in msg
        assert "kb_search" in msg
    finally:
        server.shutdown()


def test_ingest_a4_callback_triggers_immediate_refresh(tmp_path: Path):
    """C66 / A4: worker 完成回调传 (source, out_md) 必须触发 indexer.request_refresh(immediate=True)。"""
    vault = tmp_path / "vault_ingest_a4"
    vault.mkdir()

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n[ingest]\nenabled = true\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="A4Vault")

    indexer = server._indexer_for({"vault_path": "A4Vault"})
    refreshed_immediate = False

    def spy_request_refresh(*args, **kwargs):
        nonlocal refreshed_immediate
        if kwargs.get("immediate"):
            refreshed_immediate = True
        return True

    indexer.request_refresh = spy_request_refresh

    manager = server._ingest_manager_for(str(vault))
    assert manager.on_job_finished is not None

    out_file = vault / ".mortis-parsed" / "doc.md"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("# Parsed Doc\n", encoding="utf-8")

    try:
        # 调用回调，验证接收双参数且调用 request_refresh(immediate=True)
        manager.on_job_finished("doc.pdf", out_file)
        assert refreshed_immediate is True
    finally:
        server.shutdown()


def test_lifecycle_stop_and_shutdown_idempotent(tmp_path: Path):
    """C66: 停止后拒绝 request_refresh，shutdown 多次调用幂等且清理调度线程。"""
    vault = tmp_path / "vault_lifecycle"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="LifeVault")

    indexer = server._indexer_for({"vault_path": "LifeVault"})
    indexer.request_refresh()

    indexer.stop_watching()
    # 停止后应拒绝新的刷新请求
    assert indexer.request_refresh() is False

    # server shutdown 多次幂等
    server.shutdown()
    server.shutdown()
