"""C7-B：原生目录监听（fsnotify）接入 indexer 的集成行为。

native 路径仅 Windows 可用（非 Windows 平台自动退回轮询，watcher_available()
为 False 的断言会自动适配）。所有测试都在 finally 里 stop_watching，避免目录
句柄泄漏影响 pytest 临时目录的清理。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig, load_config
from mortis_rag_mcp.fsnotify import watcher_available
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.providers import StaticEmbeddingProvider


def _config(tmp_path: Path, **kwargs) -> AppConfig:
    kwargs.setdefault("watch_method", "auto")
    kwargs.setdefault("cache", CacheConfig(dir=str(tmp_path / "cache"), enabled=True))
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        **kwargs,
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_config_defaults_and_validation():
    config = AppConfig()
    assert config.watch_method == "auto"
    assert config.watch_fallback_interval == 30.0
    with pytest.raises(ValueError):
        AppConfig(watch_method="inotify")
    with pytest.raises(ValueError):
        AppConfig(watch_fallback_interval=-1.0)
    # 0 是合法值：表示关闭兜底对账，只保留事件驱动。
    assert AppConfig(watch_fallback_interval=0).watch_fallback_interval == 0.0


def test_load_config_accepts_watch_keys(tmp_path: Path):
    toml = tmp_path / "app.toml"
    toml.write_text(
        '[index]\nwatch_method = "poll"\nwatch_fallback_interval = 5.5\n',
        encoding="utf-8",
    )
    config = load_config(toml)
    assert config.watch_method == "poll"
    assert config.watch_fallback_interval == 5.5


def test_poll_method_keeps_legacy_behavior(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    indexer = MarkdownIndexer(
        vault, _config(tmp_path, watch_method="poll"), embedding_provider=StaticEmbeddingProvider(dimension=8)
    )
    indexer.start_watching(debounce_seconds=0.1)
    try:
        # 显式 poll：永远不走原生监听。
        assert indexer._fs_watcher is None
        (vault / "b.md").write_text("# B\nworld", encoding="utf-8")
        assert _wait_until(lambda: any(c.source == "b.md" for c in indexer.all_chunks()))
    finally:
        indexer.stop_watching()


@pytest.mark.skipif(not watcher_available(), reason="native watcher 仅 Windows 可用")
def test_native_watcher_triggers_sync(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))
    indexer.start_watching(debounce_seconds=0.1)
    try:
        # 前置：auto 模式确实走上了原生监听。
        assert indexer._fs_watcher is not None

        # 新增文件。
        (vault / "b.md").write_text("# B\nworld", encoding="utf-8")
        assert _wait_until(lambda: any(c.source == "b.md" for c in indexer.all_chunks()))

        # 已有文件的内容修改同样要被看到。
        (vault / "a.md").write_text("# A\nhello v2", encoding="utf-8")
        assert _wait_until(lambda: any(c.source == "a.md" and "v2" in c.content for c in indexer.all_chunks()))

        # 子目录里的新文件（bWatchSubdirectory=True）。
        sub = vault / "sub"
        sub.mkdir()
        (sub / "c.md").write_text("# C\nnested", encoding="utf-8")
        assert _wait_until(lambda: any(c.source == "sub/c.md" for c in indexer.all_chunks()))
    finally:
        indexer.stop_watching()

    assert indexer._fs_watcher is None
    assert not (indexer._watch_thread and indexer._watch_thread.is_alive())


@pytest.mark.skipif(not watcher_available(), reason="native watcher 仅 Windows 可用")
def test_native_watcher_used_for_native_method_and_fallback_on_non_windows(tmp_path: Path):
    """method="native"：平台可用时走原生，不可用时退回轮询，绝不抛异常。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    indexer = MarkdownIndexer(vault, _config(tmp_path, watch_method="native"), embedding_provider=StaticEmbeddingProvider(dimension=8))
    indexer.start_watching(debounce_seconds=0.1)
    try:
        if watcher_available():
            assert indexer._fs_watcher is not None
        else:
            assert indexer._fs_watcher is None
    finally:
        indexer.stop_watching()


