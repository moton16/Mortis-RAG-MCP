"""issue #15：多库下未决媒体意图触发 O(NxM) SQLite 扫描焊死 GIL。

本文件只钉**调用次数/执行次数**这类结构性不变量（与硬件无关、CI 可稳定复现）；
墙钟与倍率口径的性能基准不写进 tests/，落在 `.runtime/issue15/bench.py`。

每条用例都做过「修复前红 / 修复后绿」的反向确认：先在未修复的 HEAD 上跑红，
修复后才应该转绿。
"""
import hashlib
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.error import URLError

import pytest

from test_e20_media_connection import (
    close, make_indexer, media_config, native, offline,
)
from mortis_rag_mcp._indexer import watch
from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import MediaOccurrenceSpec
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.server import VaultMcpServer

# 与 issue 同量级：issue 报告 2,870 个已摄取虚拟文档、9,828 行 request_intents。
VIRTUAL_SOURCES = 500
INTENT_ROWS = 5000


def _commit_virtual_source(idx, ordinal, with_media=True):
    """提交一个已解析虚拟源（issue 里 2,892 个已摄取虚拟文档的缩尺版）。"""
    source = f"doc-{ordinal}.pdf"
    path = idx.vault_path / source
    path.write_bytes(b"%%PDF-1.4 issue15 fixture %d" % ordinal)
    body = f"# Fixture {ordinal}\n\nbefore media after\n"
    store = idx.document_store(write=True)
    rev = store.stage_revision(
        source=source, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        render_sha256=hashlib.sha256(body.encode()).hexdigest(),
        parser_fingerprint="fixture-parser", markdown=body)
    if with_media:
        blob = store.put_media_blob(data=b"\x89PNG\r\n\x1a\n" + bytes([(ordinal % 250) + 1]) * 16,
                                    mime_type="image/png", width=8, height=8)
        start = body.index("media")
        store.attach_occurrences(rev.revision_id, [MediaOccurrenceSpec(
            occurrence_id=f"issue15-occ-{ordinal}", blob_id=blob, kind="image", ordinal=0,
            mime_type="image/png", caption="fixture media", page=1,
            anchor_start=start, anchor_end=start + 5)])
    store.commit_revision(rev.revision_id)
    return source


