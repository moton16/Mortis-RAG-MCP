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
    with pytest.raises(StoreConflict, match="IMPORT_CONFLICT"):
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


def test_import_rechecks_new_job_after_derived_staging(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _write_notes(vault)
    cfg = _config(tmp_path)
    indexer = MarkdownIndexer(vault, cfg, embedding_provider=CountingProvider())
    indexer.sync()
    _commit_fact(indexer, "a.md", (vault / "a.md").read_bytes(), "parsed-A")
    archive = tmp_path / "snapshot.zip"
    indexer.export_snapshot(archive)
    original = indexer.document_store(write=True).generation_id
    writer = DocumentStore(indexer.document_store(write=True).layout, cfg)
    writer.open(write=True)
    stage = snapshot_mod._stage_derived_layers
    def interleave(*args, **kwargs):
        stage(*args, **kwargs)
        writer.enqueue_job(source="late.pdf", source_sha256="b" * 64,
                           parser_fingerprint="late-writer")
    monkeypatch.setattr(snapshot_mod, "_stage_derived_layers", interleave)
    try:
        with pytest.raises(StoreBusy, match="IMPORT_BUSY"):
            snapshot_mod.import_snapshot(indexer, archive, replace=True, confirm_replace=True)
        assert indexer.document_store(write=True).generation_id == original
        assert writer.list_jobs(source="late.pdf")[0].state == "queued"
    finally:
        writer.close()
        indexer.document_store(write=True).close()


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

    with pytest.raises(StoreBusy, match="IMPORT_BUSY"):
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

    # E04-c：门禁改为全队扫描 `has_active_ingest()`（不再只查最近 500 条），
    # 读不到队列仍然 fail closed。
    monkeypatch.setattr(DocumentStore, "has_active_ingest", boom)
    with pytest.raises(StoreBusy, match="ingest queue"):
        snapshot_mod.import_snapshot(indexer, snapshot)


def test_import_failure_rolls_back_live_derived_files(tmp_path, monkeypatch):
    """发布阶段任一步失败必须把活派生文件与内存态恢复到导入前（§20.2/§20.5）。"""
    vault = tmp_path / "vault"
    _write_notes(vault)
    (vault / "a.md").write_text("# Recovery\n\nrollbackneedle original text.\n", encoding="utf-8")
    cfg = _config(tmp_path)
    indexer = MarkdownIndexer(vault, cfg, embedding_provider=CountingProvider())
    indexer.sync()
    _commit_fact(indexer, "a.md", (vault / "a.md").read_bytes(), "parsed-A")
    indexer.failed_files["failed.pdf"] = "fixture parser unavailable"
    indexer._save_failed_files()
    snap = tmp_path / "snap.zip"
    indexer.export_snapshot(snap)

    before_bytes = indexer._chunks_cache_path.read_bytes()
    before_ids = {chunk.id for chunk in indexer.all_chunks()}
    assert before_ids
    before_vectors = indexer._vectors_cache_path.read_bytes()
    before_values = {key: list(value) for key, value in
                     indexer._vector_backend.get_vectors(before_ids).items()}
    assert before_values, "vector recovery needs a nonempty baseline, not vacuous equality"
    before_backend_ids = indexer._vector_backend.list_ids()
    assert indexer._fts is not None and indexer._fts.available
    before_fts = indexer._fts.search('"rollbackneedle"', 10)
    assert before_fts
    before_failed = dict(indexer.failed_files)
    store = indexer.document_store()
    before_generation = store.generation_id
    with ControlStore(store.layout) as control:
        control.open(create=False)
        before_epoch = control.state().epoch

    real_stage = snapshot_mod._stage_derived_layers

    def failing_stage(owner, text_target, vectors, compatible):
        real_stage(owner, text_target, vectors, compatible)  # 先制造半截状态
        raise RuntimeError("boom after derived install")

    monkeypatch.setattr(snapshot_mod, "_stage_derived_layers", failing_stage)
    with pytest.raises(RuntimeError, match="^boom after derived install$"):
        indexer.import_snapshot(snap, replace=True, confirm_replace=True)

    assert indexer._chunks_cache_path.read_bytes() == before_bytes
    assert {chunk.id for chunk in indexer.all_chunks()} == before_ids
    assert indexer._vectors_cache_path.read_bytes() == before_vectors
    assert indexer._vector_backend.list_ids() == before_backend_ids
    assert {key: list(value) for key, value in
            indexer._vector_backend.get_vectors(before_ids).items()} == before_values
    assert indexer._fts.search('"rollbackneedle"', 10) == before_fts
    assert indexer.failed_files == before_failed
    assert store.generation_id == before_generation
    with ControlStore(store.layout) as control:
        control.open(create=False)
        assert control.state().epoch == before_epoch

    # A second session must observe the restored disk state, not just old RAM.
    indexer.close_document_store()
    indexer._fts.close()
    reopened = MarkdownIndexer(vault, cfg, embedding_provider=CountingProvider())
    try:
        assert {chunk.id for chunk in reopened.all_chunks()} == before_ids
        assert reopened._vectors_cache_path.read_bytes() == before_vectors
        assert reopened._vector_backend.list_ids() == before_backend_ids
        assert {key: list(value) for key, value in
                reopened._vector_backend.get_vectors(before_ids).items()} == before_values
        assert reopened._fts.search('"rollbackneedle"', 10) == before_fts
        assert reopened.failed_files == before_failed
        assert reopened.document_store().generation_id == before_generation
        with ControlStore(store.layout) as control:
            control.open(create=False)
            assert control.state().epoch == before_epoch
    finally:
        reopened.close_document_store()
        reopened._fts.close()
