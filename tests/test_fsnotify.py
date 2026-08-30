from __future__ import annotations

import struct
import sys
import threading
import time

import pytest

from vault_mcp.fsnotify import (
    FILE_ACTION_ADDED,
    FILE_ACTION_MODIFIED,
    WindowsDirectoryWatcher,
    parse_notify_buffer,
    watcher_available,
)


def _entry(action: int, name: str, next_offset: int) -> bytes:
    """手工构造一条 FILE_NOTIFY_INFORMATION。

    FileNameLength 的单位是**字节**（文件名按 UTF-16LE 编码），NextEntryOffset
    是从本条开头到下一条开头的字节偏移，0 表示链表结束。
    """
    encoded = name.encode("utf-16-le")
    return struct.pack("<III", next_offset, action, len(encoded)) + encoded


def test_parse_notify_buffer_two_entries():
    first = _entry(FILE_ACTION_ADDED, "a.md", 20)
    second = _entry(FILE_ACTION_MODIFIED, "b.md", 0)
    assert len(first) == 20  # 12 字节头 + "a.md" 的 8 字节 UTF-16LE
    buffer = first + second
    assert parse_notify_buffer(buffer) == [
        (FILE_ACTION_ADDED, "a.md"),
        (FILE_ACTION_MODIFIED, "b.md"),
    ]


def test_parse_notify_buffer_empty():
    assert parse_notify_buffer(b"") == []


def test_parse_notify_buffer_truncated_tail_is_ignored():
    """最后一条声明的 FileNameLength 超出缓冲区实际长度 -> 丢弃它，保留前面的。"""
    first = _entry(FILE_ACTION_ADDED, "a.md", 20)
    # 声明 100 字节文件名，实际只给了 4 字节。
    truncated = struct.pack("<III", 0, FILE_ACTION_MODIFIED, 100) + b"\x00\x00\x00\x00"
    assert parse_notify_buffer(first + truncated) == [(FILE_ACTION_ADDED, "a.md")]


def test_parse_notify_buffer_short_header_is_ignored():
    """不足 12 字节头部的残片直接被忽略，不抛异常。"""
    assert parse_notify_buffer(b"\x00" * 8) == []


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows platforms only")
def test_watcher_available_is_false_off_windows():
    assert watcher_available() is False


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows platforms only")
def test_start_is_false_off_windows(tmp_path):
    watcher = WindowsDirectoryWatcher(tmp_path, lambda events: None)
    assert watcher.start() is False
    assert watcher.is_alive() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_start_returns_false_for_missing_directory(tmp_path):
    """路径不存在 -> 句柄打不开 -> 返回 False，调用方据此降级回轮询。"""
    missing = tmp_path / "does-not-exist"
    watcher = WindowsDirectoryWatcher(missing, lambda events: None)
    assert watcher.start() is False
    assert watcher.is_alive() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_watcher_reports_real_file_creation(tmp_path):
    """真实集成：在监听目录里新建文件，回调必须收到包含该文件名的事件。"""
    watched = tmp_path / "vault"
    watched.mkdir()

    seen: list[tuple[int, str]] = []
    arrived = threading.Event()

    def on_events(events):
        if events is None:
            return
        seen.extend(events)
        if any(name == "new.md" for _action, name in events):
            arrived.set()

    watcher = WindowsDirectoryWatcher(watched, on_events)
    try:
        assert watcher.start() is True
        assert watcher.is_alive() is True

        # 等监听线程真正阻塞进 ReadDirectoryChangesW 再动手，否则改动可能落在
        # 调用之前被漏掉（这是所有目录监听器的固有竞态）。
        time.sleep(0.3)
        (watched / "new.md").write_text("# hello\n", encoding="utf-8")
        assert arrived.wait(timeout=5), f"没有收到 new.md 的事件，收到的是: {seen}"
        assert any(name == "new.md" for _action, name in seen)
        assert any(action == FILE_ACTION_ADDED for action, _name in seen)
    finally:
        watcher.stop()

    # stop() 后线程应在 2 秒内退出。
    assert watcher.is_alive() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_stop_is_idempotent(tmp_path):
    watcher = WindowsDirectoryWatcher(tmp_path, lambda events: None)
    assert watcher.start() is True
    watcher.stop()
    watcher.stop()
    assert watcher.is_alive() is False
