from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
from pathlib import Path

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "VAULT_MCP_REGISTRY": str(config.parent / "vaults.toml")},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_add_exemption_pattern_pruning_and_cleanup(tmp_path):
    """验证 F-02: 内存剪枝 + 向量库清理 + failed_files 弹出 + 缓存落盘。"""
    vault = tmp_path / "vault_exemption"
    vault.mkdir(parents=True)
    (vault / "public.md").write_text("# Public\n公开笔记内容", encoding="utf-8")
    (vault / "secret.md").write_text("# Secret\n私密笔记内容", encoding="utf-8")

    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)
    indexer.sync()

    # 预设状态：public.md 和 secret.md 均被索引
    assert "public.md" in indexer._chunks
    assert "secret.md" in indexer._chunks
    secret_chunk_ids = [c.id for c in indexer._chunks["secret.md"]]
    assert len(secret_chunk_ids) > 0

    # 模拟在 failed_files 中存在因故障记录的条目
    indexer.failed_files["secret_corrupt.md"] = "Parse error"
    assert "secret_corrupt.md" in indexer.failed_files

    # 执行豁免
    t0 = time.perf_counter()
    res = indexer.add_exemption_pattern("secret*")
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert res["success"] is True
    assert elapsed_ms < 100, f"豁免规则处理耗时过长: {elapsed_ms:.2f}ms"

    # 1. 内存即时剪枝
    assert "secret.md" not in indexer._chunks
    assert "public.md" in indexer._chunks
    assert "secret.md" not in indexer._signatures
    assert "secret.md" not in indexer._stat_cache

    # 2. failed_files 联动清理
    assert "secret_corrupt.md" not in indexer.failed_files

    # 3. 底层向量存储联动删除
    if hasattr(indexer._vector_backend, "list_ids"):
        backend_ids = set(indexer._vector_backend.list_ids())
        for cid in secret_chunk_ids:
            assert cid not in backend_ids, f"向量库中残留已豁免 chunk {cid}"

    # 4. 磁盘缓存同步更新，重启重建不复活
    reloaded_indexer = MarkdownIndexer(vault, config)
    # 重新加载缓存或状态
    stats = reloaded_indexer.stats()
    assert "secret.md" not in reloaded_indexer._chunks


def test_kb_read_decoupled_from_sync_lock(tmp_path):
    """验证 kb_read 只读磁盘原文，不被 _sync_lock 阻塞。"""
    vault = tmp_path / "vault_read"
    vault.mkdir(parents=True)
    (vault / "note.md").write_text("# Note Title\nLine 1\nLine 2\nLine 3\n", encoding="utf-8")

    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)

    # 人工持有 _sync_lock 模拟后台长耗时对账/嵌入
    indexer._sync_lock.acquire()
    try:
        t0 = time.perf_counter()
        # 直接调用 read，完全不等待 _sync_lock
        content = indexer.read("note.md", start_line=1, end_line=2)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert "Line 1" in content
        assert elapsed_ms < 50, f"kb_read 发生锁争用: {elapsed_ms:.2f}ms"
    finally:
        indexer._sync_lock.release()


def test_try_sync_with_guard_and_indexing_status(tmp_path):
    """验证 F-03/04: 锁被占用时 try_sync_with_guard 快速失败，kb_search 返回 indexing 状态。"""
    vault = tmp_path / "vault_guard"
    vault.mkdir(parents=True)
    (vault / "article.md").write_text("# 文章标题\n知识库正在冷启动构建中", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    # 创建一个 indexer 并锁住 _sync_lock 模拟冷启动耗时
    indexer = MarkdownIndexer(vault, AppConfig(vault_path=str(vault)))
    indexer._sync_lock.acquire()
    try:
        # try_sync_with_guard 应该在 0.2 秒内安全放弃，返回 False
        t0 = time.perf_counter()
        ok = indexer.try_sync_with_guard(timeout=0.2)
        elapsed = time.perf_counter() - t0
        assert ok is False
        assert 0.15 <= elapsed <= 0.6
    finally:
        indexer._sync_lock.release()


def test_kb_search_cold_start_progressive_feedback(tmp_path):
    """验证 stdio 下冷启动首检的渐进式反馈与防假死。"""
    vault = tmp_path / "vault_cold"
    vault.mkdir(parents=True)
    (vault / "data.md").write_text("# 知识文档\n重要资产内容", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    # 验证 stdio 正常启动并初始化
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "ColdVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "重要资产", "vault_path": "ColdVault"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_read", "arguments": {"source": "data.md", "vault_path": "ColdVault"}}},
    ]
    responses = _run_stdio(config, requests)

    # 验证 search 与 read 均正常完成返回
    for resp in responses:
        assert "error" not in resp, resp.get("error")

    r3 = json.loads(responses[2]["result"]["content"][0]["text"])
    assert len(r3["chunks"]) > 0

    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    assert "重要资产内容" in r4["content"]


