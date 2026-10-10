"""issue #15：多库下未决媒体意图触发 O(NxM) SQLite 扫描焊死 GIL。

本文件只钉**调用次数/执行次数**这类结构性不变量（与硬件无关、CI 可稳定复现）；
墙钟与倍率口径的性能基准不写进 tests/，落在 `.runtime/issue15/bench.py`。

每条用例都做过「修复前红 / 修复后绿」的反向确认：先在未修复的 HEAD 上跑红，
修复后才应该转绿。
"""
import hashlib
from pathlib import Path
from urllib.error import URLError

import pytest

from test_e20_media_connection import (
    close, make_indexer, media_config, native, offline,
)
from mortis_rag_mcp.doc_store import MediaOccurrenceSpec

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
