from __future__ import annotations

import json
from pathlib import Path
import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp._indexer.reading import scan_headings, read_file_result
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def test_fixed_fixture_hierarchical_sections(tmp_path: Path):
    """C69b 核心固定 fixture 验证：
    Target 预期 lines=[2, 5]，Child 预期 lines=[4, 5]，A 预期 lines=[1, 7]，End 预期 lines=[8, 8]。
    """
    content = (
        "# A\n"
        "## Target\n"
        "目标正文\n"
        "### Child\n"
        "子节正文\n"
        "## Sibling\n"
        "不能混入\n"
        "# End\n"
    )
    p = tmp_path / "fixture.md"
    p.write_text(content, encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="FixVault")

    # 1. Target: lines=[2, 5], 包含 Child 及其正文，但不包含 Sibling
    res_target = server._kb_read({"source": "fixture.md", "heading": "Target", "vault_path": "FixVault"})
    assert res_target["effective_start_line"] == 2
    assert res_target["effective_end_line"] == 5
    assert res_target["start_line"] == 2
    assert res_target["end_line"] == 5
    assert "目标正文" in res_target["content"]
    assert "子节正文" in res_target["content"]
    assert "不能混入" not in res_target["content"]

    # 2. Child: lines=[4, 5]
    res_child = server._kb_read({"source": "fixture.md", "heading": "Child", "vault_path": "FixVault"})
    assert res_child["effective_start_line"] == 4
    assert res_child["effective_end_line"] == 5
    assert res_child["start_line"] == 4
    assert res_child["end_line"] == 5
    assert "子节正文" in res_child["content"]
    assert "目标正文" not in res_child["content"]

    # 3. A: lines=[1, 7], 包含 Target、Child、Sibling 全部内容，直到下一个 level 1 (# End) 之前
    res_a = server._kb_read({"source": "fixture.md", "heading": "A", "vault_path": "FixVault"})
    assert res_a["effective_start_line"] == 1
    assert res_a["effective_end_line"] == 7
    assert res_a["start_line"] == 1
    assert res_a["end_line"] == 7
    assert "不能混入" in res_a["content"]
    assert "# End" not in res_a["content"]

    # 4. End: 末尾章节 lines=[8, 8]
    res_end = server._kb_read({"source": "fixture.md", "heading": "End", "vault_path": "FixVault"})
    assert res_end["effective_start_line"] == 8
    assert res_end["effective_end_line"] == 8
    assert res_end["content"] == "# End"


def test_heading_section_not_truncated_by_frontmatter_table_string(tmp_path: Path):
    """review R5：frontmatter 里的表格字符串不得污染正文表格配对——
    title:"<table>" 与正文 HTML 表格组合时，表格内的 '# Fake' 不能被当成
    新章节把 Target 章节静默截短（应读满 4-11 行且 truncated=False）。"""
    content = (
        "---\n"
        'title: "<table>"\n'
        "---\n"
        "# Target\n"
        "target intro\n"
        "<table>\n"
        "<tr><td>\n"
        "# Fake\n"
        "</td></tr>\n"
        "</table>\n"
        "target tail that must remain in this section\n"
        "# End\n"
    )
    p = tmp_path / "doc.md"
    p.write_text(content, encoding="utf-8")

    # 标题扫描不得把表格内的 '# Fake' 当章节（否则 Target 止步于第 7 行）
    titles = [h[0] for h in scan_headings(p.read_text(encoding="utf-8").splitlines())]
    assert "Fake" not in titles
    assert titles == ["Target", "End"]

    res = read_file_result(p, "doc.md", heading="Target")
    assert res.effective_start_line == 4
    assert res.effective_end_line == 11
    assert "# Fake" in res.content
    assert "target tail" in res.content
    assert res.truncated is False


