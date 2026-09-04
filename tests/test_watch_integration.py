"""C7-B：原生目录监听（fsnotify）接入 indexer 的集成行为。

native 路径仅 Windows 可用（非 Windows 平台自动退回轮询，watcher_available()
为 False 的断言会自动适配）。所有测试都在 finally 里 stop_watching，避免目录
句柄泄漏影响 pytest 临时目录的清理。
"""

from __future__ import annotations

import time
from pathlib import Path

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
