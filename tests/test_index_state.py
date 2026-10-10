"""E04-b：additive `index_state` 合同（empty/rebuilding/unverified/ready）。

单库检索与导入消费同一 helper；隔离事实不会被 ready 自动激活。
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from pathlib import Path

from mortis_rag_mcp._indexer import watch
from mortis_rag_mcp._server.search_dispatch import _search_single_vault
from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.server import VaultMcpServer

NOTE = "# 笔记\n\n可检索的正文内容。\n"


def _config(tmp_path: Path, name: str) -> AppConfig:
    # cache.dir 必须在库外：库内缓存会被虚拟存储写入门禁拒绝。
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / f"cache-{name}"), enabled=True),
    )


def _indexer(tmp_path: Path, name: str = "vault") -> MarkdownIndexer:
    vault = tmp_path / f"vault-{name}"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "note.md").write_text(NOTE, encoding="utf-8")
    return MarkdownIndexer(vault, _config(tmp_path, name))


def _hidden_fact(tmp_path: Path, indexer: MarkdownIndexer, visibility: str = "unverified") -> None:
    """在文档库里放一条**隔离**的已解析事实（不可见于检索）。"""
    layout = resolve_storage_layout(indexer.config, Path(indexer.vault_path))
    store = DocumentStore(layout, indexer.config)
    store.open(write=True)
    try:
        staged = store.stage_revision(
            source="hidden.pdf",
            source_sha256=hashlib.sha256(b"payload").hexdigest(),
            render_sha256=hashlib.sha256(b"parsed").hexdigest(),
            parser_fingerprint="mineru-v4:current",
            markdown="parsed",
            capabilities={"coverage": "full"},
        )
        store.commit_revision(staged.revision_id,
                              source_sha256=hashlib.sha256(b"payload").hexdigest())
        store.set_visibility("hidden.pdf", visibility)
    finally:
        store.close()


def test_empty_before_any_sync(tmp_path: Path):
    indexer = _indexer(tmp_path, "empty")
    try:
        state = indexer.index_state()
        assert state["index_state"] == "empty"
        assert state["isolated_facts"] == 0 and state["visible_sources"] == 0
        assert "kb_init" in state["next_action"]
    finally:
        indexer.stop_watching()


def test_ready_after_sync(tmp_path: Path):
    indexer = _indexer(tmp_path, "ready")
    try:
        indexer.sync()
        state = indexer.index_state()
        assert state["index_state"] == "ready"
        assert state["visible_sources"] >= 1
        assert state["next_action"] == ""
    finally:
        indexer.stop_watching()


def test_rebuilding_while_refresh_is_pending(tmp_path: Path):
    indexer = _indexer(tmp_path, "rebuilding")
    try:
        indexer.sync()
        assert indexer.index_state()["index_state"] == "ready"
        assert watch.request_refresh(indexer, start_scheduler=False) is True
        state = indexer.index_state()
        assert state["index_state"] == "rebuilding"
        assert "background sync" in state["next_action"]
    finally:
        indexer.stop_watching()


def test_unverified_reports_isolated_facts_and_does_not_activate_them(tmp_path: Path):
    indexer = _indexer(tmp_path, "unverified")
    try:
        indexer.sync()
        _hidden_fact(tmp_path, indexer)
        state = indexer.index_state()
        assert state["index_state"] == "unverified"
        assert state["isolated_facts"] == 1
        assert state["visible_sources"] >= 1, "混合库保留可见内容计数"
        assert "not activated" in state["next_action"]
        # ready/unverified 都不自动激活隔离事实：检索结果里没有 hidden.pdf
        assert all(chunk.source != "hidden.pdf" for chunk in indexer.all_chunks())
    finally:
        indexer.stop_watching()


def test_single_vault_search_reports_state_when_not_ready(tmp_path: Path):
    indexer = _indexer(tmp_path, "dispatch")
    try:
        indexer.sync()
        _hidden_fact(tmp_path, indexer, visibility="exempt")
        server = object.__new__(VaultMcpServer)
        server.registry = SimpleNamespace(get=lambda path: None)
        result = _search_single_vault(server, indexer, "笔记", 5, False, None, False,
                                      None, False, None, None)
        # 检索入口自己先登记了一次刷新 → 按优先级口径是 rebuilding；隔离事实数量照常给出。
        assert result["index_state"] in ("rebuilding", "unverified")
        assert result["isolated_facts"] == 1
        assert result["next_action"]
        # 原字段保留
        assert "chunks" in result
    finally:
        indexer.stop_watching()
