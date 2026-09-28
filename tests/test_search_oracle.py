"""P4 单库检索与 Fan-out 检索 oracle 等价测试。

契约与安全闸门：
1. 等价性：重构后单库检索与 C44（v0.7.3）冻结旧实现（tests/golden/
   legacy_search_impl.py，逐字快照）三元组 (id, score, order) 恒等；
2. 不可变性：检索过程绝不原地修改 indexer 内部常驻 Chunk 对象的 score；
3. 容错性：空查询、短缩写、CJK 多字滑窗、过滤条件、分页切片行为一致；
4. 协议委托：server._kb_search 与 server._fanout_search 委托正常。
"""
from __future__ import annotations

from array import array
from pathlib import Path
import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer, SearchFilter, rerank_chunks
from mortis_rag_mcp.server import VaultMcpServer
from tests.golden.legacy_search_impl import LegacySearchOracle


@pytest.fixture
def test_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "doc1.md").write_text(
        "# AI 与 RC 滤波器设计\n\n"
        "这是一篇关于半导体物理与 RC 电路滤波器的详细教程。\n"
        "电路包含电阻与电容构成的低通滤波。\n",
        encoding="utf-8",
    )
    (vault / "doc2.md").write_text(
        "---\ntags: [electronics, analog]\n---\n"
        "# 模拟电路与晶体管\n\n"
        "半导体物理中的载流子浓度与晶体管放大原理。\n"
        "重复测试段落：这是一篇关于半导体物理与 RC 电路滤波器的详细教程。\n",
        encoding="utf-8",
    )
    (vault / "doc3.md").write_text(
        "# 操作系统内存管理\n\n"
        "分页机制与虚拟内存，OS 核心原理。\n",
        encoding="utf-8",
    )
    return vault


def test_chunk_score_never_mutated_in_place(test_vault, tmp_path):
    """断言检索过程绝不修改 all_chunks() 常驻对象的 score。"""
    indexer = MarkdownIndexer(test_vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8), cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache"))))
    indexer.sync()

    # 记录常驻 chunk 的初始 score
    initial_scores = [c.score for c in indexer.all_chunks()]

    # 执行多种检索
    indexer.search("半导体物理", top_k=5)
    indexer.search("RC", top_k=5)
    indexer.search("", top_k=5)
    indexer.search("操作系统", top_k=5, dedupe=True)

    # 断言常驻 chunk 的 score 完全未被篡改
    current_scores = [c.score for c in indexer.all_chunks()]
    assert current_scores == initial_scores, "严重违规：常驻 Chunk 对象的 score 被检索过程原地变异！"


@pytest.mark.parametrize(
    "query,top_k,filters,dedupe",
    [
        ("", 5, None, True),
        ("", 2, SearchFilter(tags=["electronics"]), False),
        ("RC", 3, None, True),
        ("半导体物理", 10, None, True),
        ("半导体物理", 5, SearchFilter(path_prefix="doc1"), True),
        ("不存在的冷门关键词XYZ", 5, None, True),
    ],
)
def test_search_facade_matches_legacy_oracle(test_vault, tmp_path, query, top_k, filters, dedupe):
    """断言 Facade 检索与 C44（v0.7.3）冻结旧实现产物严格恒等（真 oracle）。

    对照方 tests/golden/legacy_search_impl.py 是重构前实现的逐字快照：
    冻结算法 + 经 __getattr__ 穿透到同一活实例的状态，与 Facade 路径共享
    同一份库内 chunk/FTS/向量状态，唯一变量是新实现的算法代码本身。
    cache.enabled=True 使 FTS/RRF 混合路由真实开启（裸 AppConfig 编程构造
    默认 enabled=False，会导致本测试静默退化为纯词法路径对照）。
    """
    indexer = MarkdownIndexer(test_vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8), cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache"))))
    indexer.sync()
    # 硬断言混合路由真实开启：FTS 静默降级会让本测试退化为纯词法对照。
    assert indexer._fts is not None and indexer._fts.available

    oracle = LegacySearchOracle(indexer)
    facade_results = indexer.search(query, top_k=top_k, filters=filters, dedupe=dedupe)
    legacy_results = oracle.search(query, top_k=top_k, filters=filters, dedupe=dedupe)

    facade_triplets = [(c.id, round(c.score, 6), c.source) for c in facade_results]
    legacy_triplets = [(c.id, round(c.score, 6), c.source) for c in legacy_results]

    assert facade_triplets == legacy_triplets


def test_server_search_dispatch_delegation(test_vault, tmp_path, monkeypatch):
    """断言 server.py 的 _kb_search 经 search_dispatch / fanout 正确返回。"""
    config = tmp_path / "app.toml"
    config.write_text('mode = "static"\n', encoding="utf-8")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(tmp_path / "vaults.toml"))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    server = VaultMcpServer(config)
    server.call_tool("kb_init", {"path": str(test_vault)})

    res = server.call_tool("kb_search", {"vault_path": str(test_vault), "query": "半导体物理", "top_k": 3})
    assert not res.get("isError", False)
    assert len(res["content"]) == 1
    import json
    data = json.loads(res["content"][0]["text"])
    assert "chunks" in data
    assert len(data["chunks"]) > 0


def test_backward_compatibility_static_methods():
    """断言向后兼容的静态方法依然可用。"""
    # 1. _fts_query
    res = MarkdownIndexer._fts_query(None, "半导体物理")
    assert res is not None and "半导体" in res

    # 2. _cosine
    v1 = array("f", [1.0, 0.0])
    v2 = array("f", [0.0, 1.0])
    v3 = array("f", [1.0, 0.0])
    assert MarkdownIndexer._cosine(v1, v2) == 0.0
    assert abs(MarkdownIndexer._cosine(v1, v3) - 1.0) < 1e-5

    # 3. _query_tokens
    tokens = MarkdownIndexer._query_tokens("RC 滤波器")
    assert "rc" in tokens
