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
    """等长内容替换 + 时间戳未推进时，增量同步必须仍然淘汰旧 chunk。

    回归测试：快速路径曾用 (mtime_ns, size) 相等短路 sha256，但文件时间戳并非
    真纳秒（NTFS 实测刻度约 3ms，FAT32 达 2s）。等长替换（"old content" ->
    "new content"，均 18 字节）若与索引落盘落在同一刻度内，mtime_ns 与 size
    双双不变，文件被误判为未修改，旧 chunk 静默残留并继续被召回（Windows CI
    稳定复现；ext4 纳秒精度掩盖了它）。

    构造方式：先完成一次正常 sync，再把笔记的 mtime 钉到与“该条目的观测时刻
    _stat_seen_ns 完全相等”，并把 _stat_cache 的签名同步成该 (mtime, size)。此时
    新一轮读到的 (mtime_ns, size) 与缓存逐位相同——签名判据为真；但 mtime 并不
    严格早于观测时刻，构成 racily clean 条目，可信度判据成为唯一决定因素：禁用
    判据时本用例必然失败（已验证），启用时回退 sha256 精确检出内容变化。

    这里刻意不依赖 cache 是否启用：判据基于 _stat_seen_ns（进程时钟内部自比），
    在 AppConfig 默认的 cache.enabled=False 下同样生效——这一点很关键，上游的
    test_incremental_add_modify_delete_and_rename 正是在默认配置下暴露该 bug 的。

    易踩的坑：若只在 sync 之后改文件、不把签名对齐进 _stat_cache，缓存里留的是
    上一轮真实 mtime，签名判据会先短路为假，文件直接走 sha256 被正确检出，用例
    恒绿而毫无鉴别力——这正是本用例初版失效的原因。
    """
    import os

    note = tmp_path / "old.md"
    note.write_text("# Old\nold content", encoding="utf-8")
    indexer = MarkdownIndexer(
        tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4))
    )
    indexer.sync()
    assert any("old content" in chunk.content for chunk in indexer.all_chunks())
    assert "old.md" in indexer._stat_seen_ns

    # 把笔记 mtime 钉到“该条目的观测时刻”，复现时间戳粒度碰撞下的 racily clean：
    # mtime 与 seen 相等 => mtime < seen 为假 => 判据必须拒绝信任签名。
    seen = indexer._stat_seen_ns["old.md"]
    os.utime(note, ns=(seen, seen))
    indexer._stat_cache["old.md"] = (seen, note.stat().st_size)

    before = note.stat()
    note.write_text("# New\nnew content", encoding="utf-8")
    # 等长内容 + mtime 恢复成同一时刻：新一轮 (mtime_ns, size) 与缓存逐位相同。
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = note.stat()
    assert after.st_size == before.st_size, "本用例前提：内容等长"
    assert after.st_mtime_ns == before.st_mtime_ns, "本用例前提：时间戳未推进"
    assert indexer._stat_cache["old.md"] == (
        int(after.st_mtime_ns),
        int(after.st_size),
    ), "本用例前提：快速路径签名判据为真，可信度判据才是决定因素"
    assert indexer._fast_path_is_trustworthy("old.md", int(after.st_mtime_ns)) is False, (
        "本用例前提：该条目必须被判定为不可信（racily clean），才具备鉴别力"
    )

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

