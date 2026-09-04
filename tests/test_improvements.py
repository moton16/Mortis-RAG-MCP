from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import Chunk, MarkdownIndexer, rerank_chunks
from mortis_rag_mcp.registry import VaultRegistry, _process_file_lock


def test_fast_stat_skips_disk_read_when_unmodified(tmp_path, monkeypatch):
    note = tmp_path / "fast_stat_test.md"
    note.write_text("# Fast Stat\nInitial content for testing fast stat.", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))

    # First sync: reads file, populates signatures and _stat_cache
    chunks1 = indexer.sync()
    assert len(chunks1) >= 1
    assert "fast_stat_test.md" in indexer._stat_cache

    # Intercept Path.read_bytes to verify it is NOT called on second sync
    read_bytes_calls = []
    original_read_bytes = Path.read_bytes

    def intercepted_read_bytes(self, *args, **kwargs):
        read_bytes_calls.append(str(self))
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", intercepted_read_bytes)

    # Second sync without touching file: Fast-Stat must hit
    chunks2 = indexer.sync()
    assert len(chunks2) == len(chunks1)
    assert len(read_bytes_calls) == 0, "read_bytes was called despite file being unmodified!"

    # Now modify the file: Fast-Stat must detect change and read_bytes must be called
    time.sleep(0.01)
    note.write_text("# Fast Stat\nUpdated content with modifications.", encoding="utf-8")
    chunks3 = indexer.sync()
    assert len(read_bytes_calls) == 1
    assert any("Updated content" in c.content for c in chunks3)


def test_code_block_fence_heading_and_title_protection(tmp_path):
    note = tmp_path / "fence_test.md"
    content = """# Document Title

Intro text before code.

```python
# comment_not_a_heading
def calculate():
    # another_internal_comment = 123
    return 42
```

## Section Two
Text in section two.
"""
    note.write_text(content, encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    chunks = indexer.sync()

    # The title must be "Document Title", not "# comment_not_a_heading"
    assert chunks[0].title == "Document Title"

    # Verify that code block comments are not treated as headings in chunk metadata
    headings = {c.metadata["heading"] for c in chunks}
    assert "Document Title" in headings
    assert "Section Two" in headings
    assert "comment_not_a_heading" not in headings
    assert "another_internal_comment" not in headings

    # Verify intact code inside chunk content
    code_chunk = next(c for c in chunks if "def calculate():" in c.content)
    assert "# comment_not_a_heading" in code_chunk.content
    assert "# another_internal_comment = 123" in code_chunk.content

    # Test file where the first line is code fence
    code_first = tmp_path / "code_first.md"
    code_first.write_text("```bash\n# install script\nnpm install\n```\n# Actual Title\nBody", encoding="utf-8")
    chunks_code_first = indexer.sync()
    doc_chunk = next(c for c in chunks_code_first if c.source == "code_first.md")
    assert doc_chunk.title == "Actual Title"


def test_chunk_score_immutability(tmp_path):
    note = tmp_path / "immutability.md"
    note.write_text("# Test Title\nHere is some searchable text for testing immutability.", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    indexer.sync()

    # In-memory index chunks must start with score == 0.0
    for chunk in indexer.all_chunks():
        assert chunk.score == 0.0

    # Running search gives results with positive score
    results = indexer.search("searchable text")
    assert len(results) > 0
    assert results[0].score > 0.0

    # In-memory index chunks must REMAIN untouched (score == 0.0)
    for chunk in indexer.all_chunks():
        assert chunk.score == 0.0, f"Chunk {chunk.id} in indexer was mutated! score={chunk.score}"

    # Verify rerank_chunks does not mutate input chunks
    dummy_ranked = [
        Chunk("c1", "content 1", "test.md", "Title", {"chunk_index": 0}, score=0.5),
        Chunk("c2", "content 2", "test.md", "Title", {"chunk_index": 1}, score=0.3),
    ]
    class DummyReranker:
        def rerank(self, query, docs):
            return [
                {"index": 1, "relevance_score": 0.99},
                {"index": 0, "relevance_score": 0.88},
            ]

    reranked = rerank_chunks("query", dummy_ranked, DummyReranker())
    assert reranked[0].score == 0.99
    assert reranked[0].id == "c2"
    # Original input chunks must not have changed
    assert dummy_ranked[0].score == 0.5
    assert dummy_ranked[1].score == 0.3


def test_registry_cross_process_file_lock(tmp_path):
    toml_path = tmp_path / "vaults.toml"
    lock_path = tmp_path / "vaults.toml.lock"

    # Test basic lock acquire and release
    with _process_file_lock(lock_path):
        assert lock_path.exists()

    # Test registry operations succeed under file locking
    reg = VaultRegistry(toml_path)
    vault_dir = tmp_path / "v1"
    vault_dir.mkdir()
    reg.add(vault_dir, "V1")
    reg.set_weight(vault_dir, 2.5)
    reg.set_solo(vault_dir, True)

    loaded = reg.load()
    assert len(loaded) == 1
    assert loaded[0].name == "V1"
    assert loaded[0].weight == 2.5
    assert loaded[0].solo is True

    reg.remove(vault_dir)
    assert len(reg.load()) == 0


def test_short_acronym_lexical_boost_and_word_boundary(tmp_path):
    # note_rc mentions the acronym RC as a standalone word
    note_rc = tmp_path / "circuit.md"
    note_rc.write_text(
        "# 电路理论\n在集成电路互连线分析中，RC 树状网络延迟采用 Elmore 模型进行估算。",
        encoding="utf-8",
    )

    # note_src contains substring 'rc' inside other words (source, architecture, force)
    # but does NOT mention the acronym RC
    note_src = tmp_path / "architecture.md"
    note_src.write_text(
        "# 架构与源码\nsource search architecture force percent 深入探讨了复杂软件架构与树状网络分层。",
        encoding="utf-8",
    )

    indexer = MarkdownIndexer(
        tmp_path,
        AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4), use_hybrid=True),
    )
    indexer.sync()

    # Search for acronym "RC"
    rc_results = indexer.search("RC")
    assert len(rc_results) > 0
    # circuit.md must be the top result because \bRC\b matches circuit.md and NOT architecture.md
    assert rc_results[0].source == "circuit.md"

    # Search for "RC 树状网络"
    combo_results = indexer.search("RC 树状网络")
    assert len(combo_results) > 0
    assert combo_results[0].source == "circuit.md"


