from __future__ import annotations

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


def test_stdio_vault_path_argument_switches_vault(tmp_path):
    vault_a = tmp_path / "vault_a"
    vault_b = tmp_path / "vault_b"
    vault_a.mkdir(parents=True)
    vault_b.mkdir(parents=True)
    (vault_a / "alpha.md").write_text("# Alpha\n这是知识库 A 的内容。", encoding="utf-8")
    (vault_b / "beta.md").write_text("# Beta\n这是知识库 B 的内容。", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_a)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_b)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "知识库", "vault_path": str(vault_a)}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "知识库", "vault_path": str(vault_b)}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "知识库", "vault_path": str(vault_a)}}},
    ]
    responses = _run_stdio(config, requests)

    r4 = json.loads(responses[3]["result"]["content"][0]["text"])
    r5 = json.loads(responses[4]["result"]["content"][0]["text"])
    r6 = json.loads(responses[5]["result"]["content"][0]["text"])

    # Explicit vault_path switches to vault_a.
    assert r4["chunks"] and all(chunk["source"] == "alpha.md" for chunk in r4["chunks"])
    # Explicit vault_path switches to vault_b.
    assert r5["chunks"] and all(chunk["source"] == "beta.md" for chunk in r5["chunks"])
    # And back to vault_a after the explicit call.
    assert r6["chunks"] and all(chunk["source"] == "alpha.md" for chunk in r6["chunks"])


def test_stdio_stats_reports_cache_status(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nhello", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text(
        f'vault_path = "{vault.as_posix()}"\nmode = "static"\n[cache]\ndir = "{str(tmp_path / "cache").replace(chr(92), "/")}"\nenabled = true\n',
        encoding="utf-8",
    )

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_stats", "arguments": {}}},
    ])
    stats = json.loads(responses[1]["result"]["content"][0]["text"])
    assert stats["files"] == 1
    assert stats["cache_enabled"] is True


def test_stdio_relative_vault_path_returns_error(tmp_path):
    root = tmp_path / "root"
    (root / "子库").mkdir(parents=True)
    (root / "子库" / "笔记.md").write_text("# 子库笔记\n子库独有内容 ABCXYZ。", encoding="utf-8")
    (root / "根笔记.md").write_text("# 根笔记\n根目录内容。", encoding="utf-8")

    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{root.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        # 0.3.0 契约：相对路径不再解析到根库下，必须报错并提示 kb_init
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "子库独有内容", "vault_path": "子库"}}},
    ])
    r2 = responses[1]
    assert "error" in r2
    assert "absolute path" in r2["error"]["message"]


def test_stdio_unknown_vault_path_returns_error(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.md").write_text("# A\nhello", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{root.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    # 未注册的路径必须报错并提示 kb_init（root 本身经 legacy 迁移已注册，
    # 所以这里用一个从未注册过的路径）。
    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "x", "vault_path": str(tmp_path / "not_registered")}}},
    ])
    r2 = responses[1]
    assert "error" in r2
    assert "kb_init" in r2["error"]["message"]


def test_kb_vaults_lists_registered_entries(tmp_path):
    vault_one = tmp_path / "库一"
    vault_two = tmp_path / "库二"
    (vault_one / "子目录").mkdir(parents=True)
    (vault_one / "top.md").write_text("# top", encoding="utf-8")
    (vault_one / "子目录" / "deep.md").write_text("# deep", encoding="utf-8")
    vault_two.mkdir()
    (vault_two / "b.md").write_text("# b", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_one), "name": "库一"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_two)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_vaults", "arguments": {}}},
    ])
    data = json.loads(responses[3]["result"]["content"][0]["text"])
    by_name = {item["name"]: item for item in data["vaults"]}
    assert set(by_name) == {"库一", "库二"}
    assert by_name["库一"]["exists"] is True
    assert by_name["库一"]["files"] == 2  # 递归索引包含子目录里的 deep.md
    assert by_name["库二"]["path"] == str(vault_two)
