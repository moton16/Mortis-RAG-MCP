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

import pytest

from mortis_rag_mcp.indexer import Chunk, MarkdownIndexer
from mortis_rag_mcp.server import VaultMcpServer
from mortis_rag_mcp._server.fanout import _measure_payload_bytes
from mortis_rag_mcp._server.search_dispatch import _parse_budget_bytes


def _kb_init_ready(server: VaultMcpServer, path, name: str) -> None:
    """kb_init 之后显式同步一次，让索引状态确定。

    C66 起 kb_search 走「优先使用已就绪索引、后台刷新」，不再在前台阻塞等待首建完成；
    kb_init 只把首建丢进后台线程，因此紧跟其后的检索会与后台首建竞态——Windows 上
    侥幸拿到完整索引，Linux CI 上只拿到部分结果（本条曾导致 ubuntu 5 连红）。
    要断言完整索引的测试必须显式同步。
    """
    server.call_tool("kb_init", {"path": str(path), "name": name})
    server._indexer_for({"vault_path": name}).sync()


def _is_search_settled(data: dict) -> bool:
    """首建完成判据：非冷启动(status!=indexing)且后台无在飞构建。"""
    return data.get("status") != "indexing" and not data.get("indexing_in_progress")


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
    _kb_init_ready(server, budget_vault, "OSVault")

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
    _kb_init_ready(server, budget_vault, "OSVault")

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
    _kb_init_ready(server, budget_vault, "OSVault")

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
    """断言首条即超出预算时返回 returned=0、原游标、budget_hint，不二分截断；compact 模式下能放下一整条。"""
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

    # 1. full 模式下首条超预算：首条完整 chunk 约 3800+ 字节，1000 字节无法容纳完整 chunk
    budget_full = 1000
    res_full = server.call_tool(
        "kb_search",
        {"vault_path": "Giant", "query": "填充数据", "top_k": 1, "budget_bytes": budget_full},
    )
    data_full = json.loads(res_full["content"][0]["text"])

    assert data_full["truncated"] is True
    assert data_full["returned"] == 0
    assert data_full["next_offset"] == 0
    assert len(data_full["chunks"]) == 0
    assert "budget_hint" in data_full
    assert "use compact or increase budget_bytes" in data_full["budget_hint"]
    assert _measure_payload_bytes(data_full) <= budget_full

    # 2. preview 模式下的截断（预算设置 600 字节：小于完整 preview ~800-900 字节）
    budget_preview = 600
    res_preview = server.call_tool(
        "kb_search",
        {"vault_path": "Giant", "query": "填充数据", "top_k": 1, "preview": True, "budget_bytes": budget_preview},
    )
    data_preview = json.loads(res_preview["content"][0]["text"])

    assert data_preview["truncated"] is True
    assert data_preview["returned"] == 0
    assert data_preview["next_offset"] == 0
    assert len(data_preview["chunks"]) == 0
    assert "budget_hint" in data_preview
    assert _measure_payload_bytes(data_preview) <= budget_preview

    # 3. compact 模式对照：紧凑投影去掉 id/score/title/metadata 等（约 700-850 字节），1000 字节预算下能够完整放下一整条
    budget_compact = 1000
    res_compact = server.call_tool(
        "kb_search",
        {"vault_path": "Giant", "query": "填充数据", "top_k": 1, "compact": True, "budget_bytes": budget_compact},
    )
    data_compact = json.loads(res_compact["content"][0]["text"])

    assert data_compact["returned"] == 1
    assert len(data_compact["chunks"]) == 1
    assert "budget_hint" not in data_compact
    assert _measure_payload_bytes(data_compact) <= budget_compact


