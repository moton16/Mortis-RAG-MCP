"""摄取侧结构化结果与资源配额值对象（C93）。

本模块只放**纯值对象与纯函数**：不含网络、不含文件系统写、不依赖 doc_store。
目的：把「解析出来什么」与「怎么存」解耦——worker 把 `ParseResult` 里的
`MediaOccurrence` 通过 sink 写进 staged store，而不是先攒成一个 images dict。

与研究合同的对齐（`docs/v0.9.0/RESEARCH_REPORT.rev1.md`）：
- §13.1 统一解析合同：parser 指纹 / quality / capabilities / PageSpan / MediaOccurrence /
  warnings / 字节与耗时统计；页码 1-based，字符区间 0-based 半开。
- §13.2 安全配额：压缩 32MiB / 累计解压 200MiB / markdown 16MiB / JSON 8MiB /
  单媒体 8MiB / 成员 10000 / 媒体 1000 / 单图 4000 万像素；这些是**可执行起点**，
  不是 RSS 保证；`0` 不得关闭安全限制（非法值一律回落默认，与 §17.4 一致）。
- §12.5 远端恢复：`submission_phase` 覆盖 prepared/submitted/polling/downloaded/
  staged/committed/submission_unknown；受理成功但响应丢失 → `SUBMISSION_UNKNOWN`，
  禁止自动重传（费用不可逆）。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

# --------------------------------------------------------------------- 稳定 code

PARSE_PARTIAL = "PARSE_PARTIAL"
PARSE_EMPTY = "PARSE_EMPTY"
CONTRACT_UNVERIFIED = "CONTRACT_UNVERIFIED"
ARCHIVE_INVALID = "ARCHIVE_INVALID"
RESOURCE_LIMIT = "RESOURCE_LIMIT"
SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"

#: §12.5 提交阶段状态机（远端恢复用；持久在 ingest_jobs.phase / subjob 上）。
SUBMISSION_PHASES = (
    "prepared",
    "submitted",
    "polling",
    "downloaded",
    "staged",
    "committed",
    "submission_unknown",
)

#: 解析质量。`full` = 结构化完整；`light` = Agent 无页映射；`partial` = 结构缺失但有可验证文本；
#: `fallback` = 本地纯文本兜底（无版面/表格/公式能力）。
QUALITIES = ("full", "light", "partial", "fallback")

# --------------------------------------------------------------------- 资源配额


def _positive_int(value: Any, default: int) -> int:
    """非法/非正/非有限值一律**回落默认**：`0` 不是「无限制」（§17.4）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    number = int(value)
    if number <= 0:
        return default
    return number


DEFAULT_ARCHIVE_MAX_MB = 32
DEFAULT_EXTRACTED_MAX_MB = 200
DEFAULT_MARKDOWN_MAX_MB = 16
DEFAULT_JSON_MAX_MB = 8
DEFAULT_MEDIA_MAX_MB = 8
DEFAULT_MEMORY_BUDGET_MB = 128
DEFAULT_MAX_MEMBERS = 10_000
DEFAULT_MAX_MEDIA = 1_000
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000
#: ZIP 中央目录元数据上限（§20.7C：构造 ZipFile 之前的准入预算）。
DEFAULT_CD_METADATA_BYTES = 2 * 1024 * 1024


#: 各预算项的合同默认（`__post_init__` 用它做「非法值回落」，**不得**实例化自身）。
_LIMIT_DEFAULTS: dict[str, int] = {
    "archive_max_bytes": DEFAULT_ARCHIVE_MAX_MB * 1024 * 1024,
    "extracted_max_bytes": DEFAULT_EXTRACTED_MAX_MB * 1024 * 1024,
    "markdown_max_bytes": DEFAULT_MARKDOWN_MAX_MB * 1024 * 1024,
    "json_max_bytes": DEFAULT_JSON_MAX_MB * 1024 * 1024,
    "media_max_bytes": DEFAULT_MEDIA_MAX_MB * 1024 * 1024,
    "memory_budget_bytes": DEFAULT_MEMORY_BUDGET_MB * 1024 * 1024,
    "max_members": DEFAULT_MAX_MEMBERS,
    "max_media": DEFAULT_MAX_MEDIA,
    "max_image_pixels": DEFAULT_MAX_IMAGE_PIXELS,
    "cd_metadata_max_bytes": DEFAULT_CD_METADATA_BYTES,
}


