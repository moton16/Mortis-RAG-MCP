import json

import pytest
import os
import subprocess
import sys
from pathlib import Path


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


def test_stdio_initialize_tools_and_list_search(tmp_path):
    (tmp_path / "知识库.md").write_text("# 项目笔记\n\nMCP stdio 服务支持 Obsidian 检索。\n", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{tmp_path.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "MCP stdio", "top_k": 5, "use_rerank": False}}},
    ])

    assert responses[0]["result"]["serverInfo"]["name"] == "mortis-rag-mcp"
    names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert {"kb_list", "kb_search", "kb_read", "kb_stats"} <= names

    listed = json.loads(responses[2]["result"]["content"][0]["text"])
    assert listed["files"][0]["source"] == "知识库.md"

    searched = json.loads(responses[3]["result"]["content"][0]["text"])
    assert searched["chunks"]
    assert searched["chunks"][0]["source"] == "知识库.md"
    assert searched["chunks"][0]["metadata"]["heading"] == "项目笔记"


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
