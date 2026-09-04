"""kb_search 的 path/tag/mtime 过滤与分页（indexer 层 + server 层）。

过滤的正确性不依赖 FTS 下推（下推只是减少候选量的优化），所以每一条过滤
断言都会在 use_hybrid 开/关两组配置下各跑一遍。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer, SearchFilter
from mortis_rag_mcp.providers import StaticEmbeddingProvider
from mortis_rag_mcp.server import VaultMcpServer

# 两个可辨识的 mtime（整数秒，NTFS 上能精确存回）。
OLD_MTIME = 1_600_000_000.0  # 2020-09-13
NEW_MTIME = 1_700_000_000.0  # 2023-11-14

# 每篇笔记都含这个 token，保证一次查询能命中全部 chunk。
SHARED = "SHARED777"

SEMICONDUCTOR = "半导体"
MARXISM = "马原"

OLD_SEMI = f"{SEMICONDUCTOR}/doping.md"
NEW_SEMI = f"{SEMICONDUCTOR}/carrier.md"
OLD_MARX = f"{MARXISM}/surplus.md"
NEW_MARX = f"{MARXISM}/dialectics.md"


def _config(tmp_path: Path, use_hybrid: bool = True) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
        use_hybrid=use_hybrid,
    )


def _write_note(path: Path, title: str, body: str, tags: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntags: {tags}\n---\n# {title}\n{body}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _build_indexer(tmp_path: Path, use_hybrid: bool = True) -> MarkdownIndexer:
    vault = tmp_path / "vault"
    _write_note(vault / OLD_SEMI, "掺杂", f"{SHARED} 掺杂浓度 载流子", "[半导体, 物理]", OLD_MTIME)
    _write_note(vault / NEW_SEMI, "迁移率", f"{SHARED} 载流子迁移率", "[半导体]", NEW_MTIME)
    _write_note(vault / OLD_MARX, "剩余价值", f"{SHARED} 剩余价值理论", "[马原, 政治]", OLD_MTIME)
    _write_note(vault / NEW_MARX, "辩证法", f"{SHARED} 唯物辩证法", "[马原]", NEW_MTIME)
    indexer = MarkdownIndexer(
        vault,
        _config(tmp_path, use_hybrid),
        embedding_provider=StaticEmbeddingProvider(dimension=8),
    )
    indexer.sync()
    return indexer


def _sources(chunks) -> list[str]:
    return [chunk.source for chunk in chunks]


def test_chunk_metadata_carries_mtime(tmp_path):
    indexer = _build_indexer(tmp_path)
    by_source = {chunk.source: chunk for chunk in indexer.all_chunks()}
    assert len(by_source) == 4
    assert by_source[OLD_SEMI].metadata["mtime"] == OLD_MTIME
    assert by_source[NEW_SEMI].metadata["mtime"] == NEW_MTIME


def test_path_prefix_filter(tmp_path):
    for use_hybrid in (True, False):
        indexer = _build_indexer(tmp_path / f"h{use_hybrid}", use_hybrid)
        results = indexer.search(SHARED, top_k=10, use_rerank=False, filters=SearchFilter(path_prefix=SEMICONDUCTOR + "/"))
        assert sorted(_sources(results)) == sorted([OLD_SEMI, NEW_SEMI])

        nested = indexer.search(SHARED, top_k=10, use_rerank=False, filters=SearchFilter(path_prefix=MARXISM + "/"))
        assert sorted(_sources(nested)) == sorted([OLD_MARX, NEW_MARX])


def test_fts_pushdown_matches_postfilter(tmp_path):
    """FTS 下推只是优化：开/关 hybrid 过滤出来的集合必须一致。"""
    hybrid = _build_indexer(tmp_path / "hybrid", use_hybrid=True)
    legacy = _build_indexer(tmp_path / "legacy", use_hybrid=False)
    filters = SearchFilter(path_prefix=SEMICONDUCTOR + "/")
    assert hybrid._fts is not None  # 前置条件：这一组确实走了 FTS 下推

    from_hybrid = sorted(_sources(hybrid.search(SHARED, top_k=10, use_rerank=False, filters=filters)))
    from_legacy = sorted(_sources(legacy.search(SHARED, top_k=10, use_rerank=False, filters=filters)))
    assert from_hybrid == from_legacy == sorted([OLD_SEMI, NEW_SEMI])


def test_tags_filter_hit_and_miss(tmp_path):
    for use_hybrid in (True, False):
        indexer = _build_indexer(tmp_path / f"t{use_hybrid}", use_hybrid)
        # 命中：大小写不敏感 + 自动去 # 前缀。
        assert sorted(_sources(indexer.search(SHARED, 10, False, filters=SearchFilter(tags=["物理"])))) == [OLD_SEMI]
        assert sorted(_sources(indexer.search(SHARED, 10, False, filters=SearchFilter(tags=["#马原"])))) == sorted([OLD_MARX, NEW_MARX])
        # 一次给多个 tag 是"或"关系。
        assert sorted(_sources(indexer.search(SHARED, 10, False, filters=SearchFilter(tags=["物理", "政治"])))) == sorted([OLD_SEMI, OLD_MARX])
        # 不命中：空结果而不是抛异常或退回全量。
        assert indexer.search(SHARED, 10, False, filters=SearchFilter(tags=["不存在的标签"])) == []


def test_mtime_range_is_closed(tmp_path):
    indexer = _build_indexer(tmp_path)
    search = lambda **kwargs: sorted(_sources(indexer.search(SHARED, 10, False, filters=SearchFilter(**kwargs))))

    # 闭区间：端点自身必须命中。
    assert search(mtime_after=OLD_MTIME, mtime_before=NEW_MTIME) == sorted([OLD_SEMI, NEW_SEMI, OLD_MARX, NEW_MARX])
    assert search(mtime_before=OLD_MTIME) == sorted([OLD_SEMI, OLD_MARX])
    assert search(mtime_after=NEW_MTIME) == sorted([NEW_SEMI, NEW_MARX])
    # 端点外一格就把端点排除掉。
    assert search(mtime_after=OLD_MTIME + 1) == sorted([NEW_SEMI, NEW_MARX])
    assert search(mtime_before=NEW_MTIME - 1) == sorted([OLD_SEMI, OLD_MARX])
    # 空区间
    assert search(mtime_after=NEW_MTIME, mtime_before=OLD_MTIME) == []


def test_combined_filters(tmp_path):
    indexer = _build_indexer(tmp_path)
    results = indexer.search(
        SHARED,
        top_k=10,
        use_rerank=False,
        filters=SearchFilter(path_prefix=MARXISM + "/", tags=["马原"], mtime_before=OLD_MTIME),
    )
    assert _sources(results) == [OLD_MARX]


def test_pagination_is_lossless(tmp_path):
    indexer = _build_indexer(tmp_path)
    full = indexer.search(SHARED, top_k=50, use_rerank=False)
    assert len(full) == 4

    # 逐条翻页拼回去 == 不分页，且顺序一致（不重不漏）。
    paged = []
    for offset in range(len(full)):
        paged.extend(indexer.search(SHARED, 10, False, filters=SearchFilter(offset=offset, limit=1)))
    assert [chunk.id for chunk in paged] == [chunk.id for chunk in full]

    # 跨页大小的拼装同样成立。
    page_one = indexer.search(SHARED, 10, False, filters=SearchFilter(limit=3))
    page_two = indexer.search(SHARED, 10, False, filters=SearchFilter(offset=3, limit=3))
    assert len(page_one) == 3 and len(page_two) == 1
    assert [chunk.id for chunk in page_one + page_two] == [chunk.id for chunk in full]

    # limit 缺省时回落到 top_k；offset 越界得到空页。
    assert len(indexer.search(SHARED, 2, False, filters=SearchFilter(offset=1))) == 2
    assert indexer.search(SHARED, 10, False, filters=SearchFilter(offset=99)) == []

    # 空 query 分支也要先过滤后分页。
    empty_full = indexer.search("", top_k=50)
    empty_page = indexer.search("", top_k=50, filters=SearchFilter(offset=1, limit=2))
    assert [chunk.id for chunk in empty_page] == [chunk.id for chunk in empty_full][1:3]
    empty_filtered = indexer.search("", top_k=50, filters=SearchFilter(path_prefix=MARXISM + "/"))
    assert sorted(_sources(empty_filtered)) == sorted([OLD_MARX, NEW_MARX])


def test_no_filters_matches_legacy_output(tmp_path):
    """回归：不传 filters（或传一个全默认 filter）时必须和旧 search 逐字节一致。"""
    for use_hybrid in (True, False):
        indexer = _build_indexer(tmp_path / f"r{use_hybrid}", use_hybrid)
        legacy = [chunk.to_dict() for chunk in indexer.search(SHARED, 3, False)]
        explicit_none = [chunk.to_dict() for chunk in indexer.search(SHARED, 3, False, filters=None)]
        noop = [chunk.to_dict() for chunk in indexer.search(SHARED, 3, False, filters=SearchFilter())]
        assert legacy == explicit_none == noop

        legacy_empty = [chunk.to_dict() for chunk in indexer.search("", 3, False)]
        noop_empty = [chunk.to_dict() for chunk in indexer.search("", 3, False, filters=SearchFilter())]
        assert legacy_empty == noop_empty


def _server(tmp_path: Path, monkeypatch, vaults: list[Path]) -> VaultMcpServer:
    config = tmp_path / "app.toml"
    # 关掉缓存：server 层集成测试不需要 FTS，也就没必要往真实 home 目录写索引。
    config.write_text('mode = "static"\n[cache]\nenabled = false\n', encoding="utf-8")
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    server = VaultMcpServer(config)
    for vault in vaults:
        server.registry.add(str(vault))
    return server


def _search(server: VaultMcpServer, **arguments) -> list[dict]:
    result = server.call_tool("kb_search", dict(arguments))
    return json.loads(result["content"][0]["text"])["chunks"]


def test_server_kb_search_filters_and_pagination(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _write_note(vault / OLD_SEMI, "掺杂", f"{SHARED} 掺杂浓度", "[半导体]", OLD_MTIME)
    _write_note(vault / NEW_MARX, "辩证法", f"{SHARED} 唯物辩证法", "[马原]", NEW_MTIME)
    server = _server(tmp_path, monkeypatch, [vault])

    everything = _search(server, query=SHARED, top_k=10, vault_path=str(vault))
    assert len(everything) == 2

    assert [c["source"] for c in _search(server, query=SHARED, top_k=10, vault_path=str(vault), path_prefix=SEMICONDUCTOR + "/")] == [OLD_SEMI]
    assert [c["source"] for c in _search(server, query=SHARED, top_k=10, vault_path=str(vault), tags=["马原"])] == [NEW_MARX]
    # epoch 秒和 ISO 8601 字符串两种写法都要认。
    assert [c["source"] for c in _search(server, query=SHARED, top_k=10, vault_path=str(vault), mtime_after=NEW_MTIME)] == [NEW_MARX]
    iso = datetime.fromtimestamp(NEW_MTIME).isoformat()
    assert [c["source"] for c in _search(server, query=SHARED, top_k=10, vault_path=str(vault), mtime_after=iso)] == [NEW_MARX]

    page = _search(server, query=SHARED, top_k=10, vault_path=str(vault), offset=1, limit=1)
    assert [c["source"] for c in page] == [everything[1]["source"]]


def test_fanout_filters_each_vault_and_pages_globally(tmp_path, monkeypatch):
    vault_a = tmp_path / "库A"
    vault_b = tmp_path / "库B"
    _write_note(vault_a / OLD_SEMI, "A1", f"{SHARED} 掺杂", "[半导体]", OLD_MTIME)
    _write_note(vault_a / NEW_MARX, "A2", f"{SHARED} 辩证法", "[马原]", NEW_MTIME)
    _write_note(vault_b / OLD_SEMI, "B1", f"{SHARED} 掺杂", "[半导体]", OLD_MTIME)
    server = _server(tmp_path, monkeypatch, [vault_a, vault_b])

    everything = _search(server, query=SHARED, top_k=10)
    assert len(everything) == 3

    # 过滤对每个库分别生效：两个库里各只剩 半导体/ 那一条。
    filtered = _search(server, query=SHARED, top_k=10, path_prefix=SEMICONDUCTOR + "/")
    assert len(filtered) == 2
    assert {c["vault"] for c in filtered} == {str(vault_a), str(vault_b)}

    # 分页作用于合并排序后的全局结果。
    page_one = _search(server, query=SHARED, top_k=10, offset=0, limit=2)
    page_two = _search(server, query=SHARED, top_k=10, offset=2, limit=2)
    assert len(page_one) == 2 and len(page_two) == 1
    assert [c["source"] for c in page_one + page_two] == [c["source"] for c in everything]
