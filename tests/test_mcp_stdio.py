import json

import pytest
import os
import subprocess
import sys
from pathlib import Path


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={
            **os.environ,
            "VAULT_MCP_REGISTRY": str(config.parent / "vaults.toml"),
            "MORTIS_RAG_REGISTRY": str(config.parent / "vaults.toml"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_stdio_initialize_tools_and_list_search(tmp_path):
    (tmp_path / "知识库.md").write_text("# 项目笔记\n\nMCP stdio 服务支持 Obsidian 检索。\n", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{tmp_path.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "MCP stdio", "top_k": 5, "use_rerank": False}}},
    ])

    assert responses[0]["result"]["serverInfo"]["name"] == "mortis-rag-mcp"
    assert "instructions" in responses[0]["result"] and responses[0]["result"]["instructions"]
    names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert {"kb_list", "kb_list_files", "kb_search", "kb_read", "kb_stats", "kb_describe"} <= names

    listed = json.loads(responses[2]["result"]["content"][0]["text"])
    searched = json.loads(responses[3]["result"]["content"][0]["text"])
    if not listed.get("files") or searched.get("status") == "indexing":
        import time
        time.sleep(0.5)
        responses2 = _run_stdio(config, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "MCP stdio", "top_k": 5, "use_rerank": False}}},
        ])
        listed = json.loads(responses2[1]["result"]["content"][0]["text"])
        searched = json.loads(responses2[2]["result"]["content"][0]["text"])

    assert listed["files"][0]["source"] == "知识库.md"

    assert searched["chunks"]
    assert searched["chunks"][0]["source"] == "知识库.md"
    assert searched["chunks"][0]["metadata"]["heading"] == "项目笔记"


def test_stdio_kb_list_files_pagination_and_prefix(tmp_path):
    """C57/C62：分页切片、前缀过滤、total/next_offset 语义，以及缺省全量与现状一致。"""
    vault = tmp_path / "vault"
    (vault / "教材").mkdir(parents=True)
    (vault / "杂记").mkdir(parents=True)
    for i in range(3):
        (vault / "教材" / f"ch{i}.md").write_text(f"# 教材 {i}\n数字电路内容 {i}\n", encoding="utf-8")
    for i in range(2):
        (vault / "杂记" / f"m{i}.md").write_text(f"# 杂记 {i}\n随笔内容 {i}\n", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{vault.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"limit": 2, "offset": 0}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"limit": 2, "offset": 2}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"path_prefix": "教材/"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"offset": 99}}},
    ])

    full = json.loads(responses[1]["result"]["content"][0]["text"])
    if full.get("total") == 0 and full.get("indexing_in_progress"):
        import time
        time.sleep(0.5)
        responses = _run_stdio(config, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"limit": 2, "offset": 0}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"limit": 2, "offset": 2}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"path_prefix": "教材/"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_list_files", "arguments": {"offset": 99}}},
        ])
        full = json.loads(responses[1]["result"]["content"][0]["text"])
    page1 = json.loads(responses[2]["result"]["content"][0]["text"])
    page2 = json.loads(responses[3]["result"]["content"][0]["text"])
    pref = json.loads(responses[4]["result"]["content"][0]["text"])
    beyond = json.loads(responses[5]["result"]["content"][0]["text"])

    # 缺省 = 全量（与修复前一致），且不标分页截断
    assert full["total"] == 5 and len(full["files"]) == 5
    assert full["next_offset"] is None and full["page_truncated"] is False

    # 切片 + total（过滤后切片前）+ next_offset
    assert [f["source"] for f in page1["files"]] == ["教材/ch0.md", "教材/ch1.md"]
    assert page1["total"] == 5 and page1["next_offset"] == 2 and page1["page_truncated"] is True
    assert [f["source"] for f in page2["files"]] == ["教材/ch2.md", "杂记/m0.md"]
    assert page2["next_offset"] == 4

    # 前缀过滤（与 kb_search.path_prefix 同口径）
    assert pref["total"] == 3
    assert all(f["source"].startswith("教材/") for f in pref["files"])

    # offset 越界：空页 + total 仍为总数
    assert beyond["files"] == []
    assert beyond["total"] == 5
    assert beyond["next_offset"] is None


def test_stdio_kb_describe_updates_description(tmp_path):
    (tmp_path / "note.md").write_text("# Test\ncontent\n", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{tmp_path.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "kb_describe", "arguments": {"vault_path": str(tmp_path.resolve()), "description": "数电教材+课件"}
        }},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_list", "arguments": {}}},
    ])

    assert responses[0]["result"]["instructions"]
    res = json.loads(responses[1]["result"]["content"][0]["text"])
    assert res["description"] == "数电教材+课件"

    vaults_res = json.loads(responses[2]["result"]["content"][0]["text"])
    vault_item = next(v for v in vaults_res["vaults"] if v["path"] == str(tmp_path.resolve()))
    assert vault_item["description"] == "数电教材+课件"


def test_stdio_survives_lone_surrogate_in_notes(tmp_path):
    """孤立代理项不得杀掉 stdio 服务进程。

    代理项（surrogate）来自 os 解码的文件名或粘贴内容。json.dumps(
    ensure_ascii=False) 对代理项并不报错，UnicodeEncodeError 发生在
    sys.stdout.write() 编码那一刻 —— 只把 dumps 包进 try 是死代码，
    write 也必须在 try 内，否则异常逃出循环直接结束进程。
    """
    # 文件名本身含代理项，索引后 chunk 内容里就会带上它。
    weird = tmp_path / "note"
    weird.mkdir()
    try:
        (weird / "bad\ud800name.md").write_text("# 标题\n正文内容\n", encoding="utf-8")
    except (OSError, UnicodeEncodeError):  # pragma: no cover - 文件系统不接受
        pytest.skip("当前文件系统不接受代理项文件名")

    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{weird.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "kb_search", "arguments": {"query": "正文"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    ])

    # 关键：服务没有中途死掉，第 3 个请求（tools/list）仍然有响应。
    assert len(responses) == 3, f"服务在写出代理项时被杀，只回了 {len(responses)} 条"
    assert responses[-1]["result"]["tools"]
