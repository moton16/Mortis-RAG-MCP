"""issue #15：多库下未决媒体意图触发 O(NxM) SQLite 扫描焊死 GIL。

本文件只钉**调用次数/执行次数**这类结构性不变量（与硬件无关、CI 可稳定复现）；
墙钟与倍率口径的性能基准不写进 tests/，落在 `.runtime/issue15/bench.py`。

每条用例都做过「修复前红 / 修复后绿」的反向确认：先在未修复的 HEAD 上跑红，
修复后才应该转绿。
"""
import hashlib
import time
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
    """原生 watcher 在线且存活的替身（CI 的 Linux/非 Windows 平台无原生监听）。"""

    def is_alive(self):
        return True

    def stop(self):
        return None

    def close(self):
        return None


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
    """P3：`is_ignored` 必须按 source 记忆化，不能按 chunk 重算。

    现状（修复前）：`_filter_visible_chunks` 对**每个 chunk** 调一次
    `matcher.is_ignored`，而单次检索要过滤三轮（`all_chunks()` + 融合排序前后各
    一次）——issue 环境 9,270 次而不是 137 个 source（issue #15 根因 3）。

    注意本卡**只做调用内优化**：三次过滤仍必须各自重读文档库与 `.vaultignore`
    （`test_empty_query_filters_recheck_visibility` 与
    `test_ignore_final_filter_with_broken_store` 两条既有合同钉住），故上界是
    3 × source 数而不是 1 ×。
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
        # 三轮过滤仍各自读库（见 docstring），不在这里断言次数收敛；只读库次数本身
        # 不得因为记忆化而变多。
        assert len(listed) == 3, f"单次检索读库 {len(listed)} 次，与修复前口径应一致（3 次）"
        assert len(ignored_calls) <= 3 * len(sources), (
            f"is_ignored 调了 {len(ignored_calls)} 次：应按 source 记忆化，"
            f"上界为 3×{len(sources)}（共 {total_chunks} 个 chunk，修复前约 3×chunk 数）")
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
