import json
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
