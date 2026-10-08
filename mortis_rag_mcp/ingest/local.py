"""Optional local text extraction; native allocations are estimated, not isolated."""
from __future__ import annotations

import importlib
import io
import stat
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .models import PageSpan, ParseResult, ResourceLimits


class LocalUnsupported(ValueError):
    pass


def _module(name: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise LocalUnsupported(f"optional docs dependency unavailable: {name}") from exc


def validate_source(path: Path, limits: ResourceLimits) -> None:
    if path.stat().st_size > min(limits.archive_max_bytes, limits.memory_budget_bytes // 4):
        raise LocalUnsupported("local source exceeds allocation admission budget; split manually")
    with path.open("rb") as stream:
        magic = stream.read(8)
    suffix = path.suffix.lower()
    expected = {".pdf": b"%PDF-", ".docx": b"PK\x03\x04", ".pptx": b"PK\x03\x04",
                ".xlsx": b"PK\x03\x04", ".doc": b"\xd0\xcf\x11\xe0", ".ppt": b"\xd0\xcf\x11\xe0",
                ".xls": b"\xd0\xcf\x11\xe0"}
    if suffix not in expected or not magic.startswith(expected[suffix]):
        raise LocalUnsupported("document suffix/magic mismatch or unknown format")


def office_admission(path: Path, limits: ResourceLimits) -> bytes:
    from .mineru import _inspect_central_directory, _read_member_bounded, _validate_member_name

    validate_source(path, limits)
    data = path.read_bytes()
    _inspect_central_directory(data, limits)
    names: set[str] = set()
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            name = _validate_member_name(info.filename)
            if name in names:
                raise LocalUnsupported("duplicate Office archive member")
            names.add(name)
            mode = info.external_attr >> 16
            if info.flag_bits & 1 or stat.S_ISLNK(mode) or info.compress_type not in (0, 8):
                raise LocalUnsupported("unsafe Office archive member")
            if name.lower().endswith((".zip", ".bin", ".vba", ".vbs", ".exe")):
                raise LocalUnsupported("embedded objects/macros require unsupported capability")
            if info.is_dir():
                continue
            total += info.file_size
            if total > min(limits.extracted_max_bytes, limits.memory_budget_bytes // 8):
                raise LocalUnsupported("Office native expansion estimate exceeds admission budget")
            payload = _read_member_bounded(archive, info, limit=limits.json_max_bytes,
                                           remaining=limits.extracted_max_bytes)
            if name.lower().endswith((".xml", ".rels")):
                upper = payload.upper()
                if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                    raise LocalUnsupported("Office XML entities/DTD forbidden")
                try:
                    ElementTree.fromstring(payload)
                except ElementTree.ParseError as exc:
                    raise LocalUnsupported("invalid Office XML") from exc
        required = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml",
                    ".xlsx": "xl/workbook.xml"}[path.suffix.lower()]
        if required not in names or "[Content_Types].xml" not in names:
            raise LocalUnsupported("Office package does not match suffix")
    return data


def pdf_backend():
    try:
        return "pymupdf", _module("pymupdf")
    except LocalUnsupported:
        return "pypdf", _module("pypdf")


def _good_text(text: str) -> bool:
    chars = [c for c in text if not c.isspace()]
    return bool(chars) and sum(c.isprintable() and c != "\ufffd" for c in chars) / len(chars) >= .98


def parse_local(path: Path, *, limits: ResourceLimits | None = None, max_pages: int = 300,
                cancelled=lambda: False) -> ParseResult:
    limits = limits or ResourceLimits()
    validate_source(path, limits)
    suffix = path.suffix.lower()
    pieces: list[str] = []
    spans: list[PageSpan] = []
    total = 0

    def add(text: str, page: int | None = None):
        nonlocal total
        if cancelled():
            raise LocalUnsupported("local parse cancelled")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n\n"
        if not _good_text(text):
            raise LocalUnsupported("local quality revalidation failed: empty/garbled page")
        if sum(len(p.encode("utf-8")) for p in pieces) + len(text.encode("utf-8")) > limits.markdown_max_bytes:
            raise LocalUnsupported("local markdown budget exceeded")
        if page is not None:
            spans.append(PageSpan(page, total, total + len(text)))
        pieces.append(text)
        total += len(text)

    backend = suffix.lstrip(".")
    if suffix == ".pdf":
        backend, module = pdf_backend()
        if backend == "pymupdf":
            with module.open(path) as document:
                if document.is_encrypted or len(document) > max_pages:
                    raise LocalUnsupported("encrypted PDF or page limit exceeded; split manually")
                for page in document:
                    add(page.get_text("text"), page.number + 1)
        else:
            reader = module.PdfReader(str(path))
            if reader.is_encrypted or len(reader.pages) > max_pages:
                raise LocalUnsupported("encrypted PDF or page limit exceeded; split manually")
            for number, page in enumerate(reader.pages, 1):
                add(page.extract_text() or "", number)
    elif suffix in {".docx", ".pptx", ".xlsx"}:
        data = office_admission(path, limits)
        if suffix == ".docx":
            document = _module("docx").Document(io.BytesIO(data))
            add("\n".join(p.text for p in document.paragraphs))
        elif suffix == ".pptx":
            document = _module("pptx").Presentation(io.BytesIO(data))
            if len(document.slides) > max_pages:
                raise LocalUnsupported("slide limit exceeded")
            for number, slide in enumerate(document.slides, 1):
                add("\n".join(shape.text for shape in slide.shapes if shape.has_text_frame), number)
        else:
            workbook = _module("openpyxl").load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            try:
                cells = 0
                for sheet in workbook:
                    lines = [f"# Sheet: {sheet.title}"]
                    for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                        cells += len(row)
                        if cells > 100000 or cancelled():
                            raise LocalUnsupported("spreadsheet cell budget exceeded or cancelled")
                        lines.append(f"{row_number}: " + " | ".join("" if v is None else str(v) for v in row))
                    add("\n".join(lines))
            finally:
                workbook.close()
    else:
        raise LocalUnsupported("legacy Office binaries require a verified external parser")
    return ParseResult("".join(pieces), f"local-text-v1:{backend}", "local", backend,
                       quality="partial" if suffix != ".pdf" else "fallback", page_map=spans,
                       capabilities={"text": True, "page_map": bool(spans), "images": False,
                                     "tables": False, "formula": False, "layout_verified": False},
                       warnings=["local text only; no complete layout/table/formula coverage"])
