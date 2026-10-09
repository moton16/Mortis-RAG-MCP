"""独立图片源（PNG/JPEG/WebP）的薄输入适配（E09）。

范围：**用户显式提交**的图片源 → 有界 blob + 单项 occurrence，经既有 stage/CAS 发布，
交 E08 同一 sync 链消费（本模块不建第二套存储/embedding）。

明确不做：自动后台全库图片扫描（沿用既有 ingest 开关与既有 scan 后缀集合）、OCR、
LLM caption、外链下载、改写原文件。没有正文锚点就如实声明——宽高是 `width/height`，
**不是**字符位置，绝不写进 anchor。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import ParseResult, ResourceLimits, image_header_size, normalize_markdown

#: 显式提交支持的三类图片容器（与 E08 已发布 occurrence 的读取面一致）。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

#: 稳定错误码：该出口不接受此格式/未完成的能力。
IMAGE_ROUTE_UNSUPPORTED = "IMAGE_ROUTE_UNSUPPORTED"


class ImageUnsupported(ValueError):
    pass


_MAGIC: dict[str, Any] = {
    ".png": lambda head: head.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda head: head.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda head: head.startswith(b"\xff\xd8\xff"),
    ".webp": lambda head: len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP",
}
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp"}
#: 只读头部即可解析尺寸的容器（header 解析失败即视为容器损坏，不静默放过）。
_MEASURABLE = {".png", ".jpg", ".jpeg"}
_HEADER_WINDOW = 256 * 1024


def image_mime(path: Path) -> str:
    """按真实容器头判 MIME；后缀/内容不符或未知后缀 → 明确拒绝（不猜）。"""
    suffix = path.suffix.lower()
    check = _MAGIC.get(suffix)
    if check is None:
        raise ImageUnsupported(f"{IMAGE_ROUTE_UNSUPPORTED}: unsupported image suffix {suffix!r}")
    with path.open("rb") as stream:
        head = stream.read(16)
    if not check(head):
        raise ImageUnsupported(f"{IMAGE_ROUTE_UNSUPPORTED}: image suffix/magic mismatch: {path.name}")
    return _MIME[suffix]


def validate_image_source(path: Path, limits: ResourceLimits) -> dict[str, Any]:
    """显式图片源的准入：后缀/真实 magic、字节上限、像素上限（只读头部，不解码像素）。"""
    mime = image_mime(path)
    size = int(path.stat().st_size)
    if size <= 0:
        raise ImageUnsupported("empty image source")
    if size > limits.media_max_bytes:
        raise ImageUnsupported("image source exceeds media byte budget")
    with path.open("rb") as stream:
        head = stream.read(min(size, _HEADER_WINDOW))
    dimensions = image_header_size(head)
    if dimensions is None:
        if path.suffix.lower() in _MEASURABLE:
            raise ImageUnsupported("image header does not declare measurable dimensions")
        dimensions = (0, 0)  # 未识别容器（如 WebP）：不因为「不认识」拒绝合法格式
    width, height = int(dimensions[0]), int(dimensions[1])
    if width and height and width * height > limits.max_image_pixels:
        raise ImageUnsupported("image exceeds pixel budget")
    return {"mime_type": mime, "width": width, "height": height, "byte_size": size,
            "sha256": hashlib.sha256(head).hexdigest()}


def parse_image(path: Path, *, limits: ResourceLimits | None = None, sink: Any = None,
                title: str = "", description: str = "", ordinal: int = 1) -> ParseResult:
    """显式提交的图片源 → 单项有界 occurrence + 只有用户事实的代理正文。

    正文**不含** OCR/模型推断内容；`line_basis="user_metadata"` 明确声明这一点，因此
    不得宣称「图片里的文字可被检索」。文件名是真实事实，可用作默认标题。
    """
    limits = limits or ResourceLimits()
    info = validate_image_source(path, limits)
    payload = path.read_bytes()
    name = path.name
    caption = str(description or title or path.stem).strip()
    heading = str(title or path.stem).strip() or name
    lines = [f"# {heading}", ""]
    if description:
        lines.extend([str(description).strip(), ""])
    lines.extend([f"- source: {name}", f"- mime: {info['mime_type']}"])
    if info["width"] and info["height"]:
        lines.append(f"- dimensions: {info['width']}x{info['height']}")
    markdown = normalize_markdown("\n".join(lines))
    if len(markdown.encode("utf-8")) > limits.markdown_max_bytes:
        raise ImageUnsupported("image proxy markdown budget exceeded")
    occurrences: list[dict[str, Any]] = []
    if sink is not None:
        occurrence_id = sink.add(
            name=f"source{path.suffix.lower()}", data=payload, kind="image", ordinal=ordinal,
            mime_type=info["mime_type"], caption=caption,
            width=info["width"] or None, height=info["height"] or None,
            metadata={"source_format": info["mime_type"], "source_sha256": info["sha256"],
                      "line_basis": "user_metadata", "ocr": False},
        )
        occurrences.append({"occurrence_id": str(occurrence_id or ""), "ordinal": int(ordinal)})
    warnings = ["no OCR/caption inference: image text is not searchable"]
    if not info["width"] or not info["height"]:
        warnings.append("image_dimensions_unavailable: header did not declare measurable size")
    return ParseResult(
        markdown, f"image-source-v1:{info['mime_type']}", "local", "image-source",
        quality="partial",
        capabilities={"image": True, "ocr": False, "semantic_image": False,
                      "line_basis": "user_metadata", "anchor": False,
                      "width": info["width"] or None, "height": info["height"] or None,
                      "image_sha256": info["sha256"], "occurrences": occurrences},
        warnings=warnings,
    )