def test_budget_bytes_fanout_grouped(tmp_path, monkeypatch):
    """断言跨库分组模式下预算全局共享、组序不变、各组维护独立游标且顶层 next_offset 为 None。"""
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
    _kb_init_ready(server, vault1, "VaultOne")
    _kb_init_ready(server, vault2, "VaultTwo")

    # 预算 3500 字节，不足以放下两库共 6 条结果（每条 ~800 字节 + 元数据）
    budget = 3500
    res = server.call_tool(
        "kb_search",
        {"query": "笔记", "group_by_vault": True, "budget_bytes": budget},
    )
    data = json.loads(res["content"][0]["text"])

    assert "groups" in data
    assert data["truncated"] is True
    total_returned = sum(len(g["chunks"]) for g in data["groups"])
    assert total_returned == data["returned"]
    assert 1 <= total_returned < 6
    assert data["next_offset"] is None
    assert "group_next_offsets" in data
    for g in data["groups"]:
        assert "next_offset" in g
        assert "returned" in g
        assert "truncated" in g
        assert g["returned"] == len(g["chunks"])
        assert g["next_offset"] == data["group_next_offsets"][g["vault"]]
    assert _measure_payload_bytes(data) <= budget

    # 连续翻页验证：原样传回 group_next_offsets 作为 group_offsets
    res2 = server.call_tool(
        "kb_search",
        {
            "query": "笔记",
            "group_by_vault": True,
            "budget_bytes": budget,
            "group_offsets": data["group_next_offsets"],
        },
    )
    data2 = json.loads(res2["content"][0]["text"])
    total_returned_2 = sum(len(g["chunks"]) for g in data2["groups"])
    assert total_returned_2 >= 1
    assert data2["next_offset"] is None

    # 两页返回的 chunk 不重复
    ids1 = {c["id"] for g in data["groups"] for c in g["chunks"]}
    ids2 = {c["id"] for g in data2["groups"] for c in g["chunks"]}
    assert not (ids1 & ids2)


def test_budget_bytes_stdio_integration(budget_vault, tmp_path, stdio_polling):
    """断言经 stdio 真实命令行调用时，budget_bytes 严格生效且 stderr 为空。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")

    # C66：kb_search 不再阻塞等待首建（读优先）。首建进行中时响应为
    # status:"indexing"（冷启动）或带 indexing_in_progress（部分索引）；
    # stdio 子进程没有可用参数让首建同步完成，故按真实客户端契约在一条交互式
    # 会话里带真实间隔轮询到索引就绪，再断言预算行为。
    registry = str(config_path.parent / "vaults.toml")
    session = stdio_polling(
        config_path,
        prefix_requests=[
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "kb_init", "arguments": {"path": str(budget_vault), "name": "OS"}},
            },
        ],
        poll_request={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "kb_search",
                "arguments": {"vault_path": "OS", "query": "操作系统", "top_k": 8, "budget_bytes": 1800},
            },
        },
        is_settled=_is_search_settled,
        env_overrides={"VAULT_MCP_REGISTRY": registry, "MORTIS_RAG_REGISTRY": registry},
    )

    search_resp = session["observed"][-1]
    assert _is_search_settled(search_resp), f"首建未在轮询窗口内完成：{search_resp}"
    assert search_resp["truncated"] is True
    assert search_resp["returned"] >= 1
    assert _measure_payload_bytes(search_resp) <= 1800


def test_budget_bytes_fanout_flat(tmp_path, monkeypatch):
    """断言跨库平铺检索（不传 vault_path 且 group_by_vault=False）下预算完整 chunk 截断生效。"""
    v1 = tmp_path / "vault_flat_1"
    v1.mkdir()
    doc1_content = "# 库1\n" + "平铺跨库内容段落数据 " * 40
    (v1 / "doc1.md").write_text(doc1_content, encoding="utf-8")

    v2 = tmp_path / "vault_flat_2"
    v2.mkdir()
    doc2_content = "# 库2\n" + "平铺跨库内容段落数据 " * 40
    (v2 / "doc2.md").write_text(doc2_content, encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    _kb_init_ready(server, v1, "VFlat1")
    _kb_init_ready(server, v2, "VFlat2")

    # 预算 2500 字节仅够容纳 1 条（每条 ~1600 字节 + 元数据），第 2 条被截断
    budget = 2500
    res = server.call_tool("kb_search", {"query": "平铺", "budget_bytes": budget})
    data = json.loads(res["content"][0]["text"])
    assert "chunks" in data
    assert data["truncated"] is True
    assert data["returned"] == 1
    assert data["chunks"][0]["content"] == doc1_content.rstrip()
    assert _measure_payload_bytes(data) <= budget


def test_budget_bytes_offset_beyond_end(budget_vault, tmp_path, monkeypatch):
    """断言 offset 超出可用结果条数时，返回空列表且 truncated: False，next_offset 保持原 offset。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    _kb_init_ready(server, budget_vault, "OSVault")

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