def _fill_unresolved_intents(control, fingerprint, count):
    """把 request_intents 填到 issue 量级（state=submission_unknown，永不过租约）。"""
    rows = [(f"issue15-bulk-{i}", "media", f"hash-{i}", "https://media-a.invalid/embed",
             fingerprint, 1, "submission_unknown", "fixture", float(i), float(i))
            for i in range(count)]
    with control.mutation() as conn:
        control._ensure_payment_schema(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO request_intents (request_id, kind, payload_hash, endpoint, "
            "profile_fingerprint, attempt, state, reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    return len(control.list_request_intents())


@pytest.mark.usefixtures("offline")
def test_unresolved_media_intent_probed_once_per_sync(tmp_path, monkeypatch):
    """P1-a/b：整轮 sync 对「媒体未决意图」的探查必须 ≤ 1 次，而不是逐 source 一次。

    现状（修复前）：`run_sync` 的虚拟文档循环内对每个 source 调一次
    `has_unresolved_paid_intents`，而它内部是全表扫描 + 跨进程 OS 锁，实测
    16.33 ms/次 × 2,870 = 46.9 s/轮 > 30 s 重排间隔 —— 后台线程永远追不上，
    GIL 被焊死，同进程内其它库的前台检索一起变慢（issue #15 根因 1）。
    """
    def lost(items):
        raise URLError("lost response")

    idx = make_indexer(tmp_path, media_config(), lost)
    try:
        for ordinal in range(VIRTUAL_SOURCES):
            _commit_virtual_source(idx, ordinal)
        # 第一轮：首个 source 真的发一次并落为 submission_unknown，其余被未决闸门拦下，
        # 于是全部 source 进入「已 settled 但结果未知」的失败态。
        idx.sync()
        assert sum(1 for value in idx.failed_files.values()
                   if str(value).startswith("media_native:")) >= VIRTUAL_SOURCES - 1
        control = idx._paid_control_store()
        assert _fill_unresolved_intents(control, idx.media_provider.profile.fingerprint,
                                        INTENT_ROWS) >= INTENT_ROWS

        probes = []
        original = idx.has_unresolved_paid_intents

        def counting(store, kind, profile_fingerprint=""):
            probes.append(kind)
            return original(store, kind, profile_fingerprint)

        monkeypatch.setattr(idx, "has_unresolved_paid_intents", counting)
        idx.sync()
        media_probes = [kind for kind in probes if kind == "media"]
        assert len(media_probes) <= 1, (
            f"整轮 sync 只应探查一次未决媒体意图，实际 {len(media_probes)} 次"
            f"（每个虚拟 source 一次 = O(N×M) 全表扫描）")
        # 未决仍然必须拦住重算：本轮不得产生新的原生媒体向量。
        produced = [chunk for chunks in idx._chunks.values() for chunk in chunks
                    if chunk.metadata.get("kind") == "media_native"]
        assert not produced
    finally:
        close(idx)


@pytest.mark.usefixtures("offline")
def test_virtual_source_is_not_reread_on_every_sync(tmp_path, monkeypatch):
    """P1-d：已摄取虚拟源的物理校验不得每轮全量读盘重算 SHA-256。

    现状（修复前）：虚拟文档分支没有文本路径那种 `_stat_cache` 短路，每轮对每个
    虚拟源整文件读一遍并算 SHA-256（issue 环境 2,892 个已摄取虚拟文档）。修复后
    复用文本路径已论证过的判据（签名三元组 + `_fast_path_is_trustworthy`），稳态
    下第二轮起零读盘；缓存未命中时行为与修复前逐字一致。
    """
    idx = make_indexer(tmp_path, media_config())
    try:
        for ordinal in range(VIRTUAL_SOURCES):
            _commit_virtual_source(idx, ordinal, with_media=False)
        # 只统计「真的读了多少字节」：缓存命中允许保留一次 open+close 的可读性探针
        # （`test_virtual_sync.py` 钉住的那条「物理源不可读必须降为 unverified」），
        # 但绝不允许再把整文件读一遍。
        read_bytes = []
        real_open = Path.open

        class _CountingStream:
            def __init__(self, stream):
                self._stream = stream

            def read(self, *args, **kwargs):
                data = self._stream.read(*args, **kwargs)
                read_bytes.append(len(data))
                return data

            def __enter__(self):
                self._stream.__enter__()
                return self

            def __exit__(self, *exc_info):
                return self._stream.__exit__(*exc_info)

            def __getattr__(self, name):
                return getattr(self._stream, name)

        def counting(self, mode="r", *args, **kwargs):
            stream = real_open(self, mode, *args, **kwargs)
            if "r" in mode and self.suffix == ".pdf":
                return _CountingStream(stream)
            return stream

        monkeypatch.setattr(Path, "open", counting)
        idx.sync()
        assert sum(read_bytes) > 0, "第一轮必须真的读盘校验每个虚拟源"
        read_bytes.clear()
        idx.sync()
        assert sum(read_bytes) == 0, (
            f"稳态第二轮仍从虚拟源读了 {sum(read_bytes)} 字节；应为零（Fast-Stat 缓存命中）")
        # 缓存命中不得改变结果：所有虚拟源仍在索引里。
        assert len(idx._chunks) == VIRTUAL_SOURCES
    finally:
        close(idx)


class _LiveWatcher:
    """原生 watcher / 监听线程「在线且存活」的替身。

    CI 的 Linux/非 Windows 平台无原生监听；`_watch_thread` 替身还需要 `ident`
    （`stop_watching` 的 `_join_started_thread` 靠它判断是否真的 start 过）。
    """

    ident = None

    def is_alive(self):
        return True

    def stop(self):
        return None

    def close(self):
        return None


def _settle_startup_indexing(server):
    """等 `VaultMcpServer.__init__` 起的 `vault-startup` 预索引线程收工。

    该线程会遍历注册表并 `_indexer_for(...)` + `sync()`；若它在用例把库登记进
    注册表之后才走到 `registry.load()`，就会替我们多跑一轮全量（还会把监听以
    `autostart=True` 提前拉起），把「一次 kb_rebuild 的整轮 `run_sync` 次数」这
    条口径污染成 2。等它收工再开始计数，口径才确定。
    """
    del server  # 线程引用没留在 server 上，按名字找
    for thread in threading.enumerate():
        if thread.name == "vault-startup":
            thread.join(timeout=5.0)


def _plain_indexer(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for name, title in (("a.md", "甲"), ("b.md", "乙"), ("c.md", "丙")):
        (vault / name).write_text(
            f"# {title}\n\n" + ("共享针 shared needle 的内容段落。" * 60) + "\n", encoding="utf-8")
    config = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8),
                       cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True))
    return MarkdownIndexer(vault, config)


