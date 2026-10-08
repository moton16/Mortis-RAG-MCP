from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mortis_rag_mcp.server import VaultMcpServer, _tool_definitions


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
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


def test_tool_definitions_includes_kb_ingest():
    tools = _tool_definitions()
    tool_map = {t["name"]: t for t in tools}
    assert "kb_ingest" in tool_map
    ingest_tool = tool_map["kb_ingest"]
    assert "PDF/Office" in ingest_tool["description"]
    schema = ingest_tool["inputSchema"]
    assert schema["required"] == ["action"]
    assert "action" in schema["properties"]
    assert schema["properties"]["action"]["enum"] == ["submit", "status", "pending"]
    assert "sources" in schema["properties"]
    assert "job_id" in schema["properties"]
    assert "vault_path" in schema["properties"]


def test_kb_init_and_init_solo_hint_when_ingest_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "my_vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\ncontent", encoding="utf-8")
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 dummy")
    (vault / "slides.pptx").write_bytes(b"PK dummy")

    server = VaultMcpServer()
    # Default config has ingest.enabled = False
    res = server.call_tool("kb_init", {"path": str(vault), "name": "test_vault"})
    data = json.loads(res["content"][0]["text"])

    assert data["registered"] is True
    assert data["md_files"] == 1
    assert data["ingestible_docs"] == 2
    assert "hint" in data
    assert "PDF 摄取层默认未启用" in data["hint"]
    assert "[ingest] enabled = true" in data["hint"]
    assert "未确认前不要自作主张开启" in data["hint"]


def test_kb_init_hint_when_ingest_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "my_vault"
    vault.mkdir()
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 dummy")

    server = VaultMcpServer()
    server.config.ingest.enabled = True
    res = server.call_tool("kb_init", {"path": str(vault), "name": "enabled_vault"})
    data = json.loads(res["content"][0]["text"])

    assert data["ingestible_docs"] == 1
    assert "hint" in data
    assert "kb_ingest(action='pending')" in data["hint"]


def test_kb_init_solo_hint_when_ingest_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "solo_vault"
    vault.mkdir()
    (vault / "doc.docx").write_bytes(b"PK dummy")

    server = VaultMcpServer()
    res = server.call_tool("kb_init_solo", {"path": str(vault), "name": "solo_vault"})
    data = json.loads(res["content"][0]["text"])

    assert data["solo"] is True
    assert data["ingestible_docs"] == 1
    assert "hint" in data
    assert "PDF 摄取层默认未启用" in data["hint"]


def test_kb_ingest_disabled_error_on_submit(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "test_vault"
    vault.mkdir()
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 dummy")

    server = VaultMcpServer()
    server.call_tool("kb_init", {"path": str(vault)})

    # pending is allowed even when ingest is disabled (read-only scan)
    pending_res = server.call_tool("kb_ingest", {"action": "pending"})
    pending_data = json.loads(pending_res["content"][0]["text"])
    assert len(pending_data["pending"]) == 1
    assert pending_data["pending"][0]["source"] == "paper.pdf"

    # status is allowed
    status_res = server.call_tool("kb_ingest", {"action": "status"})
    status_data = json.loads(status_res["content"][0]["text"])
    assert status_data["summary"] == {}

    # submit must fail with helpful message
    with pytest.raises(ValueError, match="ingest disabled"):
        server.call_tool("kb_ingest", {"action": "submit"})


def test_kb_ingest_submit_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "test_vault"
    vault.mkdir()
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 dummy")

    server = VaultMcpServer()
    server.config.ingest.enabled = True
    server.call_tool("kb_init", {"path": str(vault)})

    with patch("mortis_rag_mcp.ingest.worker.IngestManager._client_or_make") as mock_client_factory:
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client
        # Mock parse return
        mock_parsed = MagicMock()
        mock_parsed.channel = "v4"
        mock_parsed.markdown = "# Parsed Paper\nSome content"
        mock_parsed.images = {}
        mock_client.parse.return_value = mock_parsed

        res = server.call_tool("kb_ingest", {"action": "submit", "sources": ["paper.pdf"]})
        data = json.loads(res["content"][0]["text"])

        assert data["submitted"] == 1
        assert len(data["jobs"]) == 1
        assert "hint" in data
        job_id = data["jobs"][0]["job_id"]

        # Query single job status
        status_res = server.call_tool("kb_ingest", {"action": "status", "job_id": job_id})
        status_data = json.loads(status_res["content"][0]["text"])
        assert status_data["job"]["job_id"] == job_id


def test_stdio_kb_ingest_protocol_roundtrip(tmp_path):
    vault = tmp_path / "stdio_vault"
    vault.mkdir()
    (vault / "file.pdf").write_bytes(b"%PDF-1.4 dummy")

    config = tmp_path / "app.toml"
    config.write_text(
        f'vault_path = "{vault.as_posix()}"\nmode = "static"\n[ingest]\nenabled = false\n',
        encoding="utf-8",
    )

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_ingest", "arguments": {"action": "pending"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_ingest", "arguments": {"action": "submit"}}},
    ])

    tools = [t["name"] for t in responses[1]["result"]["tools"]]
    assert "kb_ingest" in tools

    pending_data = _payload(responses[2])
    assert len(pending_data["pending"]) == 1
    assert pending_data["pending"][0]["source"] == "file.pdf"

    # submit should return isError: true with descriptive message when disabled
    assert responses[3]["result"].get("isError") is True
    err_text = responses[3]["result"]["content"][0]["text"]
    assert "ingest disabled" in err_text


