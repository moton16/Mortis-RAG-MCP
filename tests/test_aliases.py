"""v0.8.0 F5a frontmatter aliases 参与检索与缓存代际测试。

契约与安全闸门：
1. frontmatter properties.get("aliases") 支持 list 原样提取与 str 按逗号分隔提取；
2. 关键约束：meta["aliases"] 仅在 aliases 非空时写入，避免绝大多数笔记 metadata 膨胀与 v073 金测漂移；
3. 词法路 _lexical_haystack 纯函数及检索扫描：正文无别名时，通过 aliases 仍能正确定位召回；
4. 缓存代际升级（chunker 5 -> 6）：老缓存自动重建文本层，向量按 chunk.id 全量复用，断言 0 次重新 embedding；
5. full 模式单条响应字节实测：记录有无 aliases 的字节增量，确保无意外膨胀。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, ChunkingConfig, EmbeddingConfig, VectorConfig
from mortis_rag_mcp.indexer import Chunk, MarkdownIndexer
from mortis_rag_mcp._indexer.chunking import chunk_file, new_chunk
from mortis_rag_mcp._indexer.search import _lexical_haystack


class CountingProvider:
    """记录每次 embed 调用的文本，用于严格断言 0 次重新 embedding。"""

    dimension = 8

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for text in texts:
            digest = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            out.append([float((digest >> index) & 1) for index in range(self.dimension)])
        return out

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


def _create_config(cache_dir: Path) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", dimension=8),
        vector=VectorConfig(backend="memory"),
        cache=CacheConfig(dir=str(cache_dir), enabled=True),
    )


def test_aliases_extraction_list_and_str(tmp_path: Path):
    """测试 list 形式与逗号分隔 str 形式的 aliases 正确提取。"""
    cfg = AppConfig()

    # 1. list 形式 [中科大, 科大]
    text_list = "---\naliases: [中科大, 科大]\n---\n# 中国科学技术大学\n这是一所位于合肥的高校。\n"
    chunks_list = chunk_file("test_list.md", text_list, cfg)
    assert len(chunks_list) == 1
    assert chunks_list[0].metadata.get("aliases") == ["中科大", "科大"]

    # 2. str 逗号分隔形式（含英文逗号与中文逗号）
    text_str = "---\naliases: 中科大, 科大, 中国科大\n---\n# 中国科学技术大学\n这是一所位于合肥的高校。\n"
    chunks_str = chunk_file("test_str.md", text_str, cfg)
    assert len(chunks_str) == 1
    assert chunks_str[0].metadata.get("aliases") == ["中科大", "科大", "中国科大"]

    # 3. 中文全角逗号与中英混排
    text_cn_comma = "---\naliases: 中科大，科大，USTC\n---\n# 中国科学技术大学\n正文。\n"
    chunks_cn = chunk_file("test_cn.md", text_cn_comma, cfg)
    assert len(chunks_cn) == 1
    assert chunks_cn[0].metadata.get("aliases") == ["中科大", "科大", "USTC"]

    # 4. 中英混用逗号与内外部空白/引号
    text_mixed = "---\naliases: '  中科大  ', \"科大\"， USTC\n---\n# 中国科学技术大学\n正文。\n"
    chunks_mixed = chunk_file("test_mixed.md", text_mixed, cfg)
    assert len(chunks_mixed) == 1
    assert chunks_mixed[0].metadata.get("aliases") == ["中科大", "科大", "USTC"]

    # 5. 重复别名自动保序去重
    text_dup = "---\naliases: [中科大, 科大, 中科大]\n---\n# 中国科学技术大学\n正文。\n"
    chunks_dup = chunk_file("test_dup.md", text_dup, cfg)
    assert len(chunks_dup) == 1
    assert chunks_dup[0].metadata.get("aliases") == ["中科大", "科大"]

    # 6. YAML 列表缩进形式
    text_yaml_list = "---\naliases:\n  - 中科大\n  - 科大\n---\n# 中国科学技术大学\n高校正文。\n"
    chunks_yaml = chunk_file("test_yaml.md", text_yaml_list, cfg)
    assert len(chunks_yaml) == 1
    assert chunks_yaml[0].metadata.get("aliases") == ["中科大", "科大"]


def test_aliases_non_empty_key_only(tmp_path: Path):
    """关键约束断言：meta['aliases'] 仅在 aliases 非空时写入，无别名时绝不产生冗余 key。"""
    cfg = AppConfig()

    # 无 aliases frontmatter
    text_none = "# 普通笔记\n无别名内容。\n"
    chunks_none = chunk_file("none.md", text_none, cfg)
    assert "aliases" not in chunks_none[0].metadata

    # 空列表 []
    text_empty_list = "---\naliases: []\n---\n# 空列表\n内容。\n"
    chunks_empty_list = chunk_file("empty_list.md", text_empty_list, cfg)
    assert "aliases" not in chunks_empty_list[0].metadata

    # 空字符串 / 全空格
    text_empty_str = "---\naliases: ''\n---\n# 空字符\n内容。\n"
    chunks_empty_str = chunk_file("empty_str.md", text_empty_str, cfg)
    assert "aliases" not in chunks_empty_str[0].metadata

    # 仅含中英文逗号与空格的空串
    text_commas_only = "---\naliases: '  ,  ，  '\n---\n# 仅逗号\n内容。\n"
    chunks_commas = chunk_file("commas.md", text_commas_only, cfg)
    assert "aliases" not in chunks_commas[0].metadata

    # 列表中包含 None / 空字符串 / 布尔值时不被误转换为 'None' 或 'True'
    from unittest.mock import patch
    dummy_text = "---\nfoo: bar\n---\n# 标题\n内容\n"
    with patch("mortis_rag_mcp._indexer.chunking.frontmatter", return_value=(2, [], {"aliases": [None, "", "  "]})):
        chunks_none_items = chunk_file("none_items.md", dummy_text, cfg)
        assert len(chunks_none_items) == 1
        assert "aliases" not in chunks_none_items[0].metadata

    with patch("mortis_rag_mcp._indexer.chunking.frontmatter", return_value=(2, [], {"aliases": [True, False]})):
        chunks_bool_items = chunk_file("bool_items.md", dummy_text, cfg)
        assert len(chunks_bool_items) == 1
        assert "aliases" not in chunks_bool_items[0].metadata

    # 非法类型（数字、布尔、字典等）通过 properties 传入被安全忽略
    with patch("mortis_rag_mcp._indexer.chunking.frontmatter", return_value=(2, [], {"aliases": 12345})):
        chunks_invalid = chunk_file("invalid.md", dummy_text, cfg)
        assert len(chunks_invalid) == 1
        assert "aliases" not in chunks_invalid[0].metadata

    with patch("mortis_rag_mcp._indexer.chunking.frontmatter", return_value=(2, [], {"aliases": True})):
        chunks_bool = chunk_file("bool.md", dummy_text, cfg)
        assert len(chunks_bool) == 1
        assert "aliases" not in chunks_bool[0].metadata

    with patch("mortis_rag_mcp._indexer.chunking.frontmatter", return_value=(2, [], {"aliases": {"key": "val"}})):
        chunks_dict = chunk_file("dict.md", dummy_text, cfg)
        assert len(chunks_dict) == 1
        assert "aliases" not in chunks_dict[0].metadata

    # new_chunk 直接调用防御：空列表、全空字符串列表、含 None 列表均不得写入 metadata
    c1 = new_chunk("a.md", "t", "h", 1, 1, 0, [], ["line"], aliases=None)
    assert "aliases" not in c1.metadata
    c2 = new_chunk("a.md", "t", "h", 1, 1, 0, [], ["line"], aliases=[])
    assert "aliases" not in c2.metadata
    c3 = new_chunk("a.md", "t", "h", 1, 1, 0, [], ["line"], aliases=["", "  ", None])
    assert "aliases" not in c3.metadata
    c4 = new_chunk("a.md", "t", "h", 1, 1, 0, [], ["line"], aliases=[True, False])
    assert "aliases" not in c4.metadata
    c5 = new_chunk("a.md", "t", "h", 1, 1, 0, [], ["line"], aliases=["别名", "别名"])
    assert c5.metadata["aliases"] == ["别名"]


def test_lexical_haystack_pure_function():
    """断言 _lexical_haystack 构建的匹配文本符合预期（正文 + aliases 追加并转小写）。"""
    # 无 aliases
    c1 = Chunk("id1", "Hello World", "a.md", "Title", {})
    assert _lexical_haystack(c1) == "hello world"

    # 空 aliases
    c2 = Chunk("id2", "Hello World", "a.md", "Title", {"aliases": []})
    assert _lexical_haystack(c2) == "hello world"

    # 脏 aliases（全空或非正常元素）返回原正文小写
    c_dirty = Chunk("id_d", "Hello World", "a.md", "Title", {"aliases": ["", None, "  "]})
    assert _lexical_haystack(c_dirty) == "hello world"

    # 有 aliases（含中英混排与大小写）
    c3 = Chunk("id3", "量子计算机", "a.md", "Title", {"aliases": ["USTC", "中科大"]})
    assert _lexical_haystack(c3) == "量子计算机 ustc 中科大"


def test_search_retrieval_via_aliases_when_content_lacks_it(tmp_path: Path):
    """测试正文完全不含别名词汇时，检索通过 frontmatter aliases 仍能成功精准召回。"""
    vault = tmp_path / "vault"
    vault.mkdir()

    # 目标文件：正文完全不出现 "中科大" 或 "科大"，只有全称与其它描述
    target_file = vault / "中国科学技术大学.md"
    target_file.write_text(
        "---\naliases: [中科大, USTC]\n---\n"
        "# 中国科学技术大学\n\n"
        "这是一所位于安徽省合肥市的顶尖理工科院校，以基础科学和尖端工程见长。\n",
        encoding="utf-8",
    )

    # 干扰文件：正文同样不含 "中科大"
    distractor_file = vault / "北京大学.md"
    distractor_file.write_text(
        "# 北京大学\n\n"
        "位于燕园的一所综合性著名学府，拥有悠久的人文与自然科学底蕴。\n",
        encoding="utf-8",
    )

    indexer = MarkdownIndexer(vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8)))
    indexer.sync()

    # 1. 搜中文别名 "中科大"
    results_cn = indexer.search("中科大", top_k=5, dedupe=True)
    assert len(results_cn) >= 1
    assert results_cn[0].source == "中国科学技术大学.md"
    assert results_cn[0].score > 0

    # 2. 搜英文缩写别名 "USTC"（大写与小写）
    results_en = indexer.search("USTC", top_k=5, dedupe=True)
    assert len(results_en) >= 1
    assert results_en[0].source == "中国科学技术大学.md"
    assert results_en[0].score > 0

    results_lower = indexer.search("ustc", top_k=5, dedupe=True)
    assert len(results_lower) >= 1
    assert results_lower[0].source == "中国科学技术大学.md"
    assert results_lower[0].score > 0

    # 3. 搜正文与别名都不包含的内容（无公共 token 命中）
    results_none = indexer.search("量子隐形传态XYZ", top_k=5, dedupe=True)
    assert len(results_none) == 0 or all(r.score == 0 for r in results_none)


def test_chinese_comma_and_multi_chunk_retrieval(tmp_path: Path):
    """测试中文全角逗号书写的别名正确检索，以及长笔记多切块时别名穿透到所有切块且 chunk_index=0 首位保序。"""
    vault = tmp_path / "vault"
    vault.mkdir()

    # 包含中文逗号别名的多章节笔记
    multi_file = vault / "合肥微尺度物质科学国家研究中心.md"
    multi_file.write_text(
        "---\naliases: 微尺度，合肥微尺度\n---\n"
        "# 合肥微尺度物质科学国家研究中心\n\n"
        "第一章节正文，介绍基本概况。\n\n"
        "## 研究方向\n\n"
        "第二章节正文，介绍量子物理与纳米科技。\n\n"
        "## 实验装置\n\n"
        "第三章节正文，介绍尖端实验仪器装备。\n",
        encoding="utf-8",
    )

    indexer = MarkdownIndexer(vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8)))
    chunks = indexer.sync()
    assert len(chunks) >= 3
    # 断言多切块笔记的所有切块均继承 aliases 元数据
    for c in chunks:
        assert c.metadata.get("aliases") == ["微尺度", "合肥微尺度"]

    # 通过全角逗号解析出的别名 "微尺度" 检索
    res = indexer.search("微尺度", top_k=5, dedupe=False)
    assert len(res) >= 1
    # 首条命中的应当是该笔记的第一切块（chunk_index == 0）
    assert res[0].source == "合肥微尺度物质科学国家研究中心.md"
    assert res[0].metadata["chunk_index"] == 0


def test_chunker_current_generation(tmp_path):
    """R2 接续实际 C98 chunker=7；旧 5→6 断言不冒充当前契约。"""
    indexer = MarkdownIndexer(tmp_path, AppConfig())
    meta = indexer._chunks_meta()
    assert meta["chunker"] == 7
    indexer.close_document_store()


def test_cache_migration_chunker5_to_current_zero_reembedding(tmp_path: Path):
    """显式 legacy 保既有身份；实际代际升级重建文本、按 ID 复用向量。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    cache_dir = tmp_path / "cache"

    (vault / "note1.md").write_text(
        "---\naliases: [别名一]\n---\n# 笔记一\n这里是笔记一的测试正文，用于验证切块向量持久化。\n",
        encoding="utf-8",
    )
    (vault / "note2.md").write_text(
        "# 笔记二\n这里是无别名的第二篇笔记，同样包含一段正文内容。\n",
        encoding="utf-8",
    )

    cfg = _create_config(cache_dir)
    cfg.chunking = ChunkingConfig(mode="legacy_chars", mode_explicit=True)
    provider1 = CountingProvider()

    # 模拟旧版环境（chunker=5）先跑一次 sync，生成缓存
    orig_meta_fn = MarkdownIndexer._chunks_meta

    def legacy_chunks_meta(self):
        m = orig_meta_fn(self)
        m["chunker"] = 5
        return m

    MarkdownIndexer._chunks_meta = legacy_chunks_meta
    try:
        indexer_v5 = MarkdownIndexer(vault, cfg, embedding_provider=provider1)
        indexer_v5.sync()
    finally:
        MarkdownIndexer._chunks_meta = orig_meta_fn

    # 验证 v5 阶段产生了一次性的 embedding
    initial_embed_count = len(provider1.texts)
    assert initial_embed_count == 2, f"初始切块应 embed 2 条文本，实际: {initial_embed_count}"

    # 现在使用当前真实 chunker=7，显式保旧 legacy 模式。
    provider2 = CountingProvider()
    indexer_v6 = MarkdownIndexer(vault, cfg, embedding_provider=provider2)
    assert indexer_v6._chunks_meta()["chunker"] == 7

    chunks_v6 = indexer_v6.sync()
    assert len(chunks_v6) == 2

    # 核心铁律断言：文本层检测到 chunker: 5 != 7 重建了文本，但向量通过 _pending_vectors
    # 按不变的 chunk.id 成功全部复用，新 provider 收到 0 次 embed 请求！
    assert len(provider2.texts) == 0, f"升级 chunker=7 时应 0 次重新 embedding，实际请求了: {provider2.texts}"
    indexer_v5.close_document_store()
    indexer_v6.close_document_store()


