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
        encoding="utf-8",
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


def test_stdio_init_and_fanout_search_across_vaults(tmp_path):
    vault_a = tmp_path / "库A"
    vault_b = tmp_path / "库B"
    vault_a.mkdir()
    vault_b.mkdir()
    (vault_a / "alpha.md").write_text("# Alpha\n知识库A的独有内容 ALPHA123", encoding="utf-8")
    (vault_b / "beta.md").write_text("# Beta\n知识库B的独有内容 BETA456", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_a), "name": "库A"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault_b)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "ALPHA123"}}},
    ])
    # kb_init registered both vaults
    assert _payload(responses[1])["registered"] is True
    assert _payload(responses[2])["name"] == "库B"
    # Fan-out without vault_path searched both and tagged the source vault
    data = _payload(responses[3])
    assert len(data["searched"]) == 2
    assert data["errors"] == {}
    hits = [chunk for chunk in data["chunks"] if "ALPHA123" in chunk["content"]]
    assert hits and hits[0]["vault"] == str(vault_a)
    assert hits[0]["vault_name"] == "库A"


def test_stdio_duplicate_init_errors_and_unregister_removes(tmp_path):
    vault = tmp_path / "库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_unregister", "arguments": {"path": str(vault), "purge_cache": True}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "hello", "vault_path": str(vault)}}},
    ])
    dup = responses[2]
    assert "error" in dup
    assert "already registered" in dup["error"]["message"]

    unreg = _payload(responses[3])
    assert unreg["unregistered"] is True
    assert unreg["watcher_stopped"] is True
    assert unreg["cache_purged"] is True

    # After unregistering, the path is no longer searchable.
    err = responses[4]
    assert "error" in err
    assert "kb_init" in err["error"]["message"]

    # Cache files were purged for that vault.
    cache_dir = tmp_path / "cache"
    assert not any(cache_dir.rglob("*.bin")) if cache_dir.exists() else True


def test_stdio_missing_vault_folder_listed_as_not_exists(tmp_path):
    vault = tmp_path / "会消失的库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n内容", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault)}}},
    ])
    assert _payload(responses[1])["registered"] is True

    # Delete the folder behind the registry's back...
    import shutil
    shutil.rmtree(vault)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_vaults", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "内容"}}},
    ])
    data = _payload(responses[1])
    assert data["vaults"][0]["exists"] is False
    # Fan-out skips the dead vault without exploding.
    search = _payload(responses[2])
    assert search["chunks"] == []
    assert str(vault) not in search.get("searched", [])


def test_stdio_init_indexes_in_background(tmp_path):
    vault = tmp_path / "库"
    vault.mkdir()
    (vault / "a.md").write_text("# A\nhello world", encoding="utf-8")
    config = _make_config(tmp_path)

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault)}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_stats", "arguments": {}}},
    ])
    init = _payload(responses[1])
    assert init["md_files"] == 1
    # kb_stats triggers its own sync, so the single registered vault is indexed.
    stats = _payload(responses[2])
    assert stats["files"] == 1
    assert stats["chunks"] >= 1


def test_stdio_legacy_vault_path_auto_migrates(tmp_path):
    vault = tmp_path / "旧库"
    vault.mkdir()
    (vault / "legacy.md").write_text("# Legacy\n旧库内容 XYZ999", encoding="utf-8")
    config = tmp_path / "app.toml"
    config.write_text(f'vault_path = "{vault.as_posix()}"\nmode = "static"\n', encoding="utf-8")

    # No explicit registry file: legacy [vault].path must be auto-imported.
    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_vaults", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "XYZ999"}}},
    ])
    data = _payload(responses[1])
    assert len(data["vaults"]) == 1
    assert data["vaults"][0]["path"] == str(vault.resolve())
    search = _payload(responses[2])
    assert any("XYZ999" in chunk["content"] for chunk in search["chunks"])