def test_kb_stats_skipped_unsupported_excludes_ingest_exts(tmp_path):
    vault = tmp_path / "stats_vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\ncontent", encoding="utf-8")
    (vault / "book.pdf").write_bytes(b"dummy pdf")
    (vault / "report.docx").write_bytes(b"dummy docx")
    (vault / "photo.raw").write_bytes(b"dummy raw")

    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")

    from mortis_rag_mcp.ingest import INGEST_EXTS

    responses = _run_stdio(config, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "StatsVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_stats", "arguments": {"vault_path": "StatsVault"}}},
    ])

    stats = _payload(responses[2])
    skipped = stats.get("skipped_unsupported", {})
    assert "raw" in skipped
    for ext in INGEST_EXTS:
        bare = ext.lstrip(".")
        assert bare not in skipped, f"kb_stats.skipped_unsupported 误包含了可摄取格式: {bare}"


def test_kb_stats_includes_ingest_auto_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "stats_auto_vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\ncontent", encoding="utf-8")

    server = VaultMcpServer()
    server.config.ingest.enabled = True
    server.config.ingest.auto_watch = True
    # ingest_auto 快照来自 legacy 账本（.ingest_state.json）；virtual 的账本在 store 里，
    # 本用例专测 legacy 快照字段，故显式选回 legacy。
    server.config.ingest.storage = "legacy"
    server.config.ingest.max_file_size_mb = 15
    server.config.watch_method = "poll"
    server.config.watch_fallback_interval = 25.0

    server.call_tool("kb_init", {"path": str(vault), "name": "auto_vault"})
    res = server.call_tool("kb_stats", {"vault_path": "auto_vault"})
    stats = json.loads(res["content"][0]["text"])

    assert "ingest_auto" in stats
    ia = stats["ingest_auto"]
    assert ia["configured"] is True
    assert ia["effective"] is True
    assert ia["max_file_size_mb"] == 15
    assert ia["watch_method"] == "poll"
    assert ia["effective_interval"] == 25.0
    assert ia["skipped_too_large"] == 0
    assert ia["skipped_seen"] == 0
    assert ia["skipped_ignored"] == 0

    # Trigger hook to guarantee last_scan_at is recorded
    indexer = server._indexer_for({"vault_path": "auto_vault"})
    indexer._ingest_hook()
    res_after = server.call_tool("kb_stats", {"vault_path": "auto_vault"})
    stats_after = json.loads(res_after["content"][0]["text"])
    assert stats_after["ingest_auto"]["last_scan_at"] is not None

    # Negative check: vault initialized when ingest is disabled has last_scan_at None
    server_dis = VaultMcpServer()
    server_dis.config.ingest.enabled = False
    vault_dis = tmp_path / "stats_dis_vault"
    vault_dis.mkdir()
    (vault_dis / "note.md").write_text("# Note", encoding="utf-8")
    server_dis.call_tool("kb_init", {"path": str(vault_dis), "name": "dis_vault"})
    res_dis = server_dis.call_tool("kb_stats", {"vault_path": "dis_vault"})
    stats_dis = json.loads(res_dis["content"][0]["text"])
    assert stats_dis["ingest_auto"]["last_scan_at"] is None