def test_remove_exemption_pattern_is_non_blocking(tmp_path, monkeypatch):
    vault = tmp_path / "vault_remove"
    vault.mkdir(parents=True)
    (vault / "note.md").write_text("# Note\ncontent", encoding="utf-8")
    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)
    indexer.sync()
    indexer.add_exemption_pattern("note.md")

    def _fail_sync():
        raise AssertionError("Foreground sync should not be called")

    monkeypatch.setattr(indexer, "sync", _fail_sync)

    t0 = time.perf_counter()
    res = indexer.remove_exemption_pattern("note.md")
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert res["success"] is True
    assert res["removed"] is True
    assert res["sync"] == "background"
    assert elapsed_ms < 50, f"remove_exemption_pattern 耗时过长: {elapsed_ms:.2f}ms"


def test_set_file_exemption_is_non_blocking(tmp_path, monkeypatch):
    vault = tmp_path / "vault_set_exempt"
    vault.mkdir(parents=True)
    (vault / "note.md").write_text("# Note\ncontent", encoding="utf-8")
    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)
    indexer.sync()

    def _fail_sync():
        raise AssertionError("Foreground sync should not be called")

    monkeypatch.setattr(indexer, "sync", _fail_sync)

    # 1. exempt=True 方向
    t0 = time.perf_counter()
    res_true = indexer.set_file_exemption("note.md", exempt=True, method="frontmatter")
    elapsed_true_ms = (time.perf_counter() - t0) * 1000
    assert res_true["success"] is True
    assert res_true["sync"] == "background"
    assert elapsed_true_ms < 50, f"set_file_exemption(True) 耗时过长: {elapsed_true_ms:.2f}ms"

    # 2. exempt=False 方向
    t0 = time.perf_counter()
    res_false = indexer.set_file_exemption("note.md", exempt=False, method="frontmatter")
    elapsed_false_ms = (time.perf_counter() - t0) * 1000
    assert res_false["success"] is True
    assert res_false["sync"] == "background"
    assert elapsed_false_ms < 50, f"set_file_exemption(False) 耗时过长: {elapsed_false_ms:.2f}ms"


def test_prune_ignored_sources_clears_all_layers(tmp_path):
    vault = tmp_path / "vault_prune"
    vault.mkdir(parents=True)
    (vault / "doc1.md").write_text("# Doc 1\nalpha content", encoding="utf-8")
    (vault / "doc2.md").write_text("# Doc 2\nbeta content", encoding="utf-8")
    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)
    indexer.sync()

    assert "doc1.md" in indexer._chunks
    assert "doc2.md" in indexer._chunks
    doc1_chunk_ids = [c.id for c in indexer._chunks["doc1.md"]]
    assert len(doc1_chunk_ids) > 0

    # 模拟各类时序与观测状态
    indexer._signatures["doc1.md"] = (12345, 100, 12345)
    indexer._stat_cache["doc1.md"] = (12345, 100)
    indexer._stat_seen_ns["doc1.md"] = 123456789
    indexer._stat_confirmations["doc1.md"] = 1
    indexer.fast_path_warnings["doc1.md"] = "warning"
    indexer.failed_files["doc1.md"] = "previous error"

    from mortis_rag_mcp.indexer import IgnoreMatcher
    matcher = IgnoreMatcher(["doc1.md"])
    pruned = indexer._prune_ignored_sources(matcher)

    assert "doc1.md" in pruned
    assert "doc1.md" not in indexer._chunks
    assert "doc1.md" not in indexer._signatures
    assert "doc1.md" not in indexer._stat_cache
    assert "doc1.md" not in indexer._stat_seen_ns
    assert "doc1.md" not in indexer._stat_confirmations
    assert "doc1.md" not in indexer.fast_path_warnings
    assert "doc1.md" not in indexer.failed_files

    if hasattr(indexer._vector_backend, "list_ids"):
        backend_ids = set(indexer._vector_backend.list_ids())
        for cid in doc1_chunk_ids:
            assert cid not in backend_ids

    # doc2 保持完整
    assert "doc2.md" in indexer._chunks


def test_sync_progress_state_machine(tmp_path):
    """验证 P3-2: _sync_progress 状态机 phase 与 counts 在 scanning/fts/embedding/idle 间正确流转。"""
    vault = tmp_path / "vault_progress"
    vault.mkdir(parents=True)
    for i in range(5):
        (vault / f"file_{i}.md").write_text(f"# File {i}\nContent {i}", encoding="utf-8")

    config = AppConfig(vault_path=str(vault), embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(vault, config)

    # 1. 初始状态
    assert indexer._sync_state == "idle"
    assert indexer._sync_progress["phase"] == "idle"
    assert indexer._sync_progress["files_total"] == 0

    phases_seen = []
    original_markdown_files = indexer._markdown_files

    def _spy_markdown_files():
        phases_seen.append((indexer._sync_state, dict(indexer._sync_progress)))
        return original_markdown_files()

    indexer._markdown_files = _spy_markdown_files

    indexer.sync()

    # 2. 验证进入 _sync_locked 时已初始化并在 scanning 阶段，且重置了 progress
    assert len(phases_seen) == 1
    state, prog = phases_seen[0]
    assert state == "scanning"
    assert prog["phase"] == "scanning"
    assert prog["files_done"] == 0
    assert prog["files_total"] == 0

    # 3. 完成后恢复为 idle
    assert indexer._sync_state == "idle"
    assert indexer._sync_progress["phase"] == "idle"
    assert indexer._sync_progress["files_total"] == 5
    assert indexer._sync_progress["files_done"] == 5