def test_single_search_does_not_recompute_visibility_per_chunk(tmp_path, monkeypatch):
    """P3 / P3-b：单次检索的可见性过滤必须复用 documents 映射与 `.vaultignore`。

    修复前：`_filter_visible_chunks` 对**每个 chunk** 调一次 `matcher.is_ignored`
    （issue 环境 9,270 次而非 137 个 source），而且单次检索的三轮过滤
    （`all_chunks()` + 融合排序前后各一次）各自 `list_documents()` 一次
    （issue #15 根因 3）。

    P3 做了按 source 记忆化；P3-b 用 `DocumentStore.documents_epoch`——任何改变
    `documents` 行的写入（stage / commit / `set_visibility` / `invalidate_source` /
    `mark_deleted`）都推进它，而不是 `change_seq`（后者收不到纯可见性翻转）——作
    跨调用缓存失效键，把单次检索的读库次数从 **3 降到 1**；`.vaultignore` 则按
    `(mtime_ns, size, ctime_ns)` 文件签名缓存。

    两条钩子护栏仍必须绿（改动的正确性就靠它们）：
    `test_virtual_sync.py::test_empty_query_filters_recheck_visibility`（钩子里
    `set_visibility('exempt')` → epoch 推进 → 后一次过滤重读）与
    `::test_ignore_final_filter_with_broken_store`（钩子里直写 `.vaultignore` →
    文件签名变化 → matcher 重建）。
    """
    from mortis_rag_mcp._indexer.scanning import IgnoreMatcher

    idx = _plain_indexer(tmp_path)
    try:
        idx.sync()
        sources = set(idx._chunks)
        total_chunks = sum(len(chunks) for chunks in idx._chunks.values())
        assert len(sources) >= 3 and total_chunks > len(sources), "fixture 必须多 chunk/多 source"

        listed = []
        store = idx.document_store()
        real_list = store.list_documents

        def counting_list(*args, **kwargs):
            listed.append("list")
            return real_list(*args, **kwargs)

        monkeypatch.setattr(store, "list_documents", counting_list)
        ignored_calls = []
        real_ignored = IgnoreMatcher.is_ignored

        def counting_ignored(self, rel_path, is_dir=False):
            ignored_calls.append(rel_path)
            return real_ignored(self, rel_path, is_dir)

        monkeypatch.setattr(IgnoreMatcher, "is_ignored", counting_ignored)
        idx.search("shared needle", use_rerank=False)
        assert len(listed) == 1, (
            f"单次检索读库 {len(listed)} 次：P3-b 后应为 1 次（修复前为 3 次，"
            f"三轮过滤各自重读一次 documents 映射）")
        assert len(ignored_calls) <= 3 * len(sources), (
            f"is_ignored 调了 {len(ignored_calls)} 次：应按 source 记忆化，"
            f"上界为 3×{len(sources)}（共 {total_chunks} 个 chunk，修复前约 3×chunk 数）")
    finally:
        idx.stop_watching()
        idx.close_document_store()