@dataclass(slots=True, frozen=True)
class ResourceLimits:
    """一次解析请求的有界消费预算（字节数均为解压后真实字节的口径）。"""

    archive_max_bytes: int = DEFAULT_ARCHIVE_MAX_MB * 1024 * 1024
    extracted_max_bytes: int = DEFAULT_EXTRACTED_MAX_MB * 1024 * 1024
    markdown_max_bytes: int = DEFAULT_MARKDOWN_MAX_MB * 1024 * 1024
    json_max_bytes: int = DEFAULT_JSON_MAX_MB * 1024 * 1024
    media_max_bytes: int = DEFAULT_MEDIA_MAX_MB * 1024 * 1024
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET_MB * 1024 * 1024
    max_members: int = DEFAULT_MAX_MEMBERS
    max_media: int = DEFAULT_MAX_MEDIA
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS
    cd_metadata_max_bytes: int = DEFAULT_CD_METADATA_BYTES

    def __post_init__(self) -> None:
        # 允许程序化构造时给出更保守的值，但绝不允许 0/负数/NaN 把限制「关掉」。
        for name, fallback in _LIMIT_DEFAULTS.items():
            value = _positive_int(getattr(self, name), fallback)
            if value != getattr(self, name):
                object.__setattr__(self, name, value)

    @classmethod
    def from_config(cls, ingest_config: Any) -> "ResourceLimits":
        """从 `IngestConfig` 构造；缺键/非法值回落研究合同默认（§13.2/§17.4）。"""
        get = (lambda key, default: getattr(ingest_config, key, default)) if ingest_config is not None else (
            lambda key, default: default
        )

        def mb(key: str, default_mb: int) -> int:
            raw = get(key, default_mb)
            return _positive_int(raw, default_mb) * 1024 * 1024

        return cls(
            archive_max_bytes=mb("archive_max_mb", DEFAULT_ARCHIVE_MAX_MB),
            extracted_max_bytes=mb("extracted_max_mb", DEFAULT_EXTRACTED_MAX_MB),
            markdown_max_bytes=mb("markdown_max_mb", DEFAULT_MARKDOWN_MAX_MB),
            json_max_bytes=mb("json_max_mb", DEFAULT_JSON_MAX_MB),
            media_max_bytes=mb("media_max_mb", DEFAULT_MEDIA_MAX_MB),
            memory_budget_bytes=mb("memory_budget_mb", DEFAULT_MEMORY_BUDGET_MB),
            max_members=_positive_int(get("archive_max_members", DEFAULT_MAX_MEMBERS), DEFAULT_MAX_MEMBERS),
            max_media=_positive_int(get("archive_max_media", DEFAULT_MAX_MEDIA), DEFAULT_MAX_MEDIA),
            max_image_pixels=_positive_int(get("max_image_pixels", DEFAULT_MAX_IMAGE_PIXELS), DEFAULT_MAX_IMAGE_PIXELS),
        )


# --------------------------------------------------------------------- 结构结果


@dataclass(slots=True, frozen=True)
class PageSpan:
    """某一页在**规范化 Markdown**里的字符区间（0-based 半开）。page 为 1-based。"""

    page: int
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        if not isinstance(self.page, int) or isinstance(self.page, bool) or self.page < 1:
            raise ValueError(f"PageSpan.page 必须是 >=1 的整数，收到 {self.page!r}")
        if not isinstance(self.char_start, int) or not isinstance(self.char_end, int):
            raise ValueError("PageSpan 的字符区间必须是整数")
        if self.char_start < 0 or self.char_end < self.char_start:
            raise ValueError(
                f"PageSpan 区间非法：[{self.char_start}, {self.char_end})"
            )