@pytest.mark.skipif(not watcher_available(), reason="native watcher 仅 Windows 可用")
def test_no_sync_feedback_loop_with_vault_placement(tmp_path: Path):
    """placement=vault 时缓存写在库内：每次 sync 写缓存都会再触发一个文件事件。

    「无变化不重写缓存」的脏标记必须让这个反馈环收敛，否则原生监听会以
    debounce 为周期自激空转。
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    config = _config(
        tmp_path,
        watch_method="auto",
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, placement="vault"),
    )
    indexer = MarkdownIndexer(vault, config, embedding_provider=StaticEmbeddingProvider(dimension=8))

    calls = {"n": 0}
    original_sync = indexer.sync

    def counting_sync():
        calls["n"] += 1
        return original_sync()

    indexer.sync = counting_sync  # type: ignore[method-assign]
    indexer.start_watching(debounce_seconds=0.1)
    try:
        assert _wait_until(lambda: calls["n"] >= 1)
        (vault / "a.md").write_text("# A\nhello v2", encoding="utf-8")
        assert _wait_until(lambda: any("v2" in c.content for c in indexer.all_chunks()))

        # 变更被消化后，最多再有一次消化缓存写入的“幻影 sync”，然后必须停。
        time.sleep(1.5)
        stable = calls["n"]
        time.sleep(1.5)
        assert calls["n"] == stable
    finally:
        indexer.stop_watching()


def test_save_cache_skipped_when_nothing_changed(tmp_path: Path):
    """无变化的 sync 不再重写缓存文件（原生监听收敛的前提，也是纯省 IO）。"""
    (tmp_path / "a.md").write_text("# A\nhello", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))
    indexer.sync()
    chunks_path = indexer._chunks_cache_path
    assert chunks_path is not None and chunks_path.exists()
    first_mtime = chunks_path.stat().st_mtime_ns

    time.sleep(0.02)  # NTFS mtime 精度内保证可分辨
    indexer.sync()
    assert chunks_path.stat().st_mtime_ns == first_mtime

    # 有变化时照常落盘。
    (tmp_path / "a.md").write_text("# A\nhello v2", encoding="utf-8")
    indexer.sync()
    assert chunks_path.stat().st_mtime_ns != first_mtime


def test_pure_pdf_event_does_not_trigger_text_sync(tmp_path: Path):
    """Pure PDF events trigger ingest scan hook but do NOT trigger text sync."""
    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))

    hook_called = threading.Event()
    indexer._ingest_hook = lambda: hook_called.set()

    try:
        indexer._on_fs_events([(1, "document.pdf")])
        assert hook_called.wait(timeout=2.0)
        with indexer._fs_debounce_lock:
            assert indexer._fs_requested is False
    finally:
        indexer.stop_watching()


def test_pure_text_event_does_not_trigger_ingest_scan(tmp_path: Path):
    """Pure markdown/text events trigger text sync but do NOT trigger ingest hook."""
    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))

    hook_called = threading.Event()
    indexer._ingest_hook = lambda: hook_called.set()

    try:
        indexer._on_fs_events([(1, "notes.md")])
        with indexer._fs_debounce_lock:
            assert indexer._fs_requested is True
        assert not hook_called.wait(timeout=0.3)
    finally:
        indexer.stop_watching()


def test_mixed_and_indeterminate_events_trigger_both(tmp_path: Path):
    """Mixed events and indeterminate events (events=None / dir rename) trigger both."""
    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))

    hook_count = {"count": 0}
    hook_cv = threading.Condition()

    def hook():
        with hook_cv:
            hook_count["count"] += 1
            hook_cv.notify_all()

    indexer._ingest_hook = hook

    try:
        # Mixed events
        indexer._on_fs_events([(1, "doc.pdf"), (1, "note.md")])
        with hook_cv:
            hook_cv.wait_for(lambda: hook_count["count"] >= 1, timeout=2.0)
        with indexer._fs_debounce_lock:
            assert indexer._fs_requested is True
            indexer._fs_requested = False

        # Indeterminate events (events=None)
        indexer._on_fs_events(None)
        with hook_cv:
            hook_cv.wait_for(lambda: hook_count["count"] >= 2, timeout=2.0)
        with indexer._fs_debounce_lock:
            assert indexer._fs_requested is True
    finally:
        indexer.stop_watching()


def test_ingest_scan_coalescing_burst(tmp_path: Path):
    """Bursts of 100 ingest events coalesce without spawning multiple worker threads."""
    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))

    call_count = {"count": 0}
    finished = threading.Event()

    def slow_hook():
        call_count["count"] += 1
        time.sleep(0.05)
        finished.set()

    indexer._ingest_hook = slow_hook

    try:
        for _ in range(100):
            indexer.request_ingest_scan()

        assert finished.wait(timeout=5.0)
        assert indexer._ingest_worker_thread is not None
        assert indexer._ingest_worker_thread.name == "vault-ingest-scan"
        time.sleep(0.2)
        assert call_count["count"] < 10
    finally:
        indexer.stop_watching()


def test_slow_ingest_hook_does_not_block_text_sync(tmp_path: Path):
    """A slow/blocking ingest hook runs on its own thread and never stalls text sync."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nInitial", encoding="utf-8")
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))

    hook_entered = threading.Event()
    release_hook = threading.Event()

    def blocking_hook():
        hook_entered.set()
        release_hook.wait(timeout=10.0)

    indexer._ingest_hook = blocking_hook

    try:
        indexer.request_ingest_scan()
        assert hook_entered.wait(timeout=2.0)

        indexer.start_watching(interval=0.05, debounce_seconds=0.05)
        (vault / "note.md").write_text("# Note\nUpdated Content", encoding="utf-8")
        indexer.request_refresh(immediate=True)

        assert _wait_until(lambda: any("Updated Content" in c.content for c in indexer.all_chunks()), timeout=3.0)
    finally:
        release_hook.set()
        indexer.stop_watching()


