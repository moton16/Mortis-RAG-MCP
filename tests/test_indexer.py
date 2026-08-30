from __future__ import annotations

import time
from pathlib import Path

from vault_mcp.config import AppConfig, EmbeddingConfig
from vault_mcp.indexer import MarkdownIndexer


def test_markdown_chunks_include_heading_lines_index_and_tags(tmp_path):
    note = tmp_path / "中文笔记.md"
    note.write_text(
        "---\ntags: [python, rag]\n---\n# 标题\n第一段\n第二段\n\n## 小节\n第三段\n",
        encoding="utf-8",
    )
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8)))

    chunks = indexer.sync()

    assert len(chunks) >= 2
    first = chunks[0]
    assert first.source == "中文笔记.md"
    assert first.title == "标题"
    assert first.metadata["heading"] == "标题"
    assert first.metadata["start_line"] == 4
    assert first.metadata["end_line"] >= first.metadata["start_line"]
    assert first.metadata["chunk_index"] == 0
    assert first.metadata["tags"] == ["python", "rag"]
    assert first.id


def test_incremental_add_modify_delete_and_rename(tmp_path):
    old = tmp_path / "old.md"
    old.write_text("# Old\nold content", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))

    indexer.sync()
    assert any(chunk.source == "old.md" and "old content" in chunk.content for chunk in indexer.search("old"))

    old.write_text("# New\nnew content", encoding="utf-8")
    indexer.sync()
    # 修改后旧内容必须从索引移除（增量更新），而非仍然可召回。
    assert not any("old content" in chunk.content for chunk in indexer.all_chunks())
    assert any("new content" in chunk.content for chunk in indexer.search("new"))

    renamed = tmp_path / "重命名.md"
    old.rename(renamed)
    indexer.sync()
    assert not any(chunk.source == "old.md" for chunk in indexer.all_chunks())
    assert any(chunk.source == "重命名.md" for chunk in indexer.all_chunks())

    renamed.unlink()
    indexer.sync()
    assert not indexer.all_chunks()


def test_search_returns_structured_chunk_fields_and_read_returns_raw_lines(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Heading\nline one\nline two\nline three\n", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    indexer.sync()

    results = indexer.search("line two")
    assert results
    chunk = results[0]
    payload = chunk.to_dict()
    assert payload["source"] == "note.md"
    assert payload["metadata"]["heading"] == "Heading"
    assert payload["metadata"]["start_line"] == 1
    assert payload["metadata"]["end_line"] == 4
    assert payload["id"]
    assert indexer.read("note.md", 2, 3) == "line one\nline two"


def test_ignores_obsidian_temp_and_non_markdown_files(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "ignored.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "draft.tmp.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "image.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "ok.md").write_text("# OK\nkept", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))

    indexer.sync()

    assert [chunk.source for chunk in indexer.all_chunks()] == ["ok.md"]


def test_search_reranker_failure_keeps_lexical_results(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Heading\nneedle text", encoding="utf-8")

    class BrokenReranker:
        def rerank(self, query, documents):
            raise OSError("offline")

    indexer = MarkdownIndexer(
        tmp_path,
        AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)),
        reranker_provider=BrokenReranker(),
    )
    indexer.sync()

    assert indexer.search("needle", use_rerank=True)[0].source == "note.md"
