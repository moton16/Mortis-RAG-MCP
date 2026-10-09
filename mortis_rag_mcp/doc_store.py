"""版本化虚拟文档库（doc_store）——解析事实的唯一权威存储（v0.9.0 C92 / §12、§23.1）。

职责边界（§12.1）：
- 本模块**只依赖标准库**，外加 `from .config import AppConfig` 仅用于类型标注/取值。
  禁止 import `indexer.py` / `server.py` / `_server/*`——依赖必须自底向上。
- 顶层 `dependencies = []` 不可动摇；不引入 ORM、不引入新服务、不引入全局单例。

持久化拓扑（§23.1，最终合同）：

    {cache_root}/{namespace}/doc_store/
      vault_<key>.control.sqlite     本机权威控制面（不在 generation 内，不从快照恢复）
      vault_<key>.mutation.lock      固定 OS 锁（失败即拒，不 fail-open，§12.4）
      vault_<key>/generations/<gen>/docstore.sqlite   生成代次内的文档库

分层事实：
- `ControlStore`：本机控制面。权威持有 store_uuid / vault_binding / 单调 epoch /
  operation_seq / active_document_generation / writer_gate。
- `DocumentStore`：某一 document generation 内的文档库。持有 source 身份、revision
  事实、内容寻址 blob、媒体引用、quota 与备份/恢复。

错误语义一律 fail-closed：未知更高 schema 拒绝（§12.3/§23.1）；OS 锁创建/获取失败
必须抛 `LockUnavailable`（§12.4），不得像 `registry._process_file_lock` 那样 except 后
照常执行。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import AppConfig, is_windows_reserved_segment

# --------------------------------------------------------------------------- 常量

#: 本模块认识的最新 schema 版本。更高版本必须拒绝（fail closed，§12.3/§23.1）。
SCHEMA_VERSION = 1

#: 控制面 mutation 锁的默认超时（秒）。可在 mutation(timeout=...) 覆盖，测试用短超时。
MUTATION_LOCK_TIMEOUT = 5.0

#: media/journal 之外的页面开销估算常量（quota 只做近似，不作 ACID 承诺）。
PAGE_OVERHEAD_BYTES = 256

#: doc_store 默认 quota（MiB），仅当 AppConfig 未提供 doc_store.max_size_mb 时兜底。
DEFAULT_DOC_STORE_MAX_MB = 2048

_DOCSTORE_TABLES = (
    "store_meta",
    "documents",
    "document_revisions",
    "media_blobs",
    "media_occurrences",
    "media_variants",
    "ingest_jobs",
    "ingest_subjobs",
    "auto_seen",
    "derived_generations",
)


# --------------------------------------------------------------------------- 错误


class DocStoreError(RuntimeError):
    """doc_store 系列错误的基类。对外稳定 `code`；`retryable` 决定调用方是否重试。"""

    code: str = "STORE_ERROR"
    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None, fix: str = "") -> None:
        super().__init__(message)
        self.detail = message
        self.fix = fix
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - 仅调试可读性
        parts = [f"[{self.code}] {self.detail}"]
        if self.fix:
            parts.append(f"fix: {self.fix}")
        parts.append(f"retryable={self.retryable}")
        return " | ".join(parts)


class StoragePathError(DocStoreError):
    code = "STORAGE_PATH_INVALID"


class VirtualStorageDisabled(DocStoreError):
    code = "VIRTUAL_STORAGE_DISABLED"


class StoreBindingMismatch(DocStoreError):
    code = "STORE_BINDING_MISMATCH"


class StoreSchemaUnsupported(DocStoreError):
    code = "STORE_SCHEMA_UNSUPPORTED"


class StoreBusy(DocStoreError):
    code = "STORE_BUSY"
    retryable = True


class StoreCorrupt(DocStoreError):
    code = "STORE_CORRUPT"


class StoreQuotaExceeded(DocStoreError):
    code = "STORE_QUOTA_EXCEEDED"


class StoreConflict(DocStoreError):
    code = "STORE_CONFLICT"


class LockUnavailable(DocStoreError):
    code = "LOCK_UNAVAILABLE"
    retryable = True

    def __init__(self, message: str, *, retryable: bool | None = None, fix: str = "") -> None:
        super().__init__(message, retryable=True if retryable is None else retryable, fix=fix)


class SourcePathError(DocStoreError):
    code = "SOURCE_PATH_INVALID"


class StoreContractError(DocStoreError):
    code = "CONTRACT_INVALID"


class SnapshotInvalid(DocStoreError):
    code = "SNAPSHOT_INVALID"


class OwnershipLost(DocStoreError):
    """租约/owner 丢失（续租失败、过期后旧 owner 再写、job 被抢占）。"""

    code = "OWNERSHIP_LOST"


class QueueFull(DocStoreError):
    code = "QUEUE_FULL"
    retryable = True


#: 任务状态机（§12.5）：queued -> parsing -> staged -> done，可 retryable failed 回 queued。
JOB_STATES = ("queued", "parsing", "staged", "done", "failed", "cancelled", "superseded")
#: 非终态（持有/等待资源）。
JOB_ACTIVE_STATES = ("queued", "parsing", "staged")
#: 终态。
JOB_TERMINAL_STATES = ("done", "failed", "cancelled", "superseded")
#: §12.5 提交阶段（远端恢复）：prepared/submitted/polling/downloaded/staged/committed/submission_unknown。
JOB_PHASES = (
    "prepared", "send_intent", "submitted", "polling", "downloaded", "staged", "committed", "submission_unknown",
)
#: 段级 subjob 状态（ordinal >= 1；ordinal 0 是父任务的远端 submission phase）。
SUBJOB_STATES = ("pending", "running", "done", "failed")
#: checkpoint `range` 的语义标签：音频帧 / 音频毫秒 / 文档页（半开区间，不能混用）。
SUBJOB_RANGE_KINDS = ("audio_frames", "audio_ms", "document_pages")
#: 单个 checkpoint 的编码上限（字节）。超限必须**在写前**拒绝，绝不截断字符串。
CHECKPOINT_MAX_BYTES = 1_048_576
#: 释放 checkpoint 引用保护的终态（显式取消/被取代后不再视为可恢复）。
CHECKPOINT_RELEASE_STATES = ("cancelled", "superseded")
#: 租约时长常量（§23.1：租约/锁值必须有常量与配置边界，不散落 magic number）。
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_QUEUE_LIMIT = 1000


# --------------------------------------------------------------------------- 值对象


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """解析后的库路径布局（只做判决，不 mkdir、不写盘，§12.2/§11.2）。"""

    vault_path: Path
    cache_root: Path
    namespace: str
    vault_key: str
    placement: str
    enabled: bool
    doc_store_dir: Path
    vault_dir: Path
    control_path: Path
    mutation_lock_path: Path
    generations_dir: Path
    blocked_reason: str = ""

    @property
    def writable(self) -> bool:
        """虚拟写入是否被允许：需持久化开启且真实归属不在库内（§11.2/§17.4）。"""
        return self.enabled and not self.blocked_reason


@dataclass(frozen=True, slots=True)
class ControlState:
    store_uuid: str
    vault_binding: str
    epoch: int
    operation_seq: int
    active_document_generation: str
    writer_gate: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    generation_id: str
    path: str
    kind: str
    state: str
    created_at: float


@dataclass(frozen=True, slots=True)
class DocumentRevision:
    revision_id: str
    doc_id: str
    source_sha256: str
    render_sha256: str
    parser_fingerprint: str
    parsed_markdown: str
    page_map: list
    quality: str
    capabilities: Mapping[str, Any]
    state: str
    policy_version: str
    created_at: float


@dataclass(frozen=True, slots=True)
class ActiveDocument:
    doc_id: str
    source: str
    visibility: str
    active_revision: str
    revision: DocumentRevision


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    doc_id: str
    source: str
    visibility: str
    active_revision: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class StagedRevision:
    doc_id: str
    revision_id: str
    created_at: float


@dataclass(frozen=True, slots=True)
class StoreMeta:
    schema_version: int
    store_uuid: str
    vault_binding: str
    vault_epoch: int
    change_seq: int
    created_at: float
    generation_id: str
    document_count: int
    revision_count: int
    blob_count: int


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    logical_bytes: int
    limit_bytes: int
    ratio: float
    markdown_bytes: int
    media_bytes: int
    page_bytes: int
    media_count: int
    over_limit: bool


@dataclass(frozen=True, slots=True)
class JobRecord:
    """`ingest_jobs` 一行的值对象（§12.5）。"""

    job_id: str
    doc_id: str
    source: str
    source_sha256: str
    parser_fingerprint: str
    vault_epoch: int
    request_seq: int
    state: str
    phase: str
    owner_token: str
    lease_until: float
    attempts: int
    retry_after: float
    result_revision: str
    error_code: str
    error_summary: str
    created_at: float
    updated_at: float

    @property
    def lease_active(self) -> bool:
        return bool(self.owner_token) and self.lease_until > time.time()


@dataclass(frozen=True, slots=True)
class GenerationPin:
    """固定捕获版本 handle（E04-c）。

    读取/导出/备份共用同一句柄：`validate` 在批次与最终响应前验租，过期**不复活**
    （只能重新捕获），绝不把旧 chunk 与新 media 拼成一个包。
    """

    pin_id: str
    generation_id: str
    owner: str
    lease_until: float
    change_seq: int
    epoch: int


@dataclass(frozen=True, slots=True)
class SubjobRecord:
    """`ingest_subjobs` 一行的值对象（E02）：段级 checkpoint 的真实读回形态。"""

    job_id: str
    ordinal: int
    state: str
    input_hash: str
    range_kind: str
    range_start: int | None
    range_end: int | None
    remote_task_id: str
    checkpoint: str
    error: str


@dataclass(frozen=True, slots=True)
class DerivedGeneration:
    """`derived_generations` 一行的值对象（C95）：派生层相对文档事实的代次进度。"""

    profile_key: str
    change_seq: int
    chunker_fingerprint: str
    space_fingerprint: str
    status: str
    last_error: str


@dataclass(frozen=True, slots=True)
class MediaOccurrenceSpec:
    """引用**已存在** blob 的媒体出现规格（C94 流式 sink 用：解析阶段先落 blob）。"""

    occurrence_id: str
    blob_id: str
    kind: str
    ordinal: int
    mime_type: str = ""
    page: int | None = None
    bbox: Any = None
    t_start_ms: int | None = None
    t_end_ms: int | None = None
    caption: str = ""
    ocr: str = ""
    width: int | None = None
    height: int | None = None
    #: 正文字符半开区间 `[anchor_start, anchor_end)`——**媒体尺寸是 width/height，
    #: 绝不能写进 anchor 列**（E08-a）。缺省 None = 没有证据，不是 0。
    anchor_start: int | None = None
    anchor_end: int | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MediaSpec:
    """一条媒体出现（occurrence）的输入规格；`data` 是原始字节（内容寻址）。"""

    occurrence_id: str
    kind: str
    mime_type: str
    data: bytes
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    caption: str = ""
    ocr: str = ""
    page: int | None = None
    bbox: Any = None
    t_start_ms: int | None = None
    t_end_ms: int | None = None
    anchor_start: int | None = None
    anchor_end: int | None = None
    metadata: Mapping[str, Any] | None = None


def _resolve_media_anchor(item: MediaOccurrenceSpec) -> tuple[int | None, int | None]:
    """解析 occurrence 的**正文**字符半开区间 `[start, end)`（E08-a）。

    优先级：显式 `anchor_start/anchor_end` 字段 > 已核验的 metadata 锚点 >
    无证据（`None`，不猜）。媒体尺寸 `width/height` **不是**正文位置，任何情况下
    都不参与这个解析——旧实现把它们写进 anchor 列，读取侧据此定位必然错位。
    """
    meta = dict(item.metadata or {})
    raw_start = item.anchor_start if item.anchor_start is not None else meta.get("anchor_start")
    raw_end = item.anchor_end if item.anchor_end is not None else meta.get("anchor_end")
    if raw_start is None and raw_end is None:
        return None, None
    if raw_start is None or raw_end is None:
        raise StoreContractError(
            "media anchor requires both anchor_start and anchor_end",
            fix="给出完整半开区间；只有一端没有证据时不猜另一端。",
        )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (raw_start, raw_end)):
        raise StoreContractError("media anchor must be integer character offsets")
    if raw_start < 0 or raw_end < raw_start:
        raise StoreContractError(
            "media anchor must be a nonnegative half-open span",
            fix="anchor_end 必须 >= anchor_start。",
        )
    return raw_start, raw_end


# --------------------------------------------------------------------------- 路径工具


def is_within(child: str | Path, parent: str | Path) -> bool:
    """真实归属判决：realpath + normcase 后做等值/包含比较（§12.2，不用字符串前缀）。"""
    try:
        c = os.path.normcase(os.path.realpath(os.fspath(child)))
        p = os.path.normcase(os.path.realpath(os.fspath(parent)))
    except (OSError, ValueError):
        return False
    if not p:
        return False
    if c == p:
        return True
    return c.startswith(p.rstrip("\\/") + os.sep)


def _registry_read_path() -> Path:
    """注册表的**只读**定位：env 覆盖 > 新名 > 旧名独占。

    刻意不调用 `registry.registry_path()`：那条路径会把「新名不存在而旧名存在」
    当作触发 `~/.vault_mcp` → `~/.mortis_rag_mcp` 原子迁移的时机。归属检测是读
    判决，不该在别人的构造函数里搬动宿主数据目录。
    """
    override = (os.getenv("MORTIS_RAG_REGISTRY", "").strip()
                or os.getenv("VAULT_MCP_REGISTRY", "").strip())
    if override:
        return Path(override).expanduser()
    new = Path.home() / ".mortis_rag_mcp" / "vaults.toml"
    if new.is_file():
        return new
    old = Path.home() / ".vault_mcp" / "vaults.toml"
    return old if old.is_file() else new


def registered_vault_paths() -> list[str]:
    """读取已注册库路径（只读、无迁移副作用）；任何失败都返回 []（不阻断主流程）。"""
    try:
        from . import registry as _registry  # 惰性：避免顶层循环依赖

        entries = _registry.VaultRegistry(_registry_read_path()).load()
        return [str(entry.path) for entry in entries]
    except Exception:
        return []


def vault_cache_key(config: AppConfig, vault_path: str | Path) -> str:
    """库缓存身份 key，必须与 `indexer.MarkdownIndexer._cache_key` 逐字等价。

    优先显式 cache.id（免疫路径拼写差异）→ sha256(cache.id)[:16]；
    否则 raw = os.fspath(vault.resolve())，norm = normcase(realpath(raw))，
    sha256(norm)[:16]。
    """
    if config.cache.id:
        return hashlib.sha256(config.cache.id.encode("utf-8")).hexdigest()[:16]
    raw = os.fspath(Path(vault_path).resolve())
    normalized = os.path.normcase(os.path.realpath(raw))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _detect_blocked_reason(
    placement: str,
    cache_root: Path,
    vault: Path,
    registered_vaults: Sequence[str | Path] | None,
) -> str:
    """§11.2 真实归属检测：返回非空 = 禁止虚拟摄取写入（不偷偷改路径）。

    - `placement == "vault"`：subdir 是显式便携布局，**允许落在本库内**；只拒绝
      它逃出库根或解析到库根本身（越界/上跳）。
    - `placement == "home"`：cache_root 的真实路径落在本库或**显式传入的**已注册库内
      → 拒绝（用户可能把 cache.dir 指向库内，或经 symlink/junction 落库内）。

    `registered_vaults=None` 表示「只检测当前库」——本函数**不隐式读宿主注册表**：
    读路径不该有宿主副作用（连注册表迁移都不该被触发）。跨库维度由写入门禁
    `DocumentStore._enforce_writable` 补齐，或由调用方显式传入注册库列表（doctor）。
    """
    if placement == "vault":
        if not is_within(cache_root, vault):
            return (
                f"cache.subdir 解析后逃出知识库根：{cache_root} 不在 {vault} 内。"
                "修复：把 cache.subdir 改成库内的安全非根子树（如 .mcp_cache），不要上跳。"
            )
        if os.path.normcase(os.path.realpath(cache_root)) == os.path.normcase(
            os.path.realpath(vault)
        ):
            return (
                f"cache.subdir 解析到知识库根本身：{cache_root}。"
                "修复：指定一个库内的非根子树，避免缓存与用户源文件混在同一目录。"
            )
        return ""

    candidates: list[str] = [os.fspath(vault)]
    if registered_vaults:
        candidates.extend(os.fspath(v) for v in registered_vaults)
    for candidate in candidates:
        if candidate and is_within(cache_root, candidate):
            return (
                f"cache.dir 的真实路径落在知识库内（{cache_root} ⊆ {candidate}）。"
                "虚拟摄取会向用户库写入文档库，已拒绝。"
                "修复：把 cache.dir（或环境变量 MORTIS_RAG_CACHE_DIR）指向库外的本地目录。"
            )
    return ""


def resolve_storage_layout(
    config: AppConfig,
    vault_path: str | Path,
    *,
    registered_vaults: Sequence[str | Path] | None = None,
) -> StorageLayout:
    """解析库路径布局；只做等值/包含判决，不 mkdir、不写盘（§12.2/§23.1）。"""
    vault = Path(vault_path).expanduser()
    placement = (config.cache.placement or "home").lower()
    if placement == "vault":
        subdir = config.cache.subdir or ".mcp_cache"
        cache_root = vault / subdir
    else:
        cache_root = Path(config.cache.dir).expanduser()

    namespace = config.cache.namespace or "default"
    key = vault_cache_key(config, vault)

    doc_store_dir = cache_root / namespace / "doc_store"
    vault_dir = doc_store_dir / f"vault_{key}"
    blocked = _detect_blocked_reason(placement, cache_root, vault, registered_vaults)

    return StorageLayout(
        vault_path=vault,
        cache_root=cache_root,
        namespace=namespace,
        vault_key=key,
        placement=placement,
        enabled=bool(config.cache.enabled),
        doc_store_dir=doc_store_dir,
        vault_dir=vault_dir,
        control_path=doc_store_dir / f"vault_{key}.control.sqlite",
        mutation_lock_path=doc_store_dir / f"vault_{key}.mutation.lock",
        generations_dir=vault_dir / "generations",
        blocked_reason=blocked,
    )


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_source_path(source: str, vault_path: str | Path) -> str:
    """把 source 规范成库内相对 POSIX 路径（含原后缀），否则抛 `SourcePathError`。

    拒绝：空、绝对路径、盘符、UNC、NUL、"."、".."、任何上跳、resolve 后不在库内。
    `a.pdf` 与 `a.docx` 是不同源（后缀不可丢，§12.2）。
    """
    fix = "传入库内相对路径，例如 papers/attention.pdf（使用正斜杠，不得上跳或绝对化）"
    if not isinstance(source, str) or not source:
        raise SourcePathError("source 不能为空", fix=fix)
    if "\x00" in source:
        raise SourcePathError("source 含 NUL 字符", fix=fix)

    raw = source.replace("\\", "/")
    if raw.startswith("//"):
        raise SourcePathError(f"source 是 UNC 路径，禁止：{source!r}", fix=fix)
    if _DRIVE_RE.match(raw):
        raise SourcePathError(f"source 含盘符，禁止：{source!r}", fix=fix)
    if raw.startswith("/"):
        raise SourcePathError(f"source 是绝对路径，禁止：{source!r}", fix=fix)

    parts = raw.split("/")
    for part in parts:
        if part in ("", "."):
            raise SourcePathError(f"source 含空段或 '.'，禁止：{source!r}", fix=fix)
        if part == "..":
            raise SourcePathError(f"source 含上跳 '..'，禁止：{source!r}", fix=fix)
        if is_windows_reserved_segment(part):
            raise SourcePathError(
                f"source 含 Windows 设备名（{part!r}），禁止：{source!r}",
                fix="Win32 下设备名不是普通文件；请重命名后再摄取。",
            )

    rel = "/".join(parts)
    full = os.path.realpath(os.path.join(os.fspath(Path(vault_path).expanduser()), *parts))
    if not is_within(full, vault_path):
        raise SourcePathError(
            f"source 经符号链接解析后越出知识库根：{source!r}", fix=fix
        )
    return rel


# --------------------------------------------------------------------------- OS 锁

_LOCK_REGISTRY: dict[str, "_FileMutex"] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


class _FileMutex:
    """线程内可重入 + 跨线程/跨进程互斥的固定 OS 锁（§12.4）。

    - 进程内：`threading.Lock` 保证同一路径同一时刻只有一个线程真正持锁；同一线程
      重入直接放行（嵌套 mutation 不自死锁）。
    - 跨进程：Windows msvcrt / POSIX fcntl 的字节范围锁。
    - 超时或创建/打开失败一律不 fail-open：返回 False 或抛 `LockUnavailable`。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._mutex = threading.Lock()
        self._local = threading.local()
        self._handle: Any = None

    def acquire(self, timeout: float) -> bool:
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            return True
        if not self._mutex.acquire(timeout=max(0.0, timeout)):
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")
        except OSError as exc:
            self._mutex.release()
            raise LockUnavailable(
                f"无法创建/打开 mutation 锁文件 {self.path}: {exc}",
                fix="检查缓存根目录权限；不要把缓存根放到只读或不可写位置。",
            )
        if not self._acquire_os(handle, timeout):
            try:
                handle.close()
            finally:
                self._mutex.release()
            return False
        self._handle = handle
        self._local.depth = 1
        return True

    def release(self) -> None:
        depth = getattr(self._local, "depth", 0)
        if depth > 1:
            self._local.depth = depth - 1
            return
        self._local.depth = 0
        handle = self._handle
        self._handle = None
        try:
            if handle is not None:
                _release_os_lock(handle)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            self._mutex.release()

    @staticmethod
    def _acquire_os(handle: Any, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if _try_os_lock(handle):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.03)


