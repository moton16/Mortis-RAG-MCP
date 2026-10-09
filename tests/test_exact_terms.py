"""F4 exact_terms 显式硬包含测试集。

契约与规格验证：
1. 召回保障：实体词在普通搜索中被挤出 Top-K 时，加 exact_terms 后通过 route D (RRF) 提升并召回；
2. AND 语义：多 term 必须全部命中，缺一不可；
3. 大小写不敏感：lower() 子串匹配；
4. 短词与 CJK：<3 字符 ASCII 与 CJK 短词（绕过 FTS trigram 盲区）；
5. 非 hybrid 降级：use_hybrid=False / FTS 缺失时保底分生效；
6. 组合过滤：与 path_prefix / tags 组合时 AND 生效；
7. 跨库 Fan-out：exact_terms 逐库生效，跨库去重与全局排序正常；
8. Reranker 屏障：硬过滤在 reranker 之前执行，非匹配项绝不被 reranker 带回；
9. 防御解析：逗号串容错、去空去重、上限 8 条、单条 ≤100 字符截断。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer, SearchFilter
from mortis_rag_mcp.server import VaultMcpServer
from mortis_rag_mcp._server.search_dispatch import _parse_exact_terms


def _kb_init_ready(server: VaultMcpServer, path, name: str) -> None:
    """kb_init 之后显式同步一次，让索引状态确定。

    C66 起 kb_search 走「优先使用已就绪索引、后台刷新」，不再在前台阻塞等待首建；
    kb_init 只把首建丢进后台线程，紧跟其后的检索会与后台首建竞态（Windows 侥幸通过、
    Linux CI 只拿到部分结果）。要断言完整索引的测试必须显式同步。
    """
    server.call_tool("kb_init", {"path": str(path), "name": name})
    indexer = server._indexer_for({"vault_path": name})
    indexer.sync()
    # 后台首建线程与前台 sync 并存时，单次 sync 也可能读到空索引（本条在 py3.13 的
    # CI 上实测拿到 0 chunks）：等到切片真的可见再返回，别把「碰巧为空」当合同。
    deadline = time.monotonic() + 10.0
    while not indexer.all_chunks() and time.monotonic() < deadline:
        time.sleep(0.1)
        indexer.sync()
    return indexer


def test_parse_exact_terms_defensive():
    """验证 exact_terms 防御解析与规范化。"""
    # 1. None 与空值
    assert _parse_exact_terms(None) is None
    assert _parse_exact_terms("") is None
    assert _parse_exact_terms([]) is None
    assert _parse_exact_terms("   ") is None
    assert _parse_exact_terms(True) is None

    # 2. 逗号分隔字符串
    assert _parse_exact_terms("alpha, beta, gamma") == ["alpha", "beta", "gamma"]

    # 3. 去重（大小写不敏感去重）
    assert _parse_exact_terms(["KeyWord", "keyword", "KEYWORD", "Other"]) == ["KeyWord", "Other"]

    # 4. 单项超长截断 ≤100 字符
    long_term = "x" * 150
    parsed = _parse_exact_terms([long_term])
    assert parsed is not None
    assert len(parsed[0]) == 100
    assert parsed[0] == "x" * 100

    # 5. 上限 8 条
    many_terms = [f"term_{i}" for i in range(15)]
    parsed_many = _parse_exact_terms(many_terms)
    assert parsed_many is not None
    assert len(parsed_many) == 8
    assert parsed_many == [f"term_{i}" for i in range(8)]

    # 6. 列表内包含 bool 与 None 防御（bool 是 int 子类）
    assert _parse_exact_terms([True, False, None, "ValidTerm"]) == ["ValidTerm"]
    assert _parse_exact_terms([True, False, None]) is None


@pytest.fixture
def exact_terms_vault(tmp_path):
    vault = tmp_path / "exact_vault"
    vault.mkdir(parents=True)

    # 构造 10 个高频干扰文件，都包含很多次 "量子芯片"，但不含专用代号
    for i in range(10):
        content = f"# 量子芯片设计标准文档 {i}\n\n" + ("量子芯片 极高频关键词 重复出现 量子计算 " * 15)
        (vault / f"doc_{i:02d}.md").write_text(content, encoding="utf-8")

    # 构造 1 个低频但含专有名词的目标文件：仅提了一次量子芯片，但含有专有代号 "PROJECT_NEBULA_X99"
    target_content = (
        "# 绝密项目档案\n\n"
        "这是一份历史遗留记录，仅仅附带提到了量子芯片的一项测试。\n"
        "本项目核心代号为 PROJECT_NEBULA_X99，采用超导二极架构与定制 IC。\n"
    )
    (vault / "classified.md").write_text(target_content, encoding="utf-8")

    # 构造 1 个仅含 PROJECT_NEBULA_X99 但不含"超导二极"的文件（用于 AND 测试）
    partial_content = (
        "# 外部协作备忘录\n\n"
        "关于 PROJECT_NEBULA_X99 的外部配套说明，本备忘录不涉及核心超导技术。\n"
    )
    (vault / "partial.md").write_text(partial_content, encoding="utf-8")

    return vault


def test_exact_terms_boosts_rare_entity_into_top_k(exact_terms_vault, tmp_path):
    """断言：默认 top_k=5 无法召回低频实体文档；加入 exact_terms 后保证召回到 Top-5。"""
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    # 1. 默认搜索 "量子芯片"，top_k=5：高频文档霸占 Top-5，classified.md 不在结果中
    baseline_results = indexer.search("量子芯片", top_k=5)
    baseline_sources = [c.source for c in baseline_results]
    assert "classified.md" not in baseline_sources

    # 2. 加上 exact_terms=["PROJECT_NEBULA_X99"]：classified.md 必须被召回进入结果
    exact_results = indexer.search("量子芯片", top_k=5, exact_terms=["PROJECT_NEBULA_X99"])
    exact_sources = [c.source for c in exact_results]
    assert "classified.md" in exact_sources

    # 且所有返回结果必须严格包含该 exact term
    for chunk in exact_results:
        assert "project_nebula_x99" in chunk.content.lower()


def test_exact_terms_and_semantics(exact_terms_vault, tmp_path):
    """断言 AND 语义：多个 exact_terms 必须全部满足，缺少任一者即被剔除。"""
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    # classified.md 同时包含 "PROJECT_NEBULA_X99" 与 "超导二极"
    # partial.md 仅包含 "PROJECT_NEBULA_X99"
    # 当 exact_terms 同时要求两者时，partial.md 绝不能返回
    results = indexer.search(
        "项目",
        top_k=10,
        exact_terms=["PROJECT_NEBULA_X99", "超导二极"],
    )
    sources = [c.source for c in results]
    assert "classified.md" in sources
    assert "partial.md" not in sources

    for c in results:
        low = c.content.lower()
        assert "project_nebula_x99" in low
        assert "超导二极" in low


def test_exact_terms_case_insensitive(exact_terms_vault, tmp_path):
    """断言大小写不敏感匹配。"""
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    # 原文是全大写 PROJECT_NEBULA_X99，查询用全小写或混合大小写
    res_lower = indexer.search("档案", top_k=5, exact_terms=["project_nebula_x99"])
    assert any(c.source == "classified.md" for c in res_lower)

    res_mixed = indexer.search("档案", top_k=5, exact_terms=["Project_Nebula_X99"])
    assert any(c.source == "classified.md" for c in res_mixed)


def test_exact_terms_short_and_cjk(exact_terms_vault, tmp_path):
    """断言 <3 字符短词（ASCII与中文）的召回保障。"""
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    # classified.md 包含 "IC" 与 "二极"
    res_short_ascii = indexer.search("设计", top_k=5, exact_terms=["IC"])
    assert any(c.source == "classified.md" for c in res_short_ascii)

    res_short_cjk = indexer.search("设计", top_k=5, exact_terms=["二极"])
    assert any(c.source == "classified.md" for c in res_short_cjk)


def test_exact_terms_non_hybrid_fallback(exact_terms_vault, tmp_path):
    """断言非 hybrid 降级路径下（use_hybrid=False），保底分机制保证召回。"""
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
        use_hybrid=False,
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    # 在非 hybrid 模式下，查询 "无关词汇XYZ" 与 exact_terms=["PROJECT_NEBULA_X99"]
    # 正常词法无命中，保底分机制仍需成功召回 classified.md 与 partial.md
    res = indexer.search("无关词汇XYZ", top_k=5, exact_terms=["PROJECT_NEBULA_X99"])
    sources = [c.source for c in res]
    assert "classified.md" in sources
    assert "partial.md" in sources
    for c in res:
        assert c.score > 0.0


def test_exact_terms_with_filters(exact_terms_vault, tmp_path):
    """断言 exact_terms 与 SearchFilter (path_prefix / tags) 组合过滤。"""
    # 追加一个在子目录下的文件
    sub_dir = exact_terms_vault / "sub"
    sub_dir.mkdir(parents=True)
    (sub_dir / "nested.md").write_text(
        "---\ntags: [spec, v1]\n---\n# 子目录文档\n含实体 PROJECT_NEBULA_X99。",
        encoding="utf-8",
    )

    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    # 1. 过滤 path_prefix="sub/"
    flt_prefix = SearchFilter(path_prefix="sub/")
    res_prefix = indexer.search("实体", top_k=5, filters=flt_prefix, exact_terms=["PROJECT_NEBULA_X99"])
    assert len(res_prefix) == 1
    assert res_prefix[0].source == "sub/nested.md"

    # 2. 过滤 tags=["spec"]
    flt_tag = SearchFilter(tags=["spec"])
    res_tag = indexer.search("实体", top_k=5, filters=flt_tag, exact_terms=["PROJECT_NEBULA_X99"])
    assert len(res_tag) == 1
    assert res_tag[0].source == "sub/nested.md"


def test_exact_terms_empty_query(exact_terms_vault, tmp_path):
    """断言空查询下 exact_terms 依然生效，仅返回含实体的内容。"""
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    res = indexer.search("", top_k=10, exact_terms=["PROJECT_NEBULA_X99"])
    assert len(res) > 0
    for c in res:
        assert "project_nebula_x99" in c.content.lower()


def test_exact_terms_mcp_server_single_and_fanout(tmp_path, monkeypatch):
    """断言通过 MCP call_tool('kb_search') 在单库与跨库 fanout 下 exact_terms 生效。"""
    vault1 = tmp_path / "vault1"
    vault1.mkdir()
    (vault1 / "doc1.md").write_text("# 库1正文\n测试专用代码 KAPPA_999 部署在库1。", encoding="utf-8")

    vault2 = tmp_path / "vault2"
    vault2.mkdir()
    (vault2 / "doc2.md").write_text("# 库2正文\n测试专用代码 KAPPA_999 同样部署在库2。", encoding="utf-8")
    (vault2 / "doc3.md").write_text("# 库2普通文档\n普通说明内容。", encoding="utf-8")

    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    _kb_init_ready(server, vault1, "V1")
    _kb_init_ready(server, vault2, "V2")

    # 1. 单库检索指定 vault_path
    res1 = server.call_tool(
        "kb_search",
        {"vault_path": "V1", "query": "测试", "exact_terms": ["KAPPA_999"]},
    )
    data1 = json.loads(res1["content"][0]["text"])
    assert len(data1["chunks"]) == 1
    assert data1["chunks"][0]["source"] == "doc1.md"

    # 2. 跨库 fanout 检索（不传 vault_path）
    res_fanout = server.call_tool(
        "kb_search",
        {"query": "测试", "exact_terms": ["KAPPA_999"]},
    )
    data_fanout = json.loads(res_fanout["content"][0]["text"])
    chunks_fanout = data_fanout["chunks"]
    assert len(chunks_fanout) == 2
    sources = {c["source"] for c in chunks_fanout}
    assert sources == {"doc1.md", "doc2.md"}
    for c in chunks_fanout:
        assert "kappa_999" in c["content"].lower()

    # 3. 跨库分组模式 group_by_vault
    res_grouped = server.call_tool(
        "kb_search",
        {"query": "测试", "exact_terms": ["KAPPA_999"], "group_by_vault": True},
    )
    data_grouped = json.loads(res_grouped["content"][0]["text"])
    assert "groups" in data_grouped
    assert len(data_grouped["groups"]) == 2
    for g in data_grouped["groups"]:
        for c in g["chunks"]:
            assert "kappa_999" in c["content"].lower()


def test_exact_terms_regex_and_special_symbols(tmp_path):
    """断言 exact_terms 传入特殊正则符号/特殊符号时做纯字面量子串匹配，无正则注入。"""
    vault = tmp_path / "special_symbols_vault"
    vault.mkdir()
    (vault / "code.md").write_text("# 代码笔记\n我们使用 C++ 与 re.match('.*') 以及 [a-z]+ 模式。\n", encoding="utf-8")
    (vault / "other.md").write_text("# 普通笔记\n这里没有任何代码符号。\n", encoding="utf-8")

    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(vault, cfg)
    indexer.sync()

    # 1. 纯字面量匹配包含特殊字符的字符串
    res1 = indexer.search("笔记", exact_terms=["C++"])
    assert len(res1) == 1
    assert res1[0].source == "code.md"

    res2 = indexer.search("笔记", exact_terms=[".*"])
    assert len(res2) == 1
    assert res2[0].source == "code.md"

    res3 = indexer.search("笔记", exact_terms=["[a-z]+"])
    assert len(res3) == 1
    assert res3[0].source == "code.md"


def test_exact_terms_chunk_index_none_tie_breaking(tmp_path):
    """断言当 chunk.metadata['chunk_index'] 为 None 时，must_candidates 排序不崩溃。"""
    vault = tmp_path / "chunk_index_vault"
    vault.mkdir()
    (vault / "note.md").write_text("# 笔记标题\n含目标代号 TARGET_ALPHA。\n", encoding="utf-8")

    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(vault, cfg)
    indexer.sync()

    # 模拟外部构造或旧缓存产生的 chunk_index 为 None 的场景
    for c in indexer._chunks.get("note.md", []):
        c.metadata["chunk_index"] = None

    # 不应抛出 TypeError: '<' not supported between instances of 'NoneType' and 'int'
    res = indexer.search("笔记", exact_terms=["TARGET_ALPHA"])
    assert len(res) == 1
    assert res[0].source == "note.md"


def test_exact_terms_no_match_returns_empty(exact_terms_vault, tmp_path):
    """断言 exact_terms 命中数为 0 时，结果严格返回空列表，即使普通查询有高分命中。"""
    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / "cache")),
    )
    indexer = MarkdownIndexer(exact_terms_vault, cfg)
    indexer.sync()

    # "量子芯片" 原本有 10+ 篇高频命中，但加入不存在的 exact_terms 后必须为空
    res = indexer.search("量子芯片", top_k=10, exact_terms=["TOTALLY_NON_EXISTENT_TERM_404"])
    assert len(res) == 0


def test_exact_terms_combined_with_budget_bytes(exact_terms_vault, tmp_path, monkeypatch):
    """断言 exact_terms 与 budget_bytes 协同工作：既保证硬包含，又受预算截断。"""
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n', encoding="utf-8")
    reg_path = tmp_path / "vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_path))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_path))

    server = VaultMcpServer(config_path)
    _kb_init_ready(server, exact_terms_vault, "ExactVault")

    res = server.call_tool(
        "kb_search",
        {
            "vault_path": "ExactVault",
            "query": "芯片",
            "exact_terms": ["PROJECT_NEBULA_X99"],
            "budget_bytes": 1500,
        },
    )
    data = json.loads(res["content"][0]["text"])
    assert "chunks" in data
    assert len(data["chunks"]) >= 1
    assert "project_nebula_x99" in data["chunks"][0]["content"].lower()
    assert "truncated" in data