def test_ambiguous_headings_fail_closed(tmp_path: Path):
    """C69b Req 7: 同名隔远章节或不同 level 同名均计为歧义，禁止隐式合并或选第一个。"""
    content = (
        "# 卷一\n"
        "## 概述\n"
        "第一卷概述\n"
        "# 卷二\n"
        "## 概述\n"
        "第二卷概述\n"
        "# 卷三\n"
        "### 概述\n"
        "第三卷概述（虽然是三级标题但同名也歧义）\n"
    )
    (tmp_path / "ambig.md").write_text(content, encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="AmbigVault")

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"source": "ambig.md", "heading": "概述", "vault_path": "AmbigVault"})
    msg = str(exc_info.value)
    assert "3 处同名标题" in msg
    assert "2, 5, 8" in msg
    assert "start_line/end_line" in msg


def test_heading_not_found_diagnostics(tmp_path: Path):
    """C69b Req 6: 零命中时报错返回 source、total_lines 以及最多 5 个候选标题和起始行提示。"""
    content = (
        "# 标题一\n"
        "## 标题二\n"
        "### 标题三\n"
        "#### 标题四\n"
        "##### 标题五\n"
        "###### 标题六\n"
    )
    (tmp_path / "candidates.md").write_text(content, encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="CandVault")

    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"source": "candidates.md", "heading": "不存在的标题", "vault_path": "CandVault"})
    msg = str(exc_info.value)
    assert "heading 未找到: '不存在的标题'" in msg
    assert "candidates.md" in msg
    assert "total_lines: 6" in msg
    assert "'标题一' (line 1)" in msg
    assert "等共 6 个标题" in msg
    assert "start_line/end_line" in msg


def test_ignore_false_headings_in_fences_tables_and_frontmatter(tmp_path: Path):
    """C69b Req 3: 严格跳过代码块、长同字符代码块、HTML 表格与 Frontmatter 中的假标题。"""
    content = (
        "---\n"
        "title: '# Frontmatter Title'\n"
        "---\n"
        "# 真正标题一\n"
        "```python\n"
        "# Fake in 3-ticks\n"
        "## Fake Sub in 3-ticks\n"
        "```\n"
        "````markdown\n"
        "```\n"
        "# Fake in 4-ticks outer with 3-ticks inner\n"
        "```\n"
        "````\n"
        "<table>\n"
        "  <tr><td># Fake Table Heading</td></tr>\n"
        "</table>\n"
        "# 真正标题二\n"
    )
    p = tmp_path / "filter.md"
    p.write_text(content, encoding="utf-8")

    lines = content.splitlines()
    headings = scan_headings(lines)
    titles = [h[0] for h in headings]
    assert titles == ["真正标题一", "真正标题二"]


def test_chapter_headings_in_txt_and_markdown(tmp_path: Path):
    """C69b Req 4: 支持小说章节标题（如 第X章 标题、Chapter X），严格门禁避免普通正文误判。"""
    txt_content = (
        "前言文本\n"
        "第1章 英雄出少年\n"
        "这是第一章正文，少年走出了村庄。\n"
        "第2章 风云再起\n"
        "这是第二章正文。\n"
        "Chapter 3 The Journey Continues\n"
        "第三章正文内容。\n"
        "普通正文里的第4章，因为后面有标点符号和很长句子所以不是标题。\n"
    )
    p = tmp_path / "novel.txt"
    p.write_text(txt_content, encoding="utf-8")

    lines = txt_content.splitlines()
    headings = scan_headings(lines)
    assert len(headings) == 3
    assert headings[0] == ("第1章 英雄出少年", 1, 2)
    assert headings[1] == ("第2章 风云再起", 1, 4)
    assert headings[2] == ("Chapter 3 The Journey Continues", 1, 6)

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="NovVault")

    res = server._kb_read({"source": "novel.txt", "heading": "第1章 英雄出少年", "vault_path": "NovVault"})
    assert res["effective_start_line"] == 2
    assert res["effective_end_line"] == 3
    assert "少年走出了村庄" in res["content"]
    assert "第二章正文" not in res["content"]


