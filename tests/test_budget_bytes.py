"""F3 budget_bytes 输出预算 + preview metadata 瘦身测试集。

契约与规格验证：
1. 防御解析：非法值/负数/字符串容错，夹取至 [500, 100000]；
2. 缺省零变化：缺省 budget_bytes=None 时无增量字段，逐字节兼容；
3. 预算截断与 next_offset 连续性：逐条累计，超限停止，返回 truncated/returned/next_offset，支持连续翻页；
4. 首条超限二分截断：单条正文 (full) 或摘要 (preview) 超预算时二分截断，响应严格 ≤ budget；
5. preview metadata 瘦身：preview=True 时 metadata 仅保留 tags 与 mtime；preview=False 全量保留；
6. 跨库分组模式全局预算：group_by_vault=True 时全局共享预算，组序不变；
7. stdio 集成：标准输入输出通信下预算严格生效且 stderr 干净。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from mortis_rag_mcp.indexer import Chunk
from mortis_rag_mcp.server import VaultMcpServer
from mortis_rag_mcp._server.fanout import _measure_payload_bytes
from mortis_rag_mcp._server.search_dispatch import _parse_budget_bytes


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


def test_parse_budget_bytes():
    """验证 budget_bytes 防御式解析与夹取。"""
    assert _parse_budget_bytes(None) is None
    assert _parse_budget_bytes("") is None
    assert _parse_budget_bytes("abc") is None
    assert _parse_budget_bytes(True) is None
    assert _parse_budget_bytes(False) is None

    # 夹取至 [500, 100000]
    assert _parse_budget_bytes(100) == 500
    assert _parse_budget_bytes(-50) == 500
    assert _parse_budget_bytes(200000) == 100000
    assert _parse_budget_bytes("1000000") == 100000

    # 正常范围与浮点数容错
    assert _parse_budget_bytes(5000) == 5000
    assert _parse_budget_bytes("3500") == 3500
    assert _parse_budget_bytes("4200.8") == 4200

    # 边界防御：inf / -inf / nan / 超大浮点数不能抛 OverflowError
    assert _parse_budget_bytes("inf") is None
    assert _parse_budget_bytes("-inf") is None
    assert _parse_budget_bytes("nan") is None
    assert _parse_budget_bytes("1e1000") is None
    assert _parse_budget_bytes(10**50) == 100000


def test_preview_metadata_slimming_locked():
    """断言 preview=True 时 metadata 仅保留 {"tags", "mtime"}；preview=False 保持全量不变。"""
    chunk = Chunk(
        id="c123",
        content="这是一段普通内容",
        source="notes/math.md",
        title="高等数学",
        metadata={
            "heading": "微积分基本定理",
            "start_line": 12,
            "end_line": 45,
            "chunk_index": 2,
            "content_hash": "a1b2c3d4",
            "tags": ["math", "calculus"],
            "mtime": 1720000000.0,
            "source_pdf": "math.pdf",
        },
        score=0.9,
    )

    # 1. full 模式 (preview=False)
    full_dict = chunk.to_dict(preview=False)
    assert full_dict["metadata"]["heading"] == "微积分基本定理"
    assert full_dict["metadata"]["start_line"] == 12
    assert full_dict["metadata"]["end_line"] == 45
    assert full_dict["metadata"]["chunk_index"] == 2
    assert full_dict["metadata"]["content_hash"] == "a1b2c3d4"
    assert full_dict["metadata"]["tags"] == ["math", "calculus"]
    assert full_dict["metadata"]["mtime"] == 1720000000.0
    assert full_dict["metadata"]["source_pdf"] == "math.pdf"

    # 2. preview 模式 (preview=True)
    preview_dict = chunk.to_dict(preview=True)
    # metadata 内部仅保留 tags 与 mtime
    assert set(preview_dict["metadata"].keys()) == {"tags", "mtime"}
    assert preview_dict["metadata"]["tags"] == ["math", "calculus"]
    assert preview_dict["metadata"]["mtime"] == 1720000000.0
    # 冗余字段已被剔除
    assert "heading" not in preview_dict["metadata"]
    assert "start_line" not in preview_dict["metadata"]
    assert "end_line" not in preview_dict["metadata"]
    assert "chunk_index" not in preview_dict["metadata"]
    assert "content_hash" not in preview_dict["metadata"]
    # 顶层依然有 heading/start_line/end_line 与 source_pdf
    assert preview_dict["heading"] == "微积分基本定理"
    assert preview_dict["start_line"] == 12
    assert preview_dict["end_line"] == 45
    assert preview_dict["source_pdf"] == "math.pdf"


@pytest.fixture
def budget_vault(tmp_path):
    vault = tmp_path / "budget_vault"
    vault.mkdir(parents=True)

    # 创建 8 个具有明显段落大小的文件 (~400 字符每个)
    for i in range(8):
        content = (
            f"# 章节 {i+1}：操作系统核心原理\n\n"
            f"这是第 {i+1} 节关于操作系统进程调度与内存管理的详细论述内容。"
            "操作系统内核负责统一管理硬件资源，为应用程序提供虚拟化执行环境。"
            "进程是系统进行资源分配和调度的基本单位，包含独立的虚拟地址空间和上下文状态。"
            f"这里记录了特定的实验观测数据与性能指标序列，编号为 OBS-{i:03d}。\n"
        )
        (vault / f"chapter_{i+1:02d}.md").write_text(content, encoding="utf-8")

    return vault


def test_budget_bytes_default_behavior_unchanged(budget_vault, tmp_path, monkeypatch):
    """断言缺省不传 budget_bytes 时，返回结果绝不追加 truncated/returned/next_offset 字段。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(budget_vault), "name": "OSVault"})

    res = server.call_tool("kb_search", {"vault_path": "OSVault", "query": "操作系统", "top_k": 5})
    data = json.loads(res["content"][0]["text"])

    assert "chunks" in data
    assert len(data["chunks"]) == 5
    assert "truncated" not in data
    assert "returned" not in data
    assert "next_offset" not in data


