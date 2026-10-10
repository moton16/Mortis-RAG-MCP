"""Optional local text extraction; native allocations are estimated, not isolated."""
from __future__ import annotations

import hashlib
import importlib
import io
import stat
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .models import PageSpan, ParseResult, ResourceLimits


class LocalUnsupported(ValueError):
    pass


#: 单卷正文进 E02 checkpoint 的有界上限（CHECKPOINT_MAX_BYTES=1MiB，给 spans/JSON 留余量）。
#: 超限 = 有界拒绝（提示降低 local_max_pages），绝不截断、不临时落盘。
VOLUME_TEXT_MAX_BYTES = 512 * 1024

#: 没有「已验证分页/分卷能力」的格式的明确拒绝语（不承诺不存在的分页）。
_VOLUME_REFUSAL = ("bounded volume splitting only supports local PDFs; "
                   "other formats refuse rather than promise paging")


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
            lower = name.lower()
            # PowerPoint's ordinary printer settings are inert binary metadata,
            # not an embedded OLE object. The text parser never consumes them.
            printer_settings = (path.suffix.lower() == ".pptx"
                                and lower.startswith("ppt/printersettings/")
                                and lower.endswith(".bin"))
            if lower.endswith((".zip", ".bin", ".vba", ".vbs", ".exe")) and not printer_settings:
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


# ---------------------------------------------------------------- 有界分卷（E09）


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_volume(texts, *, pages, limits: ResourceLimits, cancelled,
                      offset_chars: int) -> tuple[str, list[PageSpan]]:
    """单卷规范化：质量复检/预算与 `parse_local.add` 同合同；页码是全局 1-based。"""
    pieces: list[str] = []
    spans: list[PageSpan] = []
    total = offset_chars
    volume_bytes = 0
    for page, text in zip(pages, texts):
        if cancelled():
            raise LocalUnsupported("local parse cancelled")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n\n"
        if not _good_text(text):
            raise LocalUnsupported("local quality revalidation failed: empty/garbled page")
        encoded = len(text.encode("utf-8"))
        if total + encoded > limits.markdown_max_bytes:
            raise LocalUnsupported("local markdown budget exceeded")
        volume_bytes += encoded
        if volume_bytes > VOLUME_TEXT_MAX_BYTES:
            raise LocalUnsupported(
                "volume text exceeds the bounded checkpoint budget "
                f"({VOLUME_TEXT_MAX_BYTES} bytes); lower local_max_pages")
        spans.append(PageSpan(int(page), total, total + len(text)))
        pieces.append(text)
        total += len(text)
    return "".join(pieces), spans


