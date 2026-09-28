"""P3b chunking 接缝回归测试。

契约：
1. 测试对 indexer 模块属性 ``_inject_image_notes`` 的 monkeypatch 必须能在
   ``_chunk_file`` 调用路径中被观察到（D2 决策，Facade 显式包装/注入）；
2. Facade re-export 兼容性：所有提取出的正则、常量与函数必须能从
   ``mortis_rag_mcp.indexer`` 正常导入；
3. ``MarkdownIndexer`` 实例薄委托保持原方法签名。
"""
from __future__ import annotations

from pathlib import Path
import mortis_rag_mcp.indexer as indexer_module
from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import (
    MarkdownIndexer,
    _BLOCK_IGNORE_END,
    _BLOCK_IGNORE_START,
    _CHAPTER_HEADING_RE,
    _FENCE_RE,
    _FENCE_START_RE,
    _HEADING_RE,
    _IMAGE_EXTS,
    _INDEXABLE_TEXT_EXTS,
    _MD_IMAGE_RE,
    _WIKI_IMAGE_RE,
    _image_note,
    _image_notes_for_line,
    _inject_image_notes,
    _is_chapter_heading,
)


def test_chunking_reexports_exist():
    """断言所有 chunking 相关常量与函数在 indexer Facade 上均可导入且可用。"""
    assert _HEADING_RE.match("# 标题")
    assert _BLOCK_IGNORE_START.search("<!-- rag-ignore -->")
    assert _BLOCK_IGNORE_END.search("<!-- /rag-ignore -->")
    assert _MD_IMAGE_RE.search("![alt](img.png)")
    assert _WIKI_IMAGE_RE.search("![[img.png]]")
    assert _FENCE_START_RE.match("```python")
    assert _FENCE_RE is _FENCE_START_RE
    assert ".png" in _IMAGE_EXTS
    assert ".md" in _INDEXABLE_TEXT_EXTS
    ok, ch = _is_chapter_heading("第一章 启程")
    assert ok and ch == "第一章 启程"
    assert "[图片:" in _image_note("alt", "cap", "test.png")
    assert len(_image_notes_for_line("![a](b.png)")) == 1
    assert len(_inject_image_notes(["![a](b.png)"])) == 2


def test_inject_image_notes_monkeypatch_observed(monkeypatch):
    """Facade 补丁 _inject_image_notes 必须在 _chunk_file 启用 inject_image_captions 时生效。"""
    calls: list[int] = []

    def fake_inject(lines):
        calls.append(len(lines))
        return lines + ["[图片: 打桩注入]"]

    monkeypatch.setattr(indexer_module, "_inject_image_notes", fake_inject)

    cfg = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        inject_image_captions=True,
    )
    indexer = MarkdownIndexer(Path("dummy"), cfg)
    chunks = indexer._chunk_file("test.md", "# 标题\n\n![测试](test.png)\n")
    assert calls, "Facade _inject_image_notes monkeypatch 接缝断裂：未被 _chunk_file 观察到！"
    full_content = "\n".join(c.content for c in chunks)
    assert "[图片: 打桩注入]" in full_content


def test_facade_delegators_signature_compat():
    """断言 MarkdownIndexer 上的薄委托方法功能正常。"""
    cfg = AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    indexer = MarkdownIndexer(Path("dummy"), cfg)

    # 1. _frontmatter
    end, tags, props = indexer._frontmatter(["---", "tags: [a, b]", "rag: false", "---", "# Body"])
    assert end == 3 and tags == ["a", "b"] and props.get("rag") is False

    # 2. _is_frontmatter_exempt
    exempt, reason = indexer._is_frontmatter_exempt(tags, props)
    assert exempt and "rag: false" in reason

    # 3. _strip_ignored_blocks
    cleaned, has_ig = indexer._strip_ignored_blocks(["# Hello", "<!-- rag-ignore -->", "secret", "<!-- /rag-ignore -->", "world"])
    assert has_ig and "secret" not in cleaned

    # 4. _clean_heading
    assert indexer._clean_heading("章节标题 ###") == "章节标题"

    # 5. _title
    assert indexer._title("doc.md", ["# 真正的标题", "正文"]) == "真正的标题"

    # 6. _overlap_tail
    tail, length = indexer._overlap_tail(["line1", "line2"], 10)
    assert len(tail) > 0

    # 7. _new_chunk
    c = indexer._new_chunk("a.md", "T", "H", 1, 5, 0, ["tag"], ["line 1", "line 2"])
    assert c.title == "T" and c.metadata["chunk_index"] == 0
