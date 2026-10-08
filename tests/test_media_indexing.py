from mortis_rag_mcp._indexer.media import build_media_proxy_chunks, media_chunk_links
from mortis_rag_mcp._indexer.models import Chunk
from mortis_rag_mcp.ingest.models import MediaOccurrence


def build(text, items, profile="p"):
    return build_media_proxy_chunks(text, items, source="book.pdf", revision_id="r",
                                   profile_key=profile, chunker_fingerprint="c",
                                   derived_generation_id="g")


def test_proxy_deterministic_and_heading_bounded():
    text = "# Secret\nnot adjacent\n# Figure\nbefore ![](a.png) after\n# Next\nexcluded"
    start = text.index("![]")
    item = MediaOccurrence("a", "image", 0, caption="chart", ocr="verified OCR",
                           anchor_start=start, anchor_end=start + len("![](a.png)"),
                           blob_sha256="sha", page=2)
    a, b = build(text, [item])[0], build(text, [item])[0]
    assert a.id == b.id and a.content == b.content
    assert "verified OCR" in a.content and "excluded" not in a.content
    assert "not adjacent" not in a.content
    assert a.metadata["start_line"] == 4
    assert a.metadata["synthetic_segments"][0]["char_end"] == len(a.content)
    assert a.metadata["ocr_available"] is True


def test_missing_anchor_ocr_are_not_fabricated():
    item = MediaOccurrence("a", "image", 0, width=20, height=20)
    proxy = build("text", [item])[0]
    assert proxy.metadata["ocr_available"] is False
    assert proxy.metadata["source_spans"] == []
    assert proxy.metadata["start_line"] is None
    assert len(build("text", [item])) == 1


def test_shared_caption_blob_hash_and_occurrence_identity():
    a = MediaOccurrence("a", "image", 0, caption="same", blob_sha256="first", page=1)
    b = MediaOccurrence("b", "image", 1, caption="same", blob_sha256="second", page=2)
    chunks = build("", [a, b])
    assert chunks[0].id != chunks[1].id
    assert chunks[0].metadata["embedding_key"] != chunks[1].metadata["embedding_key"]
    assert build("", [a], "other")[0].id != chunks[0].id


def test_profile_scoped_links_and_bidirectional_context():
    occurrence = MediaOccurrence("a", "image", 0, anchor_start=2, anchor_end=5)
    chunk = Chunk("text", "hello", "book.pdf", "", {
        "revision_id": "r", "source_spans": [{"char_start": 0, "char_end": 7}]})
    links = media_chunk_links([chunk], [occurrence], revision_id="r", profile_key="one",
                             chunker_fingerprint="c", derived_generation_id="g")
    assert chunk.metadata["media_occurrence_ids"] == ["a"]
    assert links == [{"revision_id": "r", "occurrence_id": "a", "profile_key": "one",
                      "chunker_fingerprint": "c", "derived_generation_id": "g",
                      "chunk_id": "text", "relation": "context"}]


def test_ambiguous_repeat_and_decorative_verified_only():
    text = "![](a.png) and ![](a.png)"
    item = MediaOccurrence("a", "image", 0, name="a.png")
    assert not build(text, [item])[0].metadata["anchor_available"]
    skipped = MediaOccurrence("b", "image", 1, metadata={"decorative_verified": True})
    assert build(text, [skipped]) == []


def test_line_local_and_global_spans_link_same_occurrence():
    markdown = "line one\nline two has media here\nline three"
    start = markdown.index("media")
    occurrence = MediaOccurrence("a", "image", 0, anchor_start=start, anchor_end=start + 5)
    global_chunk = Chunk("c1", "x", "book.pdf", "", {
        "revision_id": "r", "source_spans": [{"char_start": start, "char_end": start + 5}]})
    local_chunk = Chunk("c2", "x", "book.pdf", "", {
        "revision_id": "r", "source_spans": [{"line": 2, "start_char": start - 9, "end_char": start - 4}]})
    links = media_chunk_links([global_chunk, local_chunk], [occurrence], revision_id="r",
                              profile_key="p", chunker_fingerprint="c", derived_generation_id="g",
                              markdown=markdown)
    assert global_chunk.metadata["media_occurrence_ids"] == ["a"]
    assert local_chunk.metadata["media_occurrence_ids"] == ["a"]
    assert {link["chunk_id"] for link in links} == {"c1", "c2"}


def test_line_local_span_without_markdown_is_not_guessed():
    occurrence = MediaOccurrence("a", "image", 0, anchor_start=0, anchor_end=3)
    chunk = Chunk("c", "x", "book.pdf", "", {
        "revision_id": "r", "source_spans": [{"line": 1, "start_char": 0, "end_char": 3}]})
    links = media_chunk_links([chunk], [occurrence], revision_id="r", profile_key="p",
                              chunker_fingerprint="c", derived_generation_id="g")
    assert links == []
    assert chunk.metadata["media_occurrence_ids"] == []


def test_distinct_occurrences_do_not_cross_link():
    markdown = "aaa bbb ccc ddd"
    occ_a = MediaOccurrence("a", "image", 0, anchor_start=0, anchor_end=3)
    occ_b = MediaOccurrence("b", "image", 1, anchor_start=8, anchor_end=11)
    first = Chunk("c1", "x", "book.pdf", "", {"revision_id": "r", "source_spans": [{"char_start": 0, "char_end": 4}]})
    second = Chunk("c2", "x", "book.pdf", "", {"revision_id": "r", "source_spans": [{"char_start": 8, "char_end": 12}]})
    media_chunk_links([first, second], [occ_a, occ_b], revision_id="r", profile_key="p",
                      chunker_fingerprint="c", derived_generation_id="g", markdown=markdown)
    assert first.metadata["media_occurrence_ids"] == ["a"]
    assert second.metadata["media_occurrence_ids"] == ["b"]


SPLIT_PROFILE = {"mode": "estimated_tokens", "target_tokens": 8, "overlap_tokens": 0, "hard_limit_tokens": 48}


def proxy_with_ocr(text="before ![](a.png) after", words=80):
    start = text.index("![]")
    item = MediaOccurrence("a", "image", 0, caption="c", ocr=" ".join(["word"] * words),
                           anchor_start=start, anchor_end=start + len("![](a.png)"))
    return build_media_proxy_chunks(text, [item], source="book.pdf", revision_id="r",
                                    profile_key="p", chunker_fingerprint="c",
                                    derived_generation_id="g", chunking=SPLIT_PROFILE)


def test_proxy_budget_split_is_deterministic_and_anchored_once():
    first = proxy_with_ocr()
    second = proxy_with_ocr()
    assert len(first) > 1
    assert [c.id for c in first] == [c.id for c in second]
    assert [c.content for c in first] == [c.content for c in second]
    assert all(c.metadata["media_occurrence_ids"] == ["a"] for c in first)
    assert all(c.metadata["occurrence_id"] == "a" for c in first)
    assert first[0].metadata["source_spans"] and first[0].metadata["anchor_available"] is True
    assert all(not c.metadata["source_spans"] and c.metadata["anchor_available"] is False for c in first[1:])
    assert [c.metadata["fragment_index"] for c in first] == list(range(len(first)))


def test_oversize_proxy_fragment_disables_embedding():
    chunks = proxy_with_ocr()
    oversize = [c for c in chunks if c.metadata.get("oversize")]
    assert oversize
    assert all(c.metadata.get("embedding_disabled") is True for c in oversize)


def test_chunking_absent_keeps_single_proxy():
    item = MediaOccurrence("a", "image", 0, caption="c", ocr=" ".join(["word"] * 80))
    assert len(build("", [item])) == 1
