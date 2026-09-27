"""P3c sync_engine 专项与契约测试。

契约与安全闸门：
1. 状态同一性：run_sync 必须在 owner._chunks 等实例属性上就地变异，
   owner._chunks 字典对象同一性（is）不发生改变；
2. 锁序断言：锁获取顺序严格固定为 _sync_lock -> _cache_lock，绝不允许逆序或单独取 _cache_lock；
3. 线程名称锁：线程名必须严格维持 "vault-init" / "vault-emb" / "exempt-sync" / "vault-watch-native" / "vault-fs-debounce"；
4. Facade 薄委托：_sync_locked_impl / _chunk_has_vector / _flush_vectors_to_disk 等委托调用正常。
"""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer, Chunk
import mortis_rag_mcp._indexer.sync_engine as sync_engine


class LockSpy:
    """包装 threading.Lock 以记录获取/释放序列与时间戳。"""

    def __init__(self, name: str, real_lock: Any, events: list[tuple[str, str]]):
        self._name = name
        self._real_lock = real_lock
        self._events = events

    def acquire(self, *args, **kwargs):
        res = self._real_lock.acquire(*args, **kwargs)
        if res:
            self._events.append((self._name, "acquire"))
        return res

    def release(self):
        self._events.append((self._name, "release"))
        return self._real_lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def locked(self):
        return self._real_lock.locked()


def test_sync_engine_modifies_owner_chunks_in_place(tmp_path):
    """断言 sync_engine 在 owner._chunks 字典上就地变异，对象同一性保持不变。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note 1\n\nContent 1\n", encoding="utf-8")

    indexer = MarkdownIndexer(
        vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    )
    original_chunks_dict = indexer._chunks
    original_stat_cache = indexer._stat_cache

    chunks = indexer.sync()

    assert indexer._chunks is original_chunks_dict, "owner._chunks 字典对象同一性被破坏"
    assert indexer._stat_cache is original_stat_cache, "owner._stat_cache 字典对象同一性被破坏"
    assert "note.md" in indexer._chunks
    assert len(chunks) == 1
    assert chunks[0].title == "Note 1"


def test_lock_acquisition_order_sync_before_cache(tmp_path):
    """断言锁获取顺序严格为 _sync_lock -> _cache_lock，绝无逆序。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\nContent\n", encoding="utf-8")

    indexer = MarkdownIndexer(
        vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    )

    lock_events: list[tuple[str, str]] = []
    indexer._sync_lock = LockSpy("sync_lock", indexer._sync_lock, lock_events)
    indexer._cache_lock = LockSpy("cache_lock", indexer._cache_lock, lock_events)

    indexer.sync()

    # 验证事件序列中，任何 cache_lock.acquire 发生时，sync_lock 必须处于持有状态
    sync_lock_held = False
    cache_lock_held = False
    saw_cache_lock = False

    for name, action in lock_events:
        if name == "sync_lock":
            if action == "acquire":
                sync_lock_held = True
            elif action == "release":
                sync_lock_held = False
        elif name == "cache_lock":
            if action == "acquire":
                saw_cache_lock = True
                assert sync_lock_held, f"逆序或裸锁违规：获取 cache_lock 时 sync_lock 未被持有！"
                cache_lock_held = True
            elif action == "release":
                cache_lock_held = False

    assert saw_cache_lock, "测试中未观测到 cache_lock 获取事件"


def test_facade_delegates_to_sync_engine(tmp_path):
    """断言 Facade 上的薄委托方法正确转发给 sync_engine。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Title\n\nBody text\n", encoding="utf-8")

    indexer = MarkdownIndexer(
        vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    )
    chunks = indexer.sync()
    chunk = chunks[0]

    # _chunk_has_vector
    assert indexer._chunk_has_vector(chunk) is True

    # _reuse_vectors_by_content_hash
    reused = indexer._reuse_vectors_by_content_hash()
    assert isinstance(reused, int)

    # _flush_vectors_to_disk (memory 模式下为空操作返回)
    indexer._flush_vectors_to_disk()

    # _ensure_disk_vectors_migrated
    indexer._ensure_disk_vectors_migrated()


def test_thread_names_and_prefixes():
    """断言线程名前缀契约不变。"""
    import inspect
    import mortis_rag_mcp.indexer as indexer_module

    source = inspect.getsource(indexer_module)
    assert 'name="vault-init"' in source, "首启线程名 vault-init 发生改变"
    assert 'name="exempt-sync"' in source, "豁免同步线程名 exempt-sync 发生改变"
    assert 'name="vault-watch-native"' in source, "原生监听线程名 vault-watch-native 发生改变"
    assert 'name="vault-fs-debounce"' in source, "防抖调度线程名 vault-fs-debounce 发生改变"

    engine_source = inspect.getsource(sync_engine)
    assert 'thread_name_prefix="vault-emb"' in engine_source, "embedding 线程名前缀 vault-emb 发生改变"
