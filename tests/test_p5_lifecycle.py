from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import zipfile

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig
from mortis_rag_mcp.indexer import MarkdownIndexer, Chunk, IgnoreMatcher
from mortis_rag_mcp._indexer import snapshot as _snapshot
from mortis_rag_mcp._indexer import exemptions as _exemptions
from mortis_rag_mcp._indexer import watch as _watch


def test_snapshot_zip_slip_interception(tmp_path: Path):
    """验证快照导入严格防御路径穿越（Zip Slip）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "test.md").write_text("# Hello\nWorld\n", encoding="utf-8")

    cfg = AppConfig(cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True))
    indexer = MarkdownIndexer(vault, cfg)
    indexer.sync()

    # 构造一个含 Zip Slip 成员的恶意 zip
    bad_zip = tmp_path / "evil_slip.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../evil.txt", "attack")
        zf.writestr("manifest.json", json.dumps({
            "format": _snapshot._SNAPSHOT_FORMAT,
            "format_version": _snapshot._SNAPSHOT_VERSION,
        }))
        zf.writestr("chunks.bin", b"fake")

    with pytest.raises(ValueError, match="unexpected members"):
        indexer.import_snapshot(bad_zip)

    # 构造另一个含绝对路径成员的恶意 zip
    bad_zip_abs = tmp_path / "evil_abs.zip"
    with zipfile.ZipFile(bad_zip_abs, "w") as zf:
        zf.writestr("/evil.txt", "attack")
        zf.writestr("manifest.json", json.dumps({
            "format": _snapshot._SNAPSHOT_FORMAT,
            "format_version": _snapshot._SNAPSHOT_VERSION,
        }))
        zf.writestr("chunks.bin", b"fake")

    with pytest.raises(ValueError, match="unexpected members"):
        indexer.import_snapshot(bad_zip_abs)


def test_exemptions_lifecycle_and_cascade_prune(tmp_path: Path):
    """验证豁免增删返回值契约与级联清理 8 项状态。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    doc_a = vault / "keep.md"
    doc_a.write_text("# Keep\nNormal content\n", encoding="utf-8")
    doc_b = vault / "secret.md"
    doc_b.write_text("# Secret\nClassified\n", encoding="utf-8")

    cfg = AppConfig()
    indexer = MarkdownIndexer(vault, cfg)
    indexer.sync()

    # 确认初始状态已索引两个文件
    assert "keep.md" in indexer._chunks
    assert "secret.md" in indexer._chunks
    assert "secret.md" in indexer._signatures
    assert "secret.md" in indexer._stat_cache
    assert "secret.md" in indexer._stat_seen_ns

    # 添加豁免规则
    res = indexer.add_exemption_pattern("secret.md")
    assert res is not None
    assert res["success"] is True
    assert res["action"] == "add_pattern"
    assert res["pattern"] == "secret.md"
    assert res["pruned_files_count"] == 1
    assert res["sync"] == "background"

    # 级联清理状态断言
    assert "secret.md" not in indexer._chunks
    assert "secret.md" not in indexer._signatures
    assert "secret.md" not in indexer._stat_cache
    assert "secret.md" not in indexer._stat_seen_ns
    assert "secret.md" not in indexer._stat_confirmations
    assert "secret.md" not in indexer.failed_files
    if hasattr(indexer, "fast_path_warnings"):
        assert "secret.md" not in indexer.fast_path_warnings

    # 查验 check_exemption
    check = indexer.check_exemption("secret.md")
    assert check["is_exempt"] is True
    assert "secret.md" in check["reason"]

    # 移除豁免规则
    res_rm = indexer.remove_exemption_pattern("secret.md")
    assert res_rm["success"] is True
    assert res_rm["removed"] is True
    assert res_rm["sync"] == "background"


def test_watcher_lifecycle_graceful_stop(tmp_path: Path):
    """验证 watcher 启动与优雅停止，无孤儿线程残留。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent\n", encoding="utf-8")

    cfg = AppConfig()
    indexer = MarkdownIndexer(vault, cfg)

    # 启动轮询/原生 watcher
    indexer.start_watching(interval=0.1, debounce_seconds=0.1)

    # 获取本实例创建的 watcher 和 scheduler 线程引用
    watch_thread = indexer._watch_thread
    scheduler_thread = indexer._fs_scheduler_thread

    assert watch_thread is not None and watch_thread.is_alive()
    assert scheduler_thread is not None and scheduler_thread.is_alive()
    assert watch_thread.name in {"vault-watch-native", "Thread-", ""} or "watch" in watch_thread.name
    assert scheduler_thread.name == "vault-fs-debounce"

    # 等待首轮初始化 sync 稳定
    deadline = time.time() + 3.0
    while time.time() < deadline and getattr(indexer, "_indexing", False):
        time.sleep(0.05)

    # 停止 watcher
    indexer.stop_watching()

    # join 确认收敛
    if watch_thread is not None:
        watch_thread.join(timeout=3.0)
    if scheduler_thread is not None:
        scheduler_thread.join(timeout=3.0)

    assert indexer._fs_watcher is None
    assert indexer._watch_thread is None or not indexer._watch_thread.is_alive()
    assert indexer._fs_scheduler_thread is None or not indexer._fs_scheduler_thread.is_alive()
    assert not watch_thread.is_alive()
    assert not scheduler_thread.is_alive()
