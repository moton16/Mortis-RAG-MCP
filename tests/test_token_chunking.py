from array import array
from dataclasses import fields
from types import SimpleNamespace

import pytest

from mortis_rag_mcp._indexer.chunking import chunk_file, make_chunks
from mortis_rag_mcp._indexer.models import Chunk
from mortis_rag_mcp._indexer.token_chunking import (
    chunker_fingerprint, chunking_profile, embedding_key,
    estimate_reembedding_cost, estimate_tokens,
)


PROFILE = {"mode": "estimated_tokens", "target": 24, "overlap": 4, "hard_limit": 48}


def chunks(text, profile=PROFILE, identity=None):
    return make_chunks("note.md", "note", [], [("note", 1, text.splitlines())], 800, 120,
                       chunking=profile, source_text=text, virtual_identity=identity)


def assert_coverage(text, output):
    lines = text.splitlines()
    covered = set()
    for chunk in output:
        for span in chunk.metadata["source_spans"]:
            line, a, b = span["line"], span["start_char"], span["end_char"]
            assert 0 <= a <= b <= len(lines[line - 1])
            assert lines[line - 1][a:b] in chunk.content
            covered.update((line, i) for i in range(a, b))
        assert chunk.metadata["estimated_tokens"] == estimate_tokens(chunk.content)
        if not chunk.metadata.get("oversize"):
            assert estimate_tokens(chunk.content) <= PROFILE["hard_limit"]
    assert covered == {(line, i) for line, text in enumerate(lines, 1) for i in range(len(text))}


def test_legacy_contract():
    args = ("note.md", "note", [], [("note", 1, ["one", "two"])], 800, 120)
    old = make_chunks(*args)
    explicit = make_chunks(*args, chunking={"mode": "legacy_chars"})
    assert old == explicit
    assert [f.name for f in fields(Chunk)] == ["id", "content", "source", "title", "metadata", "score", "embedding"]
    assert "source_spans" not in old[0].metadata


@pytest.mark.parametrize("text", [
    "hello world. " * 120,
    "\u4e2d\u6587\u957f\u884c" * 150,
    "\n".join("word " * 7 for _ in range(80)),
    "````python\n" + "print('hello world')\n" * 100 + "````",
    "~~~python\n" + "variable=" + "x" * 1000,
    "| name | value |\n| --- | --- |\n" + "| hello | world |\n" * 100,
    "| " + "x" * 1000 + " | value |\n| --- | --- |\n| hello | world |",
    "e\u0301" * 500,
])
def test_bounded_complete_deterministic(text):
    output = chunks(text)
    assert output == chunks(text)
    assert len(output) < len(text) + 1
    assert_coverage(text, output)


@pytest.mark.parametrize("field,value", [("target", True), ("target", float("nan")),
                                         ("overlap", -1), ("overlap", 24), ("hard_limit", 2)])
def test_invalid_profile(field, value):
    with pytest.raises(ValueError):
        chunking_profile({**PROFILE, field: value})


def test_virtual_revision_address_and_independent_embedding_key():
    a = {"store_uuid": "store", "doc_id": "doc", "revision_id": "a"}
    b = {**a, "revision_id": "b"}
    ca, cb = chunks("hello", identity=a)[0], chunks("hello", identity=b)[0]
    assert ca.id != cb.id
    assert chunks("hello", identity=a)[0].id == ca.id
    assert embedding_key(ca.content, "space") == embedding_key(cb.content, "space")
    assert embedding_key(ca.content, "other") != embedding_key(cb.content, "space")
    assert chunker_fingerprint(PROFILE) != chunker_fingerprint({**PROFILE, "target": 25})


def test_cost_preflight_requires_verified_key_and_actual_vector():
    old, new = chunks("hello"), chunks("hello")
    old[0].embedding = array("f", [1])
    assert estimate_reembedding_cost(old, new, space_fingerprint="space")["reusable_chunk_count"] == 0
    old[0].metadata["embedding_key"] = embedding_key(old[0].content, "space")
    result = estimate_reembedding_cost(old, new, space_fingerprint="space")
    assert result["reusable_chunk_count"] == 1
    assert result["estimated_requests"] == 0
    assert result["estimated_cost"] is None
    assert estimate_reembedding_cost(old, new, space_fingerprint="new")["estimated_requests"] == 1


def test_frontmatter_ignored_heading_offsets():
    text = "---\ntags: [test]\n---\n# Title\nbody\n<!-- rag-ignore -->\nsecret\n<!-- /rag-ignore -->\n## Next\nend"
    output = chunk_file("note.md", text, SimpleNamespace(chunking=PROFILE))
    assert "secret" not in "\n".join(c.content for c in output)
    assert output[0].metadata["source_spans"][0]["line"] == 4
    assert output[-1].metadata["source_spans"][-1]["line"] == 10


def test_injected_caption_not_claimed_as_source_line():
    text = "# Title\n![graph](graph.png)\nafter image"
    output = chunk_file("note.md", text, SimpleNamespace(chunking=PROFILE, inject_image_captions=True))
    spans = [span for c in output for span in c.metadata["source_spans"]]
    assert {span["line"] for span in spans} == {1, 2, 3}
    assert any(s["kind"] == "parser_generated" for c in output for s in c.metadata["synthetic_segments"])
    for c in output:
        for span in c.metadata["source_spans"]:
            assert text.splitlines()[span["line"] - 1][span["start_char"]:span["end_char"]] in c.content