@dataclass(slots=True, frozen=True)
class MediaOccurrence:
    """一项媒体出现（图片/表格图/音频片段）。地址由 store 侧补（source+revision+occurrence）。"""

    occurrence_id: str
    kind: str
    ordinal: int
    name: str = ""
    mime_type: str = ""
    byte_size: int = 0
    page: int | None = None
    bbox: tuple[float, ...] | None = None
    t_start_ms: int | None = None
    t_end_ms: int | None = None
    caption: str = ""
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.occurrence_id, str) or not self.occurrence_id:
            raise ValueError("MediaOccurrence.occurrence_id 必须是非空字符串")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("MediaOccurrence.kind 必须是非空字符串")
        if self.page is not None and (not isinstance(self.page, int) or self.page < 1):
            raise ValueError(f"MediaOccurrence.page 必须是 >=1 的整数或 None，收到 {self.page!r}")


@dataclass(slots=True)
class ParseResult:
    """统一解析合同的结构化结果（§13.1）。`markdown` 已规范化为 LF/UTF-8 文本。"""

    markdown: str
    parser_fingerprint: str
    channel: str
    model: str
    quality: str = "full"
    capabilities: dict[str, Any] = field(default_factory=dict)
    page_map: list[PageSpan] = field(default_factory=list)
    media: list[MediaOccurrence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    submission_phase: str = "committed"
    remote_task_id: str = ""
    compressed_bytes: int = 0
    extracted_bytes: int = 0
    markdown_bytes: int = 0
    media_bytes: int = 0
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.markdown, str):
            raise ValueError("ParseResult.markdown 必须是字符串")
        if self.quality not in QUALITIES:
            raise ValueError(f"quality 必须是 {QUALITIES} 之一，收到 {self.quality!r}")
        if not self.parser_fingerprint:
            raise ValueError("ParseResult.parser_fingerprint 必填")
        if self.submission_phase not in SUBMISSION_PHASES:
            raise ValueError(
                f"submission_phase 必须是 {SUBMISSION_PHASES} 之一，收到 {self.submission_phase!r}"
            )

    @property
    def has_page_map(self) -> bool:
        return bool(self.page_map)

    def capability(self, name: str, default: bool = False) -> bool:
        value = self.capabilities.get(name, default)
        return bool(value)


# --------------------------------------------------------------------- 媒体 sink


