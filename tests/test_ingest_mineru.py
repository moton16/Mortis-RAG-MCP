import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mortis_rag_mcp.ingest.mineru import (
    MineruClient,
    MineruError,
    ParsedDocument,
    _http_bytes,
    _http_json,
    _put_upload,
)


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------- Channel selection


def test_channel_selection_with_token(tmp_path: Path):
    client = MineruClient(api_key="test-v4-token")

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 test")
    assert client.channel_for(pdf_file) == "v4"

    docx_file = tmp_path / "doc.docx"
    docx_file.write_bytes(b"PK test")
    assert client.channel_for(docx_file) == "v4"

    doc_file = tmp_path / "doc.doc"
    doc_file.write_bytes(b"doc test")
    assert client.channel_for(doc_file) == "v4"

    # Unsupported ext for v4
    txt_file = tmp_path / "doc.txt"
    txt_file.write_text("plain text", encoding="utf-8")
    with pytest.raises(MineruError) as exc_info:
        client.channel_for(txt_file)
    assert exc_info.value.code == -60002
    assert "v4 unsupported ext" in str(exc_info.value)

    # Exceeds 200MB limit
    big_file = tmp_path / "big.pdf"
    big_file.write_bytes(b"x")
    # monkeypatch st_size
    orig_stat = big_file.stat

    class FakeStat:
        st_size = 201 * 1024 * 1024

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "stat", lambda self: FakeStat() if self == big_file else orig_stat(self))
    try:
        with pytest.raises(MineruError) as exc_info:
            client.channel_for(big_file)
        assert exc_info.value.code == -60005
        assert "exceeds v4 200MB limit" in str(exc_info.value)
    finally:
        monkeypatch.undo()


def test_channel_selection_without_token(tmp_path: Path):
    client = MineruClient(api_key="")

    pdf_file = tmp_path / "small.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 test")
    assert client.channel_for(pdf_file) == "agent"

    docx_file = tmp_path / "small.docx"
    docx_file.write_bytes(b"PK test")
    assert client.channel_for(docx_file) == "agent"

    # doc is not supported by agent channel
    doc_file = tmp_path / "small.doc"
    doc_file.write_bytes(b"doc test")
    with pytest.raises(MineruError) as exc_info:
        client.channel_for(doc_file)
    assert exc_info.value.code == -30002
    assert "agent channel unsupported ext" in str(exc_info.value)

    # Exceeds 10MB limit
    big_file = tmp_path / "over10mb.pdf"
    big_file.write_bytes(b"x")
    orig_stat = big_file.stat

    class FakeStat:
        st_size = 11 * 1024 * 1024

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "stat", lambda self: FakeStat() if self == big_file else orig_stat(self))
    try:
        with pytest.raises(MineruError) as exc_info:
            client.channel_for(big_file)
        assert exc_info.value.code == -30001
        assert "exceeds agent 10MB limit" in str(exc_info.value)
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------- Error handling & retryability


