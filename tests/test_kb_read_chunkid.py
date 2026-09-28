from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig, IndexConfig, load_config
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """自动将注册表重定向至单测独立 tmp_path，杜绝污染全局 ~/.mortis_rag_mcp/vaults.toml。"""
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def _run_stdio(config: Path, requests: list[dict]) -> list[dict]:
    """通过 stdio 进程启动真实 MCP Server 验证 JSON-RPC 往返与 stderr 纯净度。"""
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    reg_file = str(config.parent / "vaults.toml")
    proc = subprocess.run(
        [sys.executable, "-m", "mortis_rag_mcp", "--serve-mcp-stdio", "--app-config", str(config)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env={**os.environ, "MORTIS_RAG_REGISTRY": reg_file, "VAULT_MCP_REGISTRY": reg_file},
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr, f"stderr 必须保持干净，实际输出: {proc.stderr}"
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


def test_stdio_search_and_read_chunk_id(tmp_path: Path):
    """F1 stdio 集成：kb_search 命中后直接用 chunk_id 调用 kb_read 原地展开。"""
    vault = tmp_path / "notes"
    vault.mkdir()
    # 构造一个多行笔记文件
    lines = [f"Line {i:03d} content for testing" for i in range(1, 101)]
    lines[50] = "SpecialKeyword target line in middle"
    (vault / "doc.md").write_text("\n".join(lines), encoding="utf-8")

    app_toml = tmp_path / "app.toml"
    app_toml.write_text('mode = "static"\n[index]\nread_max_chars = 20000\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(vault), "name": "MainVault"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_search", "arguments": {"query": "SpecialKeyword", "vault_path": "MainVault"}}},
    ]
    responses = _run_stdio(app_toml, requests)
    search_res = json.loads(responses[2]["result"]["content"][0]["text"])
    assert len(search_res["chunks"]) > 0
    target_chunk = search_res["chunks"][0]
    chunk_id = target_chunk["id"]
    assert chunk_id

    # 4. 用 chunk_id 原地展开，默认 expand_lines=30
    read_req = [
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "kb_read", "arguments": {"chunk_id": chunk_id, "vault_path": "MainVault"}}},
    ]
    read_res_list = _run_stdio(app_toml, requests[:2] + read_req)
    read_out = json.loads(read_res_list[2]["result"]["content"][0]["text"])

    assert read_out["source"] == "doc.md"
    assert read_out["chunk_id"] == chunk_id
    assert "SpecialKeyword" in read_out["content"]
    assert read_out["truncated"] is False
    # 行号区间合理展开
    assert read_out["start_line"] >= 1
    assert read_out["end_line"] >= read_out["start_line"]


def test_kb_read_chunk_id_expand_lines_variations(tmp_path: Path):
    """验证 expand_lines=0、超大值夹取 (500)、负数与非法值防御解析。"""
    vault = tmp_path / "vault_expand"
    vault.mkdir()
    lines = [f"Row {i:03d}" for i in range(1, 201)]
    lines[100] = "TargetRow in the center"
    (vault / "test.md").write_text("\n".join(lines), encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="ExpVault")
    indexer = server._indexer_for({"vault_path": "ExpVault"})
    indexer.sync()

    chunks = indexer.all_chunks()
    assert len(chunks) > 0
    cid = chunks[0].id
    c_start = chunks[0].metadata.get("start_line", 1)
    c_end = chunks[0].metadata.get("end_line", c_start)

    # 1. expand_lines = 0：精确切片行区间
    res_zero = server._kb_read({"chunk_id": cid, "expand_lines": 0, "vault_path": "ExpVault"})
    assert res_zero["chunk_id"] == cid
    assert res_zero["start_line"] == c_start
    assert res_zero["end_line"] == c_end

    # 2. expand_lines = 1000：夹取到 500
    res_large = server._kb_read({"chunk_id": cid, "expand_lines": 1000, "vault_path": "ExpVault"})
    assert res_large["start_line"] == max(1, c_start - 500)
    assert res_large["end_line"] == c_end + 500

    # 3. expand_lines = -5：夹取到 0
    res_neg = server._kb_read({"chunk_id": cid, "expand_lines": -5, "vault_path": "ExpVault"})
    assert res_neg["start_line"] == c_start
    assert res_neg["end_line"] == c_end

    # 4. expand_lines = "invalid"：非法值退回默认 30
    res_invalid = server._kb_read({"chunk_id": cid, "expand_lines": "not_a_number", "vault_path": "ExpVault"})
    assert res_invalid["start_line"] == max(1, c_start - 30)
    assert res_invalid["end_line"] == c_end + 30


