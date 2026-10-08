"""C97：旧镜像「来源精确排除」——已入库 source 的同名镜像不再双份检索。

未入库的镜像必须继续可见（legacy 兼容），不得整目录忽略 `.mortis-parsed/`。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import DocumentStore, normalize_source_path, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer

MIRROR = "# Mirror\n\nmirrored body text\n"


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    return cfg


def _commit_doc(cfg: AppConfig, vault: Path, source: str) -> None:
    layout = resolve_storage_layout(cfg, vault)
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    try:
        source = normalize_source_path(source, vault)
        physical = vault / source
        staged = store.stage_revision(
            source=source,
            source_sha256=hashlib.sha256(physical.read_bytes()).hexdigest(),
            render_sha256=hashlib.sha256(MIRROR.encode("utf-8")).hexdigest(),
            parser_fingerprint="legacy-mirror-v1:test",
            markdown=MIRROR,
            capabilities={"coverage": "full"},
        )
        store.commit_revision(staged.revision_id)
    finally:
        store.close()


def test_indexed_document_mirror_is_excluded(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / ".mortis-parsed" / "papers").mkdir(parents=True)
    (vault / "papers").mkdir(parents=True, exist_ok=True)
    (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 payload")
    (vault / ".mortis-parsed" / "papers" / "paper.md").write_text(MIRROR, encoding="utf-8")
    (vault / "note.md").write_text("# Note\n\nreal note\n", encoding="utf-8")
    cfg = _config(tmp_path)
    _commit_doc(cfg, vault, "papers/paper.pdf")

    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer.sync()
        sources = set(indexer._chunks)
        assert "papers/paper.pdf" in sources          # 虚拟文档仍可检索
        assert ".mortis-parsed/papers/paper.md" not in sources
        assert "note.md" in sources                    # 普通笔记不受影响
        # 用户自己的镜像目录内容（无对应入库 source）必须继续可见
        (vault / ".mortis-parsed" / "own.md").write_text("# Own\n\nuser content\n", encoding="utf-8")
        indexer.sync()
        assert ".mortis-parsed/own.md" in indexer._chunks
    finally:
        indexer.close_document_store()


def test_mirror_stays_indexed_without_store_fact(tmp_path: Path):
    """没有文档库事实时，镜像仍是普通物理文本（legacy 回退行为不变）。"""
    vault = tmp_path / "vault"
    (vault / ".mortis-parsed").mkdir(parents=True)
    (vault / ".mortis-parsed" / "paper.md").write_text(MIRROR, encoding="utf-8")
    cfg = _config(tmp_path)
    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer.sync()
        assert ".mortis-parsed/paper.md" in indexer._chunks
    finally:
        indexer.close_document_store()