def test_kb_rebuild_runs_a_single_full_build(tmp_path, monkeypatch):
    """P4-a：一次 `kb_rebuild` 只能跑一次整轮全量构建（温路径 2 → 1）。

    现状（修复前）：`_indexer_for` 对尚未加载的库先 `start_watching()`，监听线程
    启动即跑一次首轮全量 sync；紧接着 `rebuild()` 清空缓存再全量重建一次——两次
    全量在 `_sync_lock` 上串行（issue #15 根因 4）。温路径（库已加载、watcher 在线）
    同样会多出「重建后首轮」，实测整轮 `run_sync` 2 次。

    验收口径按**整轮 `run_sync` 次数**，而不是只看监听收放顺序：这里两条都断言。
    """
    import json

    from mortis_rag_mcp._indexer import sync_engine

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nrebuild needle 的内容。\n", encoding="utf-8")
    server = VaultMcpServer()
    runs: list[str] = []
    real_run_sync = sync_engine.run_sync

    def counting_run_sync(owner):
        # 只统计**本库**：`run_sync` 是模块级函数，别处测试遗留的 watcher 线程
        # （`VaultMcpServer.shutdown()` 未及收干净时的在飞首轮）会打到同一个桩上。
        if Path(owner.vault_path).resolve() == vault.resolve():
            runs.append(threading.current_thread().name)
        return real_run_sync(owner)

    monkeypatch.setattr(sync_engine, "run_sync", counting_run_sync)
    try:
        _settle_startup_indexing(server)
        server.call_tool("kb_init", {"path": str(vault), "name": "R"})
        indexer = server._indexer_for({"vault_path": str(vault)})
        deadline = time.time() + 5
        while indexer.last_sync is None and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)  # 等 kb_init 的后台首轮彻底落地
        runs.clear()

        events: list[str] = []
        real_stop, real_rebuild, real_start = (
            indexer.stop_watching, indexer.rebuild, indexer.start_watching)

        def spy_stop():
            events.append("stop")
            return real_stop()

        def spy_rebuild():
            events.append("rebuild")
            return real_rebuild()

        def spy_start(*args, **kwargs):
            events.append("start")
            return real_start(*args, **kwargs)

        monkeypatch.setattr(indexer, "stop_watching", spy_stop)
        monkeypatch.setattr(indexer, "rebuild", spy_rebuild)
        monkeypatch.setattr(indexer, "start_watching", spy_start)
        response = server.call_tool("kb_rebuild", {"vault_path": str(vault)})
        time.sleep(1.0)  # 等被拉起的监听线程落地：若还有首轮就会记进来
        assert events == ["stop", "rebuild", "start"], (
            f"kb_rebuild 期间监听的收放顺序不对：{events}（应为 stop → rebuild → start，"
            f"修复前是 ['rebuild']，即后台首轮与前台 rebuild 两次全量串行）")
        assert len(runs) == 1, (
            f"kb_rebuild 整轮 run_sync 跑了 {len(runs)} 次（温路径应为 1；"
            f"修复前 2 次 = 前台 rebuild + 重建后首轮）：{runs}")
        stats = json.loads(response["content"][0]["text"])
        assert stats["files"] >= 1
    finally:
        server.shutdown()


