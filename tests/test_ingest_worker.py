import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mortis_rag_mcp.ingest.mineru import MineruError, ParsedDocument
from mortis_rag_mcp.ingest.worker import IngestConfig, IngestManager


def test_submit_disabled_error(tmp_path: Path):
    cfg = IngestConfig(enabled=False)
    mgr = IngestManager(tmp_path, cfg)
    with pytest.raises(ValueError) as exc_info:
        mgr.submit(["sample.pdf"])
    err_msg = str(exc_info.value)
    assert "ingest disabled" in err_msg
    assert "enabled = true" in err_msg


def test_submit_worker_done_and_frontmatter(tmp_path: Path, monkeypatch):
    cfg = IngestConfig(enabled=True, api_key="v4-test-key", output_dirname=".mortis-parsed")
    mgr = IngestManager(tmp_path, cfg)

    # Prepare vault structure
    sub = tmp_path / "course"
    sub.mkdir(parents=True)
    pdf = sub / "digital_logic.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy digital logic content")

    # Mock MineruClient.parse
    mock_doc = ParsedDocument(
        markdown="# Digital Logic\n\nTTL gates explanation.",
        images={},
        channel="v4",
        model="vlm",
    )
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru.MineruClient.parse", lambda *a, **kw: mock_doc)

    # Submit
    res = mgr.submit(["course/digital_logic.pdf"])
    assert res["submitted"] == 1
    job_id = res["jobs"][0]["job_id"]

    # Wait for background thread
    assert mgr._worker is not None
    mgr._worker.join(timeout=5.0)

    # Check status
    st = mgr.status(job_id)
    job = st["job"]
    assert job["state"] == "done"
    assert job["channel"] == "v4"
    assert job["parse_quality"] == "full"
    assert job["finished_at"] is not None

    # Verify output file
    out_file = tmp_path / ".mortis-parsed" / "course" / "digital_logic.md"
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")

    # Verify 5 frontmatter fields
    assert 'source_pdf: "course/digital_logic.pdf"' in content
    assert f"source_sha256: {job['sha256']}" in content
    assert "parsed_by: v4" in content
    assert "parsed_at: " in content
    assert "parse_quality: full" in content
    assert "TTL gates explanation." in content


def test_sha256_idempotency_and_change_detection(tmp_path: Path, monkeypatch):
    cfg = IngestConfig(enabled=True, output_dirname=".mortis-parsed")
    mgr = IngestManager(tmp_path, cfg)

    doc = tmp_path / "note.pdf"
    doc.write_bytes(b"initial version")

    # Mock parse
    mock_doc = ParsedDocument(markdown="Parsed initial", images={}, channel="agent", model="pipeline-light")
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru.MineruClient.parse", lambda *a, **kw: mock_doc)

    # Initial scan
    pending = mgr.scan_pending()
    assert len(pending) == 1
    assert pending[0]["source"] == "note.pdf"
    assert pending[0]["reason"] == "new"

    # Submit & wait
    mgr.submit(["note.pdf"])
    mgr._worker.join(timeout=5.0)

    # Idempotency check: same sha256 -> no longer pending
    pending_after = mgr.scan_pending()
    assert len(pending_after) == 0

    # Modify source file
    doc.write_bytes(b"modified second version")

    # Changed check: pending again with reason='changed'
    pending_changed = mgr.scan_pending()
    assert len(pending_changed) == 1
    assert pending_changed[0]["source"] == "note.pdf"
    assert pending_changed[0]["reason"] == "changed"


def test_single_job_failure_isolation(tmp_path: Path, monkeypatch):
    cfg = IngestConfig(enabled=True, pymupdf_fallback=False, output_dirname=".mortis-parsed")
    mgr = IngestManager(tmp_path, cfg)

    bad_file = tmp_path / "bad.pdf"
    bad_file.write_bytes(b"bad content")
    good_file = tmp_path / "good.pdf"
    good_file.write_bytes(b"good content")

    def fake_parse(self, path, **kwargs):
        if path.name == "bad.pdf":
            raise MineruError("simulated parse crash", retryable=False)
        return ParsedDocument(markdown="# Good", images={}, channel="agent", model="light")

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru.MineruClient.parse", fake_parse)

    res = mgr.submit(["bad.pdf", "good.pdf"])
    assert res["submitted"] == 2
    mgr._worker.join(timeout=5.0)

    # Verify bad failed but good succeeded
    st_all = mgr.status()
    assert st_all["summary"]["failed"] == 1
    assert st_all["summary"]["done"] == 1

    jobs = {j["source"]: j for j in st_all["jobs"]}
    assert jobs["bad.pdf"]["state"] == "failed"
    assert "simulated parse crash" in jobs["bad.pdf"]["error"]

    assert jobs["good.pdf"]["state"] == "done"
    assert (tmp_path / ".mortis-parsed" / "good.md").exists()


def test_pymupdf_fallback(tmp_path: Path, monkeypatch):
    cfg = IngestConfig(enabled=True, pymupdf_fallback=True, output_dirname=".mortis-parsed")
    mgr = IngestManager(tmp_path, cfg)

    doc = tmp_path / "fallback.pdf"
    doc.write_bytes(b"fallback pdf content")

    def fake_parse(self, path, **kwargs):
        raise MineruError("cloud permanent failure", retryable=False)

    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru.MineruClient.parse", fake_parse)
    monkeypatch.setattr(mgr, "_pymupdf_fallback", lambda path: "Fallback local text extraction")

    mgr.submit(["fallback.pdf"])
    mgr._worker.join(timeout=5.0)

    st = mgr.status()
    job = st["jobs"][0]
    assert job["state"] == "done"
    assert job["channel"] == "pymupdf"
    assert job["parse_quality"] == "fallback"
    assert "cloud failed, local fallback used" in job["error"]

    out_file = tmp_path / ".mortis-parsed" / "fallback.md"
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "parsed_by: pymupdf" in content
    assert "parse_quality: fallback" in content
    assert "Fallback local text extraction" in content


def test_v4_images_extraction_and_markdown_rewrite(tmp_path: Path, monkeypatch):
    cfg = IngestConfig(enabled=True, api_key="v4-token", output_dirname=".mortis-parsed")
    mgr = IngestManager(tmp_path, cfg)

    doc = tmp_path / "with_images.pdf"
    doc.write_bytes(b"image pdf")

    mock_doc = ParsedDocument(
        markdown="# Pictures\n\n![diagram](images/fig1.png)\n",
        images={"images/fig1.png": b"PNG_BYTE_STREAM"},
        channel="v4",
        model="vlm",
    )
    monkeypatch.setattr("mortis_rag_mcp.ingest.mineru.MineruClient.parse", lambda *a, **kw: mock_doc)

    mgr.submit(["with_images.pdf"])
    mgr._worker.join(timeout=5.0)

    # Output md
    out_file = tmp_path / ".mortis-parsed" / "with_images.md"
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "![diagram](with_images.assets/fig1.png)" in content

    # Asset file
    asset_file = tmp_path / ".mortis-parsed" / "with_images.assets" / "fig1.png"
    assert asset_file.exists()
    assert asset_file.read_bytes() == b"PNG_BYTE_STREAM"