def _walk_volumes(handle: Any, total: int, page_text, *, limits: ResourceLimits,
                  max_pages: int, cancelled, resume_map: dict, source_sha256: str,
                  checkpoint, backend: str, volume_fingerprint: str) -> ParseResult:
    """逐卷处理（同一时刻只有当前卷的文本在内存里），全局页偏移 + 逐卷 checkpoint。"""
    pieces: list[str] = []
    spans: list[PageSpan] = []
    resumed: list[int] = []
    completed: list[int] = []
    total_chars = 0
    for ordinal, start in enumerate(range(0, total, max_pages), start=1):
        if cancelled():
            raise LocalUnsupported("local volume parse cancelled")
        end = min(start + max_pages, total)
        prior = resume_map.get(ordinal)
        reusable = (isinstance(prior, dict) and prior.get("range") == [start, end]
                    and prior.get("source_sha256") == source_sha256
                    and prior.get("volume_fingerprint") == volume_fingerprint
                    and isinstance(prior.get("markdown"), str)
                    and prior.get("sha256") == _sha256_text(str(prior.get("markdown", ""))))
        if reusable:
            volume_md = str(prior["markdown"])
            volume_spans = [PageSpan(int(p), int(s), int(e))
                            for p, s, e in (prior.get("spans") or [])]
            resumed.append(ordinal)
        else:
            volume_md, volume_spans = _normalize_volume(
                [page_text(handle, index) for index in range(start, end)],
                pages=range(start + 1, end + 1), limits=limits, cancelled=cancelled,
                offset_chars=total_chars)
            if checkpoint is not None:
                checkpoint(ordinal, (start, end), {
                    "ordinal": ordinal, "kind": "document_pages", "range": [start, end],
                    "source_sha256": source_sha256,
                    "volume_fingerprint": volume_fingerprint,
                    "sha256": _sha256_text(volume_md), "markdown": volume_md,
                    "spans": [[span.page, span.char_start, span.char_end]
                              for span in volume_spans],
                })
        pieces.append(volume_md)
        spans.extend(volume_spans)
        total_chars += len(volume_md)
        completed.append(ordinal)
    markdown = "".join(pieces)
    if len(markdown.encode("utf-8")) > limits.markdown_max_bytes:
        raise LocalUnsupported("local markdown budget exceeded")
    return ParseResult(markdown, volume_fingerprint, "local", backend, quality="partial",
                       page_map=spans,
                       capabilities={"text": True, "page_map": bool(spans), "images": False,
                                     "tables": False, "formula": False,
                                     "layout_verified": False,
                                     "volumes_total": (total + max_pages - 1) // max_pages,
                                     "volumes_done": sorted(completed),
                                     "volumes_resumed": sorted(resumed),
                                     "volume_pages": int(max_pages)},
                       warnings=["local text volumes; no complete layout/table/formula coverage"])


def parse_local_volumes(path: Path, *, limits: ResourceLimits | None = None,
                        max_pages: int = 300, cancelled=lambda: False, resume=None,
                        source_sha256: str = "", checkpoint=None) -> ParseResult:
    """本地 PDF 的**有界内存分卷**解析（E09）。

    仅 PDF：本地提取天然支持按页区间读取（pymupdf/pypdf 都不落临时文件），分卷=
    按 `max_pages` 切页的有界处理，页码保持**全局** 1-based，字符区间随卷拼接偏移；
    `checkpoint(ordinal, (start, end), payload)` 逐卷回调（E02 子记录，恢复只补缺卷，
    绝不重复已完成卷）；`resume` 是逐卷 checkpoint 读回。其他格式没有已验证的页映射
    能力 → 明确有界拒绝（不临时落盘、不承诺分页）。
    """
    limits = limits or ResourceLimits()
    if path.suffix.lower() != ".pdf":
        raise LocalUnsupported(_VOLUME_REFUSAL)
    if int(max_pages) < 1:
        raise LocalUnsupported("volume size must be >= 1 page")
    backend, module = pdf_backend()
    volume_fingerprint = f"local-volumes-v1:{backend}:{int(max_pages)}"
    resume_map = dict(resume or {})
    if backend == "pymupdf":
        document = module.open(str(path))
        try:
            if document.is_encrypted:
                raise LocalUnsupported("encrypted PDF cannot be parsed locally")
            return _walk_volumes(document, len(document),
                                 lambda handle, index: handle[index].get_text("text"),
                                 limits=limits, max_pages=max_pages, cancelled=cancelled,
                                 resume_map=resume_map, source_sha256=source_sha256,
                                 checkpoint=checkpoint, backend=backend,
                                 volume_fingerprint=volume_fingerprint)
        finally:
            document.close()
    reader = module.PdfReader(str(path))
    if reader.is_encrypted:
        raise LocalUnsupported("encrypted PDF cannot be parsed locally")
    return _walk_volumes(reader, len(reader.pages),
                         lambda handle, index: handle.pages[index].extract_text() or "",
                         limits=limits, max_pages=max_pages, cancelled=cancelled,
                         resume_map=resume_map, source_sha256=source_sha256,
                         checkpoint=checkpoint, backend=backend,
                         volume_fingerprint=volume_fingerprint)
