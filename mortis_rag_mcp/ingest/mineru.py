"""MinerU 云端解析客户端（纯标准库 urllib，零第三方依赖）。

双通道（官方文档 https://mineru.net/apiManage/docs 已核实）：
- v4 精准 API（需 token）：POST /api/v4/file-urls/batch 申请上传链接 →
  PUT 上传文件（**显式置空** Content-Type，见 `_put_upload` 注释）→ GET /api/v4/extract-results/batch/{batch_id}
  轮询 → state=done 后下载 full_zip_url（zip，内含 full.md）。
  限制：≤200MB、≤200页；每天 1000 页高优先级额度。
- Agent 轻量 API（免 token）：POST /api/v1/agent/parse/file 得 (task_id, file_url) →
  PUT 上传 → GET /api/v1/agent/parse/{task_id} 轮询 → done 后下载 markdown_url。
  限制：≤10MB、≤20页、IP 限频（超限 HTTP 429）；仅 PDF/图片/DOCX/PPTX/XLSX。

v0.9.0（C93）新增的**有界**语义（研究合同 §13.1–§13.3 / §20.7C / §20.7F）：
- HTTP 分块计**实际字节**；`Content-Length` 只用于**提前**拒绝，不作为信任来源。
- 归档（ZIP）在构造 `ZipFile` **之前**读 EOCD/中央目录并校验声明成员数与元数据上限；
  逐条校验成员名（拒绝对化/盘符/UNC/上跳/规范化重名）、加密位、symlink、未知压缩算法、
  声明≠实际；**不 `extractall`**、不落临时文件；媒体交给 sink 逐项落点。
- 结构化结果（`ingest/models.py` 的 `ParseResult`）：parser 指纹 / quality / capabilities /
  PageSpan / MediaOccurrence / warnings / 字节与耗时。
- 总 deadline（含上传/轮询/下载）、`Retry-After`（秒与 HTTP-date、拒 NaN/Inf）、URL 脱敏。
- 非幂等付费 POST 的**发送意图**与 `SUBMISSION_UNKNOWN`：受理成功但响应丢失时**禁止自动重传**
  （费用不可逆，只能由用户显式确认后重试）。

兼容：`MineruClient.parse()` 仍返回旧的 `ParsedDocument(markdown/images/channel/model)`，
`_http_json`/`_http_bytes`/`_put_upload`/`_extract_zip` 的**签名与调用约定保持不变**
（历史测试直接 monkeypatch 它们）。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time

import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import (
    ARCHIVE_INVALID,
    CONTRACT_UNVERIFIED,
    PARSE_EMPTY,
    PARSE_PARTIAL,
    RESOURCE_LIMIT,
    SUBMISSION_UNKNOWN,
    DictMediaSink,
    MediaOccurrence,
    MediaSink,
    PageSpan,
    ParseResult,
    ResourceLimits,
    extension_mime,
    image_header_size,
    image_pixel_budget_exceeded,
    normalize_markdown,
    parse_retry_after,
    parser_fingerprint,
    redact_endpoint,
)

V4_BASE = "https://mineru.net/api/v4"
AGENT_BASE = "https://mineru.net/api/v1/agent"
V4_MAX_BYTES = 200 * 1024 * 1024
AGENT_MAX_BYTES = 10 * 1024 * 1024
# v4 全格式；Agent 接口不含老 Office 三件套（doc/ppt/xls）
V4_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
AGENT_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}
# 终态错误码：额度/超限类不值得退避重试，直接失败让上层降级
_V4_FATAL_CODES = {-60005, -60006, -60017, -60018, -60019}  # 大小/页数超限、重试上限、每日额度
_AGENT_FATAL_CODES = {-30001, -30002, -30003, -30004}

#: 归档允许的压缩算法白名单（其余一律 ARCHIVE_INVALID：未知算法可能绕过配额核算）。
_ALLOWED_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
)
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"})
_READ_CHUNK = 64 * 1024
_EOCD_SIG = b"PK\x05\x06"
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_CD_ENTRY_SIG = b"PK\x01\x02"
_CD_ENTRY_SIZE = 46
_EOCD_MIN_SIZE = 22
_EOCD_MAX_SEARCH = 66 * 1024  # 22 字节 EOCD + 最多 64KiB 注释
#: 解析器规范化版本：改变规范化算法（换行/解码/去 BOM）必须同步递增，
#: 否则旧产物会被错误复用（进 parser 指纹）。
NORMALIZE_VERSION = "md-normalize-v1"
#: 适配器版本：改「怎么解析」的都要递增，进 parser 指纹。
ADAPTER_VERSION = "mineru-client-v2"


class MineruError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None,
                 retryable: bool = False, code_str: str = "", fix: str = ""):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        #: 稳定字符串 code（§12.8）：ARCHIVE_INVALID / RESOURCE_LIMIT / PARSE_PARTIAL /
        #: CONTRACT_UNVERIFIED / SUBMISSION_UNKNOWN。
        self.code_str = code_str
        self.fix = fix


@dataclass(slots=True)
class ParsedDocument:
    """旧兼容结果对象（历史 fake 与 worker 的 legacy 路径仍用）。"""

    markdown: str
    images: dict[str, bytes]   # 图片相对路径（如 images/xxx.jpg）→ 字节；Agent 通道恒为空
    channel: str               # "v4" | "agent"
    model: str                 # 实际使用的模型版本


# --------------------------------------------------------------------- 有界 IO 上下文


class _IOContext:
    """一次解析的 IO 预算：绝对 deadline + 单次响应字节上限 + 远端地址策略。

    用 thread-local 传递，**不改变** `_http_json`/`_http_bytes`/`_put_upload` 的签名——
    历史测试直接 monkeypatch 这三个模块级函数，签名破坏会让 T04 基线整体失效。
    """

    __slots__ = ("deadline", "limit_bytes", "enforce_remote_policy", "allow_loopback")

    def __init__(
        self,
        *,
        deadline: float | None = None,
        limit_bytes: int | None = None,
        enforce_remote_policy: bool = False,
        allow_loopback: bool = False,
    ) -> None:
        self.deadline = deadline
        self.limit_bytes = limit_bytes
        self.enforce_remote_policy = enforce_remote_policy
        self.allow_loopback = allow_loopback


_IO_CTX = threading.local()


def _current_io() -> _IOContext | None:
    return getattr(_IO_CTX, "ctx", None)


@contextmanager
def _io_profile(
    *,
    deadline: float | None = None,
    limit_bytes: int | None = None,
    enforce_remote_policy: bool = False,
    allow_loopback: bool = False,
) -> Iterator[None]:
    """嵌套 IO 继承外层总 deadline，内层预算只能收紧。"""
    previous = _current_io()
    if previous is not None:
        if previous.deadline is not None:
            deadline = previous.deadline if deadline is None else min(deadline, previous.deadline)
        if previous.limit_bytes is not None:
            limit_bytes = previous.limit_bytes if limit_bytes is None else min(limit_bytes, previous.limit_bytes)
        enforce_remote_policy = enforce_remote_policy or previous.enforce_remote_policy
        allow_loopback = previous.allow_loopback
    _IO_CTX.ctx = _IOContext(
        deadline=deadline,
        limit_bytes=limit_bytes,
        enforce_remote_policy=enforce_remote_policy,
        allow_loopback=allow_loopback,
    )
    try:
        yield
    finally:
        _IO_CTX.ctx = previous


def _remaining_seconds(default: float) -> float:
    """把调用方超时夹到剩余 deadline：deadline 已过 → 抛可重试的 deadline 错误。"""
    ctx = _current_io()
    if ctx is None or ctx.deadline is None:
        return float(default)
    remaining = ctx.deadline - time.monotonic()
    if remaining <= 0:
        raise MineruError("parse deadline exceeded", retryable=True, code_str=RESOURCE_LIMIT)
    return max(0.001, min(float(default), remaining))


def _read_bounded(resp: Any, limit: int | None) -> bytes:
    """分块读取并**按实际字节**计数：`Content-Length` 只用于提前拒绝，不是信任来源。"""
    declared = resp.headers.get("Content-Length") if getattr(resp, "headers", None) is not None else None
    if declared is not None:
        try:
            declared_int = int(str(declared).strip())
        except (TypeError, ValueError):
            declared_int = None
        if declared_int is not None and limit is not None and declared_int > limit:
            raise MineruError(
                f"response Content-Length {declared_int} exceeds limit {limit} bytes",
                retryable=False,
                code_str=RESOURCE_LIMIT,
                fix="这是服务端声明超限；不要静默截断，按配额拒绝并提示。",
            )
    chunks: list[bytes] = []
    total = 0
    while True:
        _remaining_seconds(60.0)
        block = resp.read(_READ_CHUNK)
        if not block:
            break
        total += len(block)
        if limit is not None and total > limit:
            raise MineruError(
                f"response exceeded limit {limit} bytes while streaming",
                retryable=False,
                code_str=RESOURCE_LIMIT,
                fix="按真实字节在读取过程中拒绝，不能等读完再判。",
            )
        chunks.append(block)
    return b"".join(chunks)


def _error_body(exc: urllib.error.HTTPError) -> str:
    """读取错误响应体（有界）用作诊断；不记完整 URL。"""
    try:
        raw = exc.read(64 * 1024)
    except Exception:  # pragma: no cover - 极端情况下 body 不可读
        return ""
    limit = 500
    try:
        text = raw[:limit].decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return ""
    return text


def _http_json(req: urllib.request.Request, timeout: float) -> dict:
    """JSON 请求（GET/POST）。**签名固定**（历史测试直接打桩）。"""
    ctx = _current_io()
    if ctx is not None and ctx.enforce_remote_policy:
        _validate_remote_url(req.full_url, allow_loopback=ctx.allow_loopback)
    try:
        with urllib.request.urlopen(req, timeout=_remaining_seconds(timeout)) as resp:
            payload = _read_bounded(resp, ctx.limit_bytes if ctx is not None else None)
    except urllib.error.HTTPError as exc:
        body = _error_body(exc)
        if exc.code == 429:
            retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            raise MineruError(f"rate limited (429), retry after {retry_after}s",
                              http_status=429, retryable=True) from exc
        raise MineruError(f"HTTP {exc.code}: {body}", http_status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"network error: {exc}", retryable=True) from exc
    except MineruError:
        raise
    try:
        _admit_structure_json(payload)
        return json.loads(payload.decode("utf-8"))
    except (MemoryError, RecursionError) as exc:
        raise MineruError("HTTP JSON allocation budget exceeded",
                          retryable=False, code_str=RESOURCE_LIMIT) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MineruError(
            f"invalid JSON response: {exc}",
            retryable=False,
            code_str=CONTRACT_UNVERIFIED,
            fix="服务端返回的 JSON 无法解析；不要按旧 schema 猜字段。",
        ) from exc


def _http_bytes(url: str, timeout: float) -> bytes:
    """下载字节（签名固定）。分块计实际字节，受 IO 预算约束。"""
    ctx = _current_io()
    if ctx is not None and ctx.enforce_remote_policy:
        _validate_remote_url(url, allow_loopback=ctx.allow_loopback)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=_remaining_seconds(timeout)) as resp:
            return _read_bounded(resp, ctx.limit_bytes if ctx is not None else None)
    except urllib.error.HTTPError as exc:
        raise MineruError(f"download failed: HTTP {exc.code}", http_status=exc.code,
                          retryable=exc.code >= 500) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"download failed: {exc}", retryable=True) from exc


def _put_upload(url: str, payload: bytes, timeout: float) -> None:
    # 官方文档明确：上传自身不需要 Content-Type —— 但**必须显式置空**，不能「不设」：
    # urllib 的 AbstractHTTPHandler.do_request_ 在「有 data 且 has_header('Content-type') 为假」时
    # 会注入 application/x-www-form-urlencoded；而 OSS V1 预签名把 CONTENT-TYPE 计入 StringToSign
    # （服务端按**实际请求头**重算签名），注入即 403 SignatureDoesNotMatch（issue #1）。
    # 传入空值头后 do_request_ 的 has_header 判据为真 → 不再注入；http.client 实际发出的是
    # 空值头（`Content-type: `），OSS V1 下空值与缺省等价，故该修法可用。
    # 注意 Request.add_header 会对键做 capitalize()，故 "Content-Type" 落到 "Content-type"，
    # 与 do_request_ 内部检查的拼写一致（大小写不匹配会让注入照旧发生）。
    #
    # §13.2 保留了这条合同：**预签名 PUT 空 Content-Type 是回归项，不许删**。
    ctx = _current_io()
    if ctx is not None and ctx.enforce_remote_policy:
        _validate_remote_url(url, allow_loopback=ctx.allow_loopback)
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": ""}, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=_remaining_seconds(timeout)) as resp:
            if resp.status not in (200, 201):
                raise MineruError(f"upload failed: HTTP {resp.status}", http_status=resp.status)
    except urllib.error.HTTPError as exc:
        raise MineruError(f"upload failed: HTTP {exc.code}", http_status=exc.code,
                          retryable=exc.code >= 500) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"upload network error: {exc}", retryable=True) from exc


def _validate_remote_url(url: str, *, allow_loopback: bool = False) -> None:
    """远端地址策略（§13.2）：只允许 HTTPS；拒绝 localhost/私有/link-local/UNC 风格。

    loopback 仅对**显式本地模型 sidecar** 放行（由调用方传入 `allow_loopback=True`）。
    """
    import ipaddress

    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        raise MineruError("remote URL has no host", retryable=False, code_str=ARCHIVE_INVALID)
    if scheme != "https":
        if not (allow_loopback and _is_loopback_host(host)):
            raise MineruError(
                f"remote URL must use HTTPS, got {scheme or '<none>'}://{host}",
                retryable=False,
                fix="远端预签名/解析地址必须 HTTPS；HTTP 只在显式本地 sidecar 策略下允许。",
            )
    if allow_loopback and _is_loopback_host(host):
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified
    ):
        raise MineruError(
            f"remote URL host is not publicly routable: {address}",
            retryable=False,
            fix="拒 localhost/private/link-local；不要向内网地址转发 token。",
        )


def _is_loopback_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"}


# --------------------------------------------------------------------- 安全归档


@dataclass(slots=True)
class ArchiveOutcome:
    """一次安全归档消费的结构化产物（**不**保留整批媒体在内存里）。"""

    markdown: str
    media: list[MediaOccurrence]
    capabilities: dict[str, Any]
    page_map: list[PageSpan]
    warnings: list[str]
    extracted_bytes: int
    markdown_bytes: int
    media_bytes: int
    partial: bool


def _find_eocd(zip_bytes: bytes) -> tuple[int, int, int, int, int]:
    """定位 EOCD 并返回 (cd_offset, cd_size, entries, disk_number, cd_disk)。

    无 EOCD / 多 disk / ZIP64 → ARCHIVE_INVALID（§20.7C：拒不支持/异常 ZIP64、多 disk）。
    """
    tail_start = max(0, len(zip_bytes) - _EOCD_MAX_SEARCH)
    index = zip_bytes.rfind(_EOCD_SIG, tail_start)
    if index < 0 or index + _EOCD_MIN_SIZE > len(zip_bytes):
        raise MineruError(
            "archive has no end-of-central-directory record",
            retryable=False,
            code_str=ARCHIVE_INVALID,
            fix="坏 ZIP：不要交给 zipfile 去猜，直接判失败。",
        )
    if zip_bytes.rfind(_ZIP64_EOCD_SIG, tail_start) >= 0 or zip_bytes.rfind(_ZIP64_LOCATOR_SIG, tail_start) >= 0:
        raise MineruError(
            "ZIP64 archives are not supported (limits cannot be pre-validated)",
            retryable=False,
            code_str=ARCHIVE_INVALID,
            fix="要求服务端返回普通 ZIP；ZIP64 需先做受限头部解析。",
        )
    entries = int.from_bytes(zip_bytes[index + 10 : index + 12], "little")
    cd_size = int.from_bytes(zip_bytes[index + 12 : index + 16], "little")
    cd_offset = int.from_bytes(zip_bytes[index + 16 : index + 20], "little")
    disk_number = int.from_bytes(zip_bytes[index + 4 : index + 6], "little")
    cd_disk = int.from_bytes(zip_bytes[index + 6 : index + 8], "little")
    if entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        raise MineruError(
            "archive uses ZIP64 sentinel values", retryable=False, code_str=ARCHIVE_INVALID
        )
    return cd_offset, cd_size, entries, disk_number, cd_disk


def _inspect_central_directory(zip_bytes: bytes, limits: ResourceLimits) -> int:
    """构造 `ZipFile` **之前**的准入：校验声明 entries 与中央目录真实字节。

    返回声明的成员数（供构造后核对，声明≠实际即拒）。
    """
    cd_offset, cd_size, entries, disk_number, cd_disk = _find_eocd(zip_bytes)
    if disk_number or cd_disk:
        raise MineruError(
            "multi-disk archives are not supported", retryable=False, code_str=ARCHIVE_INVALID
        )
    if entries > limits.max_members:
        raise MineruError(
            f"archive declares {entries} members, limit {limits.max_members}",
            retryable=False,
            code_str=RESOURCE_LIMIT,
            fix="成员数超限：在创建 ZipFile 之前拒绝，避免中央目录分配风暴。",
        )
    if cd_size > limits.cd_metadata_max_bytes:
        raise MineruError(
            f"central directory metadata {cd_size} bytes exceeds limit {limits.cd_metadata_max_bytes}",
            retryable=False,
            code_str=RESOURCE_LIMIT,
        )
    end = cd_offset + cd_size
    if end > len(zip_bytes) or cd_offset < 0:
        raise MineruError(
            "central directory offset/size is inconsistent with the archive length",
            retryable=False,
            code_str=ARCHIVE_INVALID,
        )
    # 逐条走一遍中央目录，确认声明 entries 与真实条目一致（不允许「声明 1 个、藏 N 个」）。
    cursor = cd_offset
    seen = 0
    while cursor < end:
        if cursor + _CD_ENTRY_SIZE > len(zip_bytes) or zip_bytes[cursor : cursor + 4] != _CD_ENTRY_SIG:
            raise MineruError(
                "central directory entry signature mismatch",
                retryable=False,
                code_str=ARCHIVE_INVALID,
            )
        name_len = int.from_bytes(zip_bytes[cursor + 28 : cursor + 30], "little")
        extra_len = int.from_bytes(zip_bytes[cursor + 30 : cursor + 32], "little")
        comment_len = int.from_bytes(zip_bytes[cursor + 32 : cursor + 34], "little")
        cursor += _CD_ENTRY_SIZE + name_len + extra_len + comment_len
        seen += 1
        if seen > limits.max_members:
            raise MineruError(
                f"central directory has more than {limits.max_members} entries",
                retryable=False,
                code_str=RESOURCE_LIMIT,
            )
    if cursor != end:
        raise MineruError(
            "central directory length does not match its declared size",
            retryable=False,
            code_str=ARCHIVE_INVALID,
        )
    if seen != entries:
        raise MineruError(
            f"archive declares {entries} members but central directory has {seen}",
            retryable=False,
            code_str=ARCHIVE_INVALID,
        )
    return entries


def _validate_member_name(name: str) -> str:
    """成员名安全校验 → 规范化相对 POSIX 路径（§13.2）。目录名以 '/' 结尾。"""
    if not isinstance(name, str) or not name:
        raise MineruError("archive member with empty name", retryable=False, code_str=ARCHIVE_INVALID)
    if "\x00" in name or any(ord(ch) < 32 for ch in name):
        raise MineruError(
            f"archive member name has control characters: {name!r}",
            retryable=False,
            code_str=ARCHIVE_INVALID,
        )
    if "\\" in name:
        raise MineruError(
            f"archive member uses backslashes: {name!r}", retryable=False, code_str=ARCHIVE_INVALID
        )
    is_dir = name.endswith("/")
    trimmed = name.rstrip("/")
    if not trimmed:
        raise MineruError("archive member is the archive root", retryable=False, code_str=ARCHIVE_INVALID)
    if trimmed.startswith("/") or trimmed.startswith("//"):
        raise MineruError(
            f"archive member is absolute: {name!r}", retryable=False, code_str=ARCHIVE_INVALID
        )
    if len(trimmed) >= 2 and trimmed[1] == ":":
        raise MineruError(
            f"archive member has a drive letter: {name!r}", retryable=False, code_str=ARCHIVE_INVALID
        )
    parts = trimmed.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise MineruError(
                f"archive member has an unsafe path segment: {name!r}",
                retryable=False,
                code_str=ARCHIVE_INVALID,
            )
    return trimmed + ("/" if is_dir else "")


def _read_member_bounded(zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int,
                         remaining: int) -> bytes:
    """分块读单个成员：按**实际**字节计，声明≠实际即拒，CRC 在读到 EOF 时由 zipfile 校验。"""
    if info.file_size > limit:
        raise MineruError(
            f"archive member {info.filename!r} declares {info.file_size} bytes, limit {limit}",
            retryable=False,
            code_str=RESOURCE_LIMIT,
        )
    if info.file_size > remaining:
        raise MineruError(
            f"archive exceeds cumulative extraction budget while reading {info.filename!r}",
            retryable=False,
            code_str=RESOURCE_LIMIT,
            fix="累计解压预算不足：按配额拒绝，不要先解压再判。",
        )
    chunks: list[bytes] = []
    total = 0
    try:
        with zf.open(info) as handle:
            while True:
                block = handle.read(_READ_CHUNK)
                if not block:
                    break
                total += len(block)
                if total > limit or total > remaining:
                    raise MineruError(
                        f"archive member {info.filename!r} exceeded its byte budget while streaming",
                        retryable=False,
                        code_str=RESOURCE_LIMIT,
                    )
                chunks.append(block)
    except zipfile.BadZipFile as exc:
        raise MineruError(
            f"archive member {info.filename!r} failed CRC/integrity check: {exc}",
            retryable=False,
            code_str=ARCHIVE_INVALID,
            fix="CRC 失败说明归档损坏：整批候选作废，不静默跳过。",
        ) from exc
    except OSError as exc:  # pragma: no cover - 兜底：解压层抛出的非 BadZipFile 错误
        raise MineruError(
            f"archive member {info.filename!r} failed decompression: {exc}",
            retryable=False,
            code_str=ARCHIVE_INVALID,
        ) from exc
    if total != info.file_size:
        raise MineruError(
            f"archive member {info.filename!r} declared {info.file_size} bytes but yielded {total}",
            retryable=False,
            code_str=ARCHIVE_INVALID,
            fix="声明≠实际：不要相信中央目录的 size 字段。",
        )
    return b"".join(chunks)


def _zlib() -> Any:
    try:
        import zlib

        return zlib
    except ImportError:  # pragma: no cover
        return None


def _derive_page_map(markdown: str, blocks: list[dict[str, Any]]) -> tuple[list[PageSpan], str]:
    """从结构 JSON 的块序列推导页区间（best-effort，可靠才给）。

    不可靠就返回 `([], reason)` 并标 `capabilities["page_map"]=False`——
    §13.1 明确「缺页映射 page=null 且 capability=false，不猜页」。
    """
    if not blocks or not markdown:
        return [], "no structured blocks"
    spans: list[PageSpan] = []
    cursor = 0
    current_page: int | None = None
    current_start = 0
    for block in blocks:
        if not isinstance(block, dict):
            return [], "block is not an object"
        raw_page = block.get("page_idx", block.get("page"))
        if not isinstance(raw_page, int) or isinstance(raw_page, bool):
            return [], "block has no integer page index"
        page = raw_page + 1 if "page_idx" in block else raw_page
        if page < 1:
            return [], "block page index is not 1-based"
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        probe = text.strip()[:40]
        found = markdown.find(probe, cursor)
        if found < 0:
            return [], f"block text not found in markdown (page {page})"
        if current_page is None:
            current_page = page
            current_start = found
        elif page != current_page:
            spans.append(PageSpan(page=current_page, char_start=current_start, char_end=found))
            current_page = page
            current_start = found
        cursor = found + len(probe)
    if current_page is not None:
        spans.append(PageSpan(page=current_page, char_start=current_start, char_end=len(markdown)))
    if not spans:
        return [], "no usable blocks"
    return spans, ""


def _admit_structure_json(payload: bytes) -> None:
    depth = 0
    nodes = 1
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
            continue
        if byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            nodes += 1
        elif byte in (93, 125):
            depth -= 1
        elif byte in (44, 58):
            nodes += 1
        if depth > 64 or nodes > 100_000:
            raise MineruError("structure JSON depth/node budget exceeded",
                              retryable=False, code_str=RESOURCE_LIMIT)


def _parse_structure_json(members: list[tuple[str, bytes]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], bool]:
    """结构 JSON 白名单解析。未知 schema → 不猜字段，标 partial（PARSE_PARTIAL）。"""
    capabilities: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    warnings: list[str] = []
    partial = False
    for name, payload in members:
        recognized = False
        _admit_structure_json(payload)
        try:
            document = json.loads(payload.decode("utf-8"))
        except (RecursionError, MemoryError) as exc:
            raise MineruError("structure JSON allocation budget exceeded",
                              retryable=False, code_str=RESOURCE_LIMIT) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append(f"structure json unreadable: {name} ({type(exc).__name__})")
            partial = True
            continue
        if isinstance(document, dict) and isinstance(document.get("page_count"), int):
            capabilities["page_count"] = int(document["page_count"])
            recognized = True
        if isinstance(document, list):
            candidate = [item for item in document if isinstance(item, dict)]
            if candidate and all(("page_idx" in item or "page" in item) for item in candidate):
                blocks.extend(candidate)
                recognized = True
                continue
        if not recognized:
            warnings.append(f"unrecognized structure json schema: {name}")
            partial = True
    if members and not recognized:
        partial = True
    return capabilities, blocks, warnings, partial


def _clean_markdown_dest(raw_dest: str) -> str:
    dest = raw_dest.strip()
    if dest.startswith("<") and ">" in dest:
        dest = dest[1:dest.find(">")]
    else:
        # If title or extra space exists, e.g. "path/to/img.png "title"" or 'path/to/img.png 'title''
        parts = dest.split(None, 1)
        if parts:
            dest = parts[0]
    dest = dest.split("?")[0].split("#")[0]
    dest = urllib.parse.unquote(dest)
    return dest.lstrip("/").replace("\\", "/").lower()


def _build_image_block_map(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """建立结构 JSON 中图片/图表/表格/公式块到成员路径的索引映射。"""
    mapping: dict[str, dict[str, Any]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        candidates: list[str] = []
        for field in ("img_path", "image_path", "path"):
            val = block.get(field)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
            elif isinstance(val, list):
                candidates.extend(str(x).strip() for x in val if str(x).strip())
        for cand in candidates:
            norm = cand.replace("\\", "/").lstrip("/").lower()
            mapping[norm] = block
            fname = norm.rsplit("/", 1)[-1]
            if fname and fname not in mapping:
                mapping[fname] = block
    return mapping


def _find_media_anchor(markdown: str, member_name: str, used_spans: set[tuple[int, int]]) -> tuple[int | None, int | None]:
    """在 Markdown 正文中检索图片引用的字符半开区间 [start, end)（E08-a / E15）。

    仅在有真实正文引用证据时记录；绝不拿图片尺寸冒充 anchor。
    """
    norm_member = member_name.lstrip("/").replace("\\", "/").lower()
    fname = norm_member.rsplit("/", 1)[-1]
    for match in re.finditer(r'!\[[\s\S]*?\]\(([\s\S]*?)\)', markdown):
        span = (match.start(), match.end())
        if span in used_spans:
            continue
        target = _clean_markdown_dest(match.group(1))
        if target == norm_member or target.rsplit("/", 1)[-1] == fname:
            used_spans.add(span)
            return span[0], span[1]
    for match in re.finditer(r'<img\b[^>]*?\bsrc=["\'](.*?)["\']', markdown, re.IGNORECASE):
        span = (match.start(), match.end())
        if span in used_spans:
            continue
        target = _clean_markdown_dest(match.group(1))
        if target == norm_member or target.rsplit("/", 1)[-1] == fname:
            used_spans.add(span)
            return span[0], span[1]
    return None, None


def _safe_extract_zip(
    zip_bytes: bytes,
    *,
    limits: ResourceLimits,
    sink: MediaSink | None,
) -> ArchiveOutcome:
    """安全消费 MinerU 结果归档：先准入中央目录，再逐成员受限读取。**不 extractall**。"""
    if len(zip_bytes) * 2 > limits.memory_budget_bytes:
        raise MineruError("archive buffer exceeds controlled memory budget",
                          retryable=False, code_str=RESOURCE_LIMIT)
    declared = _inspect_central_directory(zip_bytes, limits)
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise MineruError(
            f"archive is not a valid zip: {exc}", retryable=False, code_str=ARCHIVE_INVALID
        ) from exc

    warnings: list[str] = []
    with zf:
        infos = zf.infolist()
        if len(infos) != declared:
            raise MineruError(
                f"archive declares {declared} members but zipfile sees {len(infos)}",
                retryable=False,
                code_str=ARCHIVE_INVALID,
            )
        if len(infos) > limits.max_members:
            raise MineruError(
                f"archive has {len(infos)} members, limit {limits.max_members}",
                retryable=False,
                code_str=RESOURCE_LIMIT,
            )
        normalized: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            safe_name = _validate_member_name(info.filename)
            key = safe_name.rstrip("/").lower()
            if key in normalized:
                raise MineruError(
                    f"archive has duplicate member after normalization: {info.filename!r}",
                    retryable=False,
                    code_str=ARCHIVE_INVALID,
                    fix="规范化重名会让提取结果取决于遍历顺序，必须拒绝。",
                )
            if info.flag_bits & 0x1:
                raise MineruError(
                    f"archive member is encrypted: {info.filename!r}",
                    retryable=False,
                    code_str=ARCHIVE_INVALID,
                )
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise MineruError(
                    f"archive member is a symlink: {info.filename!r}",
                    retryable=False,
                    code_str=ARCHIVE_INVALID,
                )
            if not info.is_dir() and info.compress_type not in _ALLOWED_COMPRESSION:
                raise MineruError(
                    f"archive member uses unsupported compression {info.compress_type}: {info.filename!r}",
                    retryable=False,
                    code_str=ARCHIVE_INVALID,
                )
            normalized[key] = info

        markdown_infos = [
            info for key, info in normalized.items()
            if key == "full.md" or key.endswith("/full.md")
        ]
        if len(markdown_infos) > 1:
            raise MineruError(
                f"archive contains {len(markdown_infos)} full.md members (ambiguous root)",
                retryable=False,
                code_str=ARCHIVE_INVALID,
                fix="多 full.md 无法判定主文档：整批拒绝，不要任选一个。",
            )
        if not markdown_infos:
            raise MineruError("full.md not found in result zip", retryable=False,
                              code_str=CONTRACT_UNVERIFIED)

        remaining = limits.extracted_max_bytes
        retained_budget = len(zip_bytes) * 2

        def buffer_limit(maximum: int, multiplier: int) -> int:
            available = (limits.memory_budget_bytes - retained_budget) // multiplier
            if available <= 0:
                raise MineruError("archive working buffers exceed controlled memory budget",
                                  retryable=False, code_str=RESOURCE_LIMIT)
            return min(maximum, available)

        md_info = markdown_infos[0]
        markdown_bytes = _read_member_bounded(
            zf, md_info, limit=buffer_limit(limits.markdown_max_bytes, 12), remaining=remaining
        )
        retained_budget += len(markdown_bytes) * 12
        remaining -= len(markdown_bytes)
        markdown = markdown_bytes.decode("utf-8", errors="replace")

        # 1. 先抽取结构 JSON 成员（优先解析元数据再处理媒体）
        json_members: list[tuple[str, bytes]] = []
        for key in sorted(normalized):
            info = normalized[key]
            if info is md_info or info.is_dir():
                continue
            suffix = ("." + key.rsplit(".", 1)[-1]) if "." in key.rsplit("/", 1)[-1] else ""
            if suffix == ".json":
                if len(json_members) >= 8:
                    warnings.append("too many structure json members; extras ignored")
                    continue
                payload = _read_member_bounded(
                    zf, info, limit=buffer_limit(limits.json_max_bytes, 64), remaining=remaining
                )
                remaining -= len(payload)
                retained_budget += len(payload) * 64
                json_members.append((key, payload))

        structure_caps, blocks, json_warnings, partial = _parse_structure_json(json_members)
        warnings.extend(json_warnings)
        image_meta_map = _build_image_block_map(blocks)
        used_anchor_spans: set[tuple[int, int]] = set()

        # 2. 逐成员处理图片媒体并注入结构元数据（caption/OCR/page/anchor）
        media_occurrences: list[MediaOccurrence] = []
        media_bytes_total = 0
        ordinal = 0
        for key in sorted(normalized):
            info = normalized[key]
            if info is md_info or info.is_dir():
                continue
            suffix = ("." + key.rsplit(".", 1)[-1]) if "." in key.rsplit("/", 1)[-1] else ""
            if suffix not in _IMAGE_EXTS or "/" not in key:
                continue
            if len(media_occurrences) >= limits.max_media:
                raise MineruError(
                    f"archive has more than {limits.max_media} media members",
                    retryable=False,
                    code_str=RESOURCE_LIMIT,
                )
            payload = _read_member_bounded(
                zf, info, limit=buffer_limit(limits.media_max_bytes, 2), remaining=remaining
            )
            remaining -= len(payload)
            if isinstance(sink, DictMediaSink):
                retained_budget += len(payload) * 2
            if suffix != ".svg" and image_pixel_budget_exceeded(payload, limits.max_image_pixels):
                raise MineruError(
                    f"image member {info.filename!r} declares more than "
                    f"{limits.max_image_pixels} pixels",
                    retryable=False,
                    code_str=RESOURCE_LIMIT,
                    fix="按头部声明在上限内拒绝，不先解码整图。",
                )
            pixels = None if suffix == ".svg" else image_header_size(payload)
            if suffix != ".svg" and pixels is None:
                warnings.append(f"image header not recognized, pixel budget unverified: {key}")
            width, height = pixels if pixels else (None, None)

            # 从结构 JSON 块映射元数据
            norm_key = key.lstrip("/").lower()
            fname_key = norm_key.rsplit("/", 1)[-1]
            block = image_meta_map.get(norm_key) or image_meta_map.get(fname_key)

            page: int | None = None
            caption: str = ""
            ocr: str = ""
            bbox: tuple[float, ...] | None = None
            if block is not None:
                raw_page_idx = block.get("page_idx")
                raw_page = block.get("page")
                if isinstance(raw_page_idx, int) and not isinstance(raw_page_idx, bool) and raw_page_idx >= 0:
                    page = raw_page_idx + 1
                elif isinstance(raw_page, int) and not isinstance(raw_page, bool) and raw_page >= 1:
                    page = raw_page
                for ckey in ("image_caption", "table_caption", "chart_caption", "caption"):
                    raw_c = block.get(ckey)
                    if isinstance(raw_c, list):
                        c_text = "\n".join(
                            (c.get("text", "") if isinstance(c, dict) else str(c)).strip()
                            for c in raw_c
                            if (c.get("text", "") if isinstance(c, dict) else str(c)).strip()
                        )
                        if c_text:
                            caption = c_text
                            break
                    elif isinstance(raw_c, dict) and "text" in raw_c:
                        caption = str(raw_c["text"]).strip()
                        if caption:
                            break
                    elif isinstance(raw_c, str) and raw_c.strip():
                        caption = raw_c.strip()
                        break
                for okey in ("ocr", "ocr_result"):
                    raw_o = block.get(okey)
                    if isinstance(raw_o, str) and raw_o.strip():
                        ocr = raw_o.strip()
                        break
                if not ocr and block.get("type") in ("text", "equation"):
                    raw_t = block.get("text")
                    if isinstance(raw_t, str) and raw_t.strip():
                        ocr = raw_t.strip()
                raw_b = block.get("bbox")
                if isinstance(raw_b, (list, tuple)) and len(raw_b) == 4 and all(isinstance(x, (int, float)) for x in raw_b):
                    bbox = tuple(float(x) for x in raw_b)

            anchor_start, anchor_end = _find_media_anchor(markdown, key, used_anchor_spans)

            ordinal += 1
            meta = {"archive_member": key}
            occurrence = MediaOccurrence(
                occurrence_id=f"occ-{ordinal:05d}",
                kind="image",
                ordinal=ordinal,
                name=key,
                mime_type=extension_mime(key),
                byte_size=len(payload),
                page=page,
                bbox=bbox,
                caption=caption,
                ocr=ocr,
                width=width,
                height=height,
                anchor_start=anchor_start,
                anchor_end=anchor_end,
                metadata=meta,
            )
            media_occurrences.append(occurrence)
            media_bytes_total += len(payload)
            if sink is not None:
                sink.add(
                    name=key,
                    data=payload,
                    kind="image",
                    ordinal=ordinal,
                    mime_type=occurrence.mime_type,
                    page=page,
                    bbox=bbox,
                    caption=caption,
                    ocr=ocr,
                    width=width,
                    height=height,
                    anchor_start=anchor_start,
                    anchor_end=anchor_end,
                    metadata=meta,
                )

    page_map, reason = _derive_page_map(normalize_markdown(markdown), blocks)
    if reason:
        warnings.append(f"page_map unavailable: {reason}")
    capabilities: dict[str, Any] = {
        "images": bool(media_occurrences),
        "page_map": bool(page_map),
        "structured_json": bool(json_members) and not partial,
    }
    capabilities.update(structure_caps)
    return ArchiveOutcome(
        markdown=markdown,
        media=media_occurrences,
        capabilities=capabilities,
        page_map=page_map,
        warnings=warnings,
        extracted_bytes=len(markdown_bytes) + media_bytes_total
        + sum(len(payload) for _n, payload in json_members),
        markdown_bytes=len(markdown_bytes),
        media_bytes=media_bytes_total,
        partial=partial,
    )



# --------------------------------------------------------------------- 客户端


@dataclass(slots=True)
class _ChannelOutcome:
    raw_markdown: str
    outcome: ArchiveOutcome | None
    model: str
    channel: str
    remote_task_id: str = ""
    compressed_bytes: int = 0


class MineruClient:
    def __init__(self, api_key: str = "", *, model_version: str = "vlm", language: str = "ch",
                 is_ocr: bool = False, enable_formula: bool = True, enable_table: bool = True,
                 timeout: float = 30.0, limits: ResourceLimits | None = None):
        self.api_key = api_key.strip()
        self.model_version = model_version
        self.language = language
        self.is_ocr = is_ocr
        self.enable_formula = enable_formula
        self.enable_table = enable_table
        self.timeout = timeout
        self.limits = limits if limits is not None else ResourceLimits()
        #: `parse()` 的兼容出口（历史契约逐字返回，不做规范化）。
        self._legacy_markdown = ""
        self._legacy_images: dict[str, bytes] = {}
        self._parse_started = 0.0

    # ---------------------------------------------------------------- public

    def channel_for(self, path: Path) -> str:
        """通道选择：有 token 且格式/大小合规 → v4；无 token 且 ≤10MB 且 Agent 格式 → agent；
        都不满足抛 MineruError（上层决定是否 pymupdf 兜底）。

        合同：**有 key 时永不回落 agent**（有 key 的 401/403 不得绕过到免登通道，§13.3）。
        """
        ext = path.suffix.lower()
        size = path.stat().st_size
        if self.api_key:
            if ext not in V4_EXTS:
                raise MineruError(f"v4 unsupported ext: {ext}", code=-60002)
            if size > V4_MAX_BYTES:
                raise MineruError(f"file exceeds v4 200MB limit: {size}", code=-60005)
            return "v4"
        if ext not in AGENT_EXTS:
            raise MineruError(f"agent channel unsupported ext (need v4 token): {ext}", code=-30002)
        if size > AGENT_MAX_BYTES:
            raise MineruError(f"file exceeds agent 10MB limit (need v4 token): {size}", code=-30001)
        return "agent"

    def parse(self, path: Path, *, poll_interval: float = 3.0, poll_timeout: float = 600.0) -> ParsedDocument:
        """兼容入口：返回旧的 `ParsedDocument(markdown/images/channel/model)`。

        markdown **不做**规范化（保持历史逐字契约）；新的结构化消费请用
        `parse_structured()`。语义变更（有意）：非幂等付费 POST 的网络错误不再
        标 retryable，而是 `SUBMISSION_UNKNOWN`（禁止自动重传，见 §20.7F）。
        """
        result = self.parse_structured(path, poll_interval=poll_interval, poll_timeout=poll_timeout)
        return ParsedDocument(
            markdown=self._legacy_markdown,
            images=dict(self._legacy_images),
            channel=result.channel,
            model=result.model,
        )

    def parse_structured(
        self,
        path: Path,
        *,
        poll_interval: float = 3.0,
        poll_timeout: float = 600.0,
        sink: MediaSink | None = None,
        intent_recorder: Callable[[str, dict[str, Any]], None] | None = None,
        request_id: str = "",
        deadline_seconds: float | None = None,
        allow_loopback: bool = False,
    ) -> ParseResult:
        """有界解析：结构化结果 + 安全归档 + 总 deadline + 发送意图。

        `intent_recorder(kind, payload)` 在非幂等付费请求前后被调用（kind ∈ prepared/
        submitted/polling/downloaded）；`submission_unknown` 由异常表达。
        """
        started = time.monotonic()
        self._parse_started = started
        limit = poll_timeout if deadline_seconds is None else min(float(deadline_seconds), float(poll_timeout))
        deadline = started + max(0.001, float(limit))
        own_sink: DictMediaSink | None = None
        effective_sink = sink
        if effective_sink is None:
            own_sink = DictMediaSink()
            effective_sink = own_sink
        self._legacy_images = {}
        self._legacy_markdown = ""

        channel = self.channel_for(path)
        with _io_profile(deadline=deadline, enforce_remote_policy=True, allow_loopback=allow_loopback):
            if channel == "v4":
                outcome = self._parse_v4(
                    path, poll_interval=poll_interval, sink=effective_sink, deadline=deadline,
                    intent_recorder=intent_recorder, request_id=request_id,
                )
            else:
                outcome = self._parse_agent(
                    path, poll_interval=poll_interval, deadline=deadline,
                    intent_recorder=intent_recorder, request_id=request_id,
                )
        if not outcome.raw_markdown.strip():
            raise MineruError(
                "empty parse result (no extractable text)",
                retryable=False,
                code_str=PARSE_EMPTY,
                fix="空解析不得标 done：检查源文件是否受保护/加密，或是否确实无文本。",
            )
        archive = outcome.outcome
        warnings = list(archive.warnings) if archive is not None else []
        capabilities = dict(archive.capabilities) if archive is not None else {}
        capabilities.setdefault("images", bool(self._legacy_images))
        capabilities.setdefault("page_map", False)
        capabilities.setdefault("structured_json", False)
        capabilities["ocr"] = bool(self.is_ocr)
        capabilities["table"] = bool(self.enable_table)
        capabilities["formula"] = bool(self.enable_formula)
        capabilities["channel"] = outcome.channel
        if own_sink is not None:
            self._legacy_images = dict(own_sink.images)
        self._legacy_markdown = outcome.raw_markdown
        quality = "light" if outcome.channel == "agent" else ("partial" if archive is not None and archive.partial else "full")
        if outcome.channel == "v4" and archive is not None and archive.partial:
            warnings.append(PARSE_PARTIAL)
        markdown = normalize_markdown(outcome.raw_markdown)
        media = list(archive.media) if archive is not None else []
        return ParseResult(
            markdown=markdown,
            parser_fingerprint=parser_fingerprint(
                adapter=ADAPTER_VERSION,
                channel=outcome.channel,
                model=outcome.model,
                language=self.language,
                is_ocr=self.is_ocr,
                enable_table=self.enable_table,
                enable_formula=self.enable_formula,
                normalize_version=NORMALIZE_VERSION,
            ),
            channel=outcome.channel,
            model=outcome.model,
            quality=quality,
            capabilities=capabilities,
            page_map=list(archive.page_map) if archive is not None else [],
            media=media,
            warnings=warnings,
            submission_phase="committed",
            remote_task_id=outcome.remote_task_id,
            compressed_bytes=outcome.compressed_bytes,
            extracted_bytes=archive.extracted_bytes if archive is not None else len(outcome.raw_markdown.encode("utf-8")),
            markdown_bytes=len(markdown.encode("utf-8")),
            media_bytes=archive.media_bytes if archive is not None else 0,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    # ---------------------------------------------------------------- v4

    def _v4_headers(self) -> dict:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def _parse_v4(self, path: Path, *, poll_interval: float, sink: MediaSink, deadline: float,
                  intent_recorder: Callable[[str, dict[str, Any]], None] | None,
                  request_id: str) -> _ChannelOutcome:
        # 1) 申请上传链接（本地文件走 file-urls/batch；单次 ≤50 个，本客户端一次一文件一批）
        apply_url = f"{V4_BASE}/file-urls/batch"
        body = {
            "files": [{
                "name": path.name,
                "data_id": path.stem[:120],
                "is_ocr": self.is_ocr,
            }],
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        with _io_profile(limit_bytes=self.limits.json_max_bytes, enforce_remote_policy=True):
            result = self._paid_post(
                apply_url, body, intent_recorder=intent_recorder, request_id=request_id,
                label="v4 apply upload url",
            )
        if result.get("code") != 0:
            raise MineruError(f"v4 apply upload url failed: {result.get('msg')}",
                              code=result.get("code"),
                              retryable=result.get("code") not in _V4_FATAL_CODES)
        batch_id = result["data"]["batch_id"]
        upload_url = result["data"]["file_urls"][0]
        self._note(intent_recorder, "submitted", request_id=request_id, endpoint=upload_url,
                   remote_task_id=str(batch_id))
        # 2) PUT 上传（系统自动提交解析任务）
        with _io_profile(limit_bytes=None, enforce_remote_policy=True):
            payload, warnings = self._upload_payload(path)
            _put_upload(upload_url, payload, max(self.timeout, 300.0))
            del payload
        # 3) 轮询批量结果
        while time.monotonic() < deadline:
            poll_url = f"{V4_BASE}/extract-results/batch/{batch_id}"
            with _io_profile(limit_bytes=self.limits.json_max_bytes, enforce_remote_policy=True):
                data = self._poll_json(poll_url, intent_recorder=intent_recorder,
                                       request_id=request_id, remote_task_id=str(batch_id))
            if data.get("code") != 0:
                raise MineruError(f"v4 poll failed: {data.get('msg')}", code=data.get("code"))
            item = (data["data"].get("extract_result") or [{}])[0]
            state = item.get("state", "")
            if state == "done":
                zip_bytes = self._download_archive(item["full_zip_url"], intent_recorder, request_id)
                archive = _safe_extract_zip(zip_bytes, limits=self.limits, sink=sink)
                if warnings:
                    archive.warnings.extend(warnings)
                self._note(intent_recorder, "downloaded", request_id=request_id,
                           remote_task_id=str(batch_id))
                return _ChannelOutcome(
                    raw_markdown=archive.markdown,
                    outcome=archive,
                    model=self.model_version,
                    channel="v4",
                    remote_task_id=str(batch_id),
                    compressed_bytes=len(zip_bytes),
                )
            if state == "failed":
                raise MineruError(f"v4 parse failed: {item.get('err_msg')}", retryable=False)
            self._sleep_until(poll_interval, deadline)  # waiting-file/pending/running/converting 继续等
        raise MineruError(f"v4 poll timeout after {self._elapsed_s()}", retryable=True)

    @staticmethod
    def _extract_zip(zip_bytes: bytes) -> tuple[str, dict[str, bytes]]:
        """官方 zip 结构：full.md + images/ + *.json。只取 full.md 与图片。

        **兼容入口**（历史测试直接调用）：使用与生产同一套安全归档校验，
        但返回旧形态 `(markdown, images)`；markdown 不做规范化。
        """
        sink = DictMediaSink()
        archive = _safe_extract_zip(zip_bytes, limits=ResourceLimits(), sink=sink)
        return archive.markdown, dict(sink.images)

    # ---------------------------------------------------------------- agent

    def _parse_agent(self, path: Path, *, poll_interval: float, deadline: float,
                     intent_recorder: Callable[[str, dict[str, Any]], None] | None,
                     request_id: str) -> _ChannelOutcome:
        submit_url = f"{AGENT_BASE}/parse/file"
        body = {
            "file_name": path.name,
            "language": self.language,
            "enable_table": self.enable_table,
            "is_ocr": self.is_ocr,
            "enable_formula": self.enable_formula,
        }
        with _io_profile(limit_bytes=self.limits.json_max_bytes, enforce_remote_policy=True):
            result = self._paid_post(submit_url, body, intent_recorder=intent_recorder,
                                     request_id=request_id, label="agent submit parse")
        if result.get("code") != 0:
            raise MineruError(f"agent submit failed: {result.get('msg')}", code=result.get("code"),
                              retryable=result.get("code") not in _AGENT_FATAL_CODES)
        task_id = result["data"]["task_id"]
        upload_url = result["data"]["file_url"]
        self._note(intent_recorder, "submitted", request_id=request_id, endpoint=upload_url,
                   remote_task_id=str(task_id))
        with _io_profile(limit_bytes=None, enforce_remote_policy=True):
            payload, warnings = self._upload_payload(path)
            _put_upload(upload_url, payload, max(self.timeout, 300.0))
            del payload
        while time.monotonic() < deadline:
            poll_url = f"{AGENT_BASE}/parse/{task_id}"
            with _io_profile(limit_bytes=self.limits.json_max_bytes, enforce_remote_policy=True):
                data = self._poll_json(poll_url, intent_recorder=intent_recorder,
                                       request_id=request_id, remote_task_id=str(task_id))
            if data.get("code") != 0:
                raise MineruError(f"agent poll failed: {data.get('msg')}", code=data.get("code"))
            item = data["data"]
            state = item.get("state", "")
            if state == "done":
                md_bytes = self._download_bytes(item["markdown_url"], intent_recorder, request_id)
                archive = ArchiveOutcome(
                    markdown=md_bytes.decode("utf-8", errors="replace"),
                    media=[],
                    capabilities={"images": False, "page_map": False, "structured_json": False},
                    page_map=[],
                    warnings=list(warnings),
                    extracted_bytes=len(md_bytes),
                    markdown_bytes=len(md_bytes),
                    media_bytes=0,
                    partial=False,
                )
                self._note(intent_recorder, "downloaded", request_id=request_id,
                           remote_task_id=str(task_id))
                return _ChannelOutcome(
                    raw_markdown=archive.markdown,
                    outcome=archive,
                    model="pipeline-light",
                    channel="agent",
                    remote_task_id=str(task_id),
                    compressed_bytes=len(md_bytes),
                )
            if state == "failed":
                raise MineruError(f"agent parse failed: {item.get('err_msg')}",
                                  code=item.get("err_code"),
                                  retryable=item.get("err_code") not in _AGENT_FATAL_CODES)
            self._sleep_until(poll_interval, deadline)  # waiting-file/uploading/pending/running 继续等
        raise MineruError(f"agent poll timeout after {self._elapsed_s()}", retryable=True)

    # ---------------------------------------------------------------- helpers

    def _note(self, recorder: Callable[[str, dict[str, Any]], None] | None, kind: str,
              **payload: Any) -> None:
        """向调用方上报发送意图/阶段（kind 见 `SUBMISSION_PHASES`）。回调异常不影响解析。"""
        if recorder is None:
            return
        data: dict[str, Any] = {"kind": kind}
        data.update(payload)
        if data.get("endpoint"):
            data["endpoint"] = redact_endpoint(str(data["endpoint"]))
        recorder(kind, data)

    def _paid_post(self, url: str, body: dict[str, Any], *, intent_recorder: Any,
                   request_id: str, label: str) -> dict:
        """非幂等付费 POST（§20.7F）：发送前落意图；网络层错误 → SUBMISSION_UNKNOWN。

        受理成功但响应丢失时**禁止自动重传**：费用不可逆，必须由用户显式确认。
        """
        payload_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self._note(intent_recorder, "prepared", request_id=request_id, endpoint=url,
                   payload_hash=payload_hash)
        headers = self._v4_headers() if url.startswith(V4_BASE) else {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        self._note(intent_recorder, "send_intent", request_id=request_id, endpoint=url,
                   payload_hash=payload_hash)
        try:
            return _http_json(req, self.timeout)
        except MineruError as exc:
            if exc.http_status is None or exc.http_status == 429 or exc.http_status >= 500:
                self._note(intent_recorder, "submission_unknown", request_id=request_id, endpoint=url)
                raise MineruError(
                    f"submission_unknown: {label} outcome unknown after network error ({exc})",
                    retryable=False,
                    code_str=SUBMISSION_UNKNOWN,
                    fix="受理状态未知且请求非幂等：禁止自动重传；用户确认可能费用后显式重试。",
                ) from exc
            raise

    def _poll_json(self, url: str, *, intent_recorder: Any, request_id: str,
                   remote_task_id: str) -> dict:
        """轮询 GET（可安全透明重试；因此网络错误仍按 retryable 冒泡）。"""
        if url.startswith(V4_BASE):
            req = urllib.request.Request(url, headers=self._v4_headers())
        else:
            req = urllib.request.Request(url)
        with _io_profile(limit_bytes=self.limits.json_max_bytes, enforce_remote_policy=True):
            data = _http_json(req, self.timeout)
        self._note(intent_recorder, "polling", request_id=request_id, endpoint=url,
                   remote_task_id=remote_task_id)
        return data

    def _upload_payload(self, path: Path) -> tuple[bytes, list[str]]:
        """按 §13.2 的「有界缓冲」上传：超过受控缓冲预算即拒绝，不无界 read_bytes。"""
        size = path.stat().st_size
        if size > self.limits.memory_budget_bytes:
            raise MineruError(
                f"source {size} bytes exceeds controlled upload buffer "
                f"{self.limits.memory_budget_bytes} bytes",
                retryable=False,
                code_str=RESOURCE_LIMIT,
                fix="流式上传未实现前退回有界缓冲；超出预算必须拒绝而不是无界读入。",
            )
        with path.open("rb") as stream:
            return _read_bounded(stream, self.limits.memory_budget_bytes), []

    def _download_archive(self, url: str, recorder: Any, request_id: str) -> bytes:
        limit = min(self.limits.archive_max_bytes, self.limits.memory_budget_bytes // 2)
        if limit < 1:
            raise MineruError("archive download buffer budget exhausted",
                              retryable=False, code_str=RESOURCE_LIMIT)
        with _io_profile(limit_bytes=limit, enforce_remote_policy=True):
            return _http_bytes(url, max(self.timeout, 300.0))

    def _download_bytes(self, url: str, recorder: Any, request_id: str) -> bytes:
        limit = min(self.limits.markdown_max_bytes, self.limits.memory_budget_bytes // 12)
        if limit < 1:
            raise MineruError("markdown download buffer budget exhausted",
                              retryable=False, code_str=RESOURCE_LIMIT)
        with _io_profile(limit_bytes=limit, enforce_remote_policy=True):
            return _http_bytes(url, max(self.timeout, 120.0))

    def _sleep_until(self, interval: float, deadline: float) -> None:
        """可取消节奏的等待：夹到剩余 deadline，deadline 已过立即返回（由循环退出）。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        step = min(max(float(interval), 0.001), remaining, 1.0)
        time.sleep(step)

    def _elapsed_s(self) -> str:
        return f"{(time.monotonic() - self._parse_started):.1f}s"
