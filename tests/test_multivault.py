from __future__ import annotations

import json
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
    assert r2["result"]["isError"] is True
    assert "absolute path" in r2["result"]["content"][0]["text"]


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
    assert r2["result"]["isError"] is True
    assert "kb_init" in r2["result"]["content"][0]["text"]


def _register_two_vaults(config: Path, vault_a: Path, vault_b: Path, shared: str) -> None:
    vault_a.mkdir(parents=True)
    vault_b.mkdir(parents=True)
    # 两个库放同一段内容，保证基础分相同，排序差异只能来自库权重。
    (vault_a / "alpha.md").write_text(f"# Alpha\n{shared}", encoding="utf-8")
    (vault_b / "beta.md").write_text(f"# Beta\n{shared}", encoding="utf-8")
    config.write_text('mode = "static"\n', encoding="utf-8")


def _search(config: Path, arguments: dict) -> dict:
    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_search", "arguments": arguments}},
    ])
    return json.loads(responses[1]["result"]["content"][0]["text"])


def test_fanout_weight_pushes_higher_weight_vault_first(tmp_path):
    vault_a = tmp_path / "权重低"
    vault_b = tmp_path / "权重高"
    shared = "跨库共享内容 SHARED777"
    config = tmp_path / "app.toml"
    _register_two_vaults(config, vault_a, vault_b, shared)

    _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_a)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_b)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_set_weight", "arguments": {"vault_path": str(vault_b), "weight": 2.0}}},
    ])

    data = _search(config, {"query": "SHARED777", "top_k": 2})
    assert len(data["chunks"]) == 2
    assert data["chunks"][0]["vault"] == str(vault_b)
    assert data["chunks"][0]["vault_name"] == "权重高"
    # 浮点比较用 >=，避免相等分数下的舍入抖动。
    assert data["chunks"][0]["score"] >= data["chunks"][1]["score"] * 1.999


def test_fanout_default_weight_matches_single_vault_scores(tmp_path):
    """回归：所有库都是默认 1.0 时，fan-out 不得改动任何分数。"""
    vault_a = tmp_path / "库A"
    vault_b = tmp_path / "库B"
    shared = "跨库共享内容 SHARED888"
    config = tmp_path / "app.toml"
    _register_two_vaults(config, vault_a, vault_b, shared)

    _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_a)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_b)}}},
    ])

    single = _search(config, {"query": "SHARED888", "top_k": 1, "vault_path": str(vault_a)})
    fanout = _search(config, {"query": "SHARED888", "top_k": 2})
    alpha = [chunk for chunk in fanout["chunks"] if chunk["source"] == "alpha.md"]
    assert alpha and alpha[0]["score"] == single["chunks"][0]["score"]


def test_fanout_group_by_vault_returns_grouped_results(tmp_path):
    vault_a = tmp_path / "分组A"
    vault_b = tmp_path / "分组B"
    shared = "跨库共享内容 SHARED999"
    config = tmp_path / "app.toml"
    _register_two_vaults(config, vault_a, vault_b, shared)

    _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_a)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_b)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_set_weight", "arguments": {"vault_path": str(vault_b), "weight": 3.0}}},
    ])

    data = _search(config, {"query": "SHARED999", "top_k": 1, "group_by_vault": True})
    assert "chunks" not in data
    assert len(data["groups"]) == 2
    # 组顺序按组内最高分降序：权重 3.0 的库排在前面。
    assert data["groups"][0]["vault"] == str(vault_b)
    assert data["groups"][0]["vault_name"] == "分组B"
    top_a = max(chunk["score"] for chunk in data["groups"][0]["chunks"])
    top_b = max(chunk["score"] for chunk in data["groups"][1]["chunks"])
    assert top_a >= top_b
    # 分组模式下 top_k 作用于每个库，各组最多取 top_k 条。
    assert all(len(group["chunks"]) <= 1 for group in data["groups"])
    assert all(chunk["vault"] == group["vault"] for group in data["groups"] for chunk in group["chunks"])


def test_kb_set_weight_updates_registry_and_vault_listing(tmp_path, monkeypatch):
    from mortis_rag_mcp.server import VaultMcpServer

    vault = tmp_path / "库"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n可调权重的内容 WEIGHT123", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))

    server = VaultMcpServer(config)
    server.call_tool("kb_init", {"path": str(vault)})
    result = server.call_tool("kb_set_weight", {"vault_path": str(vault), "weight": 2.5})
    set_result = json.loads(result["content"][0]["text"])
    assert set_result["weight"] == 2.5
    assert set_result["path"] == str(vault)

    listing = json.loads(server.call_tool("kb_list", {})["content"][0]["text"])
    assert listing["vaults"][0]["weight"] == 2.5

    # 越界权重必须走 ValueError（call_tool 不吞异常，MCP 层会转成 -32602）。
    for bad in (0, 200):
        try:
            server.call_tool("kb_set_weight", {"vault_path": str(vault), "weight": bad})
            raised = False
        except ValueError:
            raised = True
        assert raised


def test_kb_list_lists_registered_entries(tmp_path):
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
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_list", "arguments": {}}},
    ])
    data = json.loads(responses[3]["result"]["content"][0]["text"])
    by_name = {item["name"]: item for item in data["vaults"]}
    assert set(by_name) == {"库一", "库二"}
    assert by_name["库一"]["exists"] is True
    assert by_name["库一"]["files"] == 2  # 递归索引包含子目录里的 deep.md
    assert by_name["库二"]["path"] == str(vault_two)
