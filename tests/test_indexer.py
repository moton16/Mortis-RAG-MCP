from __future__ import annotations

import time
from pathlib import Path

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


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


def test_incremental_evicts_stale_when_mtime_does_not_advance(tmp_path):
    """等长内容替换 + mtime 未推进时，增量同步必须仍然淘汰旧 chunk。

    回归测试：快速路径曾用 (mtime_ns, size) 相等短路 sha256，而 Windows 文件
    时间戳粒度受系统时钟中断（约 15.6ms）限制，等长替换（"old content" →
    "new content"）可能落在同一刻度内，使 mtime_ns 与 size 双双不变，导致旧
    chunk 静默残留、继续被召回（Windows CI 稳定复现，ext4 纳秒精度掩盖了它）。
    os.utime 把 mtime 精确回拨到变换前的值，可确定性复现同一失效模式。
    """
    import os

    note = tmp_path / "old.md"
    note.write_text("# Old\nold content", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    indexer.sync()
    assert any("old content" in chunk.content for chunk in indexer.all_chunks())

    before = note.stat()
    note.write_text("# New\nnew content", encoding="utf-8")
    # size 保持不变，并把 mtime 回拨到变换前：快速路径签名完全不变。
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = note.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns

    indexer.sync()
    assert not any("old content" in chunk.content for chunk in indexer.all_chunks())
    assert any("new content" in chunk.content for chunk in indexer.all_chunks())


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


def test_table_atomic_guard_in_chunker(tmp_path):
    note = tmp_path / "table_note.md"
    note.write_text(
        "# Real Heading\n"
        "Introduction\n"
        "<table>\n"
        "<tr><td># Fake Heading In Table</td></tr>\n"
        "<tr><td>data row 1</td></tr>\n"
        "<tr><td>data row 2</td></tr>\n"
        "</table>\n"
        "After table text\n",
        encoding="utf-8",
    )
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    chunks = indexer.sync()

    # The fake heading inside table must NOT become a chunk's heading or title
    headings = {c.metadata.get("heading") for c in chunks}
    assert "# Fake Heading In Table" not in headings
    assert "Fake Heading In Table" not in headings
    assert "Real Heading" in headings

    # The entire table block is preserved intact in chunk content
    table_chunks = [c for c in chunks if "<table>" in c.content]
    assert len(table_chunks) == 1
    assert "</table>" in table_chunks[0].content
    assert "data row 1" in table_chunks[0].content