def test_budget_bytes_all_fit(budget_vault, tmp_path, monkeypatch):
    """断言未超预算时返回 truncated: False，且返回完整条数。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(budget_vault), "name": "OSVault"})

    # 预算充足 (50000 字节)，5 条必定能全量返回
    res = server.call_tool(
        "kb_search",
        {"vault_path": "OSVault", "query": "操作系统", "top_k": 5, "budget_bytes": 50000},
    )
    data = json.loads(res["content"][0]["text"])

    assert data["truncated"] is False
    assert data["returned"] == 5
    assert data["next_offset"] == 5
    assert len(data["chunks"]) == 5
    assert _measure_payload_bytes(data) <= 50000


def test_budget_bytes_truncation_and_offset_continuity(budget_vault, tmp_path, monkeypatch):
    """断言超预算时正确截断、标记 truncated: True，并通过 next_offset 能够连续分页。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(budget_vault), "name": "OSVault"})

    # 每条 chunk 序列化后约 800-900 字节，设置预算 2200 字节，应该只能返回 2 条左右
    budget = 2200
    res1 = server.call_tool(
        "kb_search",
        {"vault_path": "OSVault", "query": "操作系统", "top_k": 8, "budget_bytes": budget},
    )
    data1 = json.loads(res1["content"][0]["text"])

    assert data1["truncated"] is True
    n1 = data1["returned"]
    assert 1 <= n1 < 8
    assert data1["next_offset"] == n1
    assert len(data1["chunks"]) == n1
    assert _measure_payload_bytes(data1) <= budget

    # 翻页验证：使用 offset = next_offset 请求下一页
    res2 = server.call_tool(
        "kb_search",
        {"vault_path": "OSVault", "query": "操作系统", "top_k": 8, "offset": data1["next_offset"], "budget_bytes": budget},
    )
    data2 = json.loads(res2["content"][0]["text"])

    n2 = data2["returned"]
    assert n2 >= 1
    assert data2["next_offset"] == n1 + n2
    assert _measure_payload_bytes(data2) <= budget

    # 第一页与第二页的 chunk ID 不重叠
    ids1 = {c["id"] for c in data1["chunks"]}
    ids2 = {c["id"] for c in data2["chunks"]}
    assert not (ids1 & ids2)


def test_budget_bytes_first_chunk_exceeds_budget(tmp_path, monkeypatch):
    """断言首条即超出预算时，将首条的 content/snippet 二分截断至预算内。"""
    vault = tmp_path / "giant_vault"
    vault.mkdir()
    # 构造一篇超长文章 (2500 字符)
    giant_content = "# 巨型文档\n" + ("超长文本段落填充数据测试 " * 150)
    (vault / "giant.md").write_text(giant_content, encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(vault), "name": "Giant"})
    server._indexer_for({"vault_path": "Giant"}).sync()

    # 1. full 模式下的截断（预算设置 650 字节：大于空元数据 530 字节，小于完整 chunk ~2000 字节）
    budget_full = 650
    res_full = server.call_tool(
        "kb_search",
        {"vault_path": "Giant", "query": "填充数据", "top_k": 1, "budget_bytes": budget_full},
    )
    data_full = json.loads(res_full["content"][0]["text"])

    assert data_full["truncated"] is True
    assert data_full["returned"] == 1
    assert data_full["next_offset"] == 1
    assert len(data_full["chunks"]) == 1
    chunk_full = data_full["chunks"][0]
    assert 0 < len(chunk_full["content"]) < len(giant_content)
    assert _measure_payload_bytes(data_full) <= budget_full

    # 2. preview 模式下的截断（预算设置 500 字节：大于空 preview ~380 字节，小于完整 preview ~550 字节）
    budget_preview = 500
    res_preview = server.call_tool(
        "kb_search",
        {"vault_path": "Giant", "query": "填充数据", "top_k": 1, "preview": True, "budget_bytes": budget_preview},
    )
    data_preview = json.loads(res_preview["content"][0]["text"])

    assert data_preview["truncated"] is True
    assert data_preview["returned"] == 1
    assert data_preview["next_offset"] == 1
    assert len(data_preview["chunks"]) == 1
    chunk_preview = data_preview["chunks"][0]
    assert "snippet" in chunk_preview
    assert _measure_payload_bytes(data_preview) <= budget_preview