def test_budget_bytes_minimum_envelope_overflow(tmp_path, monkeypatch):
    """断言当最小元数据包络本身超出 budget_bytes 时，返回 budget_exceeded=true 与精确迭代稳定的 minimum_budget_bytes。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    # 注册 3 个名字较长的库以产生大于 500 字节的跨库元数据包络
    for i in range(3):
        v = tmp_path / f"long_named_vault_corpus_data_{i}"
        v.mkdir()
        (v / "note.md").write_text(f"# 笔记{i}\n内容数据段落测试", encoding="utf-8")
        _kb_init_ready(server, v, f"CorpusVaultBranch{i}")

    # 设置极小预算 500 字节（最小包络含 searched/errors/hint/group_next_offsets 已大于 600 字节）
    res = server.call_tool(
        "kb_search",
        {"query": "笔记", "group_by_vault": True, "budget_bytes": 500},
    )
    data = json.loads(res["content"][0]["text"])

    assert data["budget_exceeded"] is True
    assert data["truncated"] is True
    assert data["returned"] == 0
    assert data["groups"] == []
    assert data["next_offset"] is None
    assert "group_next_offsets" in data
    assert data["budget_hint"] == "narrow vaults or increase budget_bytes"
    # minimum_budget_bytes 必须与当前响应实际测量值严格相等（迭代稳定）
    assert data["minimum_budget_bytes"] == _measure_payload_bytes(data)
    assert data["minimum_budget_bytes"] > 500


def test_budget_bytes_group_offsets_validation(tmp_path, monkeypatch):
    """验证 group_offsets 的防御式类型、范围、授权及互斥校验。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "a.md").write_text("# A\n内容", encoding="utf-8")
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "b.md").write_text("# B\n内容", encoding="utf-8")
    v_solo = tmp_path / "v_solo"
    v_solo.mkdir()
    (v_solo / "s.md").write_text("# S\n内容", encoding="utf-8")

    server = VaultMcpServer(config_path)
    _kb_init_ready(server, v1, "V1")
    _kb_init_ready(server, v2, "V2")
    server.call_tool("kb_init_solo", {"path": str(v_solo), "name": "VSolo"})

    # 1. group_by_vault=False 时传入 group_offsets -> ValueError
    with pytest.raises(ValueError, match="group_offsets is only allowed when group_by_vault=true"):
        server.call_tool("kb_search", {"query": "内容", "group_by_vault": False, "group_offsets": {"V1": 0}})

    # 2. 负数偏移 -> ValueError
    with pytest.raises(ValueError, match="group_offsets values must be >= 0"):
        server.call_tool("kb_search", {"query": "内容", "group_by_vault": True, "group_offsets": {"V1": -1}})

    # 3. 布尔值或浮点数 -> ValueError
    with pytest.raises(ValueError, match="group_offsets values must be non-negative integers"):
        server.call_tool("kb_search", {"query": "内容", "group_by_vault": True, "group_offsets": {"V1": True}})

    with pytest.raises(ValueError, match="group_offsets values must be non-negative integers"):
        server.call_tool("kb_search", {"query": "内容", "group_by_vault": True, "group_offsets": {"V1": 2.5}})

    # 4. 未知/未注册库 -> ValueError
    with pytest.raises(ValueError, match="unknown or unresolvable vault in group_offsets"):
        server.call_tool("kb_search", {"query": "内容", "group_by_vault": True, "group_offsets": {"NonExistentVault": 0}})

    # 5. 全局检索中传入 solo 库 -> ValueError
    with pytest.raises(ValueError, match="solo and not authorized in global search"):
        server.call_tool("kb_search", {"query": "内容", "group_by_vault": True, "group_offsets": {"VSolo": 0}})

    # 6. 重复库名/路径映射 -> ValueError
    with pytest.raises(ValueError, match="duplicate vault in group_offsets"):
        server.call_tool("kb_search", {"query": "内容", "group_by_vault": True, "group_offsets": {"V1": 0, str(v1): 0}})

    # 7. 合法 group_offsets 结合 Scoped Search 访问 solo 库
    res = server.call_tool(
        "kb_search",
        {
            "query": "内容",
            "group_by_vault": True,
            "vault_paths": ["V1", "VSolo"],
            "group_offsets": {"V1": 0, "VSolo": 0},
        },
    )
    data = json.loads(res["content"][0]["text"])
    assert "groups" in data
    assert len(data["groups"]) >= 1


