"""E08-a：媒体 occurrence 的正文锚点映射（NEW）。

尺寸（width/height）属于媒体尺寸，**不是**正文位置；anchor 列必须是有证据的字符
半开区间。真实 SQLite 落盘 + 备份副本第二连接读回；非方形尺寸与明显不同的正文
offset 对照。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from mortis_rag_mcp._indexer.media import occurrence_span
from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import (
    DocumentStore,
    MediaOccurrenceSpec,
    StoreContractError,
    resolve_storage_layout,
)
from mortis_rag_mcp.ingest.worker import StoreMediaSink

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MARKDOWN = ("# 报告标题\n\n这是一段足够长的正文，用来让图片引用出现在明显不同于尺寸的 offset。\n\n"
            "![fig](fig.png)\n\n结尾正文。\n")
ANCHOR_START = MARKDOWN.index("![fig]")
ANCHOR_END = ANCHOR_START + len("![fig](fig.png)")
WIDTH, HEIGHT = 640, 480  # 非方形：与正文 offset 明显不同


def _store(tmp_path: Path, name: str = "vault") -> DocumentStore:
    vault = tmp_path / f"vault-{name}"
    vault.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / f"cache-{name}"), enabled=True),
    )
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)
    return store


def _stage(store: DocumentStore, markdown: str = MARKDOWN) -> str:
    staged = store.stage_revision(
        source="report.md",
        source_sha256=hashlib.sha256(b"report").hexdigest(),
        render_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        parser_fingerprint="mineru-v4:current",
        markdown=markdown,
        capabilities={"coverage": "full"},
    )
    return staged.revision_id


def _columns(tmp_path: Path, store: DocumentStore, name: str) -> tuple[object, object]:
    """从**备份副本**上用第二连接读回 anchor 列的真实落盘值。"""
    copy = tmp_path / f"backup-{name}.sqlite"
    store.backup_to(copy)
    conn = sqlite3.connect(str(copy))
    try:
        row = conn.execute(
            "SELECT anchor_start, anchor_end FROM media_occurrences WHERE occurrence_id = 'occ-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0], row[1]


def test_anchor_is_text_offset_not_pixel_size(tmp_path: Path):
    store = _store(tmp_path, "anchor")
    try:
        revision = _stage(store)
        blob_id = store.put_media_blob(data=PNG, mime_type="image/png",
                                       width=WIDTH, height=HEIGHT)
        store.attach_occurrences(revision, [MediaOccurrenceSpec(
            occurrence_id="occ-1", blob_id=blob_id, kind="image", ordinal=0,
            mime_type="image/png", page=1, width=WIDTH, height=HEIGHT,
            anchor_start=ANCHOR_START, anchor_end=ANCHOR_END)])
        store.commit_revision(revision, source_sha256=hashlib.sha256(b"report").hexdigest())

        start, end = _columns(tmp_path, store, "anchor")
        assert (start, end) == (ANCHOR_START, ANCHOR_END)
        assert start != WIDTH and end != HEIGHT, "尺寸被当成正文锚点写进了 anchor 列"
        # 正文范围确实指向图片引用本身
        assert MARKDOWN[start:end] == "![fig](fig.png)"
        # 第二连接读回：metadata 也带着同一锚点（E02 恢复消费同一合同）
        other = _store(tmp_path, "anchor")
        try:
            media = other.list_media("report.md", revision_id=revision)
            assert media[0]["metadata"]["anchor_start"] == ANCHOR_START
            assert media[0]["metadata"]["anchor_end"] == ANCHOR_END
        finally:
            other.close()
    finally:
        store.close()


def test_metadata_anchor_is_used_when_field_is_absent(tmp_path: Path):
    store = _store(tmp_path, "meta")
    try:
        revision = _stage(store)
        blob_id = store.put_media_blob(data=PNG, mime_type="image/png",
                                       width=WIDTH, height=HEIGHT)
        store.attach_occurrences(revision, [MediaOccurrenceSpec(
            occurrence_id="occ-1", blob_id=blob_id, kind="image", ordinal=0,
            mime_type="image/png", width=WIDTH, height=HEIGHT,
            metadata={"anchor_start": ANCHOR_START, "anchor_end": ANCHOR_END})])
        store.commit_revision(revision, source_sha256=hashlib.sha256(b"report").hexdigest())
        assert _columns(tmp_path, store, "meta") == (ANCHOR_START, ANCHOR_END)
    finally:
        store.close()


def test_verified_metadata_beats_size_polluted_columns(tmp_path: Path):
    """历史行：anchor 列被尺寸污染 → 已核验 metadata 锚点优先，不被尺寸盖过。"""
    store = _store(tmp_path, "legacy")
    try:
        revision = _stage(store)
        blob_id = store.put_media_blob(data=PNG, mime_type="image/png",
                                       width=WIDTH, height=HEIGHT)
        store.attach_occurrences(revision, [MediaOccurrenceSpec(
            occurrence_id="occ-1", blob_id=blob_id, kind="image", ordinal=0,
            mime_type="image/png", anchor_start=ANCHOR_START, anchor_end=ANCHOR_END)])
        store.commit_revision(revision, source_sha256=hashlib.sha256(b"report").hexdigest())
        media = store.list_media("report.md", revision_id=revision)[0]

        # 模拟旧实现留下的坏行：列值是像素尺寸
        occurrence = dict(media)
        occurrence["anchor_start"] = WIDTH
        occurrence["anchor_end"] = HEIGHT
        assert occurrence_span(MARKDOWN, occurrence) == (ANCHOR_START, ANCHOR_END)
    finally:
        store.close()


def test_half_specified_anchor_is_refused(tmp_path: Path):
    store = _store(tmp_path, "half")
    try:
        revision = _stage(store)
        blob_id = store.put_media_blob(data=PNG, mime_type="image/png")
        with pytest.raises(StoreContractError):
            store.attach_occurrences(revision, [MediaOccurrenceSpec(
                occurrence_id="occ-1", blob_id=blob_id, kind="image", ordinal=0,
                mime_type="image/png", anchor_start=ANCHOR_START)])
    finally:
        store.close()


def test_sink_passes_anchor_through_and_keeps_dimensions(tmp_path: Path):
    store = _store(tmp_path, "sink")
    try:
        sink = StoreMediaSink(store)
        sink.add(name="fig.png", data=PNG, kind="image", ordinal=1, mime_type="image/png",
                 width=WIDTH, height=HEIGHT, anchor_start=ANCHOR_START, anchor_end=ANCHOR_END)
        item = sink.items[-1]
        assert item.anchor_start == ANCHOR_START and item.anchor_end == ANCHOR_END
        assert item.width == WIDTH and item.height == HEIGHT, "尺寸仍作为媒体尺寸保留"
        assert item.anchor_start != item.width
    finally:
        store.close()