def test_budget_bytes_fanout_grouped(tmp_path, monkeypatch):
    """断言跨库分组模式下预算全局共享、组序不变。"""
    vault1 = tmp_path / "v1"
    vault1.mkdir()
    for i in range(3):
        (vault1 / f"note_{i}.md").write_text(f"# V1 笔记 {i}\n" + ("内容 " * 50), encoding="utf-8")

    vault2 = tmp_path / "v2"
    vault2.mkdir()
    for i in range(3):
        (vault2 / f"note_{i}.md").write_text(f"# V2 笔记 {i}\n" + ("内容 " * 50), encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(vault1), "name": "VaultOne"})
    server.call_tool("kb_init", {"path": str(vault2), "name": "VaultTwo"})

    # 预算 2000 字节，不足以放下两库共 6 条结果
    budget = 2000
    res = server.call_tool(
        "kb_search",
        {"query": "笔记", "group_by_vault": True, "budget_bytes": budget},
    )
    data = json.loads(res["content"][0]["text"])

    assert "groups" in data
    assert data["truncated"] is True
    total_returned = sum(len(g["chunks"]) for g in data["groups"])
    assert total_returned == data["returned"]
    assert data["next_offset"] == total_returned
    assert _measure_payload_bytes(data) <= budget


def test_budget_bytes_stdio_integration(budget_vault, tmp_path):
    """断言经 stdio 真实命令行调用时，budget_bytes 严格生效且 stderr 为空。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "kb_init", "arguments": {"path": str(budget_vault), "name": "OS"}}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "kb_search", "arguments": {"vault_path": "OS", "query": "操作系统", "top_k": 8, "budget_bytes": 1800}},
        },
    ]

    responses = _run_stdio(config_path, requests)
    assert len(responses) == 3
    search_resp = json.loads(responses[2]["result"]["content"][0]["text"])
    assert search_resp["truncated"] is True
    assert search_resp["returned"] >= 1
    assert _measure_payload_bytes(search_resp) <= 1800


def test_budget_bytes_fanout_flat(tmp_path, monkeypatch):
    """断言跨库平铺检索（不传 vault_path 且 group_by_vault=False）下预算全局截断生效。"""
    v1 = tmp_path / "vault_flat_1"
    v1.mkdir()
    (v1 / "doc1.md").write_text("# 库1\n" + "平铺跨库内容段落数据 " * 40, encoding="utf-8")

    v2 = tmp_path / "vault_flat_2"
    v2.mkdir()
    (v2 / "doc2.md").write_text("# 库2\n" + "平铺跨库内容段落数据 " * 40, encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(v1), "name": "VFlat1"})
    server.call_tool("kb_init", {"path": str(v2), "name": "VFlat2"})

    # 预算仅够容纳 1 条，第 2 条被截断
    res = server.call_tool("kb_search", {"query": "平铺", "budget_bytes": 1500})
    data = json.loads(res["content"][0]["text"])
    assert "chunks" in data
    assert data["truncated"] is True
    assert data["returned"] == 1
    assert data["next_offset"] == 1
    assert _measure_payload_bytes(data) <= 1500


def test_budget_bytes_offset_beyond_end(budget_vault, tmp_path, monkeypatch):
    """断言 offset 超出可用结果条数时，返回空列表且 truncated: False，next_offset 保持原 offset。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(budget_vault), "name": "OSVault"})

    res = server.call_tool(
        "kb_search",
        {"vault_path": "OSVault", "query": "操作系统", "offset": 100, "budget_bytes": 2000},
    )
    data = json.loads(res["content"][0]["text"])
    assert "chunks" in data
    assert len(data["chunks"]) == 0
    assert data["truncated"] is False
    assert data["returned"] == 0
    assert data["next_offset"] == 100