def test_budget_bytes_immutability():
    """断言 apply_budget 严格保证入参字典与 Chunk 结构深层不可变。"""
    from mortis_rag_mcp._server.fanout import apply_budget
    import copy

    raw_result = {
        "chunks": [
            {"id": "c1", "source": "a.md", "content": "不可变数据正文1" * 8, "score": 0.95},
            {"id": "c2", "source": "b.md", "content": "不可变数据正文2" * 8, "score": 0.85},
        ],
        "searched": ["/vault/a"],
        "errors": {},
    }
    frozen_copy = copy.deepcopy(raw_result)

    # 触发预算截断（每条 ~260 字节，450 字节预算仅容纳 1 条）
    res = apply_budget(raw_result, budget_bytes=450, orig_offset=0)
    assert res["returned"] == 1
    # 原始输入字典及其内部 chunk 未被修改
    assert raw_result == frozen_copy
    assert len(raw_result["chunks"]) == 2
    assert raw_result["chunks"][0]["content"] == "不可变数据正文1" * 8


def test_budget_bytes_cold_status(tmp_path, monkeypatch):
    """断言未初始化完成的单库冷启动状态统一经由 apply_budget 输出一致的 envelope 结构。"""
    monkeypatch.setattr(VaultMcpServer, "_startup_index_all", lambda self: None)
    monkeypatch.setattr(MarkdownIndexer, "start_watching", lambda self: None)
    monkeypatch.setattr(MarkdownIndexer, "request_refresh", lambda self, **kwargs: False)
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    v = tmp_path / "cold_vault"
    v.mkdir()
    (v / "doc.md").write_text("# 文档\n内容", encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.registry.add(str(v), name="ColdVault")

    res = server.call_tool("kb_search", {"vault_path": "ColdVault", "query": "文档", "budget_bytes": 5000})
    data = json.loads(res["content"][0]["text"])

    assert data["status"] == "indexing"
    assert data["chunks"] == []
    assert data["returned"] == 0
    assert data["truncated"] is False
    assert data["next_offset"] == 0
    assert _measure_payload_bytes(data) <= 5000
    server.shutdown()


def test_budget_bytes_cjk_emoji_escaping(tmp_path, monkeypatch):
    """断言包含复杂中文、四字节 Emoji、CRLF 与转义引号的内容在包装度量下能正确合法 loads 且不超预算。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    v = tmp_path / "cjk_emoji_vault"
    v.mkdir()
    # 包含双引号、反斜杠、CRLF、Emoji（🪐、🚀、🦄）与中文
    complex_text = '# 宇宙\r\n"深空探测"\\星系\\ [🪐 探索 🚀] \r\n 符号测试: "Quotes" and \\Backslash\\ 🦄' * 10
    (v / "space.md").write_text(complex_text, encoding="utf-8")

    server = VaultMcpServer(config_path)
    server.call_tool("kb_init", {"path": str(v), "name": "SpaceVault"})
    server._indexer_for({"vault_path": "SpaceVault"}).sync()

    res = server.call_tool(
        "kb_search",
        {"vault_path": "SpaceVault", "query": "深空探测", "budget_bytes": 2000},
    )
    data = json.loads(res["content"][0]["text"])

    assert "chunks" in data
    assert len(data["chunks"]) >= 1
    # 验证反序列化出的正文包含完整的 emoji 与转义字符
    content = data["chunks"][0]["content"]
    assert "🪐" in content and "🚀" in content and "🦄" in content
    assert '"Quotes"' in content
    assert "\\Backslash\\" in content
    assert _measure_payload_bytes(data) <= 2000


