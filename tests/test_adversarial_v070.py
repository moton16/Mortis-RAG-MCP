from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig, IngestConfig
from mortis_rag_mcp.indexer import MarkdownIndexer, SearchFilter
from mortis_rag_mcp.ingest import IngestManager
from mortis_rag_mcp.ingest.mineru import MineruError
from mortis_rag_mcp.ingest.tables import convert_small_tables, split_large_table


# ----------------------------------------------------------------------
# Gate 1 (D2-B): 未闭合 <table> + 后续 150 行正文 -> 产物行数守恒、正文一字不少
# ----------------------------------------------------------------------
def test_gate_1_unclosed_table_preserves_body():
    head = [f"# 第 {i} 节" for i in range(1, 40)]
    tbl = [
        "<table><tr><th>参数</th><th>取值</th></tr>",
        "<tr><td>额定电压</td><td>3.3V</td></tr>",
        "<tr><td>管脚号</td><td>12</td></tr>",
    ]
    tail = [f"表格之后的正文第 {i} 段，包含大量需要被检索的知识点。" for i in range(1, 151)]
    doc = "\n".join(head + tbl + tail) + "\n"
    out = convert_small_tables(doc)
    assert len(out.splitlines()) == len(doc.splitlines())
    for t in tail:
        assert t in out


# ----------------------------------------------------------------------
# Gate 2 (D2-C): ```html 围栏内的完整小表 -> 原样保留，不得转成 pipe
# ----------------------------------------------------------------------
def test_gate_2_fenced_code_table_untouched():
    code = "```html\n<table>\n<tr><th>标签</th></tr>\n<tr><td>table</td></tr>\n</table>\n```\n"
    out = convert_small_tables(code)
    assert out == code


# ----------------------------------------------------------------------
# Gate 3 (D2-A): 含未闭合表的文档 -> 单个 chunk 长度 <= chunk_size * 1.5
# ----------------------------------------------------------------------
def test_gate_3_unclosed_table_chunk_bounded(tmp_path: Path):
    head = [f"# 第 {i} 节\n\n正文内容。" for i in range(1, 20)]
    tbl = ["<table><tr><th>参数</th><th>取值</th></tr>"] + [
        f"<tr><td>参数{i}</td><td>值{i}</td></tr>" for i in range(100)
    ]
    tail = [f"正文后续第 {i} 段内容。" for i in range(1, 20)]
    doc = "\n\n".join(head + tbl + tail)
    (tmp_path / "doc.md").write_text(doc, encoding="utf-8")
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4), chunk_size=500)
    indexer = MarkdownIndexer(tmp_path, cfg)
    chunks = indexer.sync()
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= 1200


# ----------------------------------------------------------------------
# Gate 4 (D3): 8 行 × 3000 字符的闭合大表 -> 所有 chunk 长度 <= chunk_size
# ----------------------------------------------------------------------
def test_gate_4_closed_wide_table_all_chunks_within_size(tmp_path: Path):
    wide = (
        "# 章节\n\n正文一段。\n\n"
        + "\n".join(f"<tr><td>{'甲乙丙丁' * 750}</td></tr>" for _ in range(8))
        + "\n\n表格之后的正文。\n"
    )
    wide = wide.replace("<tr>", "<table>\n<tr>", 1) + "</table>\n"
    (tmp_path / "wide.md").write_text(wide, encoding="utf-8")
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4), chunk_size=1200)
    indexer = MarkdownIndexer(tmp_path, cfg)
    chunks = indexer.sync()
    for c in chunks:
        assert len(c.content) <= cfg.chunk_size, f"chunk length {len(c.content)} exceeds {cfg.chunk_size}"


# ----------------------------------------------------------------------
# Gate 5 (D4a): split_large_table 保真断言：行集合守恒且 <tr> 计数守恒
# ----------------------------------------------------------------------
def test_gate_5_split_large_table_fidelity():
    table = (
        ["<table><tr><th>列A</th><th>列B</th></tr>"]
        + [f"<tr><td>数据{i}</td><td>值{i}</td></tr>" for i in range(60)]
        + ["</table>"]
    )
    parts = split_large_table(table, 10, repeat_header=False)
    flat = [l for p in parts for l in p]
    clean_tag = lambda s: s.replace("<table>", "").replace("</table>", "").strip()
    table_content_lines = [clean_tag(l) for l in table if clean_tag(l)]
    flat_content_lines = [clean_tag(l) for l in flat if clean_tag(l)]
    assert set(table_content_lines) == set(flat_content_lines)
    assert sum(l.count("<tr>") for l in table) == sum(l.count("<tr>") for l in flat)


# ----------------------------------------------------------------------
# Gate 6 (D4b): 大表分片后每个子片均含表头行
# ----------------------------------------------------------------------
def test_gate_6_split_table_repeat_header():
    table = (
        ["<table>\n<tr><th>列A</th><th>列B</th></tr>"]
        + [f"<tr><td>数据{i}</td><td>值{i}</td></tr>" for i in range(60)]
        + ["</table>"]
    )
    parts = split_large_table(table, 10, repeat_header=True)
    assert len(parts) > 1
    for p in parts:
        assert "<th>列A</th>" in "".join(p)


