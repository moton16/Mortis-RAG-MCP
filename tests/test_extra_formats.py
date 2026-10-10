"""Decode real small files; mandatory extras lane fails rather than skipping."""
import hashlib
import importlib
import io
import os
from types import SimpleNamespace

import pytest

from mortis_rag_mcp.ingest.local import parse_local
from mortis_rag_mcp._server.media_dispatch import _preview


def dependency(name):
    if os.getenv("MORTIS_REQUIRE_EXTRAS") == "1":
        return importlib.import_module(name)
    return pytest.importorskip(name)


@pytest.mark.parametrize("suffix", ["pdf", "docx", "pptx", "xlsx"])
def test_real_document_positive_decode_and_source_unchanged(tmp_path, suffix):
    path = tmp_path / f"positive.{suffix}"
    if suffix == "pdf":
        module = dependency("pymupdf")
        doc = module.open()
        doc.new_page().insert_text((36, 36), "alpha real PDF")
        doc.save(path)
        doc.close()
    elif suffix == "docx":
        doc = dependency("docx").Document()
        doc.add_paragraph("alpha real DOCX")
        doc.save(path)
    elif suffix == "pptx":
        module = dependency("pptx")
        doc = module.Presentation()
        slide = doc.slides.add_slide(doc.slide_layouts[6])
        slide.shapes.add_textbox(100, 100, 2000000, 1000000).text = "alpha real PPTX"
        doc.save(path)
    else:
        doc = dependency("openpyxl").Workbook()
        doc.active["A1"] = "alpha real XLSX"
        doc.save(path)
        doc.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = parse_local(path)
    assert "alpha" in result.markdown
    assert result.capabilities["text"] is True
    assert result.capabilities["images"] is False
    assert result.quality in {"partial", "fallback"}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert list(tmp_path.iterdir()) == [path]


def test_real_pillow_preview_is_decodable_and_bounded():
    module = dependency("PIL.Image")
    raw = io.BytesIO()
    module.new("RGB", (37, 19), "blue").save(raw, format="PNG")
    result = _preview(raw.getvalue(), SimpleNamespace(preview_max_edge=16, preview_max_bytes=10000))
    assert result is not None and result[1] == "image/jpeg"
    with module.open(io.BytesIO(result[0])) as image:
        image.load()
        assert max(image.size) <= 16