def test_poll_watch_loop_periodic_ingest_scan(tmp_path: Path):
    """In poll mode with fallback_interval=0, ingest scan still triggers on startup and cadence."""
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _config(tmp_path, watch_method="poll", watch_fallback_interval=0.0)
    indexer = MarkdownIndexer(vault, cfg, embedding_provider=StaticEmbeddingProvider(dimension=8))

    scan_requests = {"count": 0}

    def counting_scan():
        scan_requests["count"] += 1
        return True

    indexer.request_ingest_scan = counting_scan  # type: ignore[method-assign]

    try:
        indexer.start_watching(interval=0.05, debounce_seconds=0.05)
        assert _wait_until(lambda: scan_requests["count"] >= 1, timeout=2.0)
    finally:
        indexer.stop_watching()


def test_stop_watching_gracefully_terminates_ingest_worker(tmp_path: Path):
    """stop_watching terminates vault-ingest-scan worker without leaking threads."""
    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=StaticEmbeddingProvider(dimension=8))

    indexer._ingest_hook = lambda: None
    indexer.start_watching(interval=0.1, debounce_seconds=0.1)
    indexer.request_ingest_scan()

    try:
        assert _wait_until(lambda: indexer._ingest_worker_thread is not None and indexer._ingest_worker_thread.is_alive())
        worker_thread = indexer._ingest_worker_thread
        assert worker_thread.name == "vault-ingest-scan"

        indexer.stop_watching()
        worker_thread.join(timeout=2.0)
        assert not worker_thread.is_alive()
        assert indexer._ingest_worker_thread is None
        assert indexer.request_ingest_scan() is False
    finally:
        indexer.stop_watching()


def test_auto_method_degrades_to_poll_off_windows_with_ingest_scan(tmp_path: Path):
    """When native watcher is unavailable (simulating Linux/macOS), auto degrades to poll and keeps ingest scan."""
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _config(tmp_path, watch_method="auto")
    indexer = MarkdownIndexer(vault, cfg, embedding_provider=StaticEmbeddingProvider(dimension=8))

    scan_requests = {"count": 0}

    def counting_scan():
        scan_requests["count"] += 1
        return True

    indexer.request_ingest_scan = counting_scan  # type: ignore[method-assign]

    with patch("mortis_rag_mcp._indexer.watch.watcher_available", return_value=False):
        indexer.start_watching(interval=0.05, debounce_seconds=0.05)
        try:
            assert indexer._fs_watcher is None
            assert indexer._watch_thread is not None and indexer._watch_thread.is_alive()
            assert _wait_until(lambda: scan_requests["count"] >= 1, timeout=2.0)
        finally:
            indexer.stop_watching()