class MediaSink(Protocol):
    """媒体落点。**不允许**实现方把整批媒体攒在内存里再统一写（§13.2）。"""

    def add(
        self,
        *,
        name: str,
        data: bytes,
        kind: str,
        ordinal: int,
        mime_type: str,
        page: int | None = None,
        bbox: tuple[float, ...] | None = None,
        caption: str = "",
        ocr: str = "",
        width: int | None = None,
        height: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """写入一项媒体并返回 occurrence_id。"""
        ...


class DictMediaSink:
    """兼容旧 `ParsedDocument.images` 的薄 sink：仍会攒内存，**仅供历史 fake/测试**。

    生产路径必须传真正的 store sink（`stage sink`），不得使用本类。
    """

    def __init__(self) -> None:
        self.images: dict[str, bytes] = {}
        self.occurrences: list[MediaOccurrence] = []

    def add(
        self,
        *,
        name: str,
        data: bytes,
        kind: str,
        ordinal: int,
        mime_type: str,
        page: int | None = None,
        bbox: tuple[float, ...] | None = None,
        caption: str = "",
        ocr: str = "",
        width: int | None = None,
        height: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        occurrence_id = f"occ-{ordinal:05d}"
        self.images[name] = bytes(data)
        self.occurrences.append(
            MediaOccurrence(
                occurrence_id=occurrence_id,
                kind=kind,
                ordinal=ordinal,
                name=name,
                mime_type=mime_type,
                byte_size=len(data),
                page=page,
                bbox=bbox,
                caption=caption,
                width=width,
                height=height,
                metadata=dict(metadata or {}),
            )
        )
        return occurrence_id


# --------------------------------------------------------------------- 纯函数


def parser_fingerprint(
    *,
    adapter: str,
    channel: str,
    model: str,
    language: str,
    is_ocr: bool,
    enable_table: bool,
    enable_formula: bool,
    normalize_version: str = "md-normalize-v1",
) -> str:
    """parser 指纹：适配器版本 + 通道 + 模型 + 语言 + OCR/表格/公式 + 规范化版本（§12.2）。

    任何一项改变都会让指纹变化 → 不再复用旧产物（§12.2「三者不能混用」）。
    """
    payload = "|".join(
        [
            str(adapter),
            str(channel),
            str(model),
            str(language),
            "ocr=" + ("1" if is_ocr else "0"),
            "table=" + ("1" if enable_table else "0"),
            "formula=" + ("1" if enable_formula else "0"),
            str(normalize_version),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


_REDACTED_QUERY_KEYS = (
    "signature",
    "sign",
    "token",
    "accesskeyid",
    "access_key_id",
    "x-oss-signature",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "expires",
    "osaccesskeyid",
)


def redact_url(url: str, *, keep_path: bool = True) -> str:
    """脱敏 URL：丢掉 query/fragment（预签名 URL 签名字段就是凭据，§13.2 禁止记全 URL）。"""
    if not isinstance(url, str) or not url:
        return ""
    text = url.strip()
    head = text.split("#", 1)[0]
    if "?" in head:
        head = head.split("?", 1)[0]
    if not keep_path:
        return head
    return head


def redact_endpoint(url: str) -> str:
    """只保留 scheme://host，用于 `submission_unknown` 的脱敏 endpoint 展示。"""
    text = redact_url(url)
    if "://" not in text:
        return text
    scheme, rest = text.split("://", 1)
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}"


def parse_retry_after(value: Any, *, now: float | None = None, default: float = 60.0) -> float:
    """解析 `Retry-After`：支持秒数（RFC 9110 delta-seconds）与 HTTP-date。

    拒绝 NaN/Inf/负数/空值 → 回落 `default`（§13.3「Retry-After 支持秒/HTTP-date、拒 NaN/Inf」）。
    """
    if value is None:
        return float(default)
    text = str(value).strip()
    if not text:
        return float(default)
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None:
        if not math.isfinite(seconds) or seconds < 0:
            return float(default)
        return seconds
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return float(default)
    if when is None:
        return float(default)
    import time as _time

    reference = _time.time() if now is None else float(now)
    try:
        delta = when.timestamp() - reference
    except (OverflowError, OSError, ValueError):
        return float(default)
    if not math.isfinite(delta) or delta < 0:
        return 0.0
    return float(delta)


def normalize_markdown(text: str) -> str:
    """Markdown 规范化：UTF-8 解码后的 CRLF/CR → LF，去掉 BOM，保证末尾 LF。

    **不插入** `parsed_at` 之类的时间戳（§13.1：不稳定信息不能进正文 hash）。
    """
    if not isinstance(text, str):
        raise ValueError("normalize_markdown 需要字符串")
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def image_header_size(data: bytes) -> tuple[int, int] | None:
    """从图片头解析 (width, height)；只读 header，**不解码像素**。

    无法识别该格式 → None（调用方记 warning，不能因为「不认识」就拒绝合法格式）。
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 16:
        return None
    head = bytes(data[:32])
    # PNG: 8 字节签名 + IHDR
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    # GIF87a/89a
    elif head[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
    # JPEG: 扫描 SOFn
    elif head.startswith(b"\xff\xd8"):
        width = height = 0
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if index + 4 > len(data):
                break
            segment_len = int.from_bytes(data[index + 2 : index + 4], "big")
            if segment_len < 2:
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if index + 9 > len(data):
                    break
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                break
            index += 2 + segment_len
        if not width or not height:
            return None
    # BMP
    elif head[:2] == b"BM" and len(data) >= 26:
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = int.from_bytes(data[22:26], "little", signed=True)
        width, height = abs(width), abs(height)
    else:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def image_pixel_budget_exceeded(data: bytes, max_pixels: int) -> bool:
    """头部声明超过像素上限 → True。**只对可识别的头部**判定（§20.7C reserve 前准入）。"""
    size = image_header_size(data)
    if size is None:
        return False
    return size[0] * size[1] > max_pixels


def extension_mime(name: str) -> str:
    """按后缀给媒体 MIME（未知 → application/octet-stream）。不猜内容。"""
    lowered = str(name).lower().rsplit(".", 1)
    ext = ("." + lowered[-1]) if len(lowered) == 2 else ""
    return _EXT_MIME.get(ext, "application/octet-stream")


_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".json": "application/json",
    ".md": "text/markdown",
}