def test_multivault_search_with_rerank_integration(tmp_path):
    from mortis_rag_mcp.server import VaultMcpServer
    import json

    v1 = tmp_path / "vault1"
    v2 = tmp_path / "vault2"
    v1.mkdir()
    v2.mkdir()
    (v1 / "doc1.md").write_text("# Doc 1\nAlpha search content.", encoding="utf-8")
    (v2 / "doc2.md").write_text("# Doc 2\nBeta search content.", encoding="utf-8")

    server = VaultMcpServer()
    server.registry.add(v1, "V1", persist=False)
    server.registry.add(v2, "V2", persist=False)

    class DummyReranker:
        def rerank(self, query, docs):
            return [{"index": i, "relevance_score": 0.9 - i * 0.1} for i in range(len(docs))]

    idx1 = server._indexer_for({"vault_path": str(v1)})
    idx1.reranker_provider = DummyReranker()
    idx1.sync()

    idx2 = server._indexer_for({"vault_path": str(v2)})
    idx2.reranker_provider = DummyReranker()
    idx2.sync()

    # Multi-vault fan-out search with use_rerank=True must succeed (no id(chunk) KeyError)
    res = server.call_tool("kb_search", {"query": "search content", "use_rerank": True})
    data = json.loads(res["content"][0]["text"])
    assert "chunks" in data
    assert len(data["chunks"]) >= 2
    assert all("vault" in c for c in data["chunks"])


def test_nested_code_fences_and_headings(tmp_path):
    note = tmp_path / "nested.md"
    content = """# Outer Title

````markdown
```python
# python_comment_inside_nested_block
def test():
    return 1
```
````

## Heading After Fences
Content after fences.
"""
    note.write_text(content, encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    chunks = indexer.sync()

    headings = [c.metadata["heading"] for c in chunks]
    assert "Outer Title" in headings
    assert "Heading After Fences" in headings
    assert "python_comment_inside_nested_block" not in headings


def test_short_stopword_not_hijacked_as_acronym(tmp_path):
    note1 = tmp_path / "stopwords.md"
    # Contains many stopwords "in", "to", "at", "is"
    note1.write_text("# Stopwords Note\nIn to at is on of it he we up by do so. " * 5, encoding="utf-8")

    note2 = tmp_path / "actual_topic.md"
    note2.write_text("# Target Note\nQuantum computing algorithms and superconducting qubits.", encoding="utf-8")

    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    indexer.sync()

    # Search with lower-case "in" and topic keyword:
    # "in" should NOT get 5x acronym boost over "quantum"
    res = indexer.search("in quantum")
    assert len(res) > 0
    assert res[0].source == "actual_topic.md"
