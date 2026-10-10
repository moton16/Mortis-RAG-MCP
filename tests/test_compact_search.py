import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mortis_rag_mcp._indexer.models import Chunk
from mortis_rag_mcp._server.fanout import _measure_payload_bytes
from mortis_rag_mcp.server import VaultMcpServer, _tool_definitions


def _kb_init_ready(server: VaultMcpServer, path, name: str) -> None:
    """kb_init 之后显式同步一次，让索引状态确定。

    C66 起 kb_search 走「优先使用已就绪索引、后台刷新」，不再在前台阻塞等待首建；
    kb_init 只把首建丢进后台线程，紧跟其后的检索会与后台首建竞态（Windows 侥幸通过、
    Linux CI 只拿到部分结果）。要断言完整索引的测试必须显式同步。
    """
    server.call_tool("kb_init", {"path": str(path), "name": name})
    indexer = server._indexer_for({"vault_path": name})
    indexer.sync()
    # 后台首建线程与前台 sync 并存时，单次 sync 也可能读到空索引（同族用例在 Linux
    # CI 上实测拿到 0 chunks）：等到切片真的可见再返回。
    deadline = time.monotonic() + 10.0
    while not indexer.all_chunks() and time.monotonic() < deadline:
        time.sleep(0.1)
        indexer.sync()
    return indexer


def test_chunk_to_dict_compact():
    chunk = Chunk(
        id="chunk_id_123",
        content="This is the content of the chunk.\nSecond line with important info.",
        source="notes/topic.md",
        title="My Title",
        metadata={
            "heading": "Section 1",
            "start_line": 10,
            "end_line": 20,
            "tags": ["test"],
            "mtime": 123456789.0,
            "source_pdf": "original.pdf",
        },
        score=0.95,
    )

    # 1. Compact projection
    d_compact = chunk.to_dict(compact=True)
    assert set(d_compact.keys()) == {"source", "heading", "lines", "snippet"}
    assert d_compact["source"] == "notes/topic.md"
    assert d_compact["heading"] == "Section 1"
    assert d_compact["lines"] == [10, 20]
    assert "content of the chunk" in d_compact["snippet"]

    # Verify absence of other fields
    forbidden_keys = {"id", "score", "title", "metadata", "char_count", "source_pdf", "content"}
    for k in forbidden_keys:
        assert k not in d_compact

    # Verify chunk instance state was not modified
    assert chunk.id == "chunk_id_123"
    assert chunk.score == 0.95
    assert chunk.title == "My Title"

    # 2. Positional backwards compatibility
    d_full = chunk.to_dict(False)
    assert "content" in d_full
    assert "id" in d_full
    assert "score" in d_full
    assert "lines" not in d_full

    d_preview = chunk.to_dict(True)
    assert "snippet" in d_preview
    assert "char_count" in d_preview
    assert "id" in d_preview
    assert "lines" not in d_preview


def test_compact_schema_and_dispatch_tolerance(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    tools = _tool_definitions()
    search_tool = next(t for t in tools if t["name"] == "kb_search")
    props = search_tool["inputSchema"]["properties"]

    assert "compact" in props
    assert props["compact"]["type"] == "boolean"
    assert props["compact"]["default"] is False
    assert "极简预览" in props["compact"]["description"]
    assert "按行号+库回读" in props["compact"]["description"]
    # Ensure enum for mode was NOT modified to include compact
    assert props["mode"]["enum"] == ["full", "preview"]

    vault = tmp_path / "test_vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Test Heading\nHere is some searchable text.", encoding="utf-8")

    server = VaultMcpServer()
    _kb_init_ready(server, vault, "test_vault")

    # Test boolean string tolerance for compact (true/1/yes/on)
    for truthy_val in (True, "true", "1", "yes", "on", "TRUE", "On"):
        res = server.call_tool("kb_search", {
            "query": "searchable",
            "vault_path": str(vault),
            "compact": truthy_val,
            "mode": "full",  # compact should override mode="full"
        })
        data = json.loads(res["content"][0]["text"])
        chunks = data["chunks"]
        assert len(chunks) > 0
        c0 = chunks[0]
        assert set(c0.keys()) == {"source", "heading", "lines", "snippet"}
        assert "content" not in c0
        assert "id" not in c0

    # Test compact=False preserves mode="full"
    res_full = server.call_tool("kb_search", {
        "query": "searchable",
        "vault_path": str(vault),
        "compact": False,
        "mode": "full",
    })
    data_full = json.loads(res_full["content"][0]["text"])
    assert "content" in data_full["chunks"][0]


