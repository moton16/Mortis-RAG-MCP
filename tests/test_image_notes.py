"""C6：图片 alt/图注注入（inject_image_captions，默认关）。

核心承诺：默认关闭时 chunk 逐字节不变；开启后图片行后出现 `[图片: ...]` 注入行，
alt/图注可以被检索到。开启会改变 chunk.id（= 全量重新 embedding），这是
opt-in 的已知代价。
"""

from __future__ import annotations

from pathlib import Path

from vault_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from vault_mcp.indexer import MarkdownIndexer, _inject_image_notes
from vault_mcp.providers import StaticEmbeddingProvider


def _config(tmp_path: Path, inject: bool) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True),
        inject_image_captions=inject,
    )


def test_markdown_image_alt_and_title():
    lines = ["正文", "![二极管伏安特性](images/diode.png)", "尾随"]
    output = _inject_image_notes(lines)

    # 注入行紧跟图片行，其余行原样保留。
    assert output == [
        "正文",
        "![二极管伏安特性](images/diode.png)",
        "[图片: 二极管伏安特性 (diode)]",
        "尾随",
    ]

    # 带 title 的写法：title 当图注。
    output = _inject_image_notes(['![alt](a/b "整流原理")'])
    assert output == ["![alt](a/b \"整流原理\")", "[图片: alt 整流原理 (b)]"]


def test_obsidian_wiki_embed_captions():
    # 带图注 / 不带图注。
    assert _inject_image_notes(["![[img/circuit.png|电路板特写]]"]) == [
        "![[img/circuit.png|电路板特写]]",
        "[图片: 电路板特写 (circuit)]",
    ]
    assert _inject_image_notes(["![[circuit.png]]"]) == ["![[circuit.png]]", "[图片: circuit]"]

    # 嵌入的是另一篇笔记而非图片：不注入。
    assert _inject_image_notes(["![[另一篇笔记]]"]) == ["![[另一篇笔记]]"]


def test_multiple_images_and_code_blocks():
    # 一行多图：逐张各插一行，顺序与图片一致。
    output = _inject_image_notes(["![甲](a.png) 中间文字 ![乙](b.png)"])
    assert output == ["![甲](a.png) 中间文字 ![乙](b.png)", "[图片: 甲 (a)]", "[图片: 乙 (b)]"]

    # 代码块里的图片语法是示例文本，不注入；围栏成对开合。
    fenced = ["```python", '# ![示例](demo.png)', "```", "![真的](real.png)"]
    output = _inject_image_notes(fenced)
    assert output == fenced + ["[图片: 真的 (real)]"]

    # 纯函数：不修改输入。
    source = ["![alt](x.png)"]
    _inject_image_notes(source)
    assert source == ["![alt](x.png)"]


def test_default_off_leaves_chunks_byte_identical(tmp_path):
    text = "# 标题\n\n![二极管](images/diode.png)\n\n一段正文。\n"
    (tmp_path / "a.md").write_text(text, encoding="utf-8")

    off = MarkdownIndexer(tmp_path, _config(tmp_path, inject=False), embedding_provider=StaticEmbeddingProvider(dimension=8))
    off.sync()
    baseline = [chunk.to_dict() for chunk in off.all_chunks()]
    assert all("[图片:" not in chunk["content"] for chunk in baseline)

    # 与完全未启用该功能的构造路径（旧版本行为）对比：内容必须一致。
    plain = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache2"), enabled=True),
    )
    assert plain.inject_image_captions is False
    plain_indexer = MarkdownIndexer(tmp_path, plain, embedding_provider=StaticEmbeddingProvider(dimension=8))
    plain_indexer.sync()
    assert [chunk.to_dict() for chunk in plain_indexer.all_chunks()] == baseline


def test_enabled_injects_and_makes_captions_searchable(tmp_path):
    text = "# 标题\n\n![二极管伏安特性](images/diode.png)\n\n一段正文。\n"
    (tmp_path / "a.md").write_text(text, encoding="utf-8")

    indexer = MarkdownIndexer(tmp_path, _config(tmp_path, inject=True), embedding_provider=StaticEmbeddingProvider(dimension=8))
    indexer.sync()

    hit = [chunk for chunk in indexer.all_chunks() if "[图片: 二极管伏安特性 (diode)]" in chunk.content]
    assert hit, "注入行必须出现在 chunk 正文里"
    # 注入行必须真的可检索（词法路由命中）。
    results = indexer.search("二极管伏安特性", top_k=5, use_rerank=False)
    assert any("[图片:" in chunk.content for chunk in results)

    # content 变了 → id 变了：与关闭时的 id 必然不同（重嵌代价的来源）。
    off = MarkdownIndexer(
        tmp_path,
        AppConfig(
            embedding=EmbeddingConfig(mode="static", dimension=8),
            cache=CacheConfig(dir=str(tmp_path / "cache_off"), enabled=True),
        ),
        embedding_provider=StaticEmbeddingProvider(dimension=8),
    )
    off.sync()
    assert {c.id for c in indexer.all_chunks()}.isdisjoint({c.id for c in off.all_chunks()})


def test_injection_respects_frontmatter_and_block_ignores(tmp_path):
    """注入发生在豁免判断与 rag-ignore 块剔除之后：豁免内容不会被注入救活。"""
    # 文件级豁免（rag: false）：整个文件不建 chunk。
    (tmp_path / "exempt.md").write_text(
        "---\nrag: false\n---\n# 豁免\n\n![秘密图](secret.png)\n", encoding="utf-8"
    )
    # 块级豁免（rag-ignore 围栏）：围栏内的图片行不注入，围栏外的正常注入。
    (tmp_path / "normal.md").write_text(
        "# 正文\n\n"
        "<!-- rag-ignore -->\n"
        "![忽略图](ignored.png)\n"
        "<!-- /rag-ignore -->\n\n"
        "可见图 ![可见](visible.png)\n",
        encoding="utf-8",
    )
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path, inject=True), embedding_provider=StaticEmbeddingProvider(dimension=8))
    indexer.sync()

    contents = "\n".join(chunk.content for chunk in indexer.all_chunks())
    assert "secret" not in contents, "rag: false 的文件根本不应建 chunk"
    assert "[图片: 忽略图 (ignored)]" not in contents
    assert "[图片: 可见 (visible)]" in contents