def test_range_overrides_heading(tmp_path: Path):
    """C69b Req 10: heading + 显式 start_line/end_line 时，range 优先，不制造互斥 breaking。"""
    content = (
        "# 标题一\n"
        "内容一\n"
        "# 标题二\n"
        "内容二\n"
    )
    (tmp_path / "prio.md").write_text(content, encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="PrioVault")

    # 同时传入 heading="标题二" 和 start_line=1, end_line=2：range 优先返回标题一内容
    res = server._kb_read({
        "source": "prio.md",
        "heading": "标题二",
        "start_line": 1,
        "end_line": 2,
        "vault_path": "PrioVault",
    })
    assert res["start_line"] == 1
    assert res["end_line"] == 2
    assert res["content"] == "# 标题一\n内容一"


def test_chunk_id_mutually_exclusive_with_heading(tmp_path: Path):
    """C69b Req 10: chunk_id 与 heading 严格互斥报错。"""
    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)

    with pytest.raises(ValueError, match="chunk_id is mutually exclusive with start_line/end_line/heading"):
        server._kb_read({"chunk_id": "dummy_cid", "heading": "SomeHeading"})


def test_heading_continuation_across_read_max_chars(tmp_path: Path):
    """C69b Req 11: 章节正文超过 read_max_chars 时正确截断，并允许 caller 用返回的 next 游标无缝续读。"""
    heading_sec = "## BigSection\n" + ("X" * 150) + "\n## NextSec\nEnd"
    (tmp_path / "big_heading.md").write_text(heading_sec, encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n[index]\nread_max_chars = 100\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="CapVault")

    # Page 1 通过 heading 读取
    p1 = server._kb_read({"source": "big_heading.md", "heading": "BigSection", "vault_path": "CapVault"})
    assert p1["truncated"] is True
    assert p1["content_end_line"] == 2
    assert p1["next_start_line"] == 2
    assert p1["next_start_char"] > 0

    # Page 2 使用返回的 next_start_line 与 next_start_char 续读
    p2 = server._kb_read({
        "source": "big_heading.md",
        "start_line": p1["next_start_line"],
        "end_line": 2,
        "start_char": p1["next_start_char"],
        "vault_path": "CapVault",
    })
    assert p2["truncated"] is False
    assert p1["content"] + p2["content"] == "## BigSection\n" + ("X" * 150)


def test_unindexed_file_and_cache_stale_heading(tmp_path: Path):
    """C69b Req 2: 未索引文件或缓存陈旧时，heading 直接基于磁盘物理文件准确定位。"""
    (tmp_path / "live.md").write_text("# OldHeading\nold\n", encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="LiveVault")
    idx = server._indexer_for({"vault_path": "LiveVault"})
    idx.sync()

    # 磁盘直接更新为 NewHeading（索引尚未重新 sync）
    (tmp_path / "live.md").write_text("# NewHeading\nnew content\n", encoding="utf-8")

    # 直接读取 NewHeading 必须成功命中磁盘物理内容
    res = server._kb_read({"source": "live.md", "heading": "NewHeading", "vault_path": "LiveVault"})
    assert res["effective_start_line"] == 1
    assert "new content" in res["content"]


def test_heading_errors_via_handle(tmp_path: Path):
    """C69b Req: not found/ambiguous 错误均经 handle 返回 isError=True，且候选有限。"""
    content = "# 卷一\n## 概述\n1\n# 卷二\n## 概述\n2\n"
    (tmp_path / "test.md").write_text(content, encoding="utf-8")
    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n', encoding="utf-8")
    server = VaultMcpServer(app_toml)
    server.registry.add(str(tmp_path), name="HandleVault")

    # 1. 歧义标题经 handle
    resp_ambig = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "kb_read",
            "arguments": {"source": "test.md", "heading": "概述", "vault_path": "HandleVault"},
        },
    })
    assert resp_ambig["result"]["isError"] is True
    err_text = resp_ambig["result"]["content"][0]["text"]
    assert "同名标题" in err_text

    # 2. 未找到标题经 handle
    resp_nf = server.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "kb_read",
            "arguments": {"source": "test.md", "heading": "不存在", "vault_path": "HandleVault"},
        },
    })
    assert resp_nf["result"]["isError"] is True
    err_nf_text = resp_nf["result"]["content"][0]["text"]
    assert "heading 未找到" in err_nf_text