def test_kb_rebuild_on_a_cold_vault_runs_a_single_full_build(tmp_path, monkeypatch):
    """P4-a：未加载的库上 `kb_rebuild` 只能跑一次整轮全量（冷路径 3 → 1）。

    现状（修复前）实测整轮 `run_sync` **3** 次：t+0 后台监听首轮全量 → t+1.2 前台
    `rebuild()` → t+1.27 重建后首轮近空转；`stop_watching()` 只是等到了在飞的后台
    首轮，即「被串行化而非消除」。修复靠两步：`autostart=False` 不启动后台监听
    （冷路径根本没有第一次全量），rebuild 之后才 `start_watching(skip_initial_sync=True)`
    （消掉重建后的近空转）。

    同时钉住「跳过首轮」不等于「永不启动」：监听线程确实被拉起。
    """
    import json

    from mortis_rag_mcp._indexer import sync_engine

    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(3):
        (vault / f"n{i}.md").write_text(f"# N{i}\nrebuild needle 的内容。\n", encoding="utf-8")
    server = VaultMcpServer()
    runs: list[str] = []
    real_run_sync = sync_engine.run_sync

    def counting_run_sync(owner):
        # 只统计**本库**：`run_sync` 是模块级函数，别处测试遗留的 watcher 线程会打到
        # 同一个桩上（实测 `test_snapshot.py::test_server_kb_export_import_roundtrip`
        # 遗留的 `vault-watch-native` 会在本用例窗口内再跑一轮）。
        if Path(owner.vault_path).resolve() == vault.resolve():
            runs.append(threading.current_thread().name)
        return real_run_sync(owner)

    monkeypatch.setattr(sync_engine, "run_sync", counting_run_sync)
    try:
        _settle_startup_indexing(server)
        server.registry.add(str(vault), name="Cold")
        assert len(server._indexers) == 0, "冷路径前提：库尚未加载"
        response = server.call_tool("kb_rebuild", {"vault_path": str(vault)})
        time.sleep(1.5)  # 等任何被拉起的监听线程落地
        assert len(runs) == 1, (
            f"冷路径整轮 run_sync 跑了 {len(runs)} 次（应为 1；修复前 3 次："
            f"后台首轮全量 → 前台 rebuild → 重建后首轮近空转）：{runs}")
        assert json.loads(response["content"][0]["text"])["files"] >= 1
        indexer = server._indexer_for({"vault_path": str(vault)})
        assert indexer._watch_thread is not None, "rebuild 之后必须把监听拉起来"
    finally:
        server.shutdown()


def test_rebuild_writes_fts_in_one_transaction(tmp_path, monkeypatch):
    """P4-b：`rebuild()` 末尾的 FTS 回填必须是**一个**事务，而不是逐 source 一个。

    现状（修复前）：`FtsIndex.upsert_source` 内部是 `with conn:`，每个 source 一个
    独立事务（DELETE + executemany），issue 环境 137 个 source = 137 个事务
    （issue #15 根因 4）。
    """
    from mortis_rag_mcp.fts import FtsIndex

    idx = _plain_indexer(tmp_path)
    try:
        idx.sync()
        calls = {"single": 0, "batch": 0, "sources": 0}
        real_single, real_batch = FtsIndex.upsert_source, getattr(FtsIndex, "upsert_sources", None)

        def counting_single(self, source, chunks):
            calls["single"] += 1
            return real_single(self, source, chunks)

        def counting_batch(self, mapping):
            items = list(mapping)
            calls["batch"] += 1
            calls["sources"] = len(items)
            if real_batch is None:
                return None
            return real_batch(self, items)

        monkeypatch.setattr(FtsIndex, "upsert_source", counting_single)
        monkeypatch.setattr(FtsIndex, "upsert_sources", counting_batch, raising=False)
        idx.rebuild()
        assert calls["batch"] == 1, f"整批 FTS 回填事务数为 {calls['batch']}（应为 1）"
        assert calls["single"] == 0, (
            f"仍有 {calls['single']} 次逐 source 事务（修复前 = source 数）")
        assert calls["sources"] == len(idx._chunks) >= 3
        # 批量写必须保持同样的结果：`test_hybrid.py:166/171` 的口径。
        assert idx._fts.count() == len(idx.all_chunks())
    finally:
        idx.stop_watching()
        idx.close_document_store()


