"""E04-a：刷新登记机制（NEW）。

- `request_refresh` 返回接受必须**确实**保留 pending（合并窗口不得伪接受）；
- sync 执行期间/清 flag 之后的再次请求不得丢失；
- import 在释放 mutation 之后登记刷新（此前从不登记）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from mortis_rag_mcp._indexer import watch
from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def _config(tmp_path: Path, dimension: int = 8) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=dimension),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
    )


def _write_notes(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "a.md").write_text("# 甲\n\n第一份笔记的内容。\n", encoding="utf-8")
    (vault / "sub").mkdir(parents=True, exist_ok=True)
    (vault / "sub" / "b.md").write_text("# 乙\n\n子目录里的第二份笔记。\n", encoding="utf-8")


def _indexer(tmp_path: Path, name: str) -> MarkdownIndexer:
    vault = tmp_path / name / "vault"
    _write_notes(vault)
    return MarkdownIndexer(vault, _config(tmp_path / name))


def test_request_refresh_keeps_pending_inside_merge_window(tmp_path: Path):
    """合并窗口内返回 True 却不清 pending —— 接受必须等于"已安排下一轮"。"""
    indexer = _indexer(tmp_path, "A")
    try:
        indexer._last_refresh_completed_at = time.monotonic()  # 刚完成 → 处于合并窗口
        indexer._fs_requested = False
        indexer._fs_pending_since = None
        assert watch.request_refresh(indexer, start_scheduler=False) is True
        assert indexer._fs_requested is True, "返回接受但没有保留 pending（刷新被丢弃）"
        assert indexer._fs_pending_since is not None
    finally:
        indexer.stop_watching()


def test_stopped_or_missing_vault_does_not_fake_acceptance(tmp_path: Path):
    indexer = _indexer(tmp_path, "B")
    try:
        indexer._watch_stop.set()
        assert watch.request_refresh(indexer, start_scheduler=False) is False
        assert indexer._fs_requested is False
    finally:
        indexer.stop_watching()


def test_request_arriving_during_sync_schedules_another_round(tmp_path: Path):
    """调度器清 flag 后进入 sync；sync 期间的新请求必须安排下一轮（Event，不 sleep 猜时序）。"""
    indexer = _indexer(tmp_path, "C")
    sync_started = threading.Event()
    release = threading.Event()
    second_round = threading.Event()
    calls: list[int] = []

    def _fake_sync() -> None:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            sync_started.set()
            release.wait(10)
            # sync 执行期间又来一次刷新请求（调度器此刻已清 flag）
            watch.request_refresh(indexer, start_scheduler=False)
        else:
            second_round.set()

    indexer.sync = _fake_sync  # type: ignore[method-assign]
    try:
        watch._start_fs_scheduler(indexer)
        assert watch.request_refresh(indexer) is True
        assert sync_started.wait(10), "调度器没有消费已登记的刷新"
        release.set()
        assert second_round.wait(10), "sync 期间的再次请求被丢弃"
        assert len(calls) >= 2
    finally:
        indexer.stop_watching()


def test_import_registers_refresh_after_releasing_lock(tmp_path: Path):
    source = _indexer(tmp_path, "src")
    try:
        source.sync()
        snapshot = tmp_path / "src" / "snapshot.zip"
        assert source.export_snapshot(snapshot)["exported"] is True
    finally:
        source.stop_watching()

    target = _indexer(tmp_path, "dst")
    try:
        target._fs_requested = False
        imported = target.import_snapshot(snapshot)
        assert imported["imported"] is True
        assert imported.get("refresh_requested") is True, "导入完成未登记刷新"
        assert target._fs_requested is True, "接受刷新但没有保留 pending"
        # 登记的刷新确实能把文本层从本机物理源重建回来
        target.sync()
        assert len(target.all_chunks()) >= 2
    finally:
        target.stop_watching()