def test_kb_ingest_status_warning_when_contradictory(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "contra_vault"
    vault.mkdir()
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 dummy")

    server = VaultMcpServer()
    server.config.ingest.enabled = False
    server.config.ingest.auto_watch = True
    server.call_tool("kb_init", {"path": str(vault), "name": "contra_vault"})

    status_res = server.call_tool("kb_ingest", {"action": "status", "vault_path": "contra_vault"})
    data = json.loads(status_res["content"][0]["text"])
    assert "warning" in data
    assert "auto_watch=true 但 enabled=false" in data["warning"]


def test_mock_e2e_auto_ingest_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "e2e_vault"
    vault.mkdir()
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 e2e test dummy content")

    server = VaultMcpServer()
    server.config.ingest.enabled = True
    server.config.ingest.auto_watch = True
    # 本用例专测 **legacy 回退路径**（物理镜像 + 旧 IngestManager 内部打桩）；
    # v0.9.0 C96 起 ingest.storage 默认 virtual，故这里显式选回 legacy。
    server.config.ingest.storage = "legacy"

    with patch("mortis_rag_mcp.ingest.worker.IngestManager._client_or_make") as mock_client_factory:
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client
        mock_parsed = MagicMock()
        mock_parsed.channel = "v4"
        mock_parsed.markdown = "# Quantum Computing Overview\nQubits and entanglement in deep space."
        mock_parsed.images = {}
        mock_client.parse.return_value = mock_parsed

        server.call_tool("kb_init", {"path": str(vault), "name": "e2e_vault"})

        # Get indexer, which has _ingest_hook wired
        indexer = server._indexer_for({"vault_path": "e2e_vault"})
        assert callable(indexer._ingest_hook)

        # Trigger hook directly (simulates scan thread trigger)
        # review R3 两次采样：首扫只登记判稳采样，次扫才真正入队
        indexer._ingest_hook()
        indexer._ingest_hook()

        # Manager should have auto_submitted and started worker thread.
        # Wait briefly for worker to complete parsing
        manager = server._ingest_manager_for(str(vault))
        if manager._worker and manager._worker.is_alive():
            manager._worker.join(timeout=5.0)

        # Confirm markdown落盘
        parsed_md = vault / ".mortis-parsed" / "paper.md"
        assert parsed_md.exists()
        assert "Quantum Computing Overview" in parsed_md.read_text(encoding="utf-8")

        # IngestManager on_job_finished called indexer.request_refresh()
        # Verify search hits the newly parsed content
        indexer.sync()

        search_res = server.call_tool("kb_search", {
            "query": "entanglement quantum",
            "vault_path": "e2e_vault",
        })
        search_data = json.loads(search_res["content"][0]["text"])
        chunks = search_data.get("chunks") or search_data.get("results") or []
        assert len(chunks) > 0
        assert any("Quantum Computing" in r.get("content", "") or "Quantum Computing" in r.get("heading", "") or "paper.md" in r.get("source", "") for r in chunks)


def test_mock_auto_ingest_negative_when_auto_watch_false(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "neg_vault"
    vault.mkdir()
    (vault / "paper.pdf").write_bytes(b"%PDF-1.4 dummy")

    server = VaultMcpServer()
    server.config.ingest.enabled = True
    server.config.ingest.auto_watch = False

    with patch("mortis_rag_mcp.ingest.worker.IngestManager._client_or_make") as mock_client_factory:
        server.call_tool("kb_init", {"path": str(vault), "name": "neg_vault"})
        indexer = server._indexer_for({"vault_path": "neg_vault"})
        assert indexer._ingest_hook is None
        mock_client_factory.assert_not_called()