def test_http_json_429_rate_limit(monkeypatch):
    headers = {"Retry-After": "45"}

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("http://example.com", 429, "Too Many Requests", headers, io.BytesIO(b"{}"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    req = urllib.request.Request("http://example.com")
    with pytest.raises(MineruError) as exc_info:
        _http_json(req, timeout=5.0)

    err = exc_info.value
    assert err.http_status == 429
    assert err.retryable is True
    assert "45" in str(err)


def test_http_json_network_error(monkeypatch):
    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    req = urllib.request.Request("http://example.com")
    with pytest.raises(MineruError) as exc_info:
        _http_json(req, timeout=5.0)

    assert exc_info.value.retryable is True
    assert "network error" in str(exc_info.value)


def test_put_upload_status(monkeypatch):
    # 200 success
    resp_200 = MagicMock()
    resp_200.__enter__.return_value.status = 200
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: resp_200)
    _put_upload("http://example.com/put", b"payload", timeout=5.0)

    # 500 error -> retryable
    def fake_500(*a, **kw):
        raise urllib.error.HTTPError("http://example.com", 500, "Server Error", {}, io.BytesIO(b"err"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_500)
    with pytest.raises(MineruError) as exc_info:
        _put_upload("http://example.com/put", b"payload", timeout=5.0)
    assert exc_info.value.retryable is True
    assert exc_info.value.http_status == 500

    # 400 error -> not retryable
    def fake_400(*a, **kw):
        raise urllib.error.HTTPError("http://example.com", 400, "Bad Request", {}, io.BytesIO(b"err"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_400)
    with pytest.raises(MineruError) as exc_info:
        _put_upload("http://example.com/put", b"payload", timeout=5.0)
    assert exc_info.value.retryable is False
    assert exc_info.value.http_status == 400


# ---------------------------------------------------------------- Zip Extraction


def test_extract_zip_success():
    zip_bytes = _make_zip({
        "full.md": "# Chapter 1\nContent".encode("utf-8"),
        "images/fig1.png": b"\x89PNG\r\n\x1a\nfake",
        "images/sub/fig2.jpg": b"\xff\xd8\xfffake",
        "result.json": b'{"some": "data"}',
    })
    md, images = MineruClient._extract_zip(zip_bytes)
    assert md == "# Chapter 1\nContent"
    assert "images/fig1.png" in images
    assert images["images/fig1.png"] == b"\x89PNG\r\n\x1a\nfake"
    assert "images/sub/fig2.jpg" in images
    assert "result.json" not in images


def test_extract_zip_missing_full_md():
    zip_bytes = _make_zip({
        "images/fig1.png": b"data",
    })
    with pytest.raises(MineruError) as exc_info:
        MineruClient._extract_zip(zip_bytes)
    assert "full.md not found" in str(exc_info.value)
    assert exc_info.value.retryable is False


# ---------------------------------------------------------------- v4 Polling paths


def test_v4_parse_done(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="token-v4", model_version="vlm")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    calls = {"post": 0, "put": 0, "poll": 0, "get_zip": 0}
    zip_data = _make_zip({"full.md": "# V4 Content".encode("utf-8"), "images/pic.png": b"pic"})

    def fake_http_json(req, timeout):
        url = req.full_url
        if "file-urls/batch" in url:
            calls["post"] += 1
            return {"code": 0, "data": {"batch_id": "b123", "file_urls": ["http://upload.url"]}}
        if "extract-results/batch/b123" in url:
            calls["poll"] += 1
            if calls["poll"] == 1:
                return {"code": 0, "data": {"extract_result": [{"state": "running"}]}}
            return {"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "http://zip.url"}]}}
        raise ValueError(f"unexpected url: {url}")

    def fake_put(url, payload, timeout):
        calls["put"] += 1

    def fake_http_bytes(url, timeout):
        calls["get_zip"] += 1
        return zip_data

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", fake_put)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_bytes", fake_http_bytes)

    res = client.parse(doc_path, poll_interval=0.01, poll_timeout=5.0)
    assert isinstance(res, ParsedDocument)
    assert res.channel == "v4"
    assert res.model == "vlm"
    assert res.markdown == "# V4 Content"
    assert "images/pic.png" in res.images
    assert calls["post"] == 1
    assert calls["put"] == 1
    assert calls["poll"] == 2
    assert calls["get_zip"] == 1


def test_v4_fatal_code_no_retry(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="token-v4")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        return {"code": -60018, "msg": "Daily quota exceeded"}

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)

    with pytest.raises(MineruError) as exc_info:
        client.parse(doc_path, poll_interval=0.01, poll_timeout=5.0)

    assert exc_info.value.code == -60018
    assert exc_info.value.retryable is False
    assert "quota" in str(exc_info.value).lower()


def test_v4_parse_failed(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="token-v4")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        url = req.full_url
        if "file-urls/batch" in url:
            return {"code": 0, "data": {"batch_id": "b123", "file_urls": ["http://upload.url"]}}
        if "extract-results/batch/b123" in url:
            return {"code": 0, "data": {"extract_result": [{"state": "failed", "err_msg": "corrupt pdf"}]}}
        raise ValueError(f"unexpected url: {url}")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", lambda *a, **kw: None)

    with pytest.raises(MineruError) as exc_info:
        client.parse(doc_path, poll_interval=0.01, poll_timeout=5.0)

    assert "v4 parse failed: corrupt pdf" in str(exc_info.value)
    assert exc_info.value.retryable is False


def test_v4_parse_timeout(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="token-v4")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        url = req.full_url
        if "file-urls/batch" in url:
            return {"code": 0, "data": {"batch_id": "b123", "file_urls": ["http://upload.url"]}}
        if "extract-results/batch/b123" in url:
            return {"code": 0, "data": {"extract_result": [{"state": "pending"}]}}
        raise ValueError(f"unexpected url: {url}")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", lambda *a, **kw: None)

    with pytest.raises(MineruError) as exc_info:
        client.parse(doc_path, poll_interval=0.005, poll_timeout=0.02)

    assert "timeout" in str(exc_info.value)
    assert exc_info.value.retryable is True


# ---------------------------------------------------------------- Agent Polling paths


def test_agent_parse_done(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    calls = {"post": 0, "put": 0, "poll": 0, "get_md": 0}

    def fake_http_json(req, timeout):
        url = req.full_url
        if "parse/file" in url:
            calls["post"] += 1
            return {"code": 0, "data": {"task_id": "t999", "file_url": "http://upload.url"}}
        if "parse/t999" in url:
            calls["poll"] += 1
            if calls["poll"] == 1:
                return {"code": 0, "data": {"state": "uploading"}}
            return {"code": 0, "data": {"state": "done", "markdown_url": "http://md.url"}}
        raise ValueError(f"unexpected url: {url}")

    def fake_put(url, payload, timeout):
        calls["put"] += 1

    def fake_http_bytes(url, timeout):
        calls["get_md"] += 1
        return "# Agent Markdown".encode("utf-8")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", fake_put)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_bytes", fake_http_bytes)

    res = client.parse(doc_path, poll_interval=0.01, poll_timeout=5.0)
    assert isinstance(res, ParsedDocument)
    assert res.channel == "agent"
    assert res.model == "pipeline-light"
    assert res.markdown == "# Agent Markdown"
    assert res.images == {}
    assert calls["post"] == 1
    assert calls["put"] == 1
    assert calls["poll"] == 2
    assert calls["get_md"] == 1


def test_agent_fatal_code_no_retry(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        return {"code": -30001, "msg": "file too large"}

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)

    with pytest.raises(MineruError) as exc_info:
        client.parse(doc_path, poll_interval=0.01, poll_timeout=5.0)

    assert exc_info.value.code == -30001
    assert exc_info.value.retryable is False


def test_agent_parse_failed(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        url = req.full_url
        if "parse/file" in url:
            return {"code": 0, "data": {"task_id": "t999", "file_url": "http://upload.url"}}
        if "parse/t999" in url:
            return {"code": 0, "data": {"state": "failed", "err_msg": "conversion error", "err_code": -30003}}
        raise ValueError(f"unexpected url: {url}")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", lambda *a, **kw: None)

    with pytest.raises(MineruError) as exc_info:
        client.parse(doc_path, poll_interval=0.01, poll_timeout=5.0)

    assert "agent parse failed: conversion error" in str(exc_info.value)
    assert exc_info.value.code == -30003
    assert exc_info.value.retryable is False


def test_agent_parse_timeout(monkeypatch, tmp_path: Path):
    client = MineruClient(api_key="")
    doc_path = tmp_path / "test.pdf"
    doc_path.write_bytes(b"%PDF test")

    def fake_http_json(req, timeout):
        url = req.full_url
        if "parse/file" in url:
            return {"code": 0, "data": {"task_id": "t999", "file_url": "http://upload.url"}}
        if "parse/t999" in url:
            return {"code": 0, "data": {"state": "running"}}
        raise ValueError(f"unexpected url: {url}")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._http_json", fake_http_json)
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru._put_upload", lambda *a, **kw: None)

    with pytest.raises(MineruError) as exc_info:
        client.parse(doc_path, poll_interval=0.005, poll_timeout=0.02)

    assert "timeout" in str(exc_info.value)
    assert exc_info.value.retryable is True