def test_kb_read_chunk_id_not_found(tmp_path: Path):
    """验证未命中 chunk_id 抛出符合契约的清晰报错文案。"""
    vault = tmp_path / "vault_not_found"
    vault.mkdir()
    (vault / "dummy.md").write_text("dummy text\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="NFVault")
    indexer = server._indexer_for({"vault_path": "NFVault"})
    indexer.sync()

    fake_id = "0123456789abcdef9999"
    with pytest.raises(ValueError) as exc_info:
        server._kb_read({"chunk_id": fake_id, "vault_path": "NFVault"})

    msg = str(exc_info.value)
    assert msg.startswith("chunk_id not found: 0123456789ab…")
    assert "请重新 kb_search 获取新 id" in msg


def test_kb_read_chunk_id_mutual_exclusive(tmp_path: Path):
    """验证 chunk_id 与 start_line/end_line/heading 互斥报错。"""
    vault = tmp_path / "vault_mutex"
    vault.mkdir()
    (vault / "note.md").write_text("# Title\ncontent here\n", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="MutexVault")
    indexer = server._indexer_for({"vault_path": "MutexVault"})
    indexer.sync()
    cid = indexer.all_chunks()[0].id

    # 1. chunk_id + start_line
    with pytest.raises(ValueError, match="chunk_id is mutually exclusive with start_line/end_line/heading"):
        server._kb_read({"chunk_id": cid, "start_line": 1, "vault_path": "MutexVault"})

    # 2. chunk_id + end_line
    with pytest.raises(ValueError, match="chunk_id is mutually exclusive with start_line/end_line/heading"):
        server._kb_read({"chunk_id": cid, "end_line": 2, "vault_path": "MutexVault"})

    # 3. chunk_id + heading
    with pytest.raises(ValueError, match="chunk_id is mutually exclusive with start_line/end_line/heading"):
        server._kb_read({"chunk_id": cid, "heading": "Title", "vault_path": "MutexVault"})

    # 4. 空字符串与 None 不误判互斥
    res_empty = server._kb_read({
        "chunk_id": cid,
        "start_line": "",
        "end_line": None,
        "heading": "",
        "vault_path": "MutexVault",
    })
    assert res_empty["chunk_id"] == cid
    assert "Title" in res_empty["content"]


def test_kb_read_missing_source_and_chunk_id(tmp_path: Path):
    """验证 source 与 chunk_id 均未提供时报错。"""
    vault = tmp_path / "vault_empty"
    vault.mkdir()
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault), name="EmptyVault")

    with pytest.raises(ValueError, match="source or chunk_id is required for kb_read"):
        server._kb_read({"vault_path": "EmptyVault"})

    with pytest.raises(ValueError, match="source or chunk_id is required for kb_read"):
        server._kb_read({"source": "  ", "chunk_id": "", "vault_path": "EmptyVault"})


def test_read_max_chars_configuration_and_truncation(tmp_path: Path):
    """验证 [index] read_max_chars 配置生效、非法值防御兜底与截断标记。"""
    # 1. 默认配置 read_max_chars = 20000
    cfg1 = load_config(None)
    assert cfg1.index.read_max_chars == 20000

    cfg_toml = tmp_path / "custom.toml"
    cfg_toml.write_text("[index]\nread_max_chars = 300\n", encoding="utf-8")
    cfg2 = load_config(cfg_toml)
    assert cfg2.index.read_max_chars == 300

    # 2. 非法值下限防御：< 100 退回 20000
    cfg_toml.write_text("[index]\nread_max_chars = 50\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000

    # 3. 非法值上限防御：> 1000000 退回 20000
    cfg_toml.write_text("[index]\nread_max_chars = 2000000\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000

    # 4. 非法字符串/bool/inf 防御：退回 20000
    cfg_toml.write_text("[index]\nread_max_chars = 'bad_value'\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000
    cfg_toml.write_text("[index]\nread_max_chars = true\n", encoding="utf-8")
    assert load_config(cfg_toml).index.read_max_chars == 20000
    assert IndexConfig(read_max_chars=float("inf")).read_max_chars == 20000

    # 5. 截断行为验证
    vault = tmp_path / "vault_trunc"
    vault.mkdir()
    long_content = "A" * 600
    (vault / "long.md").write_text(long_content, encoding="utf-8")

    cfg_toml.write_text("[index]\nread_max_chars = 250\n", encoding="utf-8")
    server = VaultMcpServer(cfg_toml)
    server.registry.add(str(vault), name="TruncVault")
    indexer = server._indexer_for({"vault_path": "TruncVault"})
    indexer.sync()
    cid = indexer.all_chunks()[0].id

    res = server._kb_read({"chunk_id": cid, "vault_path": "TruncVault"})
    assert res["truncated"] is True
    assert len(res["content"]) == 250
    assert res["chunk_id"] == cid


def test_multi_vault_chunk_id_disambiguation(tmp_path: Path):
    """跨两库同内容：未指定 vault_path 提示显式传参；指定后精确读取。"""
    vault1 = tmp_path / "vault_one"
    vault1.mkdir()
    vault2 = tmp_path / "vault_two"
    vault2.mkdir()

    same_text = "# Shared Note\nIdentical content in both vaults\n"
    (vault1 / "shared.md").write_text(same_text, encoding="utf-8")
    (vault2 / "shared.md").write_text(same_text, encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(vault1), name="VaultOne")
    server.registry.add(str(vault2), name="VaultTwo")

    idx1 = server._indexer_for({"vault_path": "VaultOne"})
    idx2 = server._indexer_for({"vault_path": "VaultTwo"})
    idx1.sync()
    idx2.sync()

    chunk1 = idx1.all_chunks()[0]
    chunk2 = idx2.all_chunks()[0]
    # 同内容 chunk.id 相同
    assert chunk1.id == chunk2.id
    cid = chunk1.id

    # 1. 未传 vault_path 且多库存在：触发现有消歧机制报错
    with pytest.raises(ValueError, match="multiple vaults registered; pass an explicit vault_path"):
        server._kb_read({"chunk_id": cid})

    # 2. 显式传 VaultOne：成功读取
    res1 = server._kb_read({"chunk_id": cid, "vault_path": "VaultOne"})
    assert res1["source"] == "shared.md"
    assert res1["chunk_id"] == cid
    assert "Identical content" in res1["content"]

    # 3. 显式传 VaultTwo：成功读取
    res2 = server._kb_read({"chunk_id": cid, "vault_path": "VaultTwo"})
    assert res2["source"] == "shared.md"
    assert res2["chunk_id"] == cid
    assert "Identical content" in res2["content"]