def test_fts_warm_cache_populate_uses_one_transaction(tmp_path, monkeypatch):
    """P4 同型：进程启动的 warm-cache 回填（`_fts_ensure_populated`）必须单事务。

    现状（修复前）：逐 source 一个 `with conn:` 事务。这条路径被**每次进程启动**的
    warm-cache 分支（无文件变动时 sync 什么都不写，由它补回填）与 `kb_import` 的三条
    路径（`snapshot.py`）调用，与 P4-b 已消除的 rebuild 末尾模式同型（issue #15
    根因 4）。
    """
    from mortis_rag_mcp.fts import FtsIndex

    idx = _plain_indexer(tmp_path)
    try:
        idx.sync()
        assert idx._fts is not None
        assert len(idx._chunks) >= 3
        calls = {"batch": 0, "single": 0, "sources": 0}
        real_single, real_batch = FtsIndex.upsert_source, FtsIndex.upsert_sources

        def counting_single(self, source, chunks):
            calls["single"] += 1
            return real_single(self, source, chunks)

        def counting_batch(self, mapping):
            items = list(mapping)
            calls["batch"] += 1
            calls["sources"] = len(items)
            return real_batch(self, items)

        monkeypatch.setattr(FtsIndex, "upsert_source", counting_single)
        monkeypatch.setattr(FtsIndex, "upsert_sources", counting_batch)
        # 造出「行数陈旧」的启动态：索引行被清空但内存 chunks 仍在，等价于
        # warm-cache 命中、没有任何文件变动的下一次进程启动。
        idx._fts.clear()
        assert idx._fts.count() == 0
        idx._fts_ensure_populated()
        assert calls["batch"] == 1, f"回填事务数为 {calls['batch']}（应为 1）"
        assert calls["single"] == 0, (
            f"仍有 {calls['single']} 次逐 source 事务（修复前 = source 数）")
        assert calls["sources"] == len(idx._chunks) >= 3
        assert idx._fts.count() == len(idx.all_chunks())
    finally:
        idx.stop_watching()
        idx.close_document_store()


def test_read_refresh_does_not_mark_a_ready_vault_as_rebuilding(tmp_path):
    """P2：watcher 在线时，读路径的机会性刷新不得把一个就绪库判成 rebuilding。

    现状（修复前）：`request_refresh(for_read=True)` 的抑制窗口只有 1 秒，之后每次
    `kb_search` / `kb_stats` / `kb_list_files` 都会真的排一轮全量 sync —— 于是
    `refresh_status` 的 `pending` 为真 → `indexing_in_progress: True` →
    `index_state` 判 `rebuilding`，并返回「等后台同步完成再重试」（issue #15 根因 2，
    也是 issue 里「恒报 rebuilding」的直接来源）。
    """
    idx = _plain_indexer(tmp_path)
    try:
        idx.sync()
        assert idx.index_state()["index_state"] == "ready"
        # 让「距上次同步 < 1 秒」这条抑制失效：issue 里每查必排正是发生在这之后。
        idx.last_sync = time.time() - 5.0
        idx._fs_watcher = _LiveWatcher()
        assert watch.request_refresh(idx, for_read=True, start_scheduler=False) is False
        assert idx.refresh_status()["refresh_pending"] is False
        assert idx.index_state()["index_state"] == "ready", (
            "就绪的静态库被读路径自己的机会性刷新判成了 rebuilding")
        # 真实变动仍然必须排得进去：显式请求不被收窄。
        assert watch.request_refresh(idx, start_scheduler=False) is True
        assert idx.refresh_status()["refresh_pending"] is True
    finally:
        idx.stop_watching()
        idx.close_document_store()


