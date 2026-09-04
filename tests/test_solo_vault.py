"""solo（独立）库端到端行为：kb_init_solo 三态 + fan-out 排除 + 显式检索。

语义（0.6.0 定稿）：
- solo 库不参与 kb_search 不传 vault_path 的全局 fan-out，结果里以
  excluded_solo 列出被跳过的库；
- 唯一注册库是 solo 时，不传 vault_path 的检索直接报错（不悄悄搜它）；
- 显式传 vault_path 时 solo 库照常可搜、可读、可管理；
- 取消 solo 的正道是 kb_remove → kb_init（磁盘缓存保留，零成本周转）。
"""
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


def _payload(result_line: dict) -> dict:
    return json.loads(result_line["result"]["content"][0]["text"])


def _make_config(tmp_path: Path) -> Path:
    config = tmp_path / "app.toml"
    config.write_text(
        f'mode = "static"\n[cache]\ndir = "{(tmp_path / "cache").as_posix()}"\nenabled = true\n',
        encoding="utf-8",
    )
    return config


def test_solo_vault_excluded_from_fanout_but_searchable_explicitly(tmp_path):
    vault = tmp_path / "普通库"
    solo = tmp_path / "独立库"
    vault.mkdir()
    solo.mkdir()
    (vault / "a.md").write_text("# A\n普通库内容 COMMON123", encoding="utf-8")
    (solo / "b.md").write_text("# B\n独立库内容 SOLO456", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(solo)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "COMMON123 SOLO456"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "SOLO456", "vault_path": str(solo)}}},
    ])
    init = _payload(responses[2])
    assert init["solo"] is True
    assert init["registered"] is True
    listing = _payload(responses[3])
    by_name = {item["name"]: item for item in listing["vaults"]}
    assert by_name["独立库"]["solo"] is True
    assert by_name["普通库"]["solo"] is False

    # fan-out 只搜普通库；solo 库既不在 searched 也不产生结果，
    # 但必须在 excluded_solo 里点名，否则它成了检索黑洞。
    fanout = _payload(responses[4])
    assert fanout["searched"] == [str(vault)]
    assert fanout["excluded_solo"] == [str(solo)]
    assert all("SOLO456" not in chunk["content"] for chunk in fanout["chunks"])

    # 显式指定 vault_path 后照常可搜。
    explicit = _payload(responses[5])
    assert any("SOLO456" in chunk["content"] for chunk in explicit["chunks"])


def test_solo_switch_on_registered_vault_is_idempotent(tmp_path):
    vault = tmp_path / "普通库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n内容 SWITCH789", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(vault)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(vault)}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_list", "arguments": {}}},
    ])
    switched = _payload(responses[2])
    assert switched["registered"] is False
    assert switched["switched"] is True
    assert switched["solo"] is True
    again = _payload(responses[3])
    assert again["solo"] is True
    assert again["switched"] is False
    listing = _payload(responses[4])
    assert listing["vaults"][0]["solo"] is True


def test_single_solo_vault_rejects_global_search(tmp_path):
    solo = tmp_path / "独立库"
    solo.mkdir()
    (solo / "a.md").write_text("# A\n内容 SOLOONLY", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(solo)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "SOLOONLY"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "SOLOONLY", "vault_path": str(solo)}}},
    ])
    err = responses[2]
    assert err["result"]["isError"] is True
    assert "solo" in err["result"]["content"][0]["text"]
    # 显式指定后照常可搜。
    explicit = _payload(responses[3])
    assert any("SOLOONLY" in chunk["content"] for chunk in explicit["chunks"])


def test_all_solo_vaults_global_search_reports_clear_error(tmp_path):
    a = tmp_path / "独立A"
    b = tmp_path / "独立B"
    a.mkdir()
    b.mkdir()
    (a / "a.md").write_text("# A\nAAA内容", encoding="utf-8")
    (b / "b.md").write_text("# B\nBBB内容", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(a)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(b)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "AAA内容"}}},
    ])
    err = responses[3]
    assert err["result"]["isError"] is True
    # 不能报 "no readable registered vaults"——那是引导 kb_init 的误导文案。
    assert "solo" in err["result"]["content"][0]["text"]


def test_remove_then_reinit_clears_solo(tmp_path):
    """取消 solo 的正道：kb_remove → kb_init（缓存保留，重新参与全局检索）。"""
    solo = tmp_path / "独立库"
    solo.mkdir()
    (solo / "a.md").write_text("# A\n内容 AGAIN123", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init_solo", "arguments": {"path": str(solo)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_remove", "arguments": {"path": str(solo)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(solo)}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "AGAIN123"}}},
    ])
    assert _payload(responses[2])["removed"] is True
    # 重新 kb_init 后 solo 必须是 False（回到普通库）。
    assert _payload(responses[4])["vaults"][0]["solo"] is False
    # 唯一普通库不传 vault_path 走单库路径，照常可搜。
    search = _payload(responses[5])
    assert any("AGAIN123" in chunk["content"] for chunk in search["chunks"])