def test_single_vault_compact_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "single_vault"
    vault.mkdir()
    (vault / "chapter1.md").write_text("# Chapter One\nOnce upon a time in ancient empire.", encoding="utf-8")

    server = VaultMcpServer()
    _kb_init_ready(server, vault, "NovelVault")

    # 1. Compact search: top level must have vault and vault_name
    res_compact = server.call_tool("kb_search", {
        "query": "empire",
        "vault_path": "NovelVault",
        "compact": True,
    })
    data_compact = json.loads(res_compact["content"][0]["text"])

    assert data_compact["vault"] == str(vault.resolve())
    assert data_compact["vault_name"] == "NovelVault"
    assert len(data_compact["chunks"]) == 1
    c0 = data_compact["chunks"][0]
    assert set(c0.keys()) == {"source", "heading", "lines", "snippet"}
    assert "vault" not in c0
    assert "vault_name" not in c0

    # 2. Normal preview/full: top level must NOT have vault / vault_name
    res_normal = server.call_tool("kb_search", {
        "query": "empire",
        "vault_path": "NovelVault",
        "compact": False,
        "preview": True,
    })
    data_normal = json.loads(res_normal["content"][0]["text"])
    assert "vault" not in data_normal
    assert "vault_name" not in data_normal


def test_multivault_flat_compact_search(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    v1 = tmp_path / "vault1"
    v1.mkdir()
    (v1 / "doc1.md").write_text("# Doc 1\nQuantum computing simulation results.", encoding="utf-8")

    v2 = tmp_path / "vault2"
    v2.mkdir()
    (v2 / "doc2.md").write_text("# Doc 2\nQuantum entanglement teleportation protocol.", encoding="utf-8")

    server = VaultMcpServer()
    _kb_init_ready(server, v1, "V1")
    _kb_init_ready(server, v2, "V2")

    # Flat cross-vault (group_by_vault=False) with compact=True
    res = server.call_tool("kb_search", {
        "query": "quantum",
        "compact": True,
        "group_by_vault": False,
    })
    data = json.loads(res["content"][0]["text"])

    chunks = data.get("chunks", [])
    assert len(chunks) == 2

    for c in chunks:
        # Every chunk has vault (path), but NOT vault_name
        assert set(c.keys()) == {"source", "heading", "lines", "snippet", "vault"}
        assert "vault_name" not in c
        assert "id" not in c
        assert "score" not in c
        assert c["vault"] in (str(v1.resolve()), str(v2.resolve()))


def test_multivault_grouped_compact_search(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    v1 = tmp_path / "vault_alpha"
    v1.mkdir()
    (v1 / "alpha.md").write_text("# Alpha Analysis\nHigh frequency trading algorithms in market.", encoding="utf-8")

    v2 = tmp_path / "vault_beta"
    v2.mkdir()
    (v2 / "beta.md").write_text("# Beta Strategy\nMarket microstructure and trading latency.", encoding="utf-8")

    server = VaultMcpServer()
    _kb_init_ready(server, v1, "Alpha")
    _kb_init_ready(server, v2, "Beta")

    # Grouped cross-vault (group_by_vault=True) with compact=True
    res = server.call_tool("kb_search", {
        "query": "trading market",
        "compact": True,
        "group_by_vault": True,
    })
    data = json.loads(res["content"][0]["text"])

    groups = data.get("groups", [])
    assert len(groups) == 2

    for grp in groups:
        assert "vault" in grp
        assert "vault_name" in grp
        assert grp["vault_name"] in ("Alpha", "Beta")
        assert len(grp["chunks"]) == 1
        c = grp["chunks"][0]
        # Chunks inside group do NOT repeat vault or vault_name
        assert set(c.keys()) == {"source", "heading", "lines", "snippet"}
        assert "vault" not in c
        assert "vault_name" not in c
        assert "id" not in c
        assert "score" not in c


def test_compact_parity_and_readback_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    vault = tmp_path / "reading_vault"
    vault.mkdir()
    content = (
        "# Introduction\n"
        "Paragraph 1 discussing distributed systems basics.\n\n"
        "## Consensus Protocols\n"
        "Raft and Paxos are widely used distributed consensus algorithms.\n"
        "They ensure state machine replication across unreliable networks.\n\n"
        "## Performance Metrics\n"
        "Throughput and latency trade-offs in quorum systems.\n"
    )
    (vault / "dist_sys.md").write_text(content, encoding="utf-8")

    server = VaultMcpServer()
    _kb_init_ready(server, vault, "SysVault")

    # 1. Compare parity of full vs compact
    res_full = server.call_tool("kb_search", {
        "query": "consensus algorithms raft paxos",
        "vault_path": "SysVault",
        "compact": False,
        "mode": "full",
    })
    data_full = json.loads(res_full["content"][0]["text"])

    res_compact = server.call_tool("kb_search", {
        "query": "consensus algorithms raft paxos",
        "vault_path": "SysVault",
        "compact": True,
    })
    data_compact = json.loads(res_compact["content"][0]["text"])

    chunks_full = data_full["chunks"]
    chunks_compact = data_compact["chunks"]
    assert len(chunks_compact) == len(chunks_full)

    for cf, cc in zip(chunks_full, chunks_compact):
        assert cc["source"] == cf["source"]
        assert cc["heading"] == cf["heading"]
        assert cc["lines"] == [cf["start_line"], cf["end_line"]]
        # Snippet keywords should match
        assert "consensus" in cc["snippet"].lower()

    # 2. Readback roundtrip using vault + source + lines
    top_cc = chunks_compact[0]
    read_res = server.call_tool("kb_read", {
        "vault_path": data_compact["vault"],
        "source": top_cc["source"],
        "start_line": top_cc["lines"][0],
        "end_line": top_cc["lines"][1],
    })
    read_data = json.loads(read_res["content"][0]["text"])
    assert "content" in read_data
    assert "consensus" in read_data["content"].lower()
    assert "Raft" in read_data["content"] or "Paxos" in read_data["content"]


def test_compact_payload_reduction_measurement(tmp_path, monkeypatch):
    """Fixture with 30 Chinese notes measuring that compact significantly reduces bytes."""
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    v1 = tmp_path / "corpus_a"
    v1.mkdir()
    v2 = tmp_path / "corpus_b"
    v2.mkdir()

    for i in range(15):
        (v1 / f"章节_{i:02d}.md").write_text(
            f"# 第{i}章 帝星降世与星际航路\n"
            f"在浩瀚的安华帝国疆域内，星图标记了第{i}个星区的关键跳跃点。\n"
            "帝国海军第三舰队在此驻扎，负责监控超空间波动的异常数据信号。\n"
            "量子通信阵列持续接收到远古遗迹传来的加密广播，学者们尝试破译。\n"
            f"关于星际跃迁引擎的能耗报告显示，第{i}反应堆已达到临界阈值。\n",
            encoding="utf-8",
        )
        (v2 / f"档案_{i:02d}.md").write_text(
            f"# 历史档案编号{i}：帝国枢密院议事录\n"
            f"天元历三百年，枢密院针对第{i}星区的开发议案进行了长达三天的辩论。\n"
            "财政大臣指出深空探索预算必须严格控制，防止帝国金库储备被过度消耗。\n"
            "科技部提议建造新型曲率信标以加强星系间的实时联动与信息同步。\n"
            f"最终决议草案由内阁首辅副署，下发至第{i}星区总督府执行。\n",
            encoding="utf-8",
        )

    server = VaultMcpServer()
    _kb_init_ready(server, v1, "CorpusA")
    _kb_init_ready(server, v2, "CorpusB")

    # 1. Grouped multi-vault shape (group_by_vault=True)
    res_preview_grp = server.call_tool("kb_search", {
        "query": "帝国 星区 枢密院 航路",
        "top_k": 20,
        "preview": True,
        "compact": False,
        "group_by_vault": True,
    })
    data_preview_grp = json.loads(res_preview_grp["content"][0]["text"])
    bytes_preview_grp = _measure_payload_bytes(data_preview_grp)

    res_compact_grp = server.call_tool("kb_search", {
        "query": "帝国 星区 枢密院 航路",
        "top_k": 20,
        "compact": True,
        "group_by_vault": True,
    })
    data_compact_grp = json.loads(res_compact_grp["content"][0]["text"])
    bytes_compact_grp = _measure_payload_bytes(data_compact_grp)

    assert bytes_compact_grp < bytes_preview_grp
    reduction_grp_pct = (bytes_preview_grp - bytes_compact_grp) / bytes_preview_grp * 100.0
    assert reduction_grp_pct >= 30.0, f"Expected >= 30% reduction in grouped shape, got {reduction_grp_pct:.2f}%"

    # 2. Flat multi-vault shape (group_by_vault=False)
    res_preview_flat = server.call_tool("kb_search", {
        "query": "帝国 星区 枢密院 航路",
        "top_k": 20,
        "preview": True,
        "compact": False,
        "group_by_vault": False,
    })
    data_preview_flat = json.loads(res_preview_flat["content"][0]["text"])
    bytes_preview_flat = _measure_payload_bytes(data_preview_flat)

    res_compact_flat = server.call_tool("kb_search", {
        "query": "帝国 星区 枢密院 航路",
        "top_k": 20,
        "compact": True,
        "group_by_vault": False,
    })
    data_compact_flat = json.loads(res_compact_flat["content"][0]["text"])
    bytes_compact_flat = _measure_payload_bytes(data_compact_flat)

    assert bytes_compact_flat < bytes_preview_flat
    reduction_flat_pct = (bytes_preview_flat - bytes_compact_flat) / bytes_preview_flat * 100.0
    assert reduction_flat_pct >= 25.0, f"Expected >= 25% reduction in flat shape, got {reduction_flat_pct:.2f}%"
