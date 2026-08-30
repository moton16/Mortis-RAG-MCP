from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from vault_mcp.config import AppConfig
from vault_mcp.indexer import IgnoreMatcher, MarkdownIndexer


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "vault_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "VAULT_MCP_REGISTRY": str(config.parent / "vaults.toml")},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_ignore_matcher():
    rules = [
        "# comment",
        "",
        "diary/",
        "*.secret.md",
        "templates/**",
        "!diary/public.md",
    ]
    matcher = IgnoreMatcher(rules)

    assert matcher.is_ignored("diary/2026-08-19.md")[0] is True
    assert matcher.is_ignored("diary/public.md")[0] is False
    assert matcher.is_ignored("notes/passwords.secret.md")[0] is True
    assert matcher.is_ignored("templates/meeting.md")[0] is True
    assert matcher.is_ignored("notes/regular.md")[0] is False


def test_vaultignore_filters_indexer(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".vaultignore").write_text("private/\n*.draft.md\n", encoding="utf-8")
    (vault / "private").mkdir()
    (vault / "private" / "secret.md").write_text("# Secret\n私密日记内容", encoding="utf-8")
    (vault / "test.draft.md").write_text("# Draft\n未完成草稿", encoding="utf-8")
    (vault / "public.md").write_text("# Public\n公开知识文档", encoding="utf-8")

    config = AppConfig(vault_path=str(vault))
    indexer = MarkdownIndexer(vault, config)
    chunks = indexer.sync()

    sources = {chunk.source for chunk in chunks}
    assert "public.md" in sources
    assert "private/secret.md" not in sources
    assert "test.draft.md" not in sources

    stats = indexer.stats()
    assert stats["files"] == 1
    assert stats["exempt_files"] == 2


def test_frontmatter_rag_false_and_tag_exemption(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note_rag_false.md").write_text("---\nrag: false\n---\n# Title\n内容", encoding="utf-8")
    (vault / "note_tag_norag.md").write_text("---\ntags: [work, no-rag]\n---\n# Title\n内容", encoding="utf-8")
    (vault / "note_tag_simi.md").write_text("---\ntags: [私密]\n---\n# Title\n内容", encoding="utf-8")
    (vault / "note_public.md").write_text("---\ntags: [公开]\n---\n# Title\n正常内容", encoding="utf-8")

    config = AppConfig(vault_path=str(vault))
    indexer = MarkdownIndexer(vault, config)
    chunks = indexer.sync()

    sources = {chunk.source for chunk in chunks}
    assert "note_public.md" in sources
    assert "note_rag_false.md" not in sources
    assert "note_tag_norag.md" not in sources
    assert "note_tag_simi.md" not in sources


def test_block_level_comment_ignore(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    content = """# 知识点一
这是公开内容的第一段。

<!-- rag-ignore -->
### 私密备忘
这是密码和绝密信息，绝对不能被检索到！
<!-- /rag-ignore -->

# 知识点二
这是公开内容的第二段。
"""
    (vault / "mixed.md").write_text(content, encoding="utf-8")

    config = AppConfig(vault_path=str(vault))
    indexer = MarkdownIndexer(vault, config)
    chunks = indexer.sync()

    assert len(chunks) == 2
    all_content = "\n".join(chunk.content for chunk in chunks)
    assert "这是公开内容的第一段" in all_content
    assert "这是公开内容的第二段" in all_content
    assert "私密备忘" not in all_content
    assert "绝密信息" not in all_content

    # Check search
    res1 = indexer.search("公开内容")
    assert len(res1) > 0
    res2 = indexer.search("绝密信息")
    assert len(res2) == 0


def test_kb_exempt_api(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# Note A\nHello World", encoding="utf-8")
    (vault / "b.md").write_text("# Note B\nSecret Stuff", encoding="utf-8")

    config = AppConfig(vault_path=str(vault))
    indexer = MarkdownIndexer(vault, config)
    indexer.sync()

    assert len(indexer.all_chunks()) == 2

    # Add pattern
    add_res = indexer.add_exemption_pattern("b.md")
    assert add_res["success"] is True
    assert len(indexer.all_chunks()) == 1
    assert indexer.all_chunks()[0].source == "a.md"

    # Check exemption
    check_b = indexer.check_exemption("b.md")
    assert check_b["is_exempt"] is True
    assert "b.md" in check_b["reason"]

    # Remove pattern
    rem_res = indexer.remove_exemption_pattern("b.md")
    assert rem_res["success"] is True
    assert len(indexer.all_chunks()) == 2

    # Set file exemption via frontmatter
    set_res = indexer.set_file_exemption("a.md", exempt=True, method="frontmatter")
    assert set_res["success"] is True
    assert len(indexer.all_chunks()) == 1
    assert indexer.all_chunks()[0].source == "b.md"

    check_a = indexer.check_exemption("a.md")
    assert check_a["is_exempt"] is True
    assert "rag: false" in check_a["reason"]

    # Unexempt file
    unset_res = indexer.set_file_exemption("a.md", exempt=False, method="frontmatter")
    assert unset_res["success"] is True
    assert len(indexer.all_chunks()) == 2


def test_stdio_kb_exempt_tool(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "public.md").write_text("# Public\n公开知识", encoding="utf-8")
    (vault / "secret.md").write_text("# Secret\n私密秘密", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{vault.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_exempt", "arguments": {"action": "list"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_exempt", "arguments": {"action": "exempt_file", "source": "secret.md", "method": "frontmatter"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "秘密"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_exempt", "arguments": {"action": "check", "source": "secret.md"}}},
    ]

    responses = _run_stdio(config, requests)
    assert responses[0]["id"] == 1

    # list
    list_data = json.loads(responses[1]["result"]["content"][0]["text"])
    assert list_data["total_md_files"] == 2

    # exempt_file
    exempt_data = json.loads(responses[2]["result"]["content"][0]["text"])
    assert exempt_data["is_exempt"] is True

    # search (should find 0 chunks because secret.md is exempt)
    search_data = json.loads(responses[3]["result"]["content"][0]["text"])
    assert len(search_data["chunks"]) == 0

    # check
    check_data = json.loads(responses[4]["result"]["content"][0]["text"])
    assert check_data["is_exempt"] is True
    assert "rag: false" in check_data["reason"]
