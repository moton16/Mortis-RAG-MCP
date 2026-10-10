"""issue #15：多库下未决媒体意图触发 O(NxM) SQLite 扫描焊死 GIL。

本文件只钉**调用次数/执行次数**这类结构性不变量（与硬件无关、CI 可稳定复现）；
墙钟与倍率口径的性能基准不写进 tests/，落在 `.runtime/issue15/bench.py`。

每条用例都做过「修复前红 / 修复后绿」的反向确认：先在未修复的 HEAD 上跑红，
修复后才应该转绿。
"""
import hashlib
from urllib.error import URLError

import pytest

from test_e20_media_connection import (
    close, make_indexer, media_config, native, offline,
)
from mortis_rag_mcp.doc_store import MediaOccurrenceSpec

# 与 issue 同量级：issue 报告 2,870 个已摄取虚拟文档、9,828 行 request_intents。
VIRTUAL_SOURCES = 500
INTENT_ROWS = 5000


def _commit_virtual_source(idx, ordinal):
    """提交一个带媒体 occurrence 的已解析虚拟源（issue 里 2,892 个的缩尺版）。"""
    source = f"doc-{ordinal}.pdf"
    path = idx.vault_path / source
    path.write_bytes(b"%%PDF-1.4 issue15 fixture %d" % ordinal)
    body = f"# Fixture {ordinal}\n\nbefore media after\n"
    store = idx.document_store(write=True)
    rev = store.stage_revision(
        source=source, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        render_sha256=hashlib.sha256(body.encode()).hexdigest(),
        parser_fingerprint="fixture-parser", markdown=body)
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