def _try_os_lock(handle: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_os_lock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _file_mutex_for(path: str | Path) -> _FileMutex:
    """按规范化真实路径复用同一 `_FileMutex`，让同进程多实例也互相排斥。"""
    try:
        key = os.path.normcase(os.path.realpath(os.fspath(path)))
    except OSError:
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _LOCK_REGISTRY_GUARD:
        mutex = _LOCK_REGISTRY.get(key)
        if mutex is None:
            mutex = _FileMutex(path)
            _LOCK_REGISTRY[key] = mutex
        return mutex


@contextlib.contextmanager
def _mutation_lock(layout: StorageLayout, timeout: float = MUTATION_LOCK_TIMEOUT):
    mutex = _file_mutex_for(layout.mutation_lock_path)
    if not mutex.acquire(timeout):
        raise LockUnavailable(
            f"mutation 锁被占用，{timeout:.1f}s 内未取得：{layout.mutation_lock_path}",
            fix="稍后重试；若长期占用，检查是否有崩溃残留的 writer 进程。",
        )
    try:
        yield
    finally:
        mutex.release()


# --------------------------------------------------------------------------- 校验/JSON

_MAX_JSON_STRING = 1_000_000
_MAX_JSON_DEPTH = 12
_MAX_JSON_ITEMS = 200_000


def _validate_json_value(value: Any, where: str, depth: int = 0) -> None:
    """§12.3 末段：JSON 字段入库前校验类型/长度/有限数字。"""
    if depth > _MAX_JSON_DEPTH:
        raise StoreContractError(f"{where} 嵌套过深（> {_MAX_JSON_DEPTH}）")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StoreContractError(f"{where} 含非有限数字（NaN/Inf），禁止入库")
        return
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING:
            raise StoreContractError(f"{where} 字符串过长（> {_MAX_JSON_STRING}）")
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_JSON_ITEMS:
            raise StoreContractError(f"{where} 数组元素过多（> {_MAX_JSON_ITEMS}）")
        for item in value:
            _validate_json_value(item, where, depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_JSON_ITEMS:
            raise StoreContractError(f"{where} 对象键过多（> {_MAX_JSON_ITEMS}）")
        for key, item in value.items():
            if not isinstance(key, str):
                raise StoreContractError(f"{where} JSON 对象键必须是字符串")
            if len(key) > 4096:
                raise StoreContractError(f"{where} JSON 键过长")
            _validate_json_value(item, where, depth + 1)
        return
    raise StoreContractError(f"{where} 含不可序列化类型 {type(value).__name__}")


def _dump_json(value: Any, where: str) -> str:
    _validate_json_value(value, where)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StoreContractError(f"{where} 无法序列化为 JSON: {exc}") from exc


def _load_json(text: str | None, where: str, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise StoreCorrupt(f"{where} 不是合法 JSON: {exc}") from exc


def _validate_page_map(page_map: Any, where: str = "page_map") -> list:
    if page_map is None:
        return []
    if not isinstance(page_map, (list, tuple)):
        raise StoreContractError(f"{where} 必须是数组")
    normalized: list = []
    for index, page in enumerate(page_map):
        if page is None:
            normalized.append(None)
            continue
        if isinstance(page, bool) or not isinstance(page, int):
            raise StoreContractError(f"{where}[{index}] 页码必须是整数或 null")
        if page < 1:
            raise StoreContractError(f"{where}[{index}] 页码必须 >= 1（1-based）")
        normalized.append(int(page))
    return normalized


def _validate_opt_int(value: Any, where: str, *, min_value: int | None = None,
                      max_value: int | None = None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreContractError(f"{where} 必须是整数或 null")
    if min_value is not None and value < min_value:
        raise StoreContractError(f"{where} 必须 >= {min_value}")
    if max_value is not None and value > max_value:
        raise StoreContractError(f"{where} 必须 <= {max_value}")
    return int(value)


def _vault_binding(layout: StorageLayout) -> str:
    """本机绑定指纹（跨进程稳定，§12.2）。

    算法（v1）::

        vault_real = normcase(realpath(vault_path))
        payload    = "v1|" + vault_real + "|" + vault_key + "|" + namespace
        binding    = sha256(payload.encode("utf-8")).hexdigest()[:32]

    同一显式 `cache.id` 绑定两个不同实际库时，`vault_key` 相同但 `vault_real` 不同，
    指纹不同 → 写入被拒（StoreBindingMismatch）。
    """
    vault_real = os.path.normcase(os.path.realpath(os.fspath(layout.vault_path)))
    payload = f"v1|{vault_real}|{layout.vault_key}|{layout.namespace}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _configure_conn(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def _is_locked_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


# --------------------------------------------------------------------------- ControlStore

_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version INTEGER NOT NULL,
  store_uuid TEXT NOT NULL,
  vault_binding TEXT NOT NULL,
  vault_epoch INTEGER NOT NULL,
  operation_seq INTEGER NOT NULL DEFAULT 0,
  active_document_generation TEXT NOT NULL DEFAULT '',
  writer_gate INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_registry (
  generation_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


class ControlStore:
    """本机控制面 `vault_<key>.control.sqlite`（不在 generation 内，§12.4/§23.1）。

    单行 `control_meta` 是权威发布指针；`generation_registry` 记录代次路径。以下对象
    属于后续卡，**本 Lane 不建**（见 §23.1）：`generation_pins`/`profile_state`/
    `payment_authorizations`/`request_intents`（C97/C98/C100）。
    """

    def __init__(self, layout: StorageLayout) -> None:
        self.layout = layout
        self._binding = _vault_binding(layout)
        self._tls = threading.local()
        self._degraded = False

    # ------------------------------------------------------------------ conn io

    def _conn(self) -> sqlite3.Connection | None:
        return getattr(self._tls, "conn", None)

    def _open_conn(self) -> sqlite3.Connection:
        path = self.layout.control_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            _configure_conn(conn)
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise StoreCorrupt(
                f"控制库损坏或不是 SQLite 数据库：{path}: {exc}",
                fix="保留文件，勿删除；用 doctor 诊断或从备份恢复。",
            ) from exc
        except Exception:
            conn.close()
            raise
        self._tls.conn = conn
        return conn

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn()
        if conn is None:
            conn = self._open_conn()
        return conn

    def open(self, *, create: bool = True, write: bool = False) -> None:
        """打开控制库；`create=False` 且文件不存在 → StoreCorrupt（明确 code）。"""
        path = self.layout.control_path
        exists = path.is_file() and path.stat().st_size > 0
        if not exists and not create:
            raise StoreCorrupt(
                f"控制库不存在且 create=False: {path}",
                fix="先用 create=True 初始化，或检查 cache.dir / namespace 是否指向了错误位置。",
            )
        conn = self._open_conn()
        if not exists:
            self._create_schema(conn)
            return
        row = self._read_meta(conn)
        schema_version = int(row[0])
        if schema_version > SCHEMA_VERSION:
            raise StoreSchemaUnsupported(
                f"控制库 schema_version={schema_version} 高于本程序支持 {SCHEMA_VERSION}",
                fix="升级 Mortis-RAG-MCP；禁止降级或猜字段读取。",
            )
        if schema_version != SCHEMA_VERSION:
            # 低于当前版本同样不是已知 schema：fail closed，不按 v1 猜字段（§23.1）。
            raise StoreSchemaUnsupported(
                f"控制库 schema_version={schema_version} 不是已知版本（本程序支持 {SCHEMA_VERSION}）",
                fix="保留原文件，用匹配版本的程序打开或从备份恢复；禁止自动迁移。",
            )
        if str(row[2]) != self._binding:
            if write:
                raise StoreBindingMismatch(
                    "控制库的 vault_binding 与本次绑定不一致"
                    f"（库内 {row[2]} ≠ 计算 {self._binding}）",
                    fix="同一 cache.id 不得绑定两个实际库；检查 cache.id / vault 路径是否被改动。",
                )
            # 读模式降级：允许只读检视，但任何写入都会被 mutation() 再次拒绝。
            self._degraded = True

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_CONTROL_SCHEMA)
        now = time.time()
        conn.execute(
            "INSERT INTO control_meta (id, schema_version, store_uuid, vault_binding, "
            "vault_epoch, operation_seq, active_document_generation, writer_gate, created_at) "
            "VALUES (1, ?, ?, ?, 1, 0, '', 1, ?)",
            (SCHEMA_VERSION, uuid.uuid4().hex, self._binding, now),
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    def _read_meta(self, conn: sqlite3.Connection) -> tuple:
        try:
            row = conn.execute(
                "SELECT schema_version, store_uuid, vault_binding, vault_epoch, "
                "operation_seq, active_document_generation, writer_gate "
                "FROM control_meta WHERE id = 1"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise StoreCorrupt(
                f"控制库损坏或不是 SQLite 数据库：{self.layout.control_path}: {exc}",
                fix="保留文件，勿删除；用 doctor 诊断或从备份恢复。",
            ) from exc
        if row is None:
            raise StoreCorrupt(
                f"控制库缺少 control_meta 单行：{self.layout.control_path}",
                fix="控制库不完整，需从备份恢复。",
            )
        return row

    def _write(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
        try:
            conn.execute(sql, params)
            conn.commit()
        except sqlite3.OperationalError as exc:
            if _is_locked_error(exc):
                raise StoreBusy(f"控制库忙：{exc}", fix="稍后重试。") from exc
            raise StoreCorrupt(f"控制库写入失败：{exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise StoreCorrupt(f"控制库写入失败：{exc}") from exc

    # ------------------------------------------------------------------ mutation

    @contextlib.contextmanager
    def mutation(self, timeout: float = MUTATION_LOCK_TIMEOUT):
        """严格 OS 锁（线程内可重入，跨线程/跨进程互斥）；失败抛 `LockUnavailable`。"""
        with _mutation_lock(self.layout, timeout):
            if self._degraded:
                raise StoreBindingMismatch(
                    "控制库处于绑定降级（只读）状态，拒绝写入",
                    fix="恢复正确的 cache.id / vault 路径，或从备份恢复控制库。",
                )
            if self._conn() is None:
                self.open(create=True, write=True)
            conn = self._require_conn()
            depth = int(getattr(self._tls, "mutation_depth", 0))
            self._tls.mutation_depth = depth + 1
            try:
                yield conn
            except BaseException:
                # 与 DocumentStore.mutation 同因：异常逃逸不得留下未提交事务
                # （连接是线程复用的，下一次 commit 会把半截写入一并写实）。
                self._rollback_quietly(conn)
                raise
            else:
                # 只有最外层帧才清理「未显式 commit 的残留写入」：可重入锁允许嵌套
                # mutation，内层正常退出若也 rollback 会回滚外层未提交事务（D5）。
                if depth == 0 and conn.in_transaction:
                    self._rollback_quietly(conn)
            finally:
                self._tls.mutation_depth = depth

    @staticmethod
    def _rollback_quietly(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except Exception:
            pass

    # ------------------------------------------------------------------ state/api

    def state(self) -> ControlState:
        with self.mutation():
            row = self._read_meta(self._require_conn())
        return ControlState(
            store_uuid=str(row[1]),
            vault_binding=str(row[2]),
            epoch=int(row[3]),
            operation_seq=int(row[4]),
            active_document_generation=str(row[5]),
            writer_gate=int(row[6]),
            schema_version=int(row[0]),
        )

    def bump_epoch(self) -> int:
        """单调 +1 并持久化（旧 worker 凭旧 epoch 不能通行，§12.4/§12.6）。"""
        with self.mutation():
            conn = self._require_conn()
            row = self._read_meta(conn)
            new_epoch = int(row[3]) + 1
            self._write(conn, "UPDATE control_meta SET vault_epoch = ? WHERE id = 1", (new_epoch,))
            return new_epoch

    def next_operation_seq(self) -> int:
        """单调 +1 并持久化（§20.7F）。"""
        with self.mutation():
            conn = self._require_conn()
            row = self._read_meta(conn)
            new_seq = int(row[4]) + 1
            self._write(conn, "UPDATE control_meta SET operation_seq = ? WHERE id = 1", (new_seq,))
            return new_seq

    def allocate_generation(self) -> str:
        """分配下一个 document generation id（`g0001` 递增）并登记。"""
        with self.mutation():
            conn = self._require_conn()
            row = conn.execute(
                "SELECT COALESCE(MAX(CAST(SUBSTR(generation_id, 2) AS INTEGER)), 0) "
                "FROM generation_registry WHERE generation_id LIKE 'g%'"
            ).fetchone()
            number = int(row[0] or 0) + 1
            gen_id = f"g{number:04d}"
            gen_path = self.layout.generations_dir / gen_id
            conn.execute(
                "INSERT OR REPLACE INTO generation_registry "
                "(generation_id, path, kind, state, created_at) VALUES (?, ?, 'document', 'ready', ?)",
                (gen_id, str(gen_path), time.time()),
            )
            conn.commit()
            return gen_id

    def set_active_document_generation(self, gen_id: str) -> None:
        """切换 active generation；必须已登记且 kind='document'。"""
        with self.mutation():
            conn = self._require_conn()
            row = conn.execute(
                "SELECT kind FROM generation_registry WHERE generation_id = ?", (gen_id,)
            ).fetchone()
            if row is None or str(row[0]) != "document":
                raise StoreContractError(
                    f"generation {gen_id!r} 未登记或不是 document 类型",
                    fix="先 register_generation(...kind='document') 再切换 active 指针。",
                )
            self._write(
                conn,
                "UPDATE control_meta SET active_document_generation = ? WHERE id = 1",
                (gen_id,),
            )

    def register_generation(self, gen_id: str, path: str | Path, kind: str, state: str) -> None:
        with self.mutation():
            conn = self._require_conn()
            conn.execute(
                "INSERT OR REPLACE INTO generation_registry "
                "(generation_id, path, kind, state, created_at) VALUES (?, ?, ?, ?, ?)",
                (gen_id, str(path), str(kind), str(state), time.time()),
            )
            conn.commit()

    def _ensure_lifecycle_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS generation_pins (pin_id TEXT PRIMARY KEY, "
                     "generation_id TEXT NOT NULL, owner TEXT NOT NULL, lease_until REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS import_confirmations (snapshot_sha256 TEXT PRIMARY KEY, "
                     "trusted INTEGER NOT NULL, generation_id TEXT NOT NULL, confirmed_at REAL NOT NULL)")
        conn.commit()

    @contextlib.contextmanager
    def pin_generation(self, *, lease_seconds: float = 300):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise StoreContractError("Invalid generation pin lease")
        token = uuid.uuid4().hex
        with self.mutation() as conn:
            self._ensure_lifecycle_schema(conn)
            generation = str(self._read_meta(conn)[5])
            if not generation:
                raise StoreConflict("No active generation to pin")
            conn.execute("INSERT INTO generation_pins VALUES (?, ?, ?, ?)",
                         (token, generation, str(os.getpid()), time.time() + lease_seconds))
            conn.commit()
        try:
            yield generation
        finally:
            with self.mutation() as conn:
                conn.execute("DELETE FROM generation_pins WHERE pin_id = ?", (token,))
                conn.commit()

    def acquire_generation_pin(self, owner: str, *, lease_seconds: float = 300) -> tuple[str, str, float]:
        """固定**当前活动代**并返回 `(pin_id, generation_id, lease_until)`（E04-c）。"""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise StoreContractError("Invalid generation pin lease")
        token = uuid.uuid4().hex
        with self.mutation() as conn:
            self._ensure_lifecycle_schema(conn)
            generation = str(self._read_meta(conn)[5])
            if not generation:
                raise StoreConflict("No active generation to pin")
            lease_until = time.time() + lease_seconds
            conn.execute("INSERT INTO generation_pins VALUES (?, ?, ?, ?)",
                         (token, generation, owner, lease_until))
            conn.commit()
        return token, generation, lease_until

    def renew_generation_pin(self, pin_id: str, owner: str, *, lease_seconds: float = 300) -> bool:
        """未到期 + 同 owner 的 CAS 续租；过期/换 owner 一律 0 行（不复活）。"""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise StoreContractError("Invalid generation pin lease")
        now = time.time()
        with self.mutation() as conn:
            self._ensure_lifecycle_schema(conn)
            cursor = conn.execute(
                "UPDATE generation_pins SET lease_until = ? "
                "WHERE pin_id = ? AND owner = ? AND lease_until > ?",
                (now + lease_seconds, pin_id, owner, now),
            )
            conn.commit()
            return cursor.rowcount == 1

    def release_generation_pin(self, pin_id: str, owner: str) -> bool:
        with self.mutation() as conn:
            self._ensure_lifecycle_schema(conn)
            cursor = conn.execute(
                "DELETE FROM generation_pins WHERE pin_id = ? AND owner = ?", (pin_id, owner))
            conn.commit()
            return cursor.rowcount == 1

    def pin_is_live(self, pin_id: str, owner: str, *, generation_id: str = "") -> bool:
        """新鲜控制面读：租约未到期且仍指向（可选的）同一 generation。"""
        with self.mutation() as conn:
            self._ensure_lifecycle_schema(conn)
            row = conn.execute(
                "SELECT generation_id, lease_until FROM generation_pins WHERE pin_id = ? AND owner = ?",
                (pin_id, owner),
            ).fetchone()
        if row is None:
            return False
        if float(row[1]) <= time.time():
            return False
        return (not generation_id) or str(row[0]) == generation_id

    def generation_is_pinned(self, generation_id: str) -> bool:
        with self.mutation() as conn:
            self._ensure_lifecycle_schema(conn)
            return conn.execute("SELECT 1 FROM generation_pins WHERE generation_id = ? "
                                "AND lease_until > ? LIMIT 1", (generation_id, time.time())).fetchone() is not None

    def publish_import(self, generation_id: str, *, expected_generation: str,
                       expected_epoch: int, store_uuid: str, snapshot_sha256: str = "",
                       trusted: bool = False) -> int:
        with self.mutation() as conn:
            self._ensure_lifecycle_schema(conn)
            row = self._read_meta(conn)
            if str(row[5]) != expected_generation or int(row[3]) != expected_epoch:
                raise StoreConflict("Import CAS failed: active generation or epoch changed")
            record = conn.execute("SELECT path, state FROM generation_registry WHERE generation_id = ?",
                                  (generation_id,)).fetchone()
            if record is None or str(record[1]) != "validated":
                raise SnapshotInvalid("Import generation has not been validated")
            if not is_within(record[0], self.layout.generations_dir):
                raise SnapshotInvalid("Import generation escaped local storage")
            epoch = int(row[3]) + 1
            conn.execute("UPDATE control_meta SET active_document_generation = ?, vault_epoch = ?, "
                         "store_uuid = ? WHERE id = 1", (generation_id, epoch, store_uuid))
            conn.execute("UPDATE generation_registry SET state = 'retained' WHERE generation_id = ?",
                         (expected_generation,))
            conn.execute("UPDATE generation_registry SET state = 'ready' WHERE generation_id = ?",
                         (generation_id,))
            if snapshot_sha256:
                conn.execute("INSERT OR REPLACE INTO import_confirmations VALUES (?, ?, ?, ?)",
                             (snapshot_sha256, int(trusted), generation_id, time.time()))
            conn.commit()
            return epoch

    def list_generations(self) -> list[GenerationRecord]:
        with self.mutation():
            conn = self._require_conn()
            rows = conn.execute(
                "SELECT generation_id, path, kind, state, created_at FROM generation_registry "
                "ORDER BY generation_id"
            ).fetchall()
        return [GenerationRecord(str(r[0]), str(r[1]), str(r[2]), str(r[3]), float(r[4])) for r in rows]

    # ------------------------------------------- 付费授权 / 发送意图（§20.7B/§20.7F）

    def _ensure_payment_schema(self, conn: sqlite3.Connection) -> None:
        """惰性补建付费面表：既有控制库（schema_version=1）无需迁移即可获得。

        与 lifecycle 表同一策略：**只加表、不改版本号**，避免把「未建表」误判成本机
        控制库损坏；表内只存哈希/定位与脱敏阶段，不存正文或 key。
        """
        conn.execute("CREATE TABLE IF NOT EXISTS payment_authorizations ("
                     "profile_fingerprint TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                     "cost_summary_json TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '', "
                     "authorized_at REAL NOT NULL, revoked_at REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS request_intents ("
                     "request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_hash TEXT NOT NULL, "
                     "endpoint TEXT NOT NULL, profile_fingerprint TEXT NOT NULL DEFAULT '', "
                     "attempt INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', "
                     "created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.commit()

    def authorize_paid_profile(self, profile_fingerprint: str, *, scope: str,
                               cost_summary: dict | None = None, note: str = "") -> None:
        """显式授权精确 profile 的付费请求；scope 记录授权范围（如 reembed/query/rerank）。"""
        if not isinstance(profile_fingerprint, str) or not profile_fingerprint:
            raise StoreContractError("profile_fingerprint 必填")
        if not isinstance(scope, str) or not scope:
            raise StoreContractError("scope 必填")
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO payment_authorizations "
                "(profile_fingerprint, scope, cost_summary_json, note, authorized_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (profile_fingerprint, scope, _dump_json(cost_summary or {}, "cost_summary"), str(note), time.time()),
            )
            conn.commit()

    def revoke_paid_authorization(self, profile_fingerprint: str) -> None:
        if not isinstance(profile_fingerprint, str) or not profile_fingerprint:
            raise StoreContractError("profile_fingerprint 必填")
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            now = time.time()
            conn.execute(
                "INSERT INTO payment_authorizations "
                "(profile_fingerprint, scope, authorized_at, revoked_at) VALUES (?, 'revoked', ?, ?) "
                "ON CONFLICT(profile_fingerprint) DO UPDATE SET revoked_at = excluded.revoked_at",
                (profile_fingerprint, now, now),
            )
            conn.commit()

    def paid_authorization_state(self, profile_fingerprint: str) -> str:
        """返回 `authorized` / `revoked` / `absent`（未授权的 profile 不得被当成已授权）。"""
        if not isinstance(profile_fingerprint, str) or not profile_fingerprint:
            return "absent"
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            row = conn.execute("SELECT revoked_at FROM payment_authorizations WHERE profile_fingerprint = ?",
                               (profile_fingerprint,)).fetchone()
        if row is None:
            return "absent"
        return "revoked" if row[0] is not None else "authorized"

    def paid_profile_authorized(self, profile_fingerprint: str) -> bool:
        return self.paid_authorization_state(profile_fingerprint) == "authorized"

    def list_payment_authorizations(self) -> list[dict]:
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            rows = conn.execute("SELECT profile_fingerprint, scope, cost_summary_json, note, "
                               "authorized_at, revoked_at FROM payment_authorizations "
                               "ORDER BY authorized_at").fetchall()
        return [{"profile_fingerprint": r[0], "scope": r[1],
                 "cost_summary": _load_json(r[2], "cost_summary", {}), "note": r[3],
                 "authorized_at": r[4], "revoked_at": r[5]} for r in rows]

    def record_send_intent(self, request_id: str, *, kind: str, payload_hash: str, endpoint: str,
                           profile_fingerprint: str = "", attempt: int | None = 1,
                           reject_unresolved: bool = False) -> None:
        """在同一 mutation 锁内检查未决并落意图；网络发送始终在锁外。

        journal 使用 reject_unresolved=True / attempt=None，自动计数而不授权重发。
        旧调用仍接受显式 attempt；同一 request_id 不得覆盖既有记录。
        """
        for name, value in (("request_id", request_id), ("kind", kind), ("payload_hash", payload_hash),
                            ("endpoint", endpoint)):
            if not isinstance(value, str) or not value:
                raise StoreContractError(f"{name} 必填")
        if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1):
            raise StoreContractError("attempt 必须是 >= 1 的整数")
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            used = conn.execute(
                "SELECT request_id, state, attempt FROM request_intents "
                "WHERE kind = ? AND payload_hash = ? AND endpoint = ? AND profile_fingerprint = ?",
                (kind, payload_hash, endpoint, str(profile_fingerprint or "")),
            ).fetchall()
            if reject_unresolved:
                for row in used:
                    if row[1] in {"prepared", "submission_unknown"}:
                        raise StoreContractError(
                            f"PAID_REQUEST_UNRESOLVED: request_id={row[0]} state={row[1]} attempt={row[2]}"
                        )
            if attempt is None:
                attempt = len(used) + 1
            conn.execute(
                "INSERT INTO request_intents "
                "(request_id, kind, payload_hash, endpoint, profile_fingerprint, attempt, state, reason, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'prepared', '', ?, ?)",
                (request_id, kind, payload_hash, endpoint, profile_fingerprint, int(attempt),
                 time.time(), time.time()),
            )
            conn.commit()

    def mark_intent(self, request_id: str, state: str, reason: str = "", *,
                    expected_states: Sequence[str] = ("prepared", "submission_unknown")) -> bool:
        """CAS 更新未决状态。False 表示已有终态/竞争丢失；不存在仍报错。

        success/abandoned 不可被迟到响应覆盖，abandon 不触发任何网络动作。
        """
        if state not in {"success", "submission_unknown", "abandoned"}:
            raise StoreContractError(f"unknown intent state: {state}")
        if not expected_states or any(s not in {"prepared", "submission_unknown"} for s in expected_states):
            raise StoreContractError("expected_states must contain unresolved intent states")
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            if conn.execute("SELECT 1 FROM request_intents WHERE request_id = ?", (request_id,)).fetchone() is None:
                raise StoreContractError(f"未记录的发送意图：{request_id!r}")
            placeholders = ",".join("?" for _ in expected_states)
            cursor = conn.execute("UPDATE request_intents SET state = ?, reason = ?, updated_at = ? "
                                  f"WHERE request_id = ? AND state IN ({placeholders})",
                                  (state, str(reason), time.time(), request_id, *expected_states))
            conn.commit()
            return cursor.rowcount == 1

    def intent_state(self, request_id: str) -> str | None:
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            row = conn.execute("SELECT state FROM request_intents WHERE request_id = ?", (request_id,)).fetchone()
            return None if row is None else str(row[0])

    def list_request_intents(self, *, state: str = "") -> list[dict]:
        with self.mutation() as conn:
            self._ensure_payment_schema(conn)
            rows = conn.execute(
                "SELECT request_id, kind, payload_hash, endpoint, profile_fingerprint, attempt, state, "
                "reason, created_at, updated_at FROM request_intents "
                "WHERE (? = '' OR state = ?) ORDER BY created_at",
                (state, state),
            ).fetchall()
        keys = ("request_id", "kind", "payload_hash", "endpoint", "profile_fingerprint",
                "attempt", "state", "reason", "created_at", "updated_at")
        return [dict(zip(keys, row)) for row in rows]

    def close(self) -> None:
        conn = self._conn()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None

    def __enter__(self) -> "ControlStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- schema

_DOCSTORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version INTEGER NOT NULL,
  store_uuid TEXT NOT NULL,
  vault_binding TEXT NOT NULL,
  vault_epoch INTEGER NOT NULL,
  change_seq INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  source TEXT NOT NULL UNIQUE,
  active_revision TEXT,
  visibility TEXT NOT NULL DEFAULT 'unverified'
      CHECK (visibility IN ('active','exempt','deleted','unverified')),
  source_size INTEGER,
  source_mtime_ns INTEGER,
  source_ctime_ns INTEGER,
  source_sha256 TEXT,
  policy_version TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  FOREIGN KEY (doc_id, active_revision)
      REFERENCES document_revisions (doc_id, revision_id)
      DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS document_revisions (
  revision_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL REFERENCES documents (doc_id) ON DELETE CASCADE,
  source_sha256 TEXT NOT NULL,
  render_sha256 TEXT NOT NULL,
  parser_fingerprint TEXT NOT NULL,
  parsed_markdown TEXT NOT NULL,
  page_map_json TEXT NOT NULL DEFAULT '[]',
  page_count INTEGER NOT NULL DEFAULT 0,
  quality TEXT NOT NULL DEFAULT 'full',
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL CHECK (state IN ('staged','committed')),
  policy_version TEXT NOT NULL DEFAULT '',
  source_size INTEGER,
  source_mtime_ns INTEGER,
  vault_epoch INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  committed_at REAL,
  UNIQUE (doc_id, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_revisions_doc_state ON document_revisions (doc_id, state);

CREATE TABLE IF NOT EXISTS media_blobs (
  blob_id TEXT PRIMARY KEY,
  mime_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  data BLOB NOT NULL,
  checksum TEXT NOT NULL DEFAULT '',
  width INTEGER,
  height INTEGER,
  duration_ms INTEGER,
  sample_rate INTEGER,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS media_occurrences (
  revision_id TEXT NOT NULL REFERENCES document_revisions (revision_id) ON DELETE CASCADE,
  occurrence_id TEXT NOT NULL,
  blob_id TEXT NOT NULL REFERENCES media_blobs (blob_id),
  kind TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  page INTEGER,
  bbox_json TEXT,
  t_start_ms INTEGER,
  t_end_ms INTEGER,
  caption TEXT NOT NULL DEFAULT '',
  ocr TEXT NOT NULL DEFAULT '',
  anchor_start INTEGER,
  anchor_end INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (revision_id, occurrence_id)
);
CREATE INDEX IF NOT EXISTS idx_occurrence_rev_ord ON media_occurrences (revision_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_occurrence_blob ON media_occurrences (blob_id);

CREATE TABLE IF NOT EXISTS media_variants (
  revision_id TEXT NOT NULL,
  occurrence_id TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  blob_id TEXT NOT NULL REFERENCES media_blobs (blob_id),
  parent_blob_id TEXT REFERENCES media_blobs (blob_id),
  kind TEXT NOT NULL,
  preprocess_version TEXT NOT NULL DEFAULT '',
  range_json TEXT,
  PRIMARY KEY (revision_id, occurrence_id, variant_id),
  FOREIGN KEY (revision_id, occurrence_id)
      REFERENCES media_occurrences (revision_id, occurrence_id) ON DELETE CASCADE
);

-- ingest_jobs / ingest_subjobs / auto_seen / derived_generations：
-- 表已按 v1 合同建好，行为由 C94（队列/租约/发布CAS）/ C95（虚拟枚举/撤销/派生代次）/
-- C93（有界 MinerU 结构结果）落地。本 Lane 只保证 schema 正确，不写这些表。
CREATE TABLE IF NOT EXISTS ingest_jobs (
  job_id TEXT PRIMARY KEY,
  doc_id TEXT,
  source TEXT,
  source_sha256 TEXT,
  parser_fingerprint TEXT,
  vault_epoch INTEGER,
  request_seq INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  phase TEXT,
  owner_token TEXT,
  lease_until REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  retry_after REAL,
  result_revision TEXT,
  error_code TEXT,
  error_summary TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_lease ON ingest_jobs (state, lease_until);

CREATE TABLE IF NOT EXISTS ingest_subjobs (
  job_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  input_hash TEXT,
  range_json TEXT,
  state TEXT NOT NULL,
  remote_task_id TEXT,
  checkpoint TEXT,
  error TEXT,
  PRIMARY KEY (job_id, ordinal)
);

CREATE TABLE IF NOT EXISTS auto_seen (
  source TEXT PRIMARY KEY,
  source_sha256 TEXT,
  last_job_id TEXT,
  policy TEXT,
  state TEXT,
  source_size INTEGER,
  source_mtime_ns INTEGER,
  source_ctime_ns INTEGER,
  last_seen_scan REAL
);

CREATE TABLE IF NOT EXISTS derived_generations (
  profile_key TEXT PRIMARY KEY,
  change_seq INTEGER,
  chunker_fingerprint TEXT,
  space_fingerprint TEXT,
  status TEXT,
  last_error TEXT
);
"""

_VISIBILITIES = ("active", "exempt", "deleted", "unverified")


class DocumentStore:
    """某一 document generation 内的文档库 `generations/<gen>/docstore.sqlite`。"""

    def __init__(
        self,
        layout: StorageLayout,
        config: AppConfig | None = None,
        *,
        generation_id: str = "",
    ) -> None:
        self.layout = layout
        self.config = config
        self._requested_generation = generation_id or ""
        self._generation_id = ""
        self._binding = _vault_binding(layout)
        self._tls = threading.local()
        # 跨库归属检测只做一次（写门禁），避免每条 mutation 都读宿主注册表。
        self._registry_checked = False

    # ------------------------------------------------------------------ 连接

    @property
    def generation_id(self) -> str:
        return self._generation_id

    def _store_path(self, gen_id: str) -> Path:
        return self.layout.generations_dir / gen_id / "docstore.sqlite"

    def _conn(self) -> sqlite3.Connection | None:
        return getattr(self._tls, "conn", None)

    def _open_conn(self, gen_id: str) -> sqlite3.Connection:
        conn = self._conn()
        if conn is not None and getattr(self._tls, "gen", "") == gen_id:
            return conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            self._tls.gen = ""
        path = self._store_path(gen_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            _configure_conn(conn)
            self._ensure_schema(conn, path)
        except DocStoreError:
            conn.close()
            raise
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise StoreCorrupt(
                f"文档库损坏或不是 SQLite 数据库：{path}: {exc}",
                fix="保留文件，勿删除；虚拟读取 fail-closed，物理文本仍可服务（§12.6）。",
            ) from exc
        except Exception:
            conn.close()
            raise
        self._tls.conn = conn
        self._tls.gen = gen_id
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            self._create_schema(conn)
            return
        try:
            row = conn.execute("SELECT schema_version FROM store_meta WHERE id = 1").fetchone()
        except sqlite3.DatabaseError as exc:
            raise StoreCorrupt(
                f"文档库损坏或不是 SQLite 数据库：{path}: {exc}",
                fix="保留文件，勿删除；虚拟读取 fail-closed，物理文本仍可服务（§12.6）。",
            ) from exc
        if row is None:
            raise StoreCorrupt(
                f"文档库缺少 store_meta 单行：{path}",
                fix="文件不完整；保留原文件并从备份恢复，不要自动重建覆盖。",
            )
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise StoreSchemaUnsupported(
                f"文档库 schema_version={version} 高于本程序支持 {SCHEMA_VERSION}",
                fix="升级 Mortis-RAG-MCP；禁止降级或猜字段读取。",
            )
        if version != SCHEMA_VERSION:
            # 低于当前版本（例如手改/他程序产物）同样不是已知 schema：fail closed，
            # 不按 v1 猜字段读取（§23.1「unknown schema fail closed」）。
            raise StoreSchemaUnsupported(
                f"文档库 schema_version={version} 不是已知版本（本程序支持 {SCHEMA_VERSION}）",
                fix="保留原文件，用匹配版本的程序打开或从备份恢复；禁止自动迁移。",
            )

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_DOCSTORE_SCHEMA)
        now = time.time()
        conn.execute(
            "INSERT INTO store_meta (id, schema_version, store_uuid, vault_binding, "
            "vault_epoch, change_seq, created_at) VALUES (1, ?, ?, ?, ?, 0, ?)",
            (SCHEMA_VERSION, uuid.uuid4().hex, self._binding, self._current_epoch(), now),
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    def _current_epoch(self) -> int:
        """当前本机 epoch；控制面**尚未建立**时按 1 计（首个 epoch）。

        控制面存在却不可读（损坏/未知 schema/绑定冲突）时**不吞异常**：写路径若拿
        伪造的 epoch 1 给 revision 打戳，fencing 判断就失真了（§12.4/§12.5）。
        """
        if not self._control_exists():
            return 1
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=False)
            return ctrl.state().epoch
        finally:
            ctrl.close()

    def _control_exists(self) -> bool:
        """控制面文件是否已建立（只做存在性判断，不打开连接、不读注册表）。"""
        path = self.layout.control_path
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    # ------------------------------------------------------------------ generation

    def _active_generation(self) -> str:
        """当前 active document generation；控制面**尚未建立**时返回 ""。

        但文件存在却读不出来时**必须抛出**（损坏 / 未知 schema / 绑定冲突）：
        把「store 损坏」吞成「这个源没入库」会让读端把 fail-closed 误当 not-found，
        也可能瞒过跨库绑定冲突（§12.6「store 损坏 → 虚拟 fail closed」/§23.4
        「parse done 不等于 index ready」）。
        """
        if not self._control_exists():
            return ""
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=False)
            return ctrl.state().active_document_generation
        finally:
            ctrl.close()

    def _allocate_generation(self) -> str:
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=True, write=True)
            gen_id = ctrl.allocate_generation()
            ctrl.set_active_document_generation(gen_id)
            return gen_id
        finally:
            ctrl.close()

    def _resolve_generation(self, write: bool) -> str:
        if self._requested_generation:
            return self._requested_generation
        gen = self._active_generation()
        if gen:
            return gen
        if write:
            return self._allocate_generation()
        return ""

    def _ensure_read_generation(self) -> str:
        gen = self._resolve_generation(write=False)
        if gen != self._generation_id:
            self.close()
            self._generation_id = gen
        return gen

    # ------------------------------------------------------------------ lifecycle

    def _enforce_writable(self) -> None:
        enabled = self.layout.enabled and (self.config is None or bool(self.config.cache.enabled))
        if not enabled:
            raise VirtualStorageDisabled(
                "cache.enabled=False，虚拟摄取写入被拒绝（§17.4）",
                fix="在 [cache] 中设置 enabled=true，或不要向 doc_store 摄取（物理文本不受影响）。",
            )
        if self.layout.blocked_reason:
            raise VirtualStorageDisabled(
                "虚拟存储写入被拒绝：" + self.layout.blocked_reason,
                fix="把 cache.dir / cache.subdir 改到库外的本地目录后重试。",
            )
        self._check_registered_vaults_once()

    def _check_registered_vaults_once(self) -> None:
        """补齐 §11.2 的「cache 根落在**任一已注册库**内」维度（每个 store 只查一次）。

        布局解析默认只检测当前库（见 `_detect_blocked_reason`：读路径不隐式读宿主
        注册表）。跨库归属是**写**准入条件，所以在写门禁这里显式补查一次，避免
        每条 mutation 都去读注册表。
        """
        if self._registry_checked:
            return
        self._registry_checked = True
        if self.layout.placement != "home":
            return
        for candidate in registered_vault_paths():
            if candidate and is_within(self.layout.cache_root, candidate):
                raise VirtualStorageDisabled(
                    f"cache 根的真实路径落在已注册知识库内（{self.layout.cache_root} ⊆ {candidate}）",
                    fix="把 cache.dir（或环境变量 MORTIS_RAG_CACHE_DIR）指向库外的本地目录，"
                    '或显式改用 cache.placement="vault"（库内缓存是显式例外）。',
                )

    def open(self, *, create: bool = True, write: bool = False) -> None:
        """打开文档库；generation 为空时从 control 的 active_document_generation 取。"""
        if write:
            self._enforce_writable()
        gen = self._resolve_generation(write=write)
        if not gen:
            self._generation_id = ""
            return
        self._generation_id = gen
        self._open_conn(gen)

    def close(self) -> None:
        """关闭本线程连接（连接按线程独立；同线程重开即验证重启语义）。"""
        conn = self._conn()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None
            self._tls.gen = ""

    @contextlib.contextmanager
    def mutation(self, timeout: float = MUTATION_LOCK_TIMEOUT):
        """严格 OS 锁（线程内可重入，嵌套调用不自死锁）；写门禁在此统一裁决。

        失败路径**必须整体回滚**（§12.6）：sqlite3 在默认 isolation_level 下会为
        DML 隐式 BEGIN，异常逃逸时事务会留在连接上，而连接是线程复用的——下一次
        成功写入的 `commit()` 会把半截候选（例如只插了一半的 media_occurrence 与
        blob）一并写实。这里在异常与「未显式 commit」两种出口都 rollback。
        """
        with _mutation_lock(self.layout, timeout):
            self._enforce_writable()
            if not self._generation_id:
                gen = (
                    self._requested_generation
                    or self._active_generation()
                    or self._allocate_generation()
                )
                self._generation_id = gen
            active = self._active_generation()
            if not self._requested_generation and active and active != self._generation_id:
                self.close()
                self._generation_id = active
            elif self._requested_generation and active != self._requested_generation:
                raise StoreConflict("An inactive retained generation is read-only")
            path = self._store_path(self._generation_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            depth = int(getattr(self._tls, "mutation_depth", 0))
            self._tls.mutation_depth = depth + 1
            conn = self._open_conn(self._generation_id)
            try:
                yield conn
            except BaseException:
                self._rollback_quietly(conn)
                raise
            else:
                # 没走到显式 commit 的写入同样不许逃逸到下一个事务；但只有**最外层**
                # 帧有资格清理——可重入锁允许嵌套 mutation，内层正常退出若也 rollback
                # 会把外层尚未提交的事务一并回滚（审核发现 D5）。
                if depth == 0 and conn.in_transaction:
                    self._rollback_quietly(conn)
            finally:
                self._tls.mutation_depth = depth

    @staticmethod
    def _rollback_quietly(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except Exception:
            pass

    def _write(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
        try:
            conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise StoreContractError(f"约束冲突：{exc}") from exc
        except sqlite3.OperationalError as exc:
            if _is_locked_error(exc):
                raise StoreBusy(f"文档库忙：{exc}", fix="稍后重试。") from exc
            raise StoreCorrupt(f"文档库写入失败：{exc}") from exc

    # ------------------------------------------------------------------ meta/quota

    # ------------------------------------------------------------------ 读路径错误封装

    def _query_one(
        self, conn: sqlite3.Connection, sql: str, params: tuple = (), *, what: str
    ) -> tuple | None:
        """读路径统一把 sqlite 层错误翻成**稳定 code**（§12.6）。

        `documents`/`document_revisions` 等表缺失（被外部删表、半截复制、旧备份、
        手工改库）会让 `conn.execute` 抛**原生** `sqlite3.DatabaseError`，调用方只
        拿到一个没有 `code` 的 OperationalError 文本，无法判定「该恢复还是该重扫」。
        这里统一转 `STORE_CORRUPT` 并保留原始原因。
        """
        try:
            return conn.execute(sql, params).fetchone()
        except sqlite3.DatabaseError as exc:
            raise StoreCorrupt(
                f"{what} 读取失败（文档库不可用）：{exc}",
                fix="保留文件；虚拟读取 fail closed，物理文本仍可服务；用 doctor 诊断或从备份恢复。",
            ) from exc

    def _query_all(
        self, conn: sqlite3.Connection, sql: str, params: tuple = (), *, what: str
    ) -> list[tuple]:
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            raise StoreCorrupt(
                f"{what} 读取失败（文档库不可用）：{exc}",
                fix="保留文件；虚拟读取 fail closed，物理文本仍可服务；用 doctor 诊断或从备份恢复。",
            ) from exc

    def _meta_row(self, conn: sqlite3.Connection) -> tuple:
        row = self._query_one(
            conn,
            "SELECT schema_version, store_uuid, vault_binding, vault_epoch, change_seq, created_at "
            "FROM store_meta WHERE id = 1",
            what="store_meta",
        )
        if row is None:
            raise StoreCorrupt("文档库缺少 store_meta 单行", fix="从备份恢复。")
        return row

    def store_meta(self) -> StoreMeta:
        gen = self._ensure_read_generation()
        if not gen:
            raise StoreContractError(
                "doc_store 尚未初始化（无 active generation）",
                fix="先用 DocumentStore.open(write=True) 建立首个 generation。",
            )
        conn = self._open_conn(gen)
        row = self._meta_row(conn)
        doc_row = self._query_one(conn, "SELECT COUNT(*) FROM documents", what="documents 计数")
        rev_row = self._query_one(
            conn, "SELECT COUNT(*) FROM document_revisions", what="document_revisions 计数"
        )
        blob_row = self._query_one(conn, "SELECT COUNT(*) FROM media_blobs", what="media_blobs 计数")
        return StoreMeta(
            schema_version=int(row[0]),
            store_uuid=str(row[1]),
            vault_binding=str(row[2]),
            vault_epoch=int(row[3]),
            change_seq=int(row[4]),
            created_at=float(row[5]),
            generation_id=gen,
            document_count=int(doc_row[0]) if doc_row is not None else 0,
            revision_count=int(rev_row[0]) if rev_row is not None else 0,
            blob_count=int(blob_row[0]) if blob_row is not None else 0,
        )

    def change_seq(self) -> int:
        gen = self._ensure_read_generation()
        if not gen:
            return 0
        conn = self._open_conn(gen)
        row = self._query_one(conn, "SELECT change_seq FROM store_meta WHERE id = 1", what="change_seq")
        return int(row[0]) if row is not None else 0

    def integrity_check(self) -> None:
        gen = self._ensure_read_generation()
        if not gen:
            return
        conn = self._open_conn(gen)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise StoreCorrupt(f"integrity_check 执行失败：{exc}") from exc
        if not row or str(row[0]).lower() != "ok":
            raise StoreCorrupt(
                f"integrity_check 未通过：{row[0] if row else 'no result'}",
                fix="保留文件，显式维护或从备份恢复，不要静默删除。",
            )

    def _quota_limit_bytes(self) -> int:
        """逻辑容量上限（字节）。

        非法/非正值一律**回落到默认上限**，绝不解释成「无限制」：§17.4 明确
        「0 不得关闭安全限制」。`AppConfig` 会拒绝 max_size_mb<1，但程序化构造
        （测试、外部调用方、直接改属性）仍可能塞进 0/bool/NaN——那时必须更严，
        而不是更松。
        """
        doc_store_cfg = getattr(self.config, "doc_store", None) if self.config is not None else None
        max_mb: Any = getattr(doc_store_cfg, "max_size_mb", None) if doc_store_cfg is not None else None
        if isinstance(max_mb, bool) or not isinstance(max_mb, (int, float)):
            max_mb = DEFAULT_DOC_STORE_MAX_MB
        max_mb = float(max_mb)
        if not math.isfinite(max_mb) or max_mb <= 0:
            # 0/负数/NaN/Inf 一律回落默认：它们都不是「无限制」的表达方式。
            max_mb = float(DEFAULT_DOC_STORE_MAX_MB)
        return int(max_mb * 1024 * 1024)

    def _logical_breakdown(self, conn: sqlite3.Connection) -> tuple[int, int, int, int, int]:
        markdown_row = self._query_one(
            conn,
            "SELECT COALESCE(SUM(LENGTH(CAST(parsed_markdown AS BLOB))), 0) FROM document_revisions",
            what="markdown 用量",
        )
        pages_row = self._query_one(
            conn, "SELECT COALESCE(SUM(page_count), 0) FROM document_revisions", what="页用量"
        )
        media_row = self._query_one(
            conn, "SELECT COALESCE(SUM(byte_size), 0) FROM media_blobs", what="媒体用量"
        )
        count_row = self._query_one(conn, "SELECT COUNT(*) FROM media_blobs", what="媒体计数")
        markdown = int(markdown_row[0]) if markdown_row is not None else 0
        pages = int(pages_row[0]) if pages_row is not None else 0
        media_bytes = int(media_row[0]) if media_row is not None else 0
        media_count = int(count_row[0]) if count_row is not None else 0
        page_bytes = pages * PAGE_OVERHEAD_BYTES
        return markdown, media_bytes, page_bytes, media_count, pages

    def quota_status(self) -> QuotaStatus:
        limit = self._quota_limit_bytes()
        gen = self._ensure_read_generation()
        if not gen:
            return QuotaStatus(0, limit, 0.0, 0, 0, 0, 0, False)
        conn = self._open_conn(gen)
        markdown, media_bytes, page_bytes, media_count, _pages = self._logical_breakdown(conn)
        logical = markdown + media_bytes + page_bytes
        ratio = (logical / limit) if limit > 0 else 0.0
        over = bool(limit > 0 and logical > limit)
        return QuotaStatus(
            logical_bytes=logical,
            limit_bytes=limit,
            ratio=ratio,
            markdown_bytes=markdown,
            media_bytes=media_bytes,
            page_bytes=page_bytes,
            media_count=media_count,
            over_limit=over,
        )

    # ------------------------------------------------------------------ 读辅助

    def _load_revision(self, conn: sqlite3.Connection, revision_id: str) -> DocumentRevision | None:
        row = self._query_one(
            conn,
            "SELECT revision_id, doc_id, source_sha256, render_sha256, parser_fingerprint, "
            "parsed_markdown, page_map_json, quality, capabilities_json, state, policy_version, created_at "
            "FROM document_revisions WHERE revision_id = ?",
            (revision_id,),
            what=f"revision {revision_id!r}",
        )
        if row is None:
            return None
        return DocumentRevision(
            revision_id=str(row[0]),
            doc_id=str(row[1]),
            source_sha256=str(row[2]),
            render_sha256=str(row[3]),
            parser_fingerprint=str(row[4]),
            parsed_markdown=str(row[5]),
            page_map=_load_json(row[6], "page_map", []),
            quality=str(row[7]),
            capabilities=_load_json(row[8], "capabilities", {}),
            state=str(row[9]),
            policy_version=str(row[10]),
            created_at=float(row[11]),
        )

    # ------------------------------------------------------------------ 写入路径

    def stage_revision(
        self,
        *,
        source: str,
        source_sha256: str,
        render_sha256: str,
        parser_fingerprint: str,
        markdown: str,
        page_map: list | None = None,
        quality: str = "full",
        capabilities: Mapping[str, Any] | None = None,
        policy_version: str = "",
        source_size: int | None = None,
        source_mtime_ns: int | None = None,
        job_id: str = "",
        owner_token: str = "",
    ) -> StagedRevision:
        """写入一个 staged revision 候选（解析事实，未发布，§12.5 第 3 步）。

        传入 `job_id` + `owner_token` 时启用 **owner/租约 fencing**：token 不匹配（旧 owner
        晚到、租约过期后 job 被重新领取）→ `OWNERSHIP_LOST`，并且**不允许**覆盖更高
        `request_seq` 的候选（§12.5「force 增 request_seq，旧任务不能覆盖新任务」）。
        """
        if not isinstance(markdown, str):
            raise StoreContractError("markdown 必须是字符串")
        if not isinstance(source_sha256, str) or not source_sha256:
            raise StoreContractError("source_sha256 必填（二进制哈希）")
        if not isinstance(render_sha256, str) or not render_sha256:
            raise StoreContractError("render_sha256 必填（规范化 Markdown 哈希）")
        if not isinstance(parser_fingerprint, str) or not parser_fingerprint:
            raise StoreContractError("parser_fingerprint 必填")
        if not isinstance(quality, str) or not quality:
            raise StoreContractError("quality 必须是非空字符串")

        rel = normalize_source_path(source, self.layout.vault_path)
        pages = _validate_page_map(page_map)
        caps = capabilities if capabilities is not None else {}
        _validate_json_value(caps, "capabilities")
        size = _validate_opt_int(source_size, "source_size", min_value=0)
        mtime = _validate_opt_int(source_mtime_ns, "source_mtime_ns", min_value=0)
        page_json = _dump_json(pages, "page_map")
        caps_json = _dump_json(caps, "capabilities")
        markdown_bytes = len(markdown.encode("utf-8"))

        with self.mutation() as conn:
            if job_id:
                job = self._require_job_owner(conn, job_id, owner_token)
                self._assert_job_not_superseded(conn, job_id)
                if job.source != rel or job.source_sha256 != source_sha256:
                    raise StoreConflict("staged source/SHA does not match job")
            limit = self._quota_limit_bytes()
            if limit > 0:
                md_b, media_b, page_b, _mc, _p = self._logical_breakdown(conn)
                projected = md_b + media_b + page_b + markdown_bytes + len(pages) * PAGE_OVERHEAD_BYTES
                if projected > limit:
                    raise StoreQuotaExceeded(
                        f"写入后将超过 doc_store quota（{projected} > {limit} 字节）",
                        fix="显式清理/维护或提高 doc_store.max_size_mb；不能偷偷删除计费资产。",
                    )

            now = time.time()
            existing = conn.execute("SELECT doc_id FROM documents WHERE source = ?", (rel,)).fetchone()
            if existing is not None:
                doc_id = str(existing[0])
            else:
                doc_id = uuid.uuid4().hex
                self._write(
                    conn,
                    "INSERT INTO documents (doc_id, source, active_revision, visibility, updated_at) "
                    "VALUES (?, ?, NULL, 'unverified', ?)",
                    (doc_id, rel, now),
                )

            revision_id = uuid.uuid4().hex
            epoch = self._current_epoch()
            self._write(
                conn,
                "INSERT INTO document_revisions (revision_id, doc_id, source_sha256, render_sha256, "
                "parser_fingerprint, parsed_markdown, page_map_json, page_count, quality, capabilities_json, "
                "state, policy_version, source_size, source_mtime_ns, vault_epoch, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?)",
                (
                    revision_id, doc_id, source_sha256, render_sha256, parser_fingerprint,
                    markdown, page_json, len(pages), quality, caps_json,
                    str(policy_version or ""), size, mtime, epoch, now,
                ),
            )
            # 同 doc 旧 staged 候选是易失的：先删（其 occurrences 级联删，blob 留给 gc）。
            conn.execute(
                "DELETE FROM document_revisions WHERE doc_id = ? AND state = 'staged' AND revision_id <> ?",
                (doc_id, revision_id),
            )
            conn.commit()
            return StagedRevision(doc_id=doc_id, revision_id=revision_id, created_at=now)

    def append_media(self, revision_id: str, items: Sequence[MediaSpec], *,
                     job_id: str = "", owner_token: str = "") -> list[str]:
        """向 staged revision 追加媒体出现；blob 内容寻址，多文档共享同一 blob 行。

        传入 `job_id` + `owner_token` 时同样做 owner/租约 CAS（旧 owner 追加媒体 0 行）。
        """
        media_items = list(items)
        with self.mutation() as conn:
            if job_id:
                self._require_job_owner(conn, job_id, owner_token)
                self._assert_job_not_superseded(conn, job_id)
            row = conn.execute(
                "SELECT doc_id, state FROM document_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise StoreContractError(
                    f"revision {revision_id!r} 不存在",
                    fix="先 stage_revision 得到 revision_id，再 append_media。",
                )
            if str(row[1]) != "staged":
                raise StoreContractError(
                    "仅 staged revision 可追加媒体（committed 已被读端复用）",
                    fix="不要向已提交版本追加媒体；需要新版本请重新 stage_revision。",
                )
            # quota 必须在**任何插入之前**裁决（§12.3「超额拒绝提交」）：媒体是
            # 文档库里最大的一块，只在 stage_revision 查 markdown 会留下「按配额
            # 看似合法、追加媒体却能撑爆」的 fail-open 路径（审核实测发现）。
            incoming = 0
            seen_blobs: set[str] = set()
            for item in media_items:
                if not isinstance(item, MediaSpec) or not isinstance(item.data, (bytes, bytearray)):
                    raise StoreContractError("items 必须是 MediaSpec 序列且 data 必须是 bytes")
                blob_id = hashlib.sha256(bytes(item.data)).hexdigest()
                if blob_id in seen_blobs:
                    continue
                seen_blobs.add(blob_id)
                exists = conn.execute(
                    "SELECT 1 FROM media_blobs WHERE blob_id = ?", (blob_id,)
                ).fetchone()
                if exists is None:
                    incoming += len(item.data)
            limit = self._quota_limit_bytes()
            if limit > 0:
                md_b, media_b, page_b, _mc, _p = self._logical_breakdown(conn)
                projected = md_b + media_b + page_b + incoming
                if projected > limit:
                    raise StoreQuotaExceeded(
                        f"追加媒体后将超过 doc_store quota（{projected} > {limit} 字节）",
                        fix="显式清理/维护或提高 doc_store.max_size_mb；不能偷偷删除计费资产。",
                    )
            now = time.time()
            blob_ids: list[str] = []
            for ordinal, item in enumerate(media_items):
                if not isinstance(item, MediaSpec):
                    raise StoreContractError("items 必须是 MediaSpec 序列")
                data = item.data
                if not isinstance(data, (bytes, bytearray)):
                    raise StoreContractError("MediaSpec.data 必须是 bytes")
                data = bytes(data)
                if not isinstance(item.kind, str) or not item.kind:
                    raise StoreContractError("MediaSpec.kind 必须是非空字符串")
                if not isinstance(item.mime_type, str) or not item.mime_type:
                    raise StoreContractError("MediaSpec.mime_type 必须是非空字符串")
                _validate_json_value(item.metadata or {}, "media.metadata")
                page = _validate_opt_int(item.page, "media.page", min_value=1)
                t_start = _validate_opt_int(item.t_start_ms, "media.t_start_ms", min_value=0)
                t_end = _validate_opt_int(item.t_end_ms, "media.t_end_ms", min_value=0)
                width = _validate_opt_int(item.width, "media.width", min_value=0)
                height = _validate_opt_int(item.height, "media.height", min_value=0)
                duration = _validate_opt_int(item.duration_ms, "media.duration_ms", min_value=0)
                sample_rate = _validate_opt_int(item.sample_rate, "media.sample_rate", min_value=0)
                bbox_json = _dump_json(list(item.bbox), "media.bbox") if item.bbox is not None else None
                meta_json = _dump_json(item.metadata or {}, "media.metadata")

                blob_id = hashlib.sha256(data).hexdigest()
                existing = conn.execute(
                    "SELECT mime_type, byte_size FROM media_blobs WHERE blob_id = ?", (blob_id,)
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != item.mime_type or int(existing[1]) != len(data):
                        raise StoreContractError(
                            f"blob {blob_id} 已存在但 mime/byte_size 不一致"
                            f"（库内 {existing[0]}/{existing[1]} ≠ {item.mime_type}/{len(data)}）",
                            fix="内容寻址要求同一 blob_id 的字节与类型绝对一致；检查解析产物的确定性。",
                        )
                else:
                    self._write(
                        conn,
                        "INSERT INTO media_blobs (blob_id, mime_type, byte_size, data, checksum, "
                        "width, height, duration_ms, sample_rate, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            blob_id, item.mime_type, len(data), sqlite3.Binary(data), blob_id,
                            width, height, duration, sample_rate, now,
                        ),
                    )
                try:
                    conn.execute(
                        "INSERT INTO media_occurrences (revision_id, occurrence_id, blob_id, kind, ordinal, "
                        "page, bbox_json, t_start_ms, t_end_ms, caption, ocr, anchor_start, anchor_end, "
                        "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            revision_id, item.occurrence_id, blob_id, item.kind, ordinal,
                            page, bbox_json, t_start, t_end, str(item.caption or ""), str(item.ocr or ""),
                            _validate_opt_int(item.anchor_start, "media.anchor_start", min_value=0),
                            _validate_opt_int(item.anchor_end, "media.anchor_end", min_value=0),
                            meta_json,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreContractError(
                        f"occurrence 主键冲突 (revision={revision_id}, occurrence={item.occurrence_id}): {exc}",
                        fix="同一 revision 内 occurrence_id 必须唯一。",
                    ) from exc
                blob_ids.append(blob_id)
            conn.commit()
            return blob_ids

    def put_media_blob(self, *, data: bytes, mime_type: str, width: int | None = None,
                       height: int | None = None, duration_ms: int | None = None,
                       sample_rate: int | None = None, job_id: str = "",
                       owner_token: str = "") -> str:
        """把一项媒体**逐项**写入 `media_blobs`（内容寻址），返回 blob_id（§13.2）。

        这是流式 sink 的第一阶段：解析时每读到一个成员就落库并释放 RAM，
        出现（occurrence）在 revision 建立后再用 `attach_occurrences` 挂上去。
        尚无引用的 blob 由 `gc_unreferenced()` 回收，不会泄漏。
        """
        if not isinstance(data, (bytes, bytearray)):
            raise StoreContractError("media data 必须是 bytes")
        payload = bytes(data)
        if not isinstance(mime_type, str) or not mime_type:
            raise StoreContractError("mime_type 必填")
        blob_id = hashlib.sha256(payload).hexdigest()
        with self.mutation() as conn:
            if job_id:
                self._require_job_owner(conn, job_id, owner_token)
                self._assert_job_not_superseded(conn, job_id)
            existing = conn.execute(
                "SELECT mime_type, byte_size FROM media_blobs WHERE blob_id = ?", (blob_id,)
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != mime_type or int(existing[1]) != len(payload):
                    raise StoreContractError(
                        f"blob {blob_id} 已存在但 mime/byte_size 不一致"
                        f"（库内 {existing[0]}/{existing[1]} ≠ {mime_type}/{len(payload)}）"
                    )
                return blob_id
            limit = self._quota_limit_bytes()
            if limit > 0:
                md_b, media_b, page_b, _mc, _p = self._logical_breakdown(conn)
                if md_b + media_b + page_b + len(payload) > limit:
                    raise StoreQuotaExceeded(
                        f"写入媒体后将超过 doc_store quota（{md_b + media_b + page_b + len(payload)} > {limit}）",
                        fix="显式清理/维护或提高 doc_store.max_size_mb；不能偷偷删除计费资产。",
                    )
            self._write(
                conn,
                "INSERT INTO media_blobs (blob_id, mime_type, byte_size, data, checksum, width, "
                "height, duration_ms, sample_rate, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (blob_id, mime_type, len(payload), sqlite3.Binary(payload), blob_id,
                 _validate_opt_int(width, "width", min_value=0),
                 _validate_opt_int(height, "height", min_value=0),
                 _validate_opt_int(duration_ms, "duration_ms", min_value=0),
                 _validate_opt_int(sample_rate, "sample_rate", min_value=0), time.time()),
            )
            conn.commit()
        return blob_id

    def attach_occurrences(self, revision_id: str, occurrences: Sequence[MediaOccurrenceSpec], *,
                           job_id: str = "", owner_token: str = "") -> int:
        """把已落库的 blob 挂成 staged revision 的媒体出现（流式 sink 的第二阶段）。"""
        items = list(occurrences)
        with self.mutation() as conn:
            if job_id:
                self._require_job_owner(conn, job_id, owner_token)
                self._assert_job_not_superseded(conn, job_id)
            row = conn.execute(
                "SELECT state FROM document_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise StoreContractError(f"revision {revision_id!r} 不存在")
            if str(row[0]) != "staged":
                raise StoreContractError("仅 staged revision 可挂媒体出现")
            inserted = 0
            for item in items:
                if not isinstance(item, MediaOccurrenceSpec):
                    raise StoreContractError("occurrences 必须是 MediaOccurrenceSpec 序列")
                exists = conn.execute(
                    "SELECT 1 FROM media_blobs WHERE blob_id = ?", (item.blob_id,)
                ).fetchone()
                if exists is None:
                    raise StoreContractError(
                        f"blob {item.blob_id} 不存在", fix="先用 put_media_blob 落 blob 再挂出现。"
                    )
                bbox_json = _dump_json(list(item.bbox), "media.bbox") if item.bbox is not None else None
                # E08-a：anchor 列只写**正文字符半开区间**。旧实现把 width/height 写进
                # anchor_start/anchor_end（尺寸冒充正文位置），读取侧据此定位必然错位。
                # 优先级：显式 `anchor_start/anchor_end` > 已核验 metadata 锚点 > 无证据(None)。
                anchor_start, anchor_end = _resolve_media_anchor(item)
                meta = dict(item.metadata or {})
                if anchor_start is not None:
                    meta.setdefault("anchor_start", anchor_start)
                    meta.setdefault("anchor_end", anchor_end)
                meta_json = _dump_json(meta, "media.metadata")
                try:
                    conn.execute(
                        "INSERT INTO media_occurrences (revision_id, occurrence_id, blob_id, kind, ordinal, "
                        "page, bbox_json, t_start_ms, t_end_ms, caption, ocr, anchor_start, anchor_end, "
                        "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            revision_id, item.occurrence_id, item.blob_id, item.kind, int(item.ordinal),
                            _validate_opt_int(item.page, "media.page", min_value=1), bbox_json,
                            _validate_opt_int(item.t_start_ms, "media.t_start_ms", min_value=0),
                            _validate_opt_int(item.t_end_ms, "media.t_end_ms", min_value=0),
                            str(item.caption or ""), str(item.ocr or ""),
                            _validate_opt_int(anchor_start, "media.anchor_start", min_value=0),
                            _validate_opt_int(anchor_end, "media.anchor_end", min_value=0),
                            meta_json,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreContractError(
                        f"occurrence 主键冲突 (revision={revision_id}, occurrence={item.occurrence_id}): {exc}",
                        fix="同一 revision 内 occurrence_id 必须唯一。",
                    ) from exc
                inserted += 1
            conn.commit()
            return inserted

    def commit_revision(
        self,
        revision_id: str,
        *,
        expected_change_seq: int | None = None,
        source_sha256: str | None = None,
        source_size: int | None = None,
        source_mtime_ns: int | None = None,
        policy_version: str | None = None,
    ) -> int:
        """单事务发布（§12.5 第 5 步）：CAS + 切 active + change_seq+1 + 保留策略。"""
        with self.mutation() as conn:
            seq = self._commit_locked(
                conn,
                revision_id,
                expected_change_seq=expected_change_seq,
                source_sha256=source_sha256,
                source_size=source_size,
                source_mtime_ns=source_mtime_ns,
                policy_version=policy_version,
            )
            conn.commit()
            return seq

    def commit_job_revision(
        self,
        job_id: str,
        owner_token: str,
        revision_id: str,
        *,
        expected_change_seq: int | None = None,
        source_sha256: str | None = None,
        source_size: int | None = None,
        source_mtime_ns: int | None = None,
        policy_version: str | None = None,
        state: str = "done",
    ) -> int:
        """**同一事务**完成「发布 revision + 任务终态」（§12.5 第 5 步：不能先 done 后写库）。

        任一步失败（owner 丢失 / CAS 冲突 / quota）→ 整批回滚：不会出现
        「库里有新事实、任务还是 parsing」或「任务已 done、库没变」的半套状态。
        """
        if state not in JOB_TERMINAL_STATES and state != "staged":
            raise StoreContractError(
                f"commit_job_revision 的终态必须是 {JOB_TERMINAL_STATES} 之一（或 'staged'），收到 {state!r}"
            )
        with self.mutation() as conn:
            job = self._require_job_owner(conn, job_id, owner_token)
            self._assert_job_not_superseded(conn, job_id)
            candidate = conn.execute(
                "SELECT d.source, r.source_sha256 FROM document_revisions r "
                "JOIN documents d ON d.doc_id = r.doc_id WHERE r.revision_id = ?",
                (revision_id,),
            ).fetchone()
            if candidate is None or (str(candidate[0]), str(candidate[1])) != (job.source, job.source_sha256):
                raise StoreConflict("revision source/SHA does not match job")
            seq = self._commit_locked(
                conn,
                revision_id,
                expected_change_seq=expected_change_seq,
                source_sha256=source_sha256,
                source_size=source_size,
                source_mtime_ns=source_mtime_ns,
                policy_version=policy_version,
            )
            now = time.time()
            row = conn.execute(
                "SELECT source FROM ingest_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            source = str(row[0]) if row is not None and row[0] else ""
            doc_id = ""
            doc_row = conn.execute(
                "SELECT doc_id FROM document_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if doc_row is not None:
                doc_id = str(doc_row[0])
            self._write(
                conn,
                "UPDATE ingest_jobs SET state = ?, phase = ?, result_revision = ?, doc_id = COALESCE(NULLIF(?, ''), doc_id), "
                "error_code = '', error_summary = '', updated_at = ? WHERE job_id = ? AND owner_token = ?",
                (state, "committed" if state == "done" else "staged", revision_id, doc_id, now, job_id, owner_token),
            )
            if source:
                self._write(
                    conn,
                    "UPDATE auto_seen SET state = ?, last_job_id = ?, source_sha256 = COALESCE(source_sha256, source_sha256), "
                    "last_seen_scan = ? WHERE source = ?",
                    (state, job_id, now, source),
                )
            conn.commit()
            return seq

    def _commit_locked(
        self,
        conn: sqlite3.Connection,
        revision_id: str,
        *,
        expected_change_seq: int | None = None,
        source_sha256: str | None = None,
        source_size: int | None = None,
        source_mtime_ns: int | None = None,
        policy_version: str | None = None,
    ) -> int:
        """`commit_revision` 的事务主体（调用方已在 mutation 锁内）。

        CAS 顺序（§12.5 第 4/5 步）：state=staged → expected_change_seq → 源 SHA →
        epoch fencing → quota 复核 → 切 active → change_seq+1 → 保留策略。
        """
        row = conn.execute(
            "SELECT doc_id, source_sha256, state, vault_epoch, policy_version "
            "FROM document_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise StoreContractError(
                f"revision {revision_id!r} 不存在", fix="只能提交已 stage 的候选。"
            )
        doc_id = str(row[0])
        staged_sha = str(row[1])
        state = str(row[2])
        rev_epoch = int(row[3])
        rev_policy = str(row[4])
        if state != "staged":
            raise StoreContractError(
                "只能提交 staged revision", fix="committed 版本不可重复提交。"
            )

        cur_seq = int(conn.execute("SELECT change_seq FROM store_meta WHERE id = 1").fetchone()[0])
        if expected_change_seq is not None and int(expected_change_seq) != cur_seq:
            raise StoreConflict(
                f"expected_change_seq={expected_change_seq} 与当前 {cur_seq} 不符（发布被抢占）",
                fix="重新读取 change_seq 后重试；旧任务不得覆盖新事实。",
            )
        if source_sha256 is not None and str(source_sha256) != staged_sha:
            raise StoreConflict(
                "提交时源 SHA 与 staged 记录不符（源在解析后又被改动）",
                fix="废弃候选并重新扫描源。",
            )
        cur_epoch = self._current_epoch()
        if rev_epoch != cur_epoch:
            raise StoreConflict(
                f"revision 记录于 epoch {rev_epoch}，当前 epoch {cur_epoch}（旧 worker 不能复活）",
                fix="移库/import/purge 后 epoch 递增，旧候选必须重新排队。",
            )
        # 发布前再核一次 quota：stage 之后其它文档可能已经吃掉了余量（§12.3）。
        limit = self._quota_limit_bytes()
        if limit > 0:
            md_b, media_b, page_b, _mc, _p = self._logical_breakdown(conn)
            if md_b + media_b + page_b > limit:
                raise StoreQuotaExceeded(
                    f"提交后将超过 doc_store quota（{md_b + media_b + page_b} > {limit} 字节）",
                    fix="显式清理/维护或提高 doc_store.max_size_mb；不能偷偷删除计费资产。",
                )

        doc = conn.execute(
            "SELECT visibility, active_revision FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        visibility = str(doc[0])
        prev_active = str(doc[1]) if doc[1] else ""
        effective_policy = policy_version if policy_version is not None else rev_policy
        now = time.time()
        new_seq = cur_seq + 1

        conn.execute(
            "UPDATE document_revisions SET state = 'committed', committed_at = ? WHERE revision_id = ?",
            (now, revision_id),
        )
        if visibility == "exempt":
            # §12.3：exempt 不发布——保留解析事实，不设 active_revision。
            conn.execute(
                "UPDATE documents SET policy_version = ?, updated_at = ? WHERE doc_id = ?",
                (effective_policy, now, doc_id),
            )
        else:
            conn.execute(
                "UPDATE documents SET active_revision = ?, visibility = 'active', "
                "source_size = COALESCE(?, source_size), source_mtime_ns = COALESCE(?, source_mtime_ns), "
                "source_sha256 = ?, policy_version = ?, updated_at = ? WHERE doc_id = ?",
                (
                    revision_id,
                    _validate_opt_int(source_size, "source_size", min_value=0),
                    _validate_opt_int(source_mtime_ns, "source_mtime_ns", min_value=0),
                    staged_sha, effective_policy, now, doc_id,
                ),
            )
        conn.execute("UPDATE store_meta SET change_seq = ? WHERE id = 1", (new_seq,))

        # 保留 active + 上一 committed；删同 doc 其它 staged 与超保留的旧 committed。
        conn.execute(
            "DELETE FROM document_revisions WHERE doc_id = ? AND state = 'staged'", (doc_id,)
        )
        self._prune_committed(conn, doc_id, keep=[revision_id, prev_active])
        return new_seq

    def _prune_committed(self, conn: sqlite3.Connection, doc_id: str, keep: Sequence[str]) -> None:
        kept = {k for k in keep if k}
        rows = conn.execute(
            "SELECT revision_id FROM document_revisions WHERE doc_id = ? AND state = 'committed' "
            "ORDER BY COALESCE(committed_at, created_at) DESC, created_at DESC",
            (doc_id,),
        ).fetchall()
        for index, (rid,) in enumerate(rows):
            rid = str(rid)
            if rid in kept:
                continue
            if index == 0:
                # 保底：最新 committed 永不删除（即使是 exempt 无 active 的情况）。
                kept.add(rid)
                continue
            conn.execute("DELETE FROM document_revisions WHERE revision_id = ?", (rid,))

    def discard_staged(self, revision_id: str) -> bool:
        with self.mutation() as conn:
            cur = conn.execute(
                "SELECT state FROM document_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if cur is None or str(cur[0]) != "staged":
                return False
            conn.execute("DELETE FROM document_revisions WHERE revision_id = ?", (revision_id,))
            conn.commit()
            return True

    # ------------------------------------------------------------------ 任务/租约（C94）

    _JOB_COLUMNS = (
        "job_id, doc_id, source, source_sha256, parser_fingerprint, vault_epoch, request_seq, "
        "state, phase, owner_token, lease_until, attempts, retry_after, result_revision, "
        "error_code, error_summary, created_at, updated_at"
    )

    @staticmethod
    def _row_to_job(row: Sequence[Any]) -> JobRecord:
        return JobRecord(
            job_id=str(row[0]),
            doc_id=str(row[1] or ""),
            source=str(row[2] or ""),
            source_sha256=str(row[3] or ""),
            parser_fingerprint=str(row[4] or ""),
            vault_epoch=int(row[5] or 0),
            request_seq=int(row[6] or 0),
            state=str(row[7]),
            phase=str(row[8] or ""),
            owner_token=str(row[9] or ""),
            lease_until=float(row[10] or 0.0),
            attempts=int(row[11] or 0),
            retry_after=float(row[12] or 0.0),
            result_revision=str(row[13] or ""),
            error_code=str(row[14] or ""),
            error_summary=str(row[15] or ""),
            created_at=float(row[16] or 0.0),
            updated_at=float(row[17] or 0.0),
        )

    def _job_row(self, conn: sqlite3.Connection, job_id: str) -> JobRecord | None:
        row = conn.execute(
            f"SELECT {self._JOB_COLUMNS} FROM ingest_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if row is None else self._row_to_job(row)

    def _queue_limit(self) -> int:
        ingest = getattr(self.config, "ingest", None) if self.config is not None else None
        raw = getattr(ingest, "queue_limit", None) if ingest is not None else None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            return DEFAULT_QUEUE_LIMIT
        return int(raw)

    def _require_job_owner(self, conn: sqlite3.Connection, job_id: str, owner_token: str) -> JobRecord:
        """租约 fencing 的唯一判据（§12.5）：token 必须匹配、状态允许、租约未过期。"""
        job = self._job_row(conn, job_id)
        if job is None:
            raise StoreContractError(
                f"job {job_id!r} 不存在", fix="先 enqueue_job / 从 store 里读任务。"
            )
        if not owner_token or not job.owner_token or job.owner_token != owner_token:
            raise OwnershipLost(
                f"job {job_id} 的 owner_token 不匹配（旧 owner 晚到或被重新领取）",
                fix="旧 worker 必须放弃该候选：重新 claim 或让新 owner 继续。",
            )
        if job.vault_epoch != self._current_epoch():
            raise OwnershipLost(f"job {job_id} 的 vault epoch 已失效")
        if job.state in JOB_TERMINAL_STATES:
            raise OwnershipLost(f"job {job_id} 已处于终态 {job.state}，不能再写")
        if job.lease_until <= time.time():
            raise OwnershipLost(
                f"job {job_id} 的租约已过期（lease_until={job.lease_until}）",
                fix="续租成功后才能继续写；过期后的旧 owner 一律 0 行。",
            )
        return job

    def _assert_job_not_superseded(self, conn: sqlite3.Connection, job_id: str) -> None:
        """旧任务不得覆盖新任务（§12.5：force 增 request_seq 后旧任务作废）。"""
        job = self._job_row(conn, job_id)
        if job is None:
            return
        if job.state == "superseded":
            raise OwnershipLost(f"job {job_id} 已被更新的请求取代（superseded）")
        if not job.source:
            return
        row = conn.execute(
            "SELECT MAX(request_seq) FROM ingest_jobs WHERE source = ? "
            "AND state NOT IN ('cancelled', 'superseded')",
            (job.source,),
        ).fetchone()
        if row is not None and row[0] is not None and int(row[0]) > job.request_seq:
            raise StoreConflict(
                f"源 {job.source} 已存在更新的任务（request_seq={int(row[0])} > {job.request_seq}）",
                fix="旧任务的结果必须废弃；不要用晚到的解析覆盖新事实。",
            )

    def enqueue_job(
        self,
        *,
        source: str,
        source_sha256: str,
        parser_fingerprint: str,
        policy_version: str = "",
        force: bool = False,
        request_seq: int | None = None,
    ) -> tuple[JobRecord, bool]:
        """入队（§12.5 第 1 步）：容量、合并、force 递增 request_seq。

        返回 `(job, created)`；`created=False` 表示命中「同源 + 同 SHA + 同 parser + 同 epoch」
        的活跃任务（合并，不重复解析）。
        """
        rel = normalize_source_path(source, self.layout.vault_path)
        if not isinstance(source_sha256, str) or not source_sha256:
            raise StoreContractError("source_sha256 必填")
        if not isinstance(parser_fingerprint, str) or not parser_fingerprint:
            raise StoreContractError("parser_fingerprint 必填")
        with self.mutation() as conn:
            epoch = self._current_epoch()
            unknown = conn.execute(
                "SELECT job_id FROM ingest_jobs WHERE source = ? "
                "AND (error_code = 'SUBMISSION_UNKNOWN' OR phase = 'submission_unknown') LIMIT 1",
                (rel,),
            ).fetchone()
            if unknown is not None:
                raise StoreConflict("SUBMISSION_UNKNOWN: source has an unresolved paid request",
                                    fix="人工核对受理与费用；force/自动扫描不得绕过未知受理。")
            depth_row = conn.execute(
                "SELECT COUNT(*) FROM ingest_jobs WHERE state IN ('queued', 'parsing', 'staged')"
            ).fetchone()
            depth = int(depth_row[0]) if depth_row is not None else 0
            if not force:
                existing = conn.execute(
                    f"SELECT {self._JOB_COLUMNS} FROM ingest_jobs WHERE source = ? AND source_sha256 = ? "
                    "AND parser_fingerprint = ? AND vault_epoch = ? "
                    "AND state IN ('queued', 'parsing', 'staged') "
                    "ORDER BY request_seq DESC LIMIT 1",
                    (rel, source_sha256, parser_fingerprint, epoch),
                ).fetchone()
                if existing is not None:
                    return self._row_to_job(existing), False
            pending_remote = conn.execute(
                "SELECT job_id FROM ingest_jobs WHERE source = ? AND state <> 'done' "
                "AND phase IN ('send_intent', 'submitted', 'polling', 'downloaded') LIMIT 1",
                (rel,),
            ).fetchone()
            if pending_remote is not None:
                raise StoreConflict("SUBMISSION_UNKNOWN: unresolved remote request cannot be superseded",
                                    fix="先核对旧远端任务；force、取消或新源SHA均不是费用确认。")
            limit = self._queue_limit()
            if depth >= limit:
                raise QueueFull(
                    f"摄取队列已满（{depth}/{limit}）",
                    fix="等待现有任务结束或提高 ingest.queue_limit；不要丢弃已有任务。",
                )
            now = time.time()
            if request_seq is None:
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(request_seq), 0) FROM ingest_jobs WHERE source = ?", (rel,)
                ).fetchone()
                request_seq = int(seq_row[0]) + 1 if seq_row is not None else 1
            request_seq = int(request_seq)
            if force:
                conn.execute(
                    "UPDATE ingest_jobs SET state = 'superseded', owner_token = '', lease_until = 0, "
                    "updated_at = ? WHERE source = ? AND state IN ('queued', 'parsing', 'staged') "
                    "AND request_seq < ?",
                    (now, rel, request_seq),
                )
            doc_row = conn.execute("SELECT doc_id FROM documents WHERE source = ?", (rel,)).fetchone()
            doc_id = str(doc_row[0]) if doc_row is not None else ""
            job_id = uuid.uuid4().hex
            self._write(
                conn,
                "INSERT INTO ingest_jobs (job_id, doc_id, source, source_sha256, parser_fingerprint, "
                "vault_epoch, request_seq, state, phase, owner_token, lease_until, attempts, retry_after, "
                "result_revision, error_code, error_summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'prepared', '', 0, 0, 0, '', '', '', ?, ?)",
                (job_id, doc_id, rel, source_sha256, parser_fingerprint, epoch, request_seq, now, now),
            )
            conn.execute(
                "INSERT INTO auto_seen (source, source_sha256, last_job_id, policy, state, "
                "source_size, source_mtime_ns, source_ctime_ns, last_seen_scan) "
                "VALUES (?, ?, ?, ?, 'queued', NULL, NULL, NULL, ?) "
                "ON CONFLICT(source) DO UPDATE SET source_sha256 = excluded.source_sha256, "
                "last_job_id = excluded.last_job_id, state = 'queued', policy = excluded.policy, "
                "last_seen_scan = excluded.last_seen_scan",
                (rel, source_sha256, job_id, str(policy_version or ""), now),
            )
            conn.commit()
            row = self._job_row(conn, job_id)
            assert row is not None
            return row, True

    def claim_job(self, owner_token: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS,
                  now: float | None = None) -> JobRecord | None:
        """原子领取（§12.5 第 2 步）：换 owner_token + 续租；跨进程只有一个 owner。

        CAS 条件写在 UPDATE 的 WHERE 里，两个进程同时领同一 job 只有一个 `rowcount == 1`。
        """
        if not owner_token:
            raise StoreContractError("claim_job 需要非空 owner_token")
        stamp = time.time() if now is None else float(now)
        lease = max(float(lease_seconds), 1.0)
        with self.mutation() as conn:
            conn.execute(
                "UPDATE ingest_jobs SET state = 'failed', phase = 'submission_unknown', "
                "error_code = 'SUBMISSION_UNKNOWN', error_summary = 'expired remote submission requires review', "
                "owner_token = '', lease_until = 0, updated_at = ? "
                "WHERE state IN ('parsing', 'staged') AND lease_until <= ? "
                "AND phase IN ('send_intent', 'submitted', 'polling', 'downloaded', 'submission_unknown')",
                (stamp, stamp),
            )
            rows = conn.execute(
                "SELECT job_id FROM ingest_jobs "
                "WHERE state = 'queued' "
                "   OR (state = 'parsing' AND (lease_until IS NULL OR lease_until <= ?)) "
                "ORDER BY created_at, job_id LIMIT 16",
                (stamp,),
            ).fetchall()
            for (job_id,) in rows:
                cursor = conn.execute(
                    "UPDATE ingest_jobs SET owner_token = ?, lease_until = ?, state = 'parsing', "
                    "phase = 'parsing', attempts = attempts + 1, error_code = '', error_summary = '', "
                    "updated_at = ? WHERE job_id = ? "
                    "  AND (state = 'queued' "
                    "       OR (state = 'parsing' AND (lease_until IS NULL OR lease_until <= ?)))",
                    (owner_token, stamp + lease, stamp, str(job_id), stamp),
                )
                if cursor.rowcount == 1:
                    conn.commit()
                    return self._job_row(conn, str(job_id))
            conn.commit()
            return None

    def renew_lease(self, job_id: str, owner_token: str, *,
                    lease_seconds: float = DEFAULT_LEASE_SECONDS) -> JobRecord:
        """续租 CAS：影响 0 行 → OWNERSHIP_LOST（不无条件覆盖 job）。"""
        with self.mutation() as conn:
            self._require_job_owner(conn, job_id, owner_token)
            stamp = time.time()
            cursor = conn.execute(
                "UPDATE ingest_jobs SET lease_until = ?, updated_at = ? WHERE job_id = ? "
                "AND owner_token = ? AND state IN ('parsing', 'staged') AND lease_until > ?",
                (stamp + max(float(lease_seconds), 1.0), stamp, job_id, owner_token, stamp),
            )
            if cursor.rowcount != 1:
                raise OwnershipLost(
                    f"job {job_id} 续租失败（owner 不匹配、已终态或租约已过期）",
                    fix="停止接收与发布；由新 owner 重新 claim，旧 owner 的一切写入必须 0 行。",
                )
            conn.commit()
            row = self._job_row(conn, job_id)
            assert row is not None
            return row

    def report_phase(self, job_id: str, owner_token: str, *, phase: str,
                     remote_task_id: str = "", checkpoint: str = "",
                     error: str = "") -> None:
        """记录提交阶段（§12.5 远端恢复）与远端 task id。

        `ingest_subjobs` 的 ordinal 0 保留给「整份文档的远端阶段」（分卷/音频用 1..n）。
        """
        if phase not in JOB_PHASES:
            raise StoreContractError(f"phase 必须是 {JOB_PHASES} 之一，收到 {phase!r}")
        with self.mutation() as conn:
            self._require_job_owner(conn, job_id, owner_token)
            stamp = time.time()
            self._write(
                conn,
                "UPDATE ingest_jobs SET phase = ?, updated_at = ? WHERE job_id = ? AND owner_token = ?",
                (phase, stamp, job_id, owner_token),
            )
            conn.execute(
                "INSERT INTO ingest_subjobs (job_id, ordinal, input_hash, range_json, state, "
                "remote_task_id, checkpoint, error) VALUES (?, 0, NULL, NULL, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, ordinal) DO UPDATE SET state = excluded.state, "
                "remote_task_id = CASE WHEN excluded.remote_task_id <> '' "
                "                      THEN excluded.remote_task_id ELSE ingest_subjobs.remote_task_id END, "
                "checkpoint = excluded.checkpoint, error = excluded.error",
                (job_id, phase, str(remote_task_id or ""), str(checkpoint or ""), str(error or "")),
            )
            conn.commit()

    def list_subjobs(self, job_id: str) -> list[SubjobRecord]:
        """读回该 job 的全部 subjob/checkpoint 行（E02）。

        ordinal 0 是**父任务的远端 submission 阶段**；1..n 是段/分卷记录。
        """
        gen = self._ensure_read_generation()
        if not gen:
            return []
        conn = self._open_conn(gen)
        rows = self._query_all(
            conn,
            "SELECT job_id, ordinal, input_hash, range_json, state, remote_task_id, checkpoint, error "
            "FROM ingest_subjobs WHERE job_id = ? ORDER BY ordinal",
            (str(job_id),),
            what="subjob 列表",
        )
        records: list[SubjobRecord] = []
        for row in rows:
            kind, start, end = "", None, None
            try:
                payload = _load_json(row[3], "subjob.range", None) if row[3] else None
                if isinstance(payload, dict):
                    kind = str(payload.get("kind") or "")
                    start = int(payload["start"]) if payload.get("start") is not None else None
                    end = int(payload["end"]) if payload.get("end") is not None else None
            except Exception:
                kind, start, end = "", None, None
            records.append(SubjobRecord(
                job_id=str(row[0]), ordinal=int(row[1]), state=str(row[4] or ""),
                input_hash=str(row[2] or ""), range_kind=kind, range_start=start, range_end=end,
                remote_task_id=str(row[5] or ""), checkpoint=str(row[6] or ""),
                error=str(row[7] or "")))
        return records

    def record_subjob(self, job_id: str, owner_token: str, *, ordinal: int,
                      input_hash: str = "", range: Mapping[str, Any] | None = None,
                      state: str = "", checkpoint: Mapping[str, Any] | None = None,
                      remote_task_id: str = "", error: str = "") -> bool:
        """段级 checkpoint 写入（E02：合法阶段 + 完整 JSON + 写前限额拒绝）。

        * `ordinal == 0`：父任务的远端 submission 阶段，`state` 必须是 `JOB_PHASES` 之一；
        * `ordinal >= 1`：段/分卷记录，`state` 必须是 `SUBJOB_STATES` 之一。
          两者**不得混用**——否则段完成会把父任务的远端阶段覆盖掉。
        * `range`：`{"kind": audio_frames|audio_ms|document_pages, "start": int, "end": int}`
          半开区间；缺 kind 或区间非法一律拒绝（不得把页范围当毫秒）。
        * `checkpoint`：完整 JSON 编码，**不截断**；超过 `CHECKPOINT_MAX_BYTES`
          在写前明确拒绝（不写失败假成功）。
        """
        ordinal = int(ordinal)
        if ordinal == 0:
            if state not in JOB_PHASES:
                raise StoreContractError(
                    f"ordinal 0 的 state 必须是 {JOB_PHASES} 之一（父任务远端阶段），收到 {state!r}",
                    fix="段级状态请用 ordinal >= 1。",
                )
        elif ordinal >= 1:
            if state not in SUBJOB_STATES:
                raise StoreContractError(
                    f"ordinal {ordinal} 的 state 必须是 {SUBJOB_STATES} 之一，收到 {state!r}")
        else:
            raise StoreContractError("ordinal 必须 >= 0")
        range_json = None
        if range is not None:
            if not isinstance(range, Mapping):
                raise StoreContractError("subjob range 必须是 mapping")
            kind = str(range.get("kind") or "")
            if kind not in SUBJOB_RANGE_KINDS:
                raise StoreContractError(
                    f"subjob range kind 必须是 {SUBJOB_RANGE_KINDS} 之一，收到 {kind!r}",
                    fix="音频用 audio_frames/audio_ms，文档用 document_pages，不得混用。",
                )
            start, end = range.get("start"), range.get("end")
            for label, value in (("start", start), ("end", end)):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise StoreContractError(f"subjob range {label} 必须是整数")
            if start < 0 or end < start:
                raise StoreContractError("subjob range 必须是非负半开区间 [start, end)")
            range_json = _dump_json({"kind": kind, "start": start, "end": end}, "subjob.range")
        payload = ""
        if checkpoint is not None:
            if not isinstance(checkpoint, Mapping):
                raise StoreContractError("subjob checkpoint 必须是 mapping")
            payload = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
            if len(payload.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
                raise StoreContractError(
                    f"subjob checkpoint 编码后 {len(payload.encode('utf-8'))} 字节，超过 "
                    f"{CHECKPOINT_MAX_BYTES} 字节上限",
                    fix="缩小 checkpoint（只存引用与状态，不存大块正文）；绝不截断字符串。",
                )
        with self.mutation() as conn:
            self._require_job_owner(conn, job_id, owner_token)
            conn.execute(
                "INSERT INTO ingest_subjobs (job_id, ordinal, input_hash, range_json, state, "
                "remote_task_id, checkpoint, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, ordinal) DO UPDATE SET input_hash = excluded.input_hash, "
                "range_json = COALESCE(excluded.range_json, ingest_subjobs.range_json), "
                "state = excluded.state, "
                "remote_task_id = CASE WHEN excluded.remote_task_id <> '' "
                "                      THEN excluded.remote_task_id ELSE ingest_subjobs.remote_task_id END, "
                "checkpoint = excluded.checkpoint, error = excluded.error",
                (str(job_id), ordinal, str(input_hash or ""), range_json, str(state),
                 str(remote_task_id or ""), payload, str(error or "")[:500]),
            )
            conn.commit()
            return True

    def fail_job(self, job_id: str, owner_token: str, *, error_code: str,
                 error_summary: str = "", retryable: bool = False,
                 retry_after: float | None = None) -> JobRecord:
        """终态失败/可重试失败（§12.5）。可重试失败保持 `failed`，由显式 retry 回 queued。"""
        with self.mutation() as conn:
            self._require_job_owner(conn, job_id, owner_token)
            stamp = time.time()
            after = float(retry_after) if retry_after is not None else 0.0
            self._write(
                conn,
                "UPDATE ingest_jobs SET state = 'failed', owner_token = '', lease_until = 0, "
                "error_code = ?, error_summary = ?, retry_after = ?, updated_at = ? WHERE job_id = ?",
                (str(error_code)[:120], str(error_summary)[:500], after, stamp, job_id),
            )
            job = self._job_row(conn, job_id)
            assert job is not None
            conn.execute(
                "UPDATE auto_seen SET state = 'failed', last_seen_scan = ? WHERE source = ?",
                (stamp, job.source),
            )
            conn.commit()
            return job

    def cancel_job(self, job_id: str) -> bool:
        """合作式取消（§12.5：不能声称可强杀原生解析库）。终态任务不可取消。"""
        with self.mutation() as conn:
            stamp = time.time()
            cursor = conn.execute(
                "UPDATE ingest_jobs SET state = 'cancelled', owner_token = '', lease_until = 0, "
                "updated_at = ? WHERE job_id = ? AND state IN ('queued', 'parsing', 'staged')",
                (stamp, job_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def retry_job(self, job_id: str) -> JobRecord:
        """显式重试：可重试失败回到 queued（**不是**自动重传；未知受理必须人工确认后调用）。"""
        with self.mutation() as conn:
            job = self._job_row(conn, job_id)
            if job is None:
                raise StoreContractError(f"job {job_id!r} 不存在")
            if job.state not in ("failed", "cancelled"):
                raise StoreContractError(
                    f"job {job_id} 当前状态 {job.state} 不可重试",
                    fix="只有 failed/cancelled 的任务可以显式重试。",
                )
            if (job.error_code == "SUBMISSION_UNKNOWN"
                    or job.phase in {"send_intent", "submitted", "polling", "downloaded", "submission_unknown"}):
                raise StoreConflict("SUBMISSION_UNKNOWN: retry requires a bound manual confirmation",
                                    fix="当前窗口未实现费用确认入口；不得把显式 retry 当确认。")
            if job.state == "cancelled":
                # Cancel releases checkpoint blob protection. A deliberate retry
                # must regenerate segments, not reuse possibly collected media.
                conn.execute("DELETE FROM ingest_subjobs WHERE job_id=? AND ordinal>=1", (job_id,))
            stamp = time.time()
            self._write(
                conn,
                "UPDATE ingest_jobs SET state = 'queued', phase = 'prepared', owner_token = '', "
                "lease_until = 0, error_code = '', error_summary = '', retry_after = 0, "
                "updated_at = ? WHERE job_id = ?",
                (stamp, job_id),
            )
            conn.execute(
                "UPDATE auto_seen SET state = 'queued', last_seen_scan = ? WHERE source = ?",
                (stamp, job.source),
            )
            conn.commit()
            updated = self._job_row(conn, job_id)
            assert updated is not None
            return updated

    def job_status(self, job_id: str) -> JobRecord | None:
        gen = self._ensure_read_generation()
        if not gen:
            return None
        conn = self._open_conn(gen)
        return self._job_row(conn, job_id)

    def list_jobs(self, *, source: str = "", state: str = "", limit: int = 50) -> list[JobRecord]:
        gen = self._ensure_read_generation()
        if not gen:
            return []
        conn = self._open_conn(gen)
        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(normalize_source_path(source, self.layout.vault_path))
        if state:
            if state not in JOB_STATES:
                raise StoreContractError(f"state 必须是 {JOB_STATES} 之一，收到 {state!r}")
            clauses.append("state = ?")
            params.append(state)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._query_all(
            conn,
            f"SELECT {self._JOB_COLUMNS} FROM ingest_jobs {where} "
            "ORDER BY created_at DESC, job_id DESC LIMIT ?",
            (*params, max(1, int(limit))),
            what="任务列表",
        )
        return [self._row_to_job(row) for row in rows]

    def queue_depth(self) -> int:
        gen = self._ensure_read_generation()
        if not gen:
            return 0
        conn = self._open_conn(gen)
        row = self._query_one(
            conn,
            "SELECT COUNT(*) FROM ingest_jobs WHERE state IN ('queued', 'parsing', 'staged')",
            what="队列深度",
        )
        return int(row[0]) if row is not None else 0

    # ------------------------------------------------------------ 去重账本/派生代次

    def record_auto_seen(self, source: str, *, source_sha256: str = "", last_job_id: str = "",
                         state: str = "", policy: str = "", source_size: int | None = None,
                         source_mtime_ns: int | None = None,
                         source_ctime_ns: int | None = None) -> None:
        """写 `auto_seen` 去重事实（§12.3：历史 job 裁剪不删去重事实）。"""
        rel = normalize_source_path(source, self.layout.vault_path)
        with self.mutation() as conn:
            now = time.time()
            conn.execute(
                "INSERT INTO auto_seen (source, source_sha256, last_job_id, policy, state, source_size, "
                "source_mtime_ns, source_ctime_ns, last_seen_scan) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET "
                "source_sha256 = CASE WHEN excluded.source_sha256 <> '' THEN excluded.source_sha256 "
                "                     ELSE auto_seen.source_sha256 END, "
                "last_job_id = CASE WHEN excluded.last_job_id <> '' THEN excluded.last_job_id "
                "                   ELSE auto_seen.last_job_id END, "
                "policy = CASE WHEN excluded.policy <> '' THEN excluded.policy ELSE auto_seen.policy END, "
                "state = CASE WHEN excluded.state <> '' THEN excluded.state ELSE auto_seen.state END, "
                "source_size = COALESCE(excluded.source_size, auto_seen.source_size), "
                "source_mtime_ns = COALESCE(excluded.source_mtime_ns, auto_seen.source_mtime_ns), "
                "source_ctime_ns = COALESCE(excluded.source_ctime_ns, auto_seen.source_ctime_ns), "
                "last_seen_scan = excluded.last_seen_scan",
                (rel, str(source_sha256 or ""), str(last_job_id or ""), str(policy or ""),
                 str(state or ""), _validate_opt_int(source_size, "source_size", min_value=0),
                 _validate_opt_int(source_mtime_ns, "source_mtime_ns", min_value=0),
                 _validate_opt_int(source_ctime_ns, "source_ctime_ns", min_value=0), now),
            )
            conn.commit()

    def iter_auto_seen(self) -> Iterator[dict[str, Any]]:
        gen = self._ensure_read_generation()
        if not gen:
            return
        conn = self._open_conn(gen)
        rows = self._query_all(
            conn,
            "SELECT source, source_sha256, last_job_id, policy, state, source_size, source_mtime_ns, "
            "source_ctime_ns, last_seen_scan FROM auto_seen ORDER BY source",
            what="去重账本",
        )
        for row in rows:
            yield {
                "source": str(row[0]),
                "source_sha256": str(row[1] or ""),
                "last_job_id": str(row[2] or ""),
                "policy": str(row[3] or ""),
                "state": str(row[4] or ""),
                "source_size": None if row[5] is None else int(row[5]),
                "source_mtime_ns": None if row[6] is None else int(row[6]),
                "source_ctime_ns": None if row[7] is None else int(row[7]),
                "last_seen_scan": float(row[8] or 0.0),
            }

    def migrate_legacy_ledger(self, payload: Mapping[str, Any]) -> dict[str, int]:
        """把旧 `.ingest_state.json` 的**终态事实**按库迁入 store（§20.1 只读迁移）。

        只恢复 `done` / `failed`（可靠终态）与 `auto_seen` 去重事实；
        **不**恢复 `queued` / `parsing`（旧进程的中间态不得复活，也不得自动重传）。
        幂等：同 source 的去重事实与同 job_id 的任务不会重复插入。
        """
        if not isinstance(payload, Mapping):
            raise StoreContractError("旧账本必须是 JSON 对象")
        jobs = payload.get("jobs") or {}
        seen = payload.get("auto_seen") or {}
        if not isinstance(jobs, Mapping) or not isinstance(seen, Mapping):
            raise StoreContractError("旧账本 jobs/auto_seen 必须是对象")
        restored_jobs = 0
        restored_seen = 0
        skipped_active = 0
        with self.mutation() as conn:
            now = time.time()
            epoch = self._current_epoch()
            for record in jobs.values():
                if not isinstance(record, Mapping):
                    continue
                state = str(record.get("state") or "")
                if state not in ("done", "failed"):
                    skipped_active += 1
                    continue
                job_id = str(record.get("job_id") or "")
                source = str(record.get("source") or "")
                if not job_id or not source:
                    continue
                try:
                    rel = normalize_source_path(source, self.layout.vault_path)
                except DocStoreError:
                    skipped_active += 1
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM ingest_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if exists is not None:
                    continue
                self._write(
                    conn,
                    "INSERT INTO ingest_jobs (job_id, doc_id, source, source_sha256, parser_fingerprint, "
                    "vault_epoch, request_seq, state, phase, owner_token, lease_until, attempts, retry_after, "
                    "result_revision, error_code, error_summary, created_at, updated_at) "
                    "VALUES (?, '', ?, ?, '', ?, 0, ?, ?, '', 0, 0, 0, '', ?, ?, ?, ?)",
                    (
                        job_id, rel, str(record.get("sha256") or ""), epoch, state,
                        "committed" if state == "done" else "",
                        "MIGRATED_LEGACY" if state == "failed" else "",
                        str(record.get("error") or "")[:500],
                        float(record.get("submitted_at") or now),
                        float(record.get("finished_at") or record.get("submitted_at") or now),
                    ),
                )
                restored_jobs += 1
            for source, record in seen.items():
                if not isinstance(record, Mapping):
                    continue
                try:
                    rel = normalize_source_path(str(source), self.layout.vault_path)
                except DocStoreError:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM auto_seen WHERE source = ?", (rel,)
                ).fetchone()
                if exists is not None:
                    continue
                conn.execute(
                    "INSERT INTO auto_seen (source, source_sha256, last_job_id, policy, state, "
                    "source_size, source_mtime_ns, source_ctime_ns, last_seen_scan) "
                    "VALUES (?, ?, ?, '', ?, NULL, NULL, NULL, ?)",
                    (rel, str(record.get("sha256") or ""), str(record.get("last_job_id") or ""),
                     str(record.get("state") or "migrated"),
                     float(record.get("submitted_at") or now)),
                )
                restored_seen += 1
            conn.commit()
        return {
            "restored_jobs": restored_jobs,
            "restored_auto_seen": restored_seen,
            "skipped_active_states": skipped_active,
        }

    def get_derived_generation(self, profile_key: str) -> DerivedGeneration | None:
        gen = self._ensure_read_generation()
        if not gen:
            return None
        conn = self._open_conn(gen)
        row = self._query_one(
            conn,
            "SELECT profile_key, change_seq, chunker_fingerprint, space_fingerprint, status, last_error "
            "FROM derived_generations WHERE profile_key = ?",
            (str(profile_key),),
            what=f"derived_generations（{profile_key!r}）",
        )
        if row is None:
            return None
        return DerivedGeneration(
            profile_key=str(row[0]),
            change_seq=int(row[1] or 0),
            chunker_fingerprint=str(row[2] or ""),
            space_fingerprint=str(row[3] or ""),
            status=str(row[4] or ""),
            last_error=str(row[5] or ""),
        )

    def mark_derived_generation(self, profile_key: str, *, change_seq: int,
                                chunker_fingerprint: str, space_fingerprint: str,
                                status: str, last_error: str = "") -> None:
        """派生层进度（C95）：只记「已构建到哪个 change_seq」，不替代源核验。"""
        if not isinstance(profile_key, str) or not profile_key:
            raise StoreContractError("profile_key 必填")
        if status not in ("pending", "building", "ready", "failed", "stale"):
            raise StoreContractError(
                f"derived status 必须是 pending/building/ready/failed/stale，收到 {status!r}"
            )
        with self.mutation() as conn:
            if status == "ready":
                current = conn.execute("SELECT change_seq FROM store_meta WHERE id = 1").fetchone()
                if current is None or int(current[0]) != int(change_seq):
                    status = "stale"
            conn.execute(
                "INSERT INTO derived_generations (profile_key, change_seq, chunker_fingerprint, "
                "space_fingerprint, status, last_error) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_key) DO UPDATE SET change_seq = excluded.change_seq, "
                "chunker_fingerprint = excluded.chunker_fingerprint, "
                "space_fingerprint = excluded.space_fingerprint, status = excluded.status, "
                "last_error = excluded.last_error",
                (profile_key, int(change_seq), str(chunker_fingerprint), str(space_fingerprint),
                 status, str(last_error or "")[:500]),
            )
            conn.commit()

    # ------------------------------------------------------------------ 可见性

    def get_active(self, source: str, *, include_hidden: bool = False) -> ActiveDocument | None:
        rel = normalize_source_path(source, self.layout.vault_path)
        gen = self._ensure_read_generation()
        if not gen:
            return None
        conn = self._open_conn(gen)
        row = self._query_one(
            conn,
            "SELECT doc_id, source, visibility, active_revision FROM documents WHERE source = ?",
            (rel,),
            what=f"documents（{rel!r}）",
        )
        if row is None:
            return None
        doc_id, src, visibility = str(row[0]), str(row[1]), str(row[2])
        active_revision = str(row[3]) if row[3] else ""
        if not active_revision:
            return None
        if not include_hidden and visibility != "active":
            return None
        revision = self._load_revision(conn, active_revision)
        if revision is None or revision.state != "committed":
            return None
        return ActiveDocument(
            doc_id=doc_id,
            source=src,
            visibility=visibility,
            active_revision=active_revision,
            revision=revision,
        )

    def list_documents(self, visibility: str | None = None) -> list[DocumentRecord]:
        if visibility is not None and visibility not in _VISIBILITIES:
            raise StoreContractError("invalid document visibility")
        gen = self._ensure_read_generation()
        if not gen:
            return []
        rows = self._query_all(
            self._open_conn(gen),
            "SELECT doc_id, source, visibility, active_revision, updated_at FROM documents"
            + (" WHERE visibility = ?" if visibility is not None else "") + " ORDER BY source",
            (visibility,) if visibility is not None else (),
            what="文档可见性列表",
        )
        return [DocumentRecord(str(row[0]), str(row[1]), str(row[2]),
                               str(row[3] or ""), float(row[4])) for row in rows]

    def iter_visible_documents(self) -> Iterator[DocumentRecord]:
        gen = self._ensure_read_generation()
        if not gen:
            return
        conn = self._open_conn(gen)
        rows = self._query_all(
            conn,
            "SELECT doc_id, source, visibility, active_revision, updated_at FROM documents "
            "WHERE visibility = 'active' AND active_revision IS NOT NULL ORDER BY source",
            what="可见文档列表",
        )
        for row in rows:
            yield DocumentRecord(
                doc_id=str(row[0]),
                source=str(row[1]),
                visibility=str(row[2]),
                active_revision=str(row[3]),
                updated_at=float(row[4]),
            )

    def set_visibility(self, source: str, visibility: str) -> None:
        if visibility not in _VISIBILITIES:
            raise StoreContractError(
                f"visibility 必须是 {_VISIBILITIES} 之一，收到 {visibility!r}",
                fix="使用 active/exempt/deleted/unverified。",
            )
        rel = normalize_source_path(source, self.layout.vault_path)
        with self.mutation() as conn:
            cur = conn.execute("SELECT doc_id FROM documents WHERE source = ?", (rel,)).fetchone()
            if cur is None:
                raise StoreContractError(
                    f"source 未登记：{rel}", fix="只能对已 stage 过的源设置可见性。"
                )
            conn.execute(
                "UPDATE documents SET visibility = ?, updated_at = ? WHERE source = ?",
                (visibility, time.time(), rel),
            )
            conn.commit()

    def invalidate_source(self, source: str) -> None:
        """标 unverified，**保留** active_revision 与解析事实（§12.6 权限/离线不误删）。"""
        rel = normalize_source_path(source, self.layout.vault_path)
        with self.mutation() as conn:
            cur = conn.execute("SELECT doc_id FROM documents WHERE source = ?", (rel,)).fetchone()
            if cur is None:
                return
            conn.execute(
                "UPDATE documents SET visibility = 'unverified', updated_at = ? WHERE source = ?",
                (time.time(), rel),
            )
            conn.commit()

    def mark_deleted(self, source: str) -> None:
        """visibility='deleted' 且清空 active_revision；解析事实保留待显式 purge（§12.6）。"""
        rel = normalize_source_path(source, self.layout.vault_path)
        with self.mutation() as conn:
            cur = conn.execute("SELECT doc_id FROM documents WHERE source = ?", (rel,)).fetchone()
            if cur is None:
                return
            conn.execute(
                "UPDATE documents SET visibility = 'deleted', active_revision = NULL, updated_at = ? "
                "WHERE source = ?",
                (time.time(), rel),
            )
            conn.commit()

    # ------------------------------------------------------------------ 维护/恢复

    def _protected_checkpoint_blob_ids(self, conn: sqlite3.Connection) -> set[str]:
        """有效 checkpoint 引用的 blob（E02 引用保护）。

        崩溃/租约过期且**尚未 attach** 的媒体只要 checkpoint 仍可读、且所属 job 还有
        恢复可能（非 cancelled/superseded），就不算"无引用"。坏 JSON、无归属行一律
        跳过——不得让它们污染保护集合。
        """
        protected: set[str] = set()
        placeholders = ",".join("?" for _ in CHECKPOINT_RELEASE_STATES)
        rows = conn.execute(
            "SELECT s.checkpoint FROM ingest_subjobs s "
            "JOIN ingest_jobs j ON j.job_id = s.job_id "
            f"WHERE s.ordinal >= 1 AND COALESCE(s.checkpoint, '') <> '' "
            f"AND j.state NOT IN ({placeholders})",
            tuple(CHECKPOINT_RELEASE_STATES),
        ).fetchall()
        for (raw,) in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            blob_id = payload.get("blob_id")
            if isinstance(blob_id, str) and blob_id:
                protected.add(blob_id)
            media = payload.get("media")
            if isinstance(media, dict):
                for key in ("blob_id", "parent_blob_id"):
                    value = media.get(key)
                    if isinstance(value, str) and value:
                        protected.add(value)
        return protected

    def _protected_checkpoint_revision_ids(self, conn: sqlite3.Connection) -> set[str]:
        """持有可恢复 checkpoint 的源对应的 staged revision（E02：`recover` 保护）。"""
        placeholders = ",".join("?" for _ in CHECKPOINT_RELEASE_STATES)
        sources = conn.execute(
            "SELECT DISTINCT j.source FROM ingest_jobs j "
            "JOIN ingest_subjobs s ON s.job_id = j.job_id "
            f"WHERE s.ordinal >= 1 AND COALESCE(s.checkpoint, '') <> '' "
            f"AND j.state NOT IN ({placeholders}) AND COALESCE(j.source, '') <> ''",
            tuple(CHECKPOINT_RELEASE_STATES),
        ).fetchall()
        sources = [str(row[0]) for row in sources if row[0]]
        if not sources:
            return set()
        marks = ",".join("?" for _ in sources)
        rows = conn.execute(
            "SELECT revision_id FROM document_revisions WHERE state = 'staged' AND doc_id IN "
            f"(SELECT doc_id FROM documents WHERE source IN ({marks}))",
            tuple(sources),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def gc_unreferenced(self) -> int:
        """无活解析租约时，删除无 occurrence / variant 引用**且未被 checkpoint 保护**的 blob。"""
        with self.mutation() as conn:
            live = conn.execute(
                "SELECT 1 FROM ingest_jobs WHERE state IN ('parsing', 'staged') "
                "AND lease_until > ? LIMIT 1", (time.time(),),
            ).fetchone()
            if live is not None:
                raise StoreConflict("GC blocked by an active parse lease",
                                    fix="等待解析发布或租约结束，不能回收尚未 attach 的媒体。")
            protected = self._protected_checkpoint_blob_ids(conn)
            rows = conn.execute(
                "SELECT b.blob_id FROM media_blobs b "
                "WHERE NOT EXISTS (SELECT 1 FROM media_occurrences o WHERE o.blob_id = b.blob_id) "
                "AND NOT EXISTS (SELECT 1 FROM media_variants v "
                "                WHERE v.blob_id = b.blob_id OR v.parent_blob_id = b.blob_id)"
            ).fetchall()
            deleted = 0
            for (blob_id,) in rows:
                if str(blob_id) in protected:
                    continue
                conn.execute("DELETE FROM media_blobs WHERE blob_id = ?", (str(blob_id),))
                deleted += 1
            conn.commit()
            return deleted

    @contextlib.contextmanager
    def pin_generation(self, *, lease_seconds: float = 300):
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False)
            with ctrl.pin_generation(lease_seconds=lease_seconds) as generation:
                yield generation
        finally:
            ctrl.close()

    def iter_media(self, source: str, *, revision_id: str):
        """Walk one captured revision, rather than mistaking a page for all media."""
        offset = 0
        while True:
            page = self.list_media(source, revision_id=revision_id, offset=offset, limit=1000)
            yield from page
            if len(page) < 1000:
                break
            offset += len(page)

    def list_media(self, source: str, *, revision_id: str = "", offset: int = 0,
                   limit: int = 100) -> list[dict]:
        active = self.get_active(source)
        if active is None or (revision_id and active.revision.revision_id != revision_id):
            raise StoreConflict("Media source/revision is stale or unavailable")
        conn = self._open_conn(self._ensure_read_generation())
        rows = conn.execute("SELECT occurrence_id, blob_id, kind, ordinal, page, caption, ocr, "
                            "t_start_ms, t_end_ms, metadata_json FROM media_occurrences "
                            "WHERE revision_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                            (active.revision.revision_id, min(max(int(limit), 1), 1000), max(int(offset), 0)))
        return [{"revision_id": active.revision.revision_id, "occurrence_id": r[0], "blob_id": r[1],
                 "kind": r[2], "ordinal": r[3], "page": r[4], "caption": r[5], "ocr": r[6],
                 "t_start_ms": r[7], "t_end_ms": r[8], "metadata": _load_json(r[9], "media", {})}
                for r in rows]

    def read_media(self, source: str, *, revision_id: str, occurrence_id: str,
                   variant_id: str = "original", max_bytes: int = 20 * 1024 * 1024,
                   include_data: bool = False) -> dict:
        if type(include_data) is not bool or type(max_bytes) is not int or max_bytes <= 0:
            raise StoreContractError("Invalid media read budget/options")
        with self.pin_generation():
            active = self.get_active(source)
            if active is None or active.revision.revision_id != revision_id:
                raise StoreConflict("Media source/revision is stale or unavailable")
            conn = self._open_conn(self._ensure_read_generation())
            if variant_id == "original":
                row = conn.execute("SELECT b.blob_id, b.mime_type, b.byte_size, b.width, b.height, "
                                   "b.duration_ms FROM media_occurrences o JOIN media_blobs b ON b.blob_id=o.blob_id "
                                   "WHERE o.revision_id=? AND o.occurrence_id=?", (revision_id, occurrence_id)).fetchone()
            else:
                row = conn.execute("SELECT b.blob_id, b.mime_type, b.byte_size, b.width, b.height, b.duration_ms "
                                   "FROM media_variants v JOIN media_blobs b ON b.blob_id=v.blob_id "
                                   "WHERE v.revision_id=? AND v.occurrence_id=? AND v.variant_id=?",
                                   (revision_id, occurrence_id, variant_id)).fetchone()
            if row is None:
                raise StoreContractError("MEDIA_NOT_FOUND")
            result = dict(zip(("blob_id", "mime_type", "byte_size", "width", "height", "duration_ms"), row))
            result.update(source=source, revision_id=revision_id, occurrence_id=occurrence_id, variant_id=variant_id)
            if include_data:
                if row[2] > max_bytes:
                    raise StoreQuotaExceeded("MEDIA_TOO_LARGE")
                result["data"] = bytes(conn.execute("SELECT data FROM media_blobs WHERE blob_id=?", (row[0],)).fetchone()[0])
            return result

    def put_media_variant(self, *, revision_id: str, occurrence_id: str, data: bytes,
                          mime_type: str, kind: str = "preview", variant_id: str = "preview",
                          parent_blob_id: str | None = None, preprocess_version: str = "",
                          range_json: str | None = None, width: int | None = None,
                          height: int | None = None, duration_ms: int | None = None) -> str:
        """写入**派生媒体变体**（preview 等，§20.7D）：blob 内容寻址 + 变体关联。

        - `original` 不是变体：它由 occurrence 直接指向原 blob，禁止在这里重复登记；
        - occurrence 必须已提交存在（FK + 显式检查），拒绝挂孤立变体；
        - `parent_blob_id` 留空时写 NULL（不是空串），否则外键会指向不存在的 blob；
        - 同一 `(revision, occurrence, variant_id)` 覆盖为最新，重复写入天然幂等。
        返回变体 blob_id；quota/归属不满足时抛错，由调用方决定是否降级。
        """
        if not isinstance(variant_id, str) or not variant_id:
            raise StoreContractError("variant_id 必填")
        if variant_id == "original":
            raise StoreContractError("original 不是派生变体；occurrence 已直接指向原 blob")
        if not isinstance(kind, str) or not kind:
            raise StoreContractError("variant kind 必填")
        blob_id = self.put_media_blob(data=data, mime_type=mime_type, width=width,
                                     height=height, duration_ms=duration_ms)
        with self.mutation() as conn:
            row = conn.execute("SELECT state FROM document_revisions WHERE revision_id = ?",
                               (revision_id,)).fetchone()
            if row is None or str(row[0]) != "committed":
                raise StoreContractError(
                    f"revision {revision_id!r} 未提交，不能挂派生变体",
                    fix="先 commit_revision，再写 preview/segment 变体。",
                )
            exists = conn.execute("SELECT 1 FROM media_occurrences WHERE revision_id = ? "
                                  "AND occurrence_id = ?", (revision_id, occurrence_id)).fetchone()
            if exists is None:
                raise StoreContractError(f"occurrence {occurrence_id!r} 不属于该 revision，拒绝孤立变体")
            conn.execute(
                "INSERT OR REPLACE INTO media_variants (revision_id, occurrence_id, variant_id, "
                "blob_id, parent_blob_id, kind, preprocess_version, range_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (revision_id, occurrence_id, variant_id, blob_id,
                 (str(parent_blob_id) if parent_blob_id else None), kind,
                 str(preprocess_version), range_json),
            )
            conn.commit()
        return blob_id

    def _ensure_media_links(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS media_chunk_links (revision_id TEXT NOT NULL, "
                     "occurrence_id TEXT NOT NULL, profile_key TEXT NOT NULL, chunker_fingerprint TEXT NOT NULL, "
                     "derived_generation_id TEXT NOT NULL, chunk_id TEXT NOT NULL, relation TEXT NOT NULL, "
                     "PRIMARY KEY(revision_id, occurrence_id, profile_key, derived_generation_id, chunk_id, relation), "
                     "FOREIGN KEY(revision_id, occurrence_id) REFERENCES media_occurrences(revision_id, occurrence_id) ON DELETE CASCADE)")

    def set_media_chunk_links(self, *, revision_id: str, occurrence_id: str, profile_key: str,
                             chunker_fingerprint: str, derived_generation_id: str,
                             links: Sequence[Mapping[str, str]]) -> None:
        with self.mutation() as conn:
            self._ensure_media_links(conn)
            conn.execute("DELETE FROM media_chunk_links WHERE revision_id=? AND occurrence_id=? "
                         "AND profile_key=? AND derived_generation_id=?",
                         (revision_id, occurrence_id, profile_key, derived_generation_id))
            for link in links:
                conn.execute("INSERT INTO media_chunk_links VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (revision_id, occurrence_id, profile_key, chunker_fingerprint,
                              derived_generation_id, str(link["chunk_id"]), str(link["relation"])))
            conn.commit()

    def replace_media_chunk_links(self, *, revision_id: str, profile_key: str,
                                  derived_generation_id: str,
                                  links: Sequence[Mapping[str, str]],
                                  chunker_fingerprint: str = "") -> int:
        """按 (revision, profile, generation) **整代替换** links（E08-b）。

        整代替换是撤销语义的关键：同一代里被移除的 occurrence/chunk 不会残留旧链，
        而其它代（其它 profile/generation）完全不受影响。返回写入的链接条数。
        """
        with self.mutation() as conn:
            self._ensure_media_links(conn)
            conn.execute("DELETE FROM media_chunk_links WHERE revision_id=? AND profile_key=? "
                         "AND derived_generation_id=?",
                         (revision_id, profile_key, derived_generation_id))
            written = 0
            for link in links:
                conn.execute("INSERT OR IGNORE INTO media_chunk_links VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (revision_id, str(link["occurrence_id"]), profile_key,
                              str(link.get("chunker_fingerprint") or chunker_fingerprint),
                              derived_generation_id, str(link["chunk_id"]), str(link["relation"])))
                written += 1
            conn.commit()
            return written

    def delete_media_chunk_links(self, *, source: str = "", revision_id: str = "",
                                 profile_key: str = "") -> int:
        """撤销派生召回（E08-c）：按 source/revision/profile 删除 links，**不动**解析事实与 blob。

        返回删除条数；三个条件都为空时拒绝（避免误清全表）。
        """
        if not (source or revision_id or profile_key):
            raise StoreContractError("delete_media_chunk_links requires a bounded scope")
        with self.mutation() as conn:
            self._ensure_media_links(conn)
            conditions: list[str] = []
            params: list[Any] = []
            if revision_id:
                conditions.append("revision_id=?")
                params.append(revision_id)
            if profile_key:
                conditions.append("profile_key=?")
                params.append(profile_key)
            if source:
                conditions.append("revision_id IN (SELECT r.revision_id FROM document_revisions r "
                                  "JOIN documents d ON d.doc_id=r.doc_id WHERE d.source=?)")
                params.append(source)
            cursor = conn.execute("DELETE FROM media_chunk_links WHERE " + " AND ".join(conditions), params)
            conn.commit()
            return int(cursor.rowcount)

    def list_media_chunk_links(self, *, revision_id: str, occurrence_id: str = "") -> list[dict]:
        """读取路径用的**跨 profile** 链接列举（E08-e）：不预设 profile/generation。

        返回 `(profile_key, derived_generation_id, occurrence_id, chunk_id, relation)`，
        供 kb_read_media 从 links 双向取 context，同时保留 revision/occurrence 归属。
        """
        gen = self._ensure_read_generation()
        if not gen or not revision_id:
            return []
        conn = self._open_conn(gen)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='media_chunk_links'").fetchone() is None:
            return []
        rows = conn.execute("SELECT profile_key, derived_generation_id, occurrence_id, chunk_id, relation "
                            "FROM media_chunk_links WHERE revision_id=? AND (?='' OR occurrence_id=?)",
                            (revision_id, occurrence_id, occurrence_id))
        return [dict(zip(("profile_key", "derived_generation_id", "occurrence_id", "chunk_id", "relation"),
                         row)) for row in rows]

    def get_media_chunk_links(self, *, revision_id: str, profile_key: str,
                             derived_generation_id: str, occurrence_id: str = "") -> list[dict]:
        gen = self._ensure_read_generation()
        if not gen:
            return []
        conn = self._open_conn(gen)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='media_chunk_links'").fetchone() is None:
            return []
        rows = conn.execute("SELECT occurrence_id, chunk_id, relation, chunker_fingerprint FROM media_chunk_links "
                            "WHERE revision_id=? AND profile_key=? AND derived_generation_id=? "
                            "AND (?='' OR occurrence_id=?)",
                            (revision_id, profile_key, derived_generation_id, occurrence_id, occurrence_id))
        return [dict(zip(("occurrence_id", "chunk_id", "relation", "chunker_fingerprint"), row)) for row in rows]

    def prepare_import(self, backup: str | Path, *, trust_parsed_documents: bool = False) -> dict:
        if type(trust_parsed_documents) is not bool:
            raise StoreContractError("trust_parsed_documents must be bool")
        self._enforce_writable()
        source = Path(backup)
        if source.stat().st_size > self._quota_limit_bytes():
            raise StoreQuotaExceeded("Imported document database exceeds local quota")
        validate_backup(source)
        ctrl = ControlStore(self.layout)
        ctrl.open(create=True, write=True)
        try:
            state = ctrl.state()
            gen = ctrl.allocate_generation()
            target_path = self._store_path(gen)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            incoming = sqlite3.connect(_readonly_uri(source), uri=True)
            target = sqlite3.connect(str(target_path))
            try:
                incoming.execute("PRAGMA trusted_schema=OFF")
                target.executescript(_DOCSTORE_SCHEMA)
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("BEGIN")
                target.execute("PRAGMA defer_foreign_keys=ON")
                for table in _DOCSTORE_TABLES:
                    columns = [str(r[1]) for r in target.execute(f'PRAGMA table_info("{table}")')]
                    quoted = ",".join('"' + c + '"' for c in columns)
                    placeholders = ",".join("?" for _ in columns)
                    for row in incoming.execute(f'SELECT {quoted} FROM "{table}"'):
                        target.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', row)
                for (source_path,) in target.execute("SELECT source FROM documents"):
                    normalize_source_path(source_path, self.layout.vault_path)
                for row in target.execute("SELECT source_sha256, render_sha256, parsed_markdown "
                                          "FROM document_revisions"):
                    if len(str(row[0])) != 64 or any(c not in "0123456789abcdef" for c in str(row[0])) or hashlib.sha256(
                            str(row[2]).encode("utf-8")).hexdigest() != row[1]:
                        raise SnapshotInvalid("Imported revision hash is invalid")
                for blob_id, checksum, size, data in target.execute(
                        "SELECT blob_id, checksum, byte_size, data FROM media_blobs"):
                    if len(data) != size or hashlib.sha256(data).hexdigest() not in (blob_id, checksum):
                        raise SnapshotInvalid("Imported media checksum is invalid")
                target.execute("DELETE FROM ingest_jobs")
                target.execute("DELETE FROM ingest_subjobs")
                target.execute("DELETE FROM derived_generations")
                target.execute("UPDATE documents SET visibility='unverified'")
                if trust_parsed_documents:
                    for doc_id, relative, source_sha in target.execute(
                            "SELECT doc_id, source, source_sha256 FROM documents"):
                        physical = self.layout.vault_path / relative
                        if physical.is_file() and _file_sha256(physical) == source_sha:
                            target.execute("UPDATE documents SET visibility='active' WHERE doc_id=?", (doc_id,))
                target.execute("UPDATE store_meta SET vault_binding=?, vault_epoch=? WHERE id=1",
                               (self._binding, state.epoch + 1))
                if target.execute("PRAGMA foreign_key_check").fetchone():
                    raise SnapshotInvalid("Imported references are invalid")
                logical = sum(self._logical_breakdown(target)[:3])
                if logical > self._quota_limit_bytes():
                    raise StoreQuotaExceeded("Imported logical assets exceed local quota")
                store_uuid = str(target.execute("SELECT store_uuid FROM store_meta WHERE id=1").fetchone()[0])
                target.commit()
                target.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            finally:
                incoming.close()
                target.close()
            ctrl.register_generation(gen, target_path.parent, "document", "validated")
            return {"generation_id": gen, "expected_generation": state.active_document_generation,
                    "expected_epoch": state.epoch, "store_uuid": store_uuid}
        finally:
            ctrl.close()

    def publish_import(self, prepared: dict, *, snapshot_sha256: str = "", trusted: bool = False) -> int:
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=True)
            epoch = ctrl.publish_import(**prepared, snapshot_sha256=snapshot_sha256, trusted=trusted)
            self.close()
            self._generation_id = ""
            self.open(create=False)
            return epoch
        finally:
            ctrl.close()

    def restore_generation(self, generation_id: str) -> int:
        path = self._store_path(generation_id)
        validate_backup(path)
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=True)
            state = ctrl.state()
            ctrl.register_generation(generation_id, path.parent, "document", "validated")
            conn = sqlite3.connect(_readonly_uri(path), uri=True)
            try:
                store_uuid = str(conn.execute("SELECT store_uuid FROM store_meta WHERE id=1").fetchone()[0])
            finally:
                conn.close()
            return self.publish_import({"generation_id": generation_id,
                "expected_generation": state.active_document_generation,
                "expected_epoch": state.epoch, "store_uuid": store_uuid})
        finally:
            ctrl.close()

    def retained_backup(self, path: str | Path) -> dict:
        with self.pin_generation() as generation:
            self.backup_to(path)
            return {"path": str(path), "generation_id": generation,
                    "change_seq": self.change_seq(), "retained": True}

    def capture_generation_pin(self, *, lease_seconds: float = 300) -> GenerationPin:
        """捕获固定版本 handle（E04-c）：控制面 active generation + change_seq + epoch。

        捕获与当前读代不一致立即释放并报冲突——不允许跨代读取。
        """
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=True)
            owner = f"pid{os.getpid()}:{uuid.uuid4().hex}"
            pin_id, generation, lease_until = ctrl.acquire_generation_pin(
                owner, lease_seconds=lease_seconds)
            epoch = int(ctrl.state().epoch)
        finally:
            ctrl.close()
        current = self._ensure_read_generation()
        if current and generation and current != generation:
            try:
                ctrl = ControlStore(self.layout)
                ctrl.open(create=False, write=True)
                ctrl.release_generation_pin(pin_id, owner)
            except Exception:
                pass
            finally:
                try:
                    ctrl.close()
                except Exception:
                    pass
            raise StoreConflict(
                f"captured generation {generation!r} differs from the readable generation {current!r}",
                fix="重新捕获固定版本；跨代读取会把旧 chunk 与新 media 拼在一起。",
            )
        return GenerationPin(pin_id=pin_id, generation_id=generation, owner=owner,
                             lease_until=lease_until, change_seq=self.change_seq(), epoch=epoch)

    def validate_generation_pin(self, pin: GenerationPin) -> bool:
        """批次/最终响应前验租（E04-c）：过期、被释放、换代或换 epoch 一律 False。"""
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=False)
            if not ctrl.pin_is_live(pin.pin_id, pin.owner, generation_id=pin.generation_id):
                return False
            state = ctrl.state()
        except Exception:
            return False
        finally:
            ctrl.close()
        if str(state.active_document_generation or "") != pin.generation_id:
            return False
        return int(state.epoch) == pin.epoch

    def renew_generation_pin(self, pin: GenerationPin, *, lease_seconds: float = 300) -> bool:
        """未到期同 owner 续租；过期返回 False（不复活）。"""
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=True)
            return ctrl.renew_generation_pin(pin.pin_id, pin.owner, lease_seconds=lease_seconds)
        finally:
            ctrl.close()

    def release_generation_pin(self, pin: GenerationPin) -> bool:
        ctrl = ControlStore(self.layout)
        try:
            ctrl.open(create=False, write=True)
            return ctrl.release_generation_pin(pin.pin_id, pin.owner)
        finally:
            ctrl.close()

    def has_active_ingest(self) -> bool:
        """全队列活跃/unknown 检查（E04-c）：不受 `list_jobs` 的 LIMIT 截断影响。

        老 job 也可能仍在跑；只查最近 500 条会漏掉它们（§20.7F 门禁 fail closed）。
        """
        gen = self._ensure_read_generation()
        if not gen:
            return False
        conn = self._open_conn(gen)
        row = self._query_one(
            conn,
            "SELECT COUNT(*) FROM ingest_jobs WHERE state IN ('queued', 'parsing', 'staged') "
            "OR phase IN ('send_intent', 'submitted', 'polling', 'downloaded', 'submission_unknown')",
            what="活跃摄取任务计数",
        )
        return bool(row and int(row[0]) > 0)

    def backup_to(self, path: str | Path, *, pages: int = -1, progress: Any = None) -> Path:
        """`sqlite3.Connection.backup` 一致性备份（不是 copy 主文件，§20.2）。

        E04-c：`pages`/`progress` 是可控分页与进度接缝——长时间备份按页推进并在进度
        回调里续租/验租，不在一次调用里持有 store 事务；任何失败都删除临时输出，
        不留半截备份冒充成功。
        """
        gen = self._ensure_read_generation()
        if not gen:
            raise StoreContractError(
                "无 active generation，无法备份", fix="先建立 doc_store。"
            )
        conn = self._open_conn(gen)
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        target = sqlite3.connect(str(dest))
        try:
            conn.backup(target, pages=pages, progress=progress)
            target.commit()
        except BaseException:
            try:
                target.close()
            except Exception:
                pass
            try:
                dest.unlink()
            except OSError:
                pass
            raise
        finally:
            try:
                target.close()
            except Exception:
                pass
        return dest

    def recover(self, *, stale_before: float | None = None) -> int:
        """丢弃 staged 候选；不触碰 committed 事实（§12.6）。

        **活候选保护**（C94 修 Lane A 遗留 P3）：仍被「活跃租约任务」持有的源，其 staged
        候选不清——否则 `recover()` 会删掉另一个活 writer 正在写的半成品。
        """
        with self.mutation() as conn:
            stamp = time.time()
            live_rows = conn.execute(
                "SELECT DISTINCT source FROM ingest_jobs WHERE state IN ('parsing', 'staged') "
                "AND source IS NOT NULL AND source <> '' AND lease_until > ?",
                (stamp,),
            ).fetchall()
            live_sources = {str(row[0]) for row in live_rows}
            if live_sources:
                placeholders = ",".join("?" for _ in live_sources)
                rows = conn.execute(
                    "SELECT revision_id FROM document_revisions WHERE state = 'staged' "
                    f"AND doc_id NOT IN (SELECT doc_id FROM documents WHERE source IN ({placeholders}))"
                    + (" AND created_at < ?" if stale_before is not None else ""),
                    (*sorted(live_sources), *((float(stale_before),) if stale_before is not None else ())),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT revision_id FROM document_revisions WHERE state = 'staged'"
                    + (" AND created_at < ?" if stale_before is not None else ""),
                    ((float(stale_before),) if stale_before is not None else ()),
                ).fetchall()
            # E02：持有可恢复 checkpoint 的 staged 候选不得被清掉——它的媒体还没 attach，
            # 清掉等于把已确认的转录/sink 事实变成不可恢复。
            protected = self._protected_checkpoint_revision_ids(conn)
            deleted = 0
            for (revision_id,) in rows:
                if str(revision_id) in protected:
                    continue
                conn.execute("DELETE FROM document_revisions WHERE revision_id = ?", (str(revision_id),))
                deleted += 1
            conn.commit()
            return deleted


# --------------------------------------------------------------------------- 备份校验


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _assert_backup_schema(conn: sqlite3.Connection) -> None:
    """备份 schema 白名单校验（只读、trusted_schema=OFF、禁扩展）。

    只承认与本机受信 schema **逐字一致**的对象集合（`media_chunk_links` 允许缺失）；
    trigger/view/虚拟表/额外对象一律拒。垃圾文件或非 SQLite 内容在这里第一次读
    schema 时就会失败，必须转成稳定 `SnapshotInvalid`（fail closed、不删文件）——
    调用方按 code 判「快照坏了」，不能让原生 sqlite3 错误漏出去。
    """
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.enable_load_extension(False)
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(_DOCSTORE_SCHEMA)
            objects = {str(r[0]): (str(r[1]), str(r[2])) for r in expected.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL")}
            actual = {str(r[0]): (str(r[1]), str(r[2])) for r in conn.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL")}
            optional = actual.pop("media_chunk_links", None)
            if optional is not None:
                helper = DocumentStore.__new__(DocumentStore)
                helper._ensure_media_links(expected)
                wanted = expected.execute(
                    "SELECT type, sql FROM sqlite_master WHERE name='media_chunk_links'").fetchone()
                if optional != (str(wanted[0]), str(wanted[1])):
                    raise SnapshotInvalid("Snapshot media link schema is invalid")
            if actual != objects:
                raise SnapshotInvalid("Snapshot schema differs from the trusted local schema")
        finally:
            expected.close()
    except sqlite3.DatabaseError as exc:
        raise SnapshotInvalid(
            f"备份不是合法的 SQLite 数据库：{exc}",
            fix="保留原文件，重新导出快照；不要就地修改或覆盖。",
        ) from exc


def validate_backup(path: str | Path) -> dict:
    """只读校验备份：schema_version / 必需表 / integrity_check（§20.2/§23.1）。"""
    target = Path(path)
    if not target.is_file():
        raise SnapshotInvalid(f"备份文件不存在：{target}", fix="检查备份路径。")
    try:
        conn = sqlite3.connect(_readonly_uri(target), uri=True)
    except sqlite3.Error as exc:
        raise SnapshotInvalid(f"无法以只读方式打开备份：{exc}", fix="检查文件权限。") from exc
    try:
        # 垃圾文件/非 SQLite 内容在首次读 schema 时暴露：统一转成稳定 `SnapshotInvalid`
        _assert_backup_schema(conn)
        try:
            row = conn.execute("SELECT schema_version FROM store_meta WHERE id = 1").fetchone()
            present = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        except sqlite3.DatabaseError as exc:
            raise SnapshotInvalid(
                f"备份不是合法的 docstore 数据库：{exc}", fix="重新导出快照。"
            ) from exc
        missing = [table for table in _DOCSTORE_TABLES if table not in present]
        if missing:
            raise SnapshotInvalid(
                f"备份缺少必需表：{missing}",
                fix="快照导出不完整，请重新导出。",
            )
        if row is None:
            raise StoreCorrupt(
                "备份缺少 store_meta 单行", fix="快照导出不完整，请重新导出。"
            )
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise StoreSchemaUnsupported(
                f"备份 schema_version={version} 高于本程序支持 {SCHEMA_VERSION}",
                fix="升级 Mortis-RAG-MCP 后再导入；禁止降级读取。",
            )
        if version != SCHEMA_VERSION:
            raise StoreSchemaUnsupported(
                f"备份 schema_version={version} 不是已知版本（本程序支持 {SCHEMA_VERSION}）",
                fix="用匹配版本的程序导入；禁止猜字段读取。",
            )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise StoreCorrupt(
                f"备份 integrity_check 未通过：{integrity[0] if integrity else 'no result'}",
                fix="备份已损坏，请重新导出。",
            )
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return {
            "schema_version": version,
            "user_version": user_version,
            "integrity": "ok",
            "tables": sorted(present),
            "path": str(target),
        }
    finally:
        conn.close()
