"""E04-c：固定捕获版本 handle 的 validate/renew/release、到期不复活与备份接缝（NEW）。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import (
    ControlStore,
    DocumentStore,
    StoreConflict,
    resolve_storage_layout,
)
from mortis_rag_mcp.indexer import MarkdownIndexer

NOTE = "# 笔记\n\n可导出与备份的正文。\n"


def _config(tmp_path: Path, name: str = "vault") -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / f"cache-{name}"), enabled=True),
    )


def _store(tmp_path: Path, name: str = "vault") -> DocumentStore:
    vault = tmp_path / name
    vault.mkdir(parents=True, exist_ok=True)
    cfg = _config(tmp_path, name)
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)
    return store


def test_pin_validate_renew_release(tmp_path: Path):
    store = _store(tmp_path, "lifecycle")
    try:
        pin = store.capture_generation_pin()
        assert pin.generation_id
        assert store.validate_generation_pin(pin) is True
        # 未到期 + 同 owner 的 CAS 续租
        assert store.renew_generation_pin(pin) is True
        assert store.validate_generation_pin(pin) is True
        assert store.release_generation_pin(pin) is True
        # 释放后不可再用作验租依据（不复活）
        assert store.validate_generation_pin(pin) is False
        assert store.renew_generation_pin(pin) is False
    finally:
        store.close()


def test_expired_pin_is_not_revived(tmp_path: Path):
    store = _store(tmp_path, "expired")
    try:
        pin = store.capture_generation_pin(lease_seconds=0.01)
        time.sleep(0.05)
        assert store.validate_generation_pin(pin) is False, "到期 pin 仍被判为有效"
        assert store.renew_generation_pin(pin) is False, "过期 pin 被续租复活"
        # 另一个 owner 不能续租未到期的 pin
        fresh = store.capture_generation_pin()
        ctrl = ControlStore(store.layout)
        try:
            ctrl.open(create=False, write=True)
            assert ctrl.renew_generation_pin(fresh.pin_id, "someone-else") is False
            assert ctrl.release_generation_pin(fresh.pin_id, fresh.owner) is True
        finally:
            ctrl.close()
    finally:
        store.close()


def test_backup_progress_failure_discards_partial_output(tmp_path: Path):
    store = _store(tmp_path, "backup")
    try:
        staged = store.stage_revision(
            source="note.md", source_sha256="a" * 64, render_sha256="b" * 64,
            parser_fingerprint="fixture", markdown="parsed")
        store.commit_revision(staged.revision_id, source_sha256="a" * 64)
        target = tmp_path / "partial.sqlite"

        def _boom(status: int, remaining: int, total: int) -> None:
            raise RuntimeError("injected backup failure")

        with pytest.raises(RuntimeError):
            store.backup_to(target, pages=1, progress=_boom)
        assert not target.exists(), "备份失败却留下半截输出冒充成功"
        # 无 progress 的正常备份仍然可用
        assert store.backup_to(target).is_file()
    finally:
        store.close()


def test_export_rejects_when_pin_no_longer_valid(tmp_path: Path):
    """最终发布前验租失败 → 不写出快照（不把旧 chunk 与新 media 拼包）。"""
    vault = tmp_path / "vault-export"
    vault.mkdir()
    (vault / "note.md").write_text(NOTE, encoding="utf-8")
    indexer = MarkdownIndexer(vault, _config(tmp_path, "export"))
    try:
        indexer.sync()
        store = indexer.document_store(write=True)
        assert store.generation_id
        store.validate_generation_pin = lambda pin: False  # type: ignore[method-assign]
        out = tmp_path / "out.zip"
        with pytest.raises(StoreConflict, match="READ_LEASE_EXPIRED"):
            indexer.export_snapshot(out)
        assert not out.exists()
    finally:
        indexer.close_document_store()
        indexer.stop_watching()
