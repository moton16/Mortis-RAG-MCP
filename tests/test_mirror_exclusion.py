"""C97：旧镜像「来源精确排除」——已入库 source 的同名镜像不再双份检索。

未入库的镜像必须继续可见（legacy 兼容），不得整目录忽略 `.mortis-parsed/`。

E03-a：路径吻合不是归属证明。同路径上的**普通用户文件**必须继续可检索，
只有经单项 proof（frontmatter source/SHA + 物理源 SHA + 唯一 ledger 归属）
验证的镜像才被排除。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import DocumentStore, normalize_source_path, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer

MIRROR = "# Mirror\n\nmirrored body text\n"


def _write_ledger(vault: Path, source: str, sha: str) -> None:
    (vault / ".mortis-parsed" / ".ingest_state.json").write_text(
        json.dumps({"jobs": {"j1": {"source": source, "state": "done", "sha256": sha}}}),
        encoding="utf-8",
    )


def _write_proven_mirror(vault: Path, source: str, body: str = MIRROR,
                         *, mirror_rel: str | None = None) -> str:
    """写一个**可证明归属**的旧镜像：frontmatter + 物理源 SHA + 唯一 ledger。"""
    sha = hashlib.sha256((vault / source).read_bytes()).hexdigest()
    rel = mirror_rel or (source.rsplit(".", 1)[0] + ".md")
    target = vault / ".mortis-parsed" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        f"source_pdf: {source}\n"
        f"source_sha256: {sha}\n"
        "parsed_by: mineru-v4\n"
        "---\n" + body,
        encoding="utf-8",
    )
    _write_ledger(vault, source, sha)
    return ".mortis-parsed/" + rel


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
    # 已证明归属的镜像（frontmatter + 物理源 SHA + 唯一 ledger done）
    _write_proven_mirror(vault, "papers/paper.pdf")
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


def test_same_path_unowned_file_remains_searchable(tmp_path: Path):
    """F04 反例：同路径、无归属证明的普通文件不得因「已入库 source」被排除。"""
    vault = tmp_path / "vault"
    (vault / "papers").mkdir(parents=True)
    (vault / ".mortis-parsed" / "papers").mkdir(parents=True)
    (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 payload")
    # 同路径上只是用户的普通 markdown：无 frontmatter、无 ledger → 无归属证明
    (vault / ".mortis-parsed" / "papers" / "paper.md").write_text(
        "# User notes\n\nmy own summary of the paper\n", encoding="utf-8")
    cfg = _config(tmp_path)
    _commit_doc(cfg, vault, "papers/paper.pdf")
    before = hashlib.sha256((vault / "papers" / "paper.pdf").read_bytes()).hexdigest()

    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer.sync()
        sources = set(indexer._chunks)
        assert "papers/paper.pdf" in sources
        assert ".mortis-parsed/papers/paper.md" in sources, "无归属证明的普通文件被误排除"
    finally:
        indexer.close_document_store()
    assert hashlib.sha256((vault / "papers" / "paper.pdf").read_bytes()).hexdigest() == before


def test_proven_mirror_exclusion_is_re_evaluated_when_source_changes(tmp_path: Path):
    """证明失效（物理源 SHA 变化）后重新评估：普通文件不得被永久隐藏。"""
    vault = tmp_path / "vault"
    (vault / "papers").mkdir(parents=True)
    (vault / ".mortis-parsed" / "papers").mkdir(parents=True)
    (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 payload")
    _write_proven_mirror(vault, "papers/paper.pdf")
    cfg = _config(tmp_path)
    _commit_doc(cfg, vault, "papers/paper.pdf")

    indexer = MarkdownIndexer(vault, cfg)
    try:
        indexer.sync()
        assert ".mortis-parsed/papers/paper.md" not in indexer._chunks
        (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 CHANGED")
        indexer.sync()
        assert ".mortis-parsed/papers/paper.md" in indexer._chunks, "证明失效后仍隐藏该文件"
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
