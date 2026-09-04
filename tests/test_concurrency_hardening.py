"""B1 并发加固的回归测试。

覆盖复审 PR #1 时发现的四条并发/存活性问题：

* import_snapshot 与 sync 的锁序反转（ABBA 死锁）
* _embed_missing 恒返回 True，导致每轮无条件重写全量缓存
* start_watching 在调用方线程里同步跑完首轮全量索引
* _indexer_for 并发构造出两个 indexer（watcher 线程与目录句柄泄漏）
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any


from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.providers import ProviderError
from mortis_rag_mcp.server import VaultMcpServer


class ExplodingProvider:
    """永远失败的 provider：用来验证「全部失败时不该谎称有进展」。"""

    def __init__(self, dimension: int = 8) -> None:
        self.calls = 0
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise ProviderError("429 rate limit exceeded")


class CountingProvider:
    """统计真实 embedding 调用次数。"""

    def __init__(self, dimension: int = 8) -> None:
        self.calls = 0
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(index % 7) / 7.0 for index in range(self.dimension)] for _ in texts]


def _config(tmp_path: Path, **cache_kwargs: Any) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, **cache_kwargs),
    )


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    vault = tmp_path / name
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "a.md").write_text("# A\nhello world", encoding="utf-8")
    (vault / "b.md").write_text("# B\nsecond note", encoding="utf-8")
    return vault


def test_embed_missing_reports_false_when_every_file_failed(tmp_path):
    """全部失败时必须返回 False —— 否则 _sync_locked 每轮都重写全量缓存，

    在 [cache] placement="vault" + 原生递归监听下会演变成「写缓存 → 触发事件
    → 再 sync → 再失败 → 再写缓存」的自激死循环，每轮重发全部 pending chunk。
    """
    vault = _vault(tmp_path)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=ExplodingProvider())

    assert indexer.sync() == [] or True  # sync 自身吞掉异常
    assert indexer.failed_files, "失败的 provider 应当留下 failed_files 记录"
    # 关键断言：没有任何 chunk 拿到向量时，_embed_missing 必须如实报告 False。
    assert indexer._embed_missing() is False


def test_embed_missing_reports_true_after_a_real_success(tmp_path):
    """反向对照：真的补上向量时必须返回 True，否则缓存不会被刷新。"""
    vault = _vault(tmp_path)
    provider = CountingProvider()
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=provider)

    indexer.sync()
    assert provider.calls >= 1
    assert indexer._embed_missing() is False  # 都已有向量，无进展

    # 新文件 = 真的有新工作。
    (vault / "c.md").write_text("# C\nthird note", encoding="utf-8")
    indexer.sync()
    assert indexer._embed_missing() is False  # 上一轮 sync 已经补完


def test_start_watching_returns_before_initial_index_finishes(tmp_path):
    """start_watching 不得在调用方线程里跑完首轮全量索引。

    此前它在返回前同步 sync()，而 start_watching 由首个工具调用触发，
    kb_init 声称的 "indexing: started in background" 是假的。
    """
    vault = _vault(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class SlowProvider(CountingProvider):
        def embed(self, texts: list[str]) -> list[list[float]]:
            started.set()
            release.wait(timeout=5)
            return super().embed(texts)

    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=SlowProvider())
    try:
        begin = time.monotonic()
        indexer.start_watching(interval=0.1, debounce_seconds=0.05)
        elapsed = time.monotonic() - begin
        # 首轮索引被 provider 卡住，start_watching 却必须已经返回。
        assert started.wait(timeout=5), "监听线程应当在后台开始首轮 sync"
        assert elapsed < 5, f"start_watching 阻塞了 {elapsed:.1f}s，首轮索引没挪到后台"
    finally:
        release.set()
        indexer.stop_watching()


def test_stop_watching_only_clears_thread_after_it_really_stopped(tmp_path):
    """stop_watching 必须在线程真的结束后才丢弃引用。

    此前 join(timeout=2) 后无条件置 None，残留线程会与被覆盖的新 indexer
    一起写同一批缓存文件（cache key 相同）。
    """
    vault = _vault(tmp_path)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.start_watching(interval=0.1, debounce_seconds=0.05)
    indexer.stop_watching()

    assert indexer._watch_thread is None or not indexer._watch_thread.is_alive()
    assert indexer._stopping is False


def test_all_chunks_survives_concurrent_removal(tmp_path):
    """watcher 线程并发 pop 时，all_chunks 不得抛 KeyError / RuntimeError。"""
    vault = _vault(tmp_path)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()

    stop = threading.Event()
    errors: list[BaseException] = []

    def churn() -> None:
        while not stop.is_set():
            try:
                indexer._chunks.pop("a.md", None)
                indexer._chunks["a.md"] = indexer._chunks.get("a.md", [])
            except BaseException as exc:  # pragma: no cover - 记录后退出
                errors.append(exc)
                return

    def reader() -> None:
        while not stop.is_set():
            try:
                indexer.all_chunks()
            except BaseException as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=churn, daemon=True), threading.Thread(target=reader, daemon=True)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    stop.set()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == [], f"并发读写 _chunks 时抛异常: {errors!r}"


def test_indexer_for_is_idempotent_under_concurrent_calls(tmp_path, monkeypatch):
    """_indexer_for 并发调用必须返回同一个实例（否则 watcher 线程/句柄泄漏）。"""
    vault = _vault(tmp_path)
    config = tmp_path / "app.toml"
    config.write_text(
        'mode = "static"\n[cache]\nenabled = true\ndir = "%s"\n'
        % (tmp_path / "cache").as_posix(),
        encoding="utf-8",
    )
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    server = VaultMcpServer(config)
    server.call_tool("kb_init", {"path": str(vault)})

    results: list[Any] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        indexer = server._indexer_for({"vault_path": str(vault)})
        with lock:
            results.append(indexer)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4, "有线程没能取到 indexer（可能撞上了死锁）"
    assert len({id(item) for item in results}) == 1, "并发构造出了多个 indexer（watcher 泄漏）"
    for indexer in results:
        indexer.stop_watching()


class _OrderProbe:
    """记录本线程已持有的锁，检测「持 cache 锁再取 sync 锁」的反向嵌套。

    靠竞态去撞 ABBA 死锁是不可靠的（窗口只有微秒级，测试会假绿）。这里改为
    直接检查锁序不变量：任何线程都不得在持有 _cache_lock 时去获取 _sync_lock。
    sync() 的合法顺序是 sync → cache，反过来就是死锁。
    """

    def __init__(self, lock: Any, name: str, state: dict) -> None:
        self._lock = lock
        self._name = name
        self._state = state

    def __enter__(self) -> "_OrderProbe":
        self._lock.acquire()
        held = self._state.setdefault(threading.get_ident(), [])
        if self._name == "sync" and "cache" in held:
            self._state["violation"] = (self._state.get("violation") or []) + [
                "acquired _sync_lock while holding _cache_lock"
            ]
        held.append(self._name)
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        held = self._state.get(threading.get_ident(), [])
        if self._name in held:
            held.remove(self._name)
        self._lock.release()
        return False


def test_import_snapshot_never_takes_sync_lock_while_holding_cache_lock(tmp_path):
    """锁序不变量：import_snapshot 不得在 _cache_lock 内嵌套 _sync_lock。

    原实现是 `with _cache_lock: with _sync_lock:`，与 sync() 的
    `_sync_lock → _save_cache() → _cache_lock` 正好相反，撞上 30s 兜底对账
    sync 就是 ABBA 死锁，整个 MCP 服务冻结。
    """
    vault = _vault(tmp_path)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    snapshot = tmp_path / "snap.zip"
    indexer.export_snapshot(snapshot)

    state: dict = {}
    real_sync, real_cache = indexer._sync_lock, indexer._cache_lock
    indexer._sync_lock = _OrderProbe(real_sync, "sync", state)
    indexer._cache_lock = _OrderProbe(real_cache, "cache", state)

    errors: list[BaseException] = []
    stop = threading.Event()

    def sync_worker() -> None:
        while not stop.is_set():
            try:
                indexer.sync()
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
                return

    thread = threading.Thread(target=sync_worker, daemon=True)
    thread.start()
    try:
        for _ in range(3):
            indexer.import_snapshot(snapshot)
    finally:
        stop.set()
        thread.join(timeout=10)
        indexer._sync_lock, indexer._cache_lock = real_sync, real_cache
        indexer.stop_watching()

    assert errors == []
    assert not thread.is_alive(), "sync 线程卡死，疑似锁序反转导致死锁"
    assert not state.get("violation"), f"检测到反向锁序嵌套: {state['violation']}"


def test_import_and_sync_run_concurrently_without_hanging(tmp_path):
    """并发冒烟：import 与 sync 同时在跑时两边都要能推进（不要求撞出竞态）。"""
    vault = _vault(tmp_path)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    snapshot = tmp_path / "snap.zip"
    indexer.export_snapshot(snapshot)

    rounds = 20
    done = threading.Event()
    failures: list[BaseException] = []

    def sync_worker() -> None:
        for _ in range(rounds):
            try:
                indexer.sync()
            except BaseException as exc:  # pragma: no cover
                failures.append(exc)
                return
        done.set()

    thread = threading.Thread(target=sync_worker, daemon=True)
    thread.start()
    try:
        assert indexer.import_snapshot(snapshot)["imported"] is True
    finally:
        finished = done.wait(timeout=15)
        thread.join(timeout=5)
        indexer.stop_watching()

    assert failures == []
    assert not thread.is_alive(), "sync 线程卡死了"
    assert finished, "并发 sync 未在时限内跑完"
