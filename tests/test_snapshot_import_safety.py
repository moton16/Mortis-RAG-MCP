"""C97 快照导入安全：ZIP 构造前准入、docstore 冲突/显式替换、失败回滚、忙线门禁。

合同来源：docs/v0.9.0/RESEARCH_REPORT.rev1.md §20.2 / §20.7C / §20.7F。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from mortis_rag_mcp._indexer import snapshot as snapshot_mod
from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import ControlStore, DocumentStore, StoreBusy, StoreConflict
from mortis_rag_mcp.indexer import MarkdownIndexer


class CountingProvider:
    def __init__(self, dimension: int = 8) -> None:
        self.calls = 0
        self.dimension = dimension

    def embed(self, texts):
        self.calls += 1
        return [[float(index % 7) / 7.0 for index in range(self.dimension)] for _ in texts]


def _config(tmp_path: Path, dimension: int = 8) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=dimension),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
    )


def _write_notes(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "a.md").write_text("# 甲\n\n第一份笔记。\n", encoding="utf-8")
    (vault / "b.md").write_text("# 乙\n\n第二份笔记。\n", encoding="utf-8")


def _commit_fact(indexer: MarkdownIndexer, source: str, payload: bytes, markdown: str) -> None:
    store = indexer.document_store(write=True)
    sha = hashlib.sha256(payload).hexdigest()
    staged = store.stage_revision(
        source=source,
        source_sha256=sha,
        render_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        parser_fingerprint="fixture",
        markdown=markdown,
    )
    store.commit_revision(staged.revision_id, source_sha256=sha)


def test_zip_pre_admission_rejects_member_flood_before_zipfile(tmp_path):
    """成员数超限在构造 ZipFile **之前**拒绝（§20.7C），不是先分配再拒。"""
    vault = tmp_path / "vault"
    _write_notes(vault)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())

    flood = tmp_path / "flood.zip"
    with zipfile.ZipFile(flood, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "vault-mcp-snapshot", "format_version": 1}))
        for index in range(snapshot_mod._SNAPSHOT_MAX_MEMBERS + 8):
            zf.writestr(f"junk{index}.bin", b"")

    with pytest.raises(ValueError, match="rejected before reading"):
        indexer.import_snapshot(flood)


def test_docstore_import_requires_explicit_replace_and_retains_backup(tmp_path):
    """默认不 merge 未知包；显式 replace+confirm 才替换且旧 generation 可恢复（§20.7F）。"""
    vault_a = tmp_path / "A" / "vault"
    _write_notes(vault_a)
    indexer_a = MarkdownIndexer(vault_a, _config(tmp_path / "A"), embedding_provider=CountingProvider())
    indexer_a.sync()
    _commit_fact(indexer_a, "a.md", (vault_a / "a.md").read_bytes(), "parsed-A")
    snap = tmp_path / "A" / "snap.zip"
    indexer_a.export_snapshot(snap)

    vault_b = tmp_path / "B" / "vault"
    _write_notes(vault_b)
    indexer_b = MarkdownIndexer(vault_b, _config(tmp_path / "B"), embedding_provider=CountingProvider())
    indexer_b.sync()
    _commit_fact(indexer_b, "b.md", (vault_b / "b.md").read_bytes(), "parsed-B")
    store_b = indexer_b.document_store(write=True)
    original = store_b.generation_id
    assert original

    # 默认拒绝：本机已有 committed 事实，未知包不得静默替换。
    with pytest.raises(StoreConflict):
        snapshot_mod.import_snapshot(indexer_b, snap)
    assert store_b.generation_id == original

    # 显式确认才替换；被替换 generation 登记 retained_backup（不删除）。
    snapshot_mod.import_snapshot(indexer_b, snap, replace=True, confirm_replace=True)
    assert store_b.generation_id != original
    ctrl = ControlStore(store_b.layout)
    try:
        states = {record.generation_id: record.state for record in ctrl.list_generations()}
    finally:
        ctrl.close()
    assert states.get(original) == "retained_backup"


def test_active_ingest_blocks_import_with_import_busy(tmp_path):
    """存在活跃摄取任务时默认拒绝导入（§20.7F），不覆盖未完成资产。"""
    vault = tmp_path / "vault"
    _write_notes(vault)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    snap = tmp_path / "snap.zip"
    indexer.export_snapshot(snap)

    store = indexer.document_store(write=True)
    store.enqueue_job(
        source="a.md",
        source_sha256=hashlib.sha256((vault / "a.md").read_bytes()).hexdigest(),
        parser_fingerprint="fixture",
    )

    with pytest.raises(StoreBusy):
        indexer.import_snapshot(snap)


def test_unreadable_ingest_queue_blocks_import(tmp_path, monkeypatch):
    """读不到摄取队列 ≠ 没有活跃任务：忙线门禁必须 fail closed（旧实现静默放行）。"""
    vault = tmp_path / "vault"
    _write_notes(vault)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    _commit_fact(indexer, "a.md", (vault / "a.md").read_bytes(), "parsed-A")
    snapshot = tmp_path / "snap.zip"
    indexer.export_snapshot(snapshot)

    def boom(self, **kwargs):
        raise RuntimeError("control db locked")

    monkeypatch.setattr(DocumentStore, "list_jobs", boom)
    with pytest.raises(StoreBusy, match="ingest queue"):
        snapshot_mod.import_snapshot(indexer, snapshot)


def test_import_failure_rolls_back_live_derived_files(tmp_path, monkeypatch):
    """发布阶段任一步失败必须把活派生文件与内存态恢复到导入前（§20.2/§20.5）。"""
    vault = tmp_path / "vault"
    _write_notes(vault)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    snap = tmp_path / "snap.zip"
    indexer.export_snapshot(snap)

    before_bytes = indexer._chunks_cache_path.read_bytes()
    before_ids = {chunk.id for chunk in indexer.all_chunks()}
    assert before_ids

    real_stage = snapshot_mod._stage_derived_layers

    def failing_stage(owner, text_target, vectors, compatible):
        real_stage(owner, text_target, vectors, compatible)  # 先制造半截状态
        raise RuntimeError("boom after derived install")

    monkeypatch.setattr(snapshot_mod, "_stage_derived_layers", failing_stage)
    with pytest.raises(RuntimeError):
        indexer.import_snapshot(snap)

    assert indexer._chunks_cache_path.read_bytes() == before_bytes
    assert {chunk.id for chunk in indexer.all_chunks()} == before_ids