# ----------------------------------------------------------------------
# Gate 7 (D4c): 分片后所有 chunk 的 start_line/end_line 与磁盘原文一致
# ----------------------------------------------------------------------
def test_gate_7_chunk_line_numbers_match_file(tmp_path: Path):
    table = (
        ["# 标题", "", "前置正文", "<table>", "<tr><th>Col1</th><th>Col2</th></tr>"]
        + [f"<tr><td>{i}</td><td>val_{i}</td></tr>" for i in range(50)]
        + ["</table>", "", "后续正文"]
    )
    content = "\n".join(table)
    file_path = tmp_path / "table_lines.md"
    file_path.write_text(content, encoding="utf-8")
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4), chunk_size=300)
    indexer = MarkdownIndexer(tmp_path, cfg)
    chunks = indexer.sync()
    file_lines = content.splitlines()
    for c in chunks:
        s_line = c.metadata["start_line"]
        e_line = c.metadata["end_line"]
        assert 1 <= s_line <= len(file_lines)
        assert s_line <= e_line <= len(file_lines)
        slice_lines = file_lines[s_line - 1 : e_line]
        if "后续正文" in c.content:
            assert any("后续正文" in l for l in slice_lines)


# ----------------------------------------------------------------------
# Gate 8 (D5): 单行表格（> chunk_size）每个 chunk 都是完整合法片段
# ----------------------------------------------------------------------
def test_gate_8_single_line_table_wrapped(tmp_path: Path):
    one_line = (
        "# 章节\n\n<table>"
        + "".join(f"<tr><td>cell{i}</td></tr>" for i in range(400))
        + "</table>\n"
    )
    (tmp_path / "single_line.md").write_text(one_line, encoding="utf-8")
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4), chunk_size=500)
    indexer = MarkdownIndexer(tmp_path, cfg)
    chunks = indexer.sync()
    for c in chunks:
        if "cell" in c.content:
            assert "<table" in c.content and "</table>" in c.content


# ----------------------------------------------------------------------
# Gate 9 (D7): 注入 retryable=True 的 MineruError -> 任务不被标记 done
# ----------------------------------------------------------------------
def test_gate_9_retryable_mineru_error(tmp_path: Path):
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True))
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy content")
    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", side_effect=MineruError("Rate limited", retryable=True)):
        mgr.submit(["sample.pdf"])
        if mgr._worker:
            mgr._worker.join(timeout=5.0)
        status = mgr.status()
        assert status["summary"].get("done", 0) == 0
        job = status["jobs"][0]
        assert job["state"] == "failed"
        assert job.get("retryable") is True
        # force 重试
        mgr.submit(["sample.pdf"], force=True)
        status2 = mgr.status()
        assert status2["summary"].get("queued", 0) + status2["summary"].get("parsing", 0) >= 1


# ----------------------------------------------------------------------
# Gate 10 (D1 / D12): sources 参数校验
# ----------------------------------------------------------------------
def test_gate_10_sources_validation(tmp_path: Path):
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True))
    with pytest.raises((ValueError, TypeError), match="sources must be a list"):
        mgr.submit("sample.pdf")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="path traversal"):
        mgr.submit(["../secret.pdf"])
    with pytest.raises(ValueError, match="path traversal"):
        mgr._validate_safe_source("../../etc/passwd")


# ----------------------------------------------------------------------
# Gate 11 (D8a): pending 剪枝
# ----------------------------------------------------------------------
def test_gate_11_scan_pending_skips_excluded_dirs(tmp_path: Path):
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True))
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "huge.pdf").write_bytes(b"%PDF-1.4 test")
    node_dir = tmp_path / "node_modules"
    node_dir.mkdir()
    (node_dir / "lib.pdf").write_bytes(b"%PDF-1.4 test")
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(b"%PDF-1.4 test")

    pending = mgr.scan_pending()
    sources = [p["source"] for p in pending]
    assert "valid.pdf" in sources
    assert not any(".git" in s for s in sources)
    assert not any("node_modules" in s for s in sources)


# ----------------------------------------------------------------------
# Gate 12 (D11): 重复 submit 去重
# ----------------------------------------------------------------------
def test_gate_12_duplicate_submit_dedup(tmp_path: Path):
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    with patch.object(mgr, "_worker_loop"):
        r1 = mgr.submit(["doc.pdf"])
        assert r1["submitted"] == 1
        r2 = mgr.submit(["doc.pdf"])
        assert r2["submitted"] == 0
        assert len(mgr.status()["jobs"]) == 1


# ----------------------------------------------------------------------
# Gate 13 (D14): path_prefix 过滤支持摄取产物
# ----------------------------------------------------------------------
def test_gate_13_path_prefix_matches_parsed_products(tmp_path: Path):
    parsed_dir = tmp_path / ".mortis-parsed" / "教材"
    parsed_dir.mkdir(parents=True)
    md_file = parsed_dir / "数电.md"
    md_file.write_text(
        "---\nsource_pdf: 教材/数电.pdf\n---\n# 数字电路\n\n组合逻辑电路设计步骤：真值表、卡诺图化简、逻辑表达式。",
        encoding="utf-8",
    )
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4))
    indexer = MarkdownIndexer(tmp_path, cfg)
    indexer.sync()

    f = SearchFilter(path_prefix="教材/")
    chunks = indexer.search("组合逻辑", filters=f)
    assert len(chunks) > 0
    assert chunks[0].metadata.get("source_pdf") == "教材/数电.pdf"

    if indexer._fts is not None:
        fts_chunks = indexer._fts.search("组合逻辑", filters=f)
        assert len(fts_chunks) > 0


# ----------------------------------------------------------------------
# Gate 14 (D6b): server 级 IngestManager 并发获取幂等
# ----------------------------------------------------------------------
def test_gate_14_server_concurrent_ingest_manager_lock(tmp_path: Path):
    from mortis_rag_mcp.server import VaultMcpServer

    server = VaultMcpServer()
    vault_str = str(tmp_path)
    managers: list[IngestManager] = []

    def get_mgr():
        m = server._ingest_manager_for(vault_str)
        managers.append(m)

    threads = [threading.Thread(target=get_mgr) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(managers) == 10
    assert all(m is managers[0] for m in managers)