def test_read_refresh_is_throttled_without_a_native_watcher(tmp_path):
    """P2（非原生面）：轮询/降级模式下读路径的兜底必须按低频对账周期限流。

    修复前：抑制窗口只有 `_READ_REFRESH_MIN_INTERVAL_SECONDS=1.0` 秒，于是
    Linux/macOS / 显式 `watch_method="poll"` / 原生 watcher 降级三种情形下，读路径
    仍会在 1 秒后**每查排一轮全量 sync**，并把自己判成 `rebuilding`（issue #15
    根因 2 的未修面）。修复后按 `watch_fallback_interval`（默认 30s，与原生模式的
    兜底全量同一口径）限流，同时保住「签名轮询看不见的变动」（`.vaultignore` 等）
    的有界新鲜度。
    """
    idx = _plain_indexer(tmp_path)
    try:
        idx.sync()
        assert idx.index_state()["index_state"] == "ready"
        # 非原生：没有 `_fs_watcher`，只有在线且存活的轮询/降级监听线程。
        idx._fs_watcher = None
        idx._watch_thread = _LiveWatcher()
        idx.last_sync = time.time() - 5.0  # 让 1 秒抑制窗口失效（修复前正是这里开始每查必排）
        assert watch.request_refresh(idx, for_read=True, start_scheduler=False) is False, (
            "轮询模式下距上次同步 5 秒仍登记了全量 sync（修复前抑制窗口只有 1 秒）")
        assert idx.refresh_status()["refresh_pending"] is False
        assert idx.index_state()["index_state"] == "ready", (
            "轮询模式下的就绪库被读路径自己的机会性刷新判成了 rebuilding")

        # 兜底仍然在，只是按对账周期：超过 `watch_fallback_interval` 后必须能排进去。
        idx.last_sync = time.time() - (float(idx.config.watch_fallback_interval) + 1.0)
        assert watch.request_refresh(idx, for_read=True, start_scheduler=False) is True
        assert idx.refresh_status()["refresh_pending"] is True
    finally:
        idx.stop_watching()
        idx.close_document_store()


def test_read_refresh_fallback_disabled_never_schedules_from_reads(tmp_path):
    """`watch_fallback_interval<=0`（显式关闭周期对账）时读路径不得自行兜底。"""
    idx = _plain_indexer(tmp_path)
    try:
        idx.sync()
        config = replace(idx.config, watch_fallback_interval=0.0)
        idx.config = config
        idx._fs_watcher = None
        idx._watch_thread = _LiveWatcher()
        idx.last_sync = time.time() - 3600.0
        assert watch.request_refresh(idx, for_read=True, start_scheduler=False) is False, (
            "关掉周期对账后读路径仍每查排一轮全量，等于把开关变成「每查一次」")
        # 显式请求（非 for_read）不受影响。
        assert watch.request_refresh(idx, start_scheduler=False) is True
    finally:
        idx.stop_watching()
        idx.close_document_store()


@pytest.mark.usefixtures("offline")
def test_local_free_media_route_does_not_probe_the_intent_store(tmp_path, monkeypatch):
    """P1-c：零成本（loopback、无凭据）媒体 route 连意图库都不该碰。

    现状（修复前）：`media_native_for_revision` 里 `owner._paid_control_store()` 与
    「媒体未决意图」探查排在 `is_local_free` 短路**之前**，于是零成本媒体 route 只要
    有媒体工作就每轮白探一次意图库，结果又被后置短路丢弃（纯浪费，且破坏「零成本
    端点完全不触意图库」这条性质）。修复后媒体探查应为 0 次。

    本用例同时钉住「跳过探查不得顺手把工作也跳掉」：零成本 media 仍必须产出原生
    媒体向量（走本地直发分支）。
    """
    def valid(items):
        return [{"index": i, "embedding": [0, 1]} for i, _ in enumerate(items)]

    cfg = media_config(media_endpoint="http://127.0.0.1:8000/v1/embeddings", media_api_key="")
    idx = make_indexer(tmp_path, cfg, valid)
    try:
        assert idx.media_provider.is_local_free is True
        _commit_virtual_source(idx, 0)
        probes = []
        original = idx.has_unresolved_paid_intents

        def counting(store, kind, profile_fingerprint=""):
            probes.append(kind)
            return original(store, kind, profile_fingerprint)

        monkeypatch.setattr(idx, "has_unresolved_paid_intents", counting)
        idx.sync()
        media_probes = [kind for kind in probes if kind == "media"]
        assert media_probes == [], (
            f"零成本媒体 route 仍探查了 {len(media_probes)} 次未决意图；应为 0 次"
            f"（判序修复前 `unresolved_paid()` 排在 `is_local_free` 之前）")
        assert [chunk for chunk in idx._chunks["doc-0.pdf"]
                if chunk.metadata.get("kind") == "media_native"], (
            "零成本 media 必须仍然产出原生媒体向量，不能被「跳过探查」顺带跳过")
    finally:
        close(idx)
