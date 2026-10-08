"""T06：统一解析合同的结构化结果（C93）。

只验证「解析出来什么」：parser 指纹 / quality / capabilities / PageSpan /
MediaOccurrence / warnings / 字节统计；未知 schema → PARSE_PARTIAL，空产物 → 失败
（不允许 done），不编页、不伪造 OCR。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from mortis_rag_mcp.ingest.mineru import MineruClient, MineruError
from mortis_rag_mcp.ingest.models import (
    PARSE_EMPTY,
    PARSE_PARTIAL,
    PageSpan,
    ResourceLimits,
    parser_fingerprint,
)


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _run_v4(monkeypatch, tmp_path: Path, zip_bytes: bytes, *, api_key: str = "v4-token"):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        url = req.full_url
        if "file-urls/batch" in url:
            return {"code": 0, "data": {"batch_id": "b1", "file_urls": ["https://upload.example/x"]}}
        if "extract-results/batch/b1" in url:
            return {"code": 0, "data": {"extract_result": [{"state": "done",
                                                           "full_zip_url": "https://zip.example/z"}]}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", lambda *a, **kw: None)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_bytes", lambda *a, **kw: zip_bytes)
    client = MineruClient(api_key=api_key)
    return client.parse_structured(doc, poll_interval=0.001, poll_timeout=5.0)


def test_structured_result_with_page_map(monkeypatch, tmp_path: Path):
    markdown = "Alpha\nBeta\nGamma\n"
    blocks = [
        {"type": "text", "page_idx": 0, "text": "Alpha"},
        {"type": "text", "page_idx": 1, "text": "Beta"},
        {"type": "text", "page_idx": 1, "text": "Gamma"},
    ]
    zip_bytes = _make_zip({
        "full.md": markdown.encode("utf-8"),
        "content_list.json": json.dumps(blocks).encode("utf-8"),
        "images/fig.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 24,
    })
    result = _run_v4(monkeypatch, tmp_path, zip_bytes)

    assert result.quality == "full"
    assert result.capability("page_map") is True
    assert result.capability("structured_json") is True
    assert result.capability("images") is True
    assert result.page_map == [PageSpan(page=1, char_start=0, char_end=6),
                               PageSpan(page=2, char_start=6, char_end=17)]
    assert len(result.media) == 1
    occurrence = result.media[0]
    assert occurrence.kind == "image"
    assert occurrence.page is None          # 归档不提供页信息：不猜页
    assert occurrence.byte_size == 32
    assert result.markdown_bytes == len(result.markdown.encode("utf-8"))
    assert result.duration_ms >= 0.0


def test_unknown_json_schema_is_partial_not_done(monkeypatch, tmp_path: Path):
    zip_bytes = _make_zip({
        "full.md": b"# Title\nbody\n",
        "mystery.json": json.dumps({"totally": "unknown"}).encode("utf-8"),
    })
    result = _run_v4(monkeypatch, tmp_path, zip_bytes)
    assert result.quality == "partial"
    assert PARSE_PARTIAL in result.warnings
    assert any("unrecognized structure json schema" in w for w in result.warnings)
    assert result.capability("structured_json") is False


def test_bad_page_index_does_not_guess_pages(monkeypatch, tmp_path: Path):
    blocks = [{"type": "text", "page_idx": -1, "text": "Alpha"}]
    zip_bytes = _make_zip({
        "full.md": b"Alpha\n",
        "content_list.json": json.dumps(blocks).encode("utf-8"),
    })
    result = _run_v4(monkeypatch, tmp_path, zip_bytes)
    assert result.page_map == []
    assert result.capability("page_map") is False
    assert any("page_map unavailable" in w for w in result.warnings)


def test_block_text_missing_from_markdown_keeps_capability_false(monkeypatch, tmp_path: Path):
    blocks = [{"type": "text", "page_idx": 0, "text": "not-in-markdown"}]
    zip_bytes = _make_zip({
        "full.md": b"Alpha\n",
        "content_list.json": json.dumps(blocks).encode("utf-8"),
    })
    result = _run_v4(monkeypatch, tmp_path, zip_bytes)
    assert result.page_map == []
    assert result.capability("page_map") is False


def test_empty_result_fails_instead_of_done(monkeypatch, tmp_path: Path):
    zip_bytes = _make_zip({"full.md": b"   \n\n"})
    with pytest.raises(MineruError) as exc_info:
        _run_v4(monkeypatch, tmp_path, zip_bytes)
    assert exc_info.value.code_str == PARSE_EMPTY
    assert exc_info.value.retryable is False


def test_agent_channel_is_light_and_does_not_fake_ocr(monkeypatch, tmp_path: Path):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        url = req.full_url
        if "parse/file" in url:
            return {"code": 0, "data": {"task_id": "t1", "file_url": "https://upload.example/x"}}
        if "parse/t1" in url:
            return {"code": 0, "data": {"state": "done", "markdown_url": "https://md.example/m"}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", lambda *a, **kw: None)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_bytes",
                        lambda *a, **kw: b"# Agent\n")

    result = MineruClient(api_key="").parse_structured(doc, poll_interval=0.001, poll_timeout=5.0)
    assert result.channel == "agent"
    assert result.model == "pipeline-light"
    assert result.quality == "light"
    assert result.capability("page_map") is False
    assert result.capability("images") is False
    assert result.capability("ocr") is False   # 未开启 OCR 时不得声称有 OCR
    assert result.page_map == []


def test_parser_fingerprint_is_stable_and_sensitive():
    base = dict(adapter="a", channel="v4", model="vlm", language="ch", is_ocr=False,
                enable_table=True, enable_formula=True)
    assert parser_fingerprint(**base) == parser_fingerprint(**base)
    assert parser_fingerprint(**base) != parser_fingerprint(**{**base, "model": "pipeline"})
    assert parser_fingerprint(**base) != parser_fingerprint(**{**base, "is_ocr": True})
    assert parser_fingerprint(**base) != parser_fingerprint(**{**base, "language": "en"})


def test_limits_never_disable_safety_with_zero_or_nan():
    limits = ResourceLimits(markdown_max_bytes=0, media_max_bytes=-5,
                            extracted_max_bytes=float("nan"), max_members=0)
    defaults = ResourceLimits()
    assert limits.markdown_max_bytes == defaults.markdown_max_bytes
    assert limits.media_max_bytes == defaults.media_max_bytes
    assert limits.extracted_max_bytes == defaults.extracted_max_bytes
    assert limits.max_members == defaults.max_members


@pytest.mark.parametrize('prefix,newline', [('', '\r\n'), ('\ufeff', '\r\n'), ('\ufeff', '\n')])
def test_page_map_uses_normalized_offsets(monkeypatch, tmp_path, prefix, newline):
    blocks = [{'page_idx': 0, 'text': 'Alpha'}, {'page_idx': 1, 'text': 'Beta'}]
    archive = _make_zip({'full.md': (prefix + 'Alpha' + newline + 'Beta' + newline).encode(),
                         'content_list.json': json.dumps(blocks).encode()})
    result = _run_v4(monkeypatch, tmp_path, archive)
    assert result.markdown == 'Alpha\nBeta\n'
    assert result.page_map == [PageSpan(page=1, char_start=0, char_end=6),
                               PageSpan(page=2, char_start=6, char_end=11)]
    assert result.markdown[result.page_map[1].char_start:result.page_map[1].char_end] == 'Beta\n'


def test_mixed_valid_and_unknown_json_is_partial(monkeypatch, tmp_path):
    archive = _make_zip({'full.md': b'Alpha\n', 'a.json': b'{"page_count":1}',
                         'z.json': b'{"unknown":true}'})
    result = _run_v4(monkeypatch, tmp_path, archive)
    assert result.quality == 'partial'
    assert not result.capability('structured_json')
    assert PARSE_PARTIAL in result.warnings