def test_full_mode_aliases_byte_overhead_measurement():
    """复测有 aliases 与无 aliases 笔记在 full 模式下的单条序列化字节数。"""
    c_without = Chunk(
        id="a" * 40,
        content="这是一段约五百字符的常规知识库笔记内容。" * 20,
        source="知识库/中国科学技术大学.md",
        title="中国科学技术大学",
        metadata={
            "heading": "中国科学技术大学",
            "start_line": 1,
            "end_line": 25,
            "chunk_index": 0,
            "tags": ["高校", "科研"],
            "mtime": 1727400000.0,
            "content_hash": "b" * 16,
        },
    )

    c_with = Chunk(
        id="a" * 40,
        content="这是一段约五百字符的常规知识库笔记内容。" * 20,
        source="知识库/中国科学技术大学.md",
        title="中国科学技术大学",
        metadata={
            "heading": "中国科学技术大学",
            "start_line": 1,
            "end_line": 25,
            "chunk_index": 0,
            "tags": ["高校", "科研"],
            "mtime": 1727400000.0,
            "content_hash": "b" * 16,
            "aliases": ["中科大", "USTC", "中国科大"],
        },
    )

    bytes_without = len(json.dumps(c_without.to_dict(), ensure_ascii=False).encode("utf-8"))
    bytes_with = len(json.dumps(c_with.to_dict(), ensure_ascii=False).encode("utf-8"))
    overhead = bytes_with - bytes_without

    # 记录并断言增量在合理范围内（仅多了 aliases 键值对，通常 40-70 字节，远低于 100 字节）
    assert overhead > 0
    assert overhead < 100, f"aliases 导致的 full 模式单条字节增量过大: {overhead} bytes"
