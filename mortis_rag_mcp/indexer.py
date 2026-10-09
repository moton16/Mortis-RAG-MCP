from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import zipfile
from array import array
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import AppConfig
from .embedding_capabilities import normalize_endpoint, resolve_embedding_profile
from .doc_store import (
    ControlStore,
    DocStoreError,
    DocumentStore,
    StorageLayout,
    is_within,
    resolve_storage_layout,
    vault_cache_key,
)
from .paid_requests import (
    PaidRequestJournal,
    UNRESOLVED_INTENT_STATES,
    open_paid_control,
    paid_request_guard,
)
from .fsnotify import WindowsDirectoryWatcher
from .fts import FtsIndex
from .ingest import INGEST_EXTS
from .ingest.tables import iter_table_blocks, split_table_into_chunks
from .providers import EmbeddingProvider, ProviderError, RerankerProvider, create_embedding_provider, create_media_provider, create_reranker_provider
from .vector import create_vector_backend
# v0.8.0 P2：数据模型与缓存编解码提取至私有包 _indexer/（本文件转为 Facade）。
# 下方 re-export 保持 `from mortis_rag_mcp.indexer import ...` 公开导入面 100% 不变。
from ._indexer.cache_codec import _CacheCodec, _VectorsCodec
from ._indexer.models import (
    Chunk,
    SearchFilter,
    _EMB_DTYPE,
    _candidate_terms,
    _extract_snippet,
    dedupe_by_content_hash,
    path_prefix_match,
)
from ._indexer import scanning as _scanning
from ._indexer.scanning import (
    IgnoreMatcher,
    _FUTURE_MTIME_MIN_OBSERVATION_GAP_NS,
    _FUTURE_MTIME_RECHECK_LIMIT,
    _MTIME_TICK_COARSE_NS,
    _MTIME_TICK_PROBE_MAX_SAMPLES,
    _MTIME_TICK_PROBE_MIN_SAMPLES,
    _MTIME_TRUST_MARGIN_NS,
)
from ._indexer import sync_engine as _sync_engine
from ._indexer import search as _search
from ._indexer.search import (
    _ASCII_RE,
    _CJK_RE,
    _RRF_K,
    _SHORT_STOPWORDS,
    _WORD_RE,
    _to_emb,
    cosine as _cosine_fn,
    fts_query as _fts_query_fn,
    hybrid_rank as _hybrid_rank_fn,
    query_tokens as _query_tokens_fn,
    rerank_chunks,
    semantic_rank as _semantic_rank_fn,
)
from ._indexer import chunking as _chunking
from ._indexer.chunking import (
    _BLOCK_IGNORE_END,
    _BLOCK_IGNORE_START,
    _CHAPTER_HEADING_RE,
    _FENCE_RE,
    _FENCE_START_RE,
    _HEADING_RE,
    _IMAGE_EXTS,
    _INDEXABLE_TEXT_EXTS,
    _MD_IMAGE_RE,
    _WIKI_IMAGE_RE,
    _image_note,
    _image_notes_for_line,
    _inject_image_notes,
    _is_chapter_heading,
)



from ._indexer import snapshot as _snapshot
from ._indexer.snapshot import (
    _SNAPSHOT_FORMAT,
    _SNAPSHOT_VERSION,
    _SNAPSHOT_MEMBERS,
    _SNAPSHOT_MEMBER_LIMITS,
)
from ._indexer import exemptions as _exemptions
from ._indexer import reading as _reading
from ._indexer import watch as _watch
from ._indexer.watch import _FS_MAX_DEBOUNCE_WAIT




def _probe_mtime_tick_ns(mtime_samples: Iterable[int]) -> int | None:
    """Facade 包装：保留模块属性 monkeypatch 接缝（tests/test_indexer.py 打桩点）。

    真实实现已提取至 _indexer/scanning.py；本包装是**唯一调用路径**——测试对
    indexer 模块属性的补丁在调用时被解析（D2 决策），补丁后
    _finalize_mtime_tick_probe 观察到的就是补丁。
    """
    return _scanning._probe_mtime_tick_ns(mtime_samples)


# 线程名契约静态锚点（v0.8.0 迁移至 _indexer/exemptions.py 与 _indexer/watch.py 维持不变）：
# name="vault-init"
# name="exempt-sync"
# name="vault-watch-native"
# name="vault-fs-debounce"


class MarkdownIndexer:
    _extract_snippet = staticmethod(_extract_snippet)
    _candidate_terms = staticmethod(_candidate_terms)

    def __init__(
        self,
        vault_path: str | Path,
        config: AppConfig | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        reranker_provider: RerankerProvider | None = None,
        media_provider: Any = None,
        *,
        load_vectors: bool = True,
    ) -> None:
        # load_vectors=False：只读探测用（如 kb_read 的跨库 chunk_id 寻址）——跳过向量层
        # 全量加载，省掉每个未加载库一次的向量反序列化；文本层与 FTS 仍会加载
        # （残余代价：构造期 FTS 仍可能写盘，见 docs/PROJECT_GUIDE.md 的索引层说明）。
        self.vault_path = Path(vault_path).expanduser()
        self.config = config or AppConfig(vault_path=str(self.vault_path))
        self._chunking_config = replace(
            self.config.chunking,
            legacy_chunk_size=int(self.config.chunk_size),
            legacy_chunk_overlap=int(self.config.chunk_overlap),
        )
        # Programmatic legacy callers may set character parameters after AppConfig().
        if (not self._chunking_config.mode_explicit
                and (self.config.chunk_size, self.config.chunk_overlap) != (1200, 0)):
            self._chunking_config = replace(self._chunking_config, mode="legacy_chars", legacy_explicit=True)
        self._embedding_profile = resolve_embedding_profile(self.config.embedding)
        self._paid_profile_requires_approval = False
        self._paid_profile_error: str | None = None
        self._chunking_compatibility_notice: str | None = None
        self._embedding_paused = False
        self.embedding_provider = embedding_provider or create_embedding_provider(self.config.embedding)
        # E07：原生媒体能力按**显式声明**装配。未声明 → None（无该能力）；声明不完整或
        # 无已声明 transport（Q09 缺协议）→ 记录「该 route 不可用」，但**不**影响文本路径。
        self.media_provider: Any = None
        self._media_capability_error: str | None = None
        if media_provider is not None:
            self.media_provider = media_provider
        else:
            try:
                self.media_provider = create_media_provider(self.config.embedding)
            except Exception as exc:
                self._media_capability_error = str(exc)
        self.reranker_provider = reranker_provider
        if reranker_provider is None:
            try:
                self.reranker_provider = create_reranker_provider(self.config.reranker)
            except Exception:
                self.reranker_provider = None
        # C100：付费闸门必须在 provider 构造后立即接线，否则外部文本 embedding/rerank
        # 会被 provider 自身拒绝（`EMBEDDING_PENDING_APPROVAL`），等于既没用上渠道、
        # 又让人误以为「没配 key」。控制库连接保持惰性，只读探测不建库。
        self._paid_control: ControlStore | None = None
        self._paid_lock = threading.Lock()
        self._configure_paid_providers()
        self._chunks: dict[str, list[Chunk]] = {}
        self._signatures: dict[str, str] = {}
        self._stat_cache: dict[str, tuple[int, int, int]] = {}
        # 记录每个 source 的 (mtime_ns, size, ctime_ns) 签名是在哪个进程时钟时刻通过
        # 内容级验证（read + sha256）的。快速路径判据要求文件 mtime 严格早于该时刻
        # 减去安全余量（余量从实际时间戳刻度推导，见 _effective_margin_ns）才信任
        # 签名相等，以识别「改动落在时间戳粒度同一刻度内」的 racily clean 条目。
        # 论证与残余风险见 _fast_path_is_trustworthy。
        self._stat_seen_ns: dict[str, int] = {}
        # 每个 source 的签名在**多少个不同的墙钟时刻**被内容级验证确认过（首次登记
        # 记 1）。仅供「mtime 停在未来」的条目判稳：这类条目的余量归纳永远不成立，
        # 若不做兜底会永久失去零读盘快速路径。见 _fast_path_is_trustworthy。
        self._stat_confirmations: dict[str, int] = {}
        # 本轮扫描采样到的 mtime（供刻度探测）；样本足够即探一次，之后不再采。
        self._mtime_tick_samples: list[int] = []
        # 刻度探测结果与状态：None = 未探测或样本不足（退回固定下限余量）。
        self._mtime_tick_ns: int | None = None
        self._mtime_tick_probed = False
        # 非 None 表示快速路径已被整体禁用（探测到粗刻度），值为可读的原因。
        self._fast_path_disabled_reason: str | None = None
        # 快速路径的观测性告警（source -> 说明），如「mtime 停在未来、复核够次数后
        # 按稳定条目接受」。与 failed_files 同类：只是诊断数据，不影响检索结果。
        self.fast_path_warnings: dict[str, str] = {}
        # 本轮扫描的结束时刻（纳秒），仅供观测/诊断。
        self._scan_completed_ns: int = 0
        # 文本层缓存失效时，初始化阶段读出来的向量暂存到这里，等 sync 重建
        # 文本后再按 chunk.id 补挂（见 _load_vectors_cache / _attach_pending_vectors）。
        self._pending_vectors: dict[str, Any] = {}
        self.failed_files: dict[str, str] = {}
        self.last_sync: float | None = None
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        # native 监听（Windows ReadDirectoryChangesW）：watcher 本体 + 防抖状态。
        # 防抖定时器到点后做一次全量 sync；事件密集时不断顺延但受
        # _FS_MAX_DEBOUNCE_WAIT 封顶（见 _on_fs_events）。
        self._fs_watcher: WindowsDirectoryWatcher | None = None
        # 防抖：常驻调度线程 + 条件变量。每事件新建/取消 threading.Timer 在
        # 编辑器风暴下是每秒数千次线程生灭；单调度线程只在有事件时唤醒。
        self._fs_debounce_lock = threading.Lock()
        self._fs_debounce_cv = threading.Condition(self._fs_debounce_lock)
        self._fs_scheduler_thread: threading.Thread | None = None
        self._fs_requested = False
        self._fs_refresh_immediate: bool = False
        self._fs_pending_since: float | None = None
        self._fs_debounce_seconds: float = 0.5
        # 连续同步的最小间隔：避免高频事件把 sync 压成紧密循环。
        self._fs_last_sync_at: float = 0.0
        self._fs_scheduler_start_lock = threading.Lock()
        self._refresh_error: str | None = None
        self._refresh_requested_at: float | None = None
        self._last_refresh_completed_at: float = 0.0
        self._READ_REFRESH_MIN_INTERVAL_SECONDS: float = 1.0
        # Ingest auto-scan coordination (Card C58c / C61)
        self._ingest_hook: Callable[[], Any] | None = None
        self._ingest_lock = threading.Lock()
        self._ingest_cv = threading.Condition(self._ingest_lock)
        self._ingest_dirty: bool = False
        self._ingest_worker_thread: threading.Thread | None = None
        self._ingest_stopping: bool = False
        self._last_ingest_scan_at: float = 0.0
        self._ingest_scan_failures: int = 0
        self._last_ingest_error: str | None = None
        self._ingest_scan_start_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        # 是否正在 sync（供 kb_stats 报进度）；连续失败次数用于监听线程的退避。
        self._indexing = False
        self._sync_failures = 0
        self._stopping = False
        self._sync_state: str = "idle"
        self._sync_progress: dict[str, Any] = {
            "phase": "idle",
            "files_done": 0,
            "files_total": 0,
            "chunks_done": 0,
            "chunks_total": 0,
        }
        # v0.9.0 Lane A（C91/C92）：版本化文档库（解析事实）与派生索引分离。
        # 布局只在这里解析路径，store 连接惰性建立——读路径不因为一次查询就建库。
        self._storage_layout: StorageLayout | None = None
        self._doc_store: DocumentStore | None = None
        self._doc_store_write_opened = False
        self._doc_store_lock = threading.Lock()
        self._chunks_cache_path: Path | None = None
        self._chunks_cache_loaded: bool = False
        self._vectors_cache_path: Path | None = None
        self._fts_cache_path: Path | None = None
        self._vectors_db_path: Path | None = None
        self._failed_cache_path: Path | None = None
        self._fts: FtsIndex | None = None
        self._vector_backend: Any = None
        if self.config.cache.enabled and self.config.cache.dir:
            try:
                self._init_cache_paths(load_vectors=load_vectors)
            except OSError:
                self._chunks_cache_path = None
                self._vectors_cache_path = None
                self._fts_cache_path = None
                self._vectors_db_path = None
                self._failed_cache_path = None
        # FTS index: derived/rebuildable; any failure degrades to hybrid-off.
        if self.config.use_hybrid and self._fts_cache_path is not None:
            try:
                self._fts = FtsIndex(self._fts_cache_path)
                if not self._fts.available:
                    self._fts = None
                else:
                    # Warm-cache upgrade path: chunks were loaded from the .bin
                    # cache, so no file counts as "changed" and sync() alone would
                    # never populate FTS. Rebuild from in-memory chunks whenever
                    # the row count differs from the chunk count (also covers a
                    # crash mid-build). Idempotent: upsert is delete-by-source.
                    self._fts_ensure_populated()
            except Exception:
                self._fts = None
        # Vector backend seam: default memory (numpy brute-force over chunk.embedding);
        # sqlite_vec when configured AND importable, else falls back to memory.
        self._vector_backend = create_vector_backend(self.config.vector, self, self._vectors_db_path)
        # Disk-backed mode (sqlite_vec): embeddings are NOT retained on Chunk —
        # that is the actual memory win. RAM bookkeeping set tracks which chunk
        # ids are already persisted so sync never re-embeds them.
        self._vectors_on_disk = bool(getattr(self._vector_backend, "on_disk", False))
        self._disk_vectors: set[str] = set()
        if not self._vectors_on_disk and self.config.vector.backend == "sqlite_vec" and load_vectors:
            # Configured sqlite_vec but import/load failed -> fell back to memory;
            # the vectors cache was skipped during init, so load it now.
            try:
                self._load_vectors_cache()
            except Exception:
                pass

    def _cache_key(self) -> str:
        """Stable cache identity.

        Priority: explicit cache.id (immune to path spelling) -> normalized vault
        path (case-insensitive on Windows, symlinks resolved). Either way the key
        is stable across agents and sessions, so a cache built by one agent is
        found by another.

        v0.9.0 C91：实现统一到 `doc_store.vault_cache_key` —— 派生缓存与文档库
        必须是同一个身份，否则同一个库会分裂出第二份 store 绑定（§12.2）。
        """
        return vault_cache_key(self.config, self.vault_path)

    def _init_cache_paths(self, *, load_vectors: bool = True) -> None:
        root = self._cache_root()
        namespace = self.config.cache.namespace or "default"
        base = root / namespace
        chunks_dir = base / "chunks"
        vectors_dir = base / "vectors"
        fts_dir = base / "fts"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        vectors_dir.mkdir(parents=True, exist_ok=True)
        fts_dir.mkdir(parents=True, exist_ok=True)
        key = self._cache_key()
        self._chunks_cache_path = chunks_dir / f"vault_{key}.chunks.bin"
        model_hash = hashlib.sha256(self.config.embedding.model.encode("utf-8")).hexdigest()[:8]
        stem = f"vault_{key}.{model_hash}.{self.config.embedding.dimension}"
        space = self._embedding_profile.fingerprint
        legacy_bin = vectors_dir / f"{stem}.vec.bin"
        self._vectors_cache_path = vectors_dir / f"{stem}.{space}.vec.bin"
        # Reuse a proven compatible pre-R2 path, never overwrite an incompatible space.
        legacy = _VectorsCodec.load(legacy_bin) if legacy_bin.exists() else None
        if legacy and legacy[0] == self._vectors_meta():
            self._vectors_cache_path = legacy_bin
        self._fts_cache_path = fts_dir / f"vault_{key}.fts.sqlite"
        # sqlite-vec backend keeps its own db (never shares the FTS file).
        # 文件名必须带上 model 与 dimension：vec0 表的维度在建表时就固化了
        # （CREATE VIRTUAL TABLE ... vec0(embedding float[N])），改维度后
        # IF NOT EXISTS 会静默保留旧表，之后所有插入都失败且被 upsert 的
        # except 吞掉 —— 向量库从此语义检索归零且不可自愈。
        self._vectors_db_path = (
            vectors_dir / f"{stem}.{space}.vec.sqlite"
        )
        legacy_db = vectors_dir / f"{stem}.vec.sqlite"
        if legacy_db.exists():
            try:
                with sqlite3.connect(legacy_db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                    row = conn.execute("SELECT value FROM vector_space_meta WHERE key='fingerprint'").fetchone()
                if row and row[0] == space:
                    self._vectors_db_path = legacy_db
            except sqlite3.Error:
                pass  # Unknown/mismatched old stores stay untouched.
        # 失败名单：与 chunks/vectors/fts 平级的纯可观测性文件，进程重启后
        # 让 kb_stats 仍能报出上一轮的失败原因。
        self._failed_cache_path = base / f"vault_{key}.failed.json"
        # 文档库布局（C91）：只解析路径与真实归属，不建目录。归属冲突/路径非法
        # 不阻断物理文本路径，只让虚拟存储保持不可写（打开 store 时报明确错误）。
        try:
            self._storage_layout = resolve_storage_layout(self.config, self.vault_path)
        except DocStoreError:
            self._storage_layout = None
        self._load_chunks_cache()
        self._load_failed_files()
        # With the disk-backed sqlite_vec backend, vectors are not loaded into
        # RAM (that's the memory win); the disk store is migrated/flushed by
        # _ensure_disk_vectors_migrated() on first sync.
        if load_vectors and self.config.vector.backend != "sqlite_vec":
            self._load_vectors_cache()
        self._sweep_stale_cache()

    def _protected_cache_subtree(self) -> Path | None:
        """缓存根下**不得**被 TTL/清理触碰的子树：文档库与 control 所在目录。

        §12.6「TTL 仅明确白名单派生文件，不递归删所有 sqlite」：文档库是解析事实
        （可能等于已付费的解析全文与媒体），按 TTL 删掉它等于丢资产。
        """
        try:
            layout = self._storage_layout or resolve_storage_layout(self.config, self.vault_path)
        except DocStoreError:
            return None
        return layout.doc_store_dir

    def _sweep_stale_cache(self) -> None:
        """Delete cache files older than cache.max_age_days (0 disables)."""
        max_age = self.config.cache.max_age_days
        if max_age <= 0 or self._cache_root() is None:
            return
        cutoff = time.time() - max_age * 86400
        root = self._cache_root()
        protected = self._protected_cache_subtree()
        for pattern in ("*.bin", "*.sqlite"):
            for cache_file in root.rglob(pattern):
                if protected is not None and is_within(cache_file, protected):
                    continue
                try:
                    if cache_file.stat().st_mtime < cutoff:
                        cache_file.unlink()
                except OSError:
                    pass

    def _cache_root(self) -> Path:
        if self.config.cache.placement == "vault":
            # Keep vectors next to the notes, inside a hidden subfolder of the vault.
            return Path(self.vault_path).expanduser() / self.config.cache.subdir
        return Path(self.config.cache.dir).expanduser()

    # --------------------------------------------------------- 文档库（C91/C92）

    def document_store(self, *, write: bool = False) -> DocumentStore:
        """本库的版本化文档库（解析事实）——C92 接缝，连接惰性建立。

        只负责解析布局与打开连接；摄取、对账与发布由 Lane B 起的 worker 调用。
        读路径不得因为一次查询就建库：`write=False` 走只读语义，库不存在时由
        store 报出明确错误，这里不 mkdir。归属冲突（home 根落库内等）或
        `cache.enabled=false` 时由 store 抛 `VirtualStorageDisabled`。
        """
        with self._doc_store_lock:
            store = self._doc_store
            if store is None:
                layout = self._storage_layout or resolve_storage_layout(self.config, self.vault_path)
                self._storage_layout = layout
                store = DocumentStore(layout, self.config)
                self._doc_store = store
                store.open(write=write)
                self._doc_store_write_opened = write
            elif write and not self._doc_store_write_opened:
                # 先被只读路径打开过：补齐写门禁与 generation 解析（open 幂等）。
                store.open(write=True)
                self._doc_store_write_opened = True
            return store

    def _existing_document_store(self) -> DocumentStore | None:
        with self._doc_store_lock:
            if self._doc_store is not None:
                return self._doc_store
            layout = self._storage_layout or resolve_storage_layout(self.config, self.vault_path)
            if not layout.control_path.exists():
                return None
        return self.document_store()

    def close_document_store(self) -> None:
        """释放本库文档库连接（幂等）。退出/探测路径必须调用，别留句柄。"""
        with self._doc_store_lock:
            store = self._doc_store
            self._doc_store = None
            self._doc_store_write_opened = False
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        with self._paid_lock:
            control = self._paid_control
            self._paid_control = None
        if control is not None:
            try:
                control.close()
            except Exception:
                pass

    # -------------------------------------------------- 付费请求闸门（C100）

    def _paid_control_store(self) -> ControlStore | None:
        """惰性打开本机控制库；`[cache] enabled=false` 或控制库不可读时返回 None。"""
        if not getattr(self.config.cache, "enabled", False):
            return None
        with self._paid_lock:
            if self._paid_control is not None:
                return self._paid_control
            try:
                layout = self._storage_layout or resolve_storage_layout(self.config, self.vault_path)
                self._storage_layout = layout
                control = open_paid_control(layout)
            except Exception:
                return None
            self._paid_control = control
            return control

    def _reranker_profile_fingerprint(self) -> str:
        config = self.config.reranker
        material = "|".join(("rerank", str(getattr(config, "adapter", "openai")),
                             str(getattr(config, "model", "")),
                             normalize_endpoint(str(getattr(config, "endpoint", "") or ""))))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _paid_guard_for(self, fingerprint: str, kind: str):
        def guard(profile_fingerprint: str) -> bool:
            if profile_fingerprint != fingerprint:
                # 装配身份不一致仍拒绝，不能把旧空间用于新配置。
                return False
            control = self._paid_control_store()
            if control is None:
                return False
            return paid_request_guard(control, profile_fingerprint)
        return guard

    def has_unresolved_paid_intents(self, control: ControlStore, kind: str,
                                    profile_fingerprint: str = "") -> bool:
        """是否还有结果未知（`prepared` / `submission_unknown`）的付费意图。

        这是**整个 profile 级**的暂停判据，不是按载荷拦：外部嵌入按 `batch_size`
        切片，某批结果未知时同一文件的前导批已发过并计费，而调用方「全有或全无」——
        只拦失败批会让每轮 sync 都重发前导批却永远完不成（实测每次重计费 1 批）。
        读不到控制库时返回 True（不能证明安全 → fail closed）。
        """
        try:
            items = control.list_request_intents()
        except Exception:
            return True
        return any(item["kind"] == kind and item["state"] in UNRESOLVED_INTENT_STATES
                   and (not profile_fingerprint
                        or item["profile_fingerprint"] == str(profile_fingerprint))
                   for item in items)

    def unresolved_paid_intents(self) -> list[dict[str, Any]]:
        """本库当前所有未决付费意图（供 `kb_stats` 观测，不触发任何网络动作）。"""
        control = self._paid_control_store()
        if control is None:
            return []
        try:
            items = control.list_request_intents()
        except Exception:
            return []
        return [item for item in items if item["state"] in UNRESOLVED_INTENT_STATES]

    def configure_paid_provider(self, kind: str, provider: Any, profile_fingerprint: str) -> bool:
        """把持久 journal + 闸门装配到任何 `configure_paid_requests` provider 上。

        媒体/转录等新 provider 必须经此装配（§20.7B 覆盖 media/transcription）：
        返回 False 表示该 provider 不支持付费装配，调用方必须自行拒绝外部请求。
        """
        configure = getattr(provider, "configure_paid_requests", None)
        if not callable(configure):
            return False
        configure(PaidRequestJournal(self._paid_control_store, kind),
                  self._paid_guard_for(profile_fingerprint, kind), profile_fingerprint)
        return True

    def _configure_paid_providers(self) -> None:
        self.configure_paid_provider("embed", self.embedding_provider,
                                     self._embedding_profile.fingerprint)
        self.configure_paid_provider("rerank", self.reranker_provider,
                                     self._reranker_profile_fingerprint())
        # E07：原生媒体 provider 走同一持久 journal/闸门（§20.7B 覆盖 media）。
        if self.media_provider is not None:
            self.configure_paid_provider("media", self.media_provider,
                                         self.media_provider.profile.fingerprint)

    def may_use_paid_profile(self, profile_fingerprint: str, *, kind: str = "embed") -> bool:
        """统一闸门（§20.7B）：embed_missing / query / fanout / rerank / 媒体 / 转录
        在发起任何外部请求前都必须先问它；返回 False 时外部请求数必须为 0。"""
        return bool(self._paid_guard_for(profile_fingerprint, kind)(profile_fingerprint))

    def reembedding_approval_summary(self) -> dict[str, Any] | None:
        """旧 public 入口保留；R2 不再派生新的费用审批需求。"""
        return None

    # ------------------------------------------------------------------ cache

    def _chunks_meta(self) -> dict[str, Any]:
        profile = self._chunking_config
        return {
            "key": self._cache_key(),
            "chunk_size": profile.legacy_chunk_size,
            "chunk_overlap": profile.legacy_chunk_overlap,
            # 图片注入会改写 chunk.content（继而改写 chunk.id），必须参与失效
            # 判据。此前漏了它：缓存失效只看文件字节 sha256，翻转这个开关后
            # 文件字节没变 → 存量库既不重切块也不重嵌，CHANGELOG 承诺的
            # 「开启会全量重嵌」实际完全没生效。
            "inject_image_captions": bool(self.config.inject_image_captions),
            "table_guard": True,
            "chunker": 7,
            "chunking_mode": profile.mode,
            "target_tokens": profile.target_tokens,
            "overlap_tokens": profile.overlap_tokens,
            "hard_limit_tokens": profile.hard_limit_tokens,
            "estimator_profile": profile.estimator_profile,
            "structure_guard_version": ("structure-estimated-v2" if profile.mode == "estimated_tokens"
                                        else "structure-v1"),
            "proxy_version": "media-proxy-v1",
        }

    def _vectors_meta(self) -> dict[str, Any]:
        return {
            "key": self._cache_key(),
            "embedding_mode": self.config.embedding.mode,
            "embedding_model": self.config.embedding.model,
            "dimension": self.config.embedding.dimension,
            # endpoint / send_dimensions 必须参与失效判据：把 endpoint 从 A 厂
            # 换到 B 厂（自建同名 bge-m3）会静默复用 A 厂算出的向量；翻转
            # send_dimensions（MRL）会让新旧两种维度混在同一个缓存文件里，
            # numpy 侧因 ragged 输入抛错后回退到 _cosine，而 _cosine 用
            # min(len) 截断再算余弦 —— 出来的是毫无意义的相似度。
            "endpoint": self.config.embedding.endpoint,
            "send_dimensions": bool(self.config.embedding.send_dimensions),
            "space_fingerprint": self._embedding_profile.fingerprint,
        }

    def _load_chunks_cache(self) -> None:
        """Load the text-layer cache: file signatures + chunks without vectors.

        The chunks layer depends only on vault identity and chunking parameters,
        so switching embedding models/dimensions never invalidates it.
        """
        if self._chunks_cache_path is None:
            return
        loaded = _CacheCodec.load(self._chunks_cache_path)
        if not loaded:
            return
        meta, files = loaded
        if (not self._chunking_config.mode_explicit and not self._chunking_config.legacy_explicit
                and self.config.chunking.mode != "legacy_chars"):
            mode = meta.get("chunking_mode", "legacy_chars")
            if mode in {"legacy_chars", "estimated_tokens"}:
                values = {"mode": mode}
                for name in ("target_tokens", "overlap_tokens", "hard_limit_tokens", "estimator_profile"):
                    if name in meta:
                        values[name] = meta[name]
                for cached, effective in (("chunk_size", "legacy_chunk_size"),
                                          ("chunk_overlap", "legacy_chunk_overlap")):
                    value = meta.get(cached)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= (1 if cached == "chunk_size" else 0):
                        values[effective] = value
                self._chunking_config = replace(self._chunking_config, **values)
                self._chunking_compatibility_notice = "Existing library chunking profile retained; explicit mode required to migrate."
        if meta != self._chunks_meta():
            return
        self._chunks_cache_loaded = True
        self._chunks = {source: chunks for source, (_, chunks) in files.items()}
        self._signatures = {source: signature for source, (signature, _) in files.items()}

    def _load_vectors_cache(self) -> None:
        """Load the vector layer and attach embeddings to matching chunks.

        Vectors are keyed by chunk id (sha1 of source+index+content), so unchanged
        text reuses its vectors even when unrelated files changed. Only a mismatch
        of model/dimension invalidates this layer.
        """
        if self._vectors_cache_path is None:
            return
        loaded = _VectorsCodec.load(self._vectors_cache_path)
        if not loaded:
            return
        meta, vectors = loaded
        if meta != self._vectors_meta():
            return
        if not self._chunks:
            # 文本层缓存被判失效时（chunker 版本提升），初始化到这一步
            # self._chunks 还是空的，向量无处可挂。直接丢掉的话，sync 重建
            # 出来的每个 chunk 都"缺向量"，整个库会被重新 embedding 一遍——
            # 而这正是 bump chunker 想避免的代价。先存着，等 sync 重建文本后
            # 由 _attach_pending_vectors 按 id 补挂。
            self._pending_vectors.update(vectors)
            return
        for chunks in self._chunks.values():
            for chunk in chunks:
                vector = vectors.get(chunk.id)
                if vector is not None:
                    chunk.embedding = vector

    def _attach_pending_vectors(self) -> None:
        """把初始化时无处可挂的向量按 chunk.id 补挂到重建出来的 chunk 上。

        文本以稳定 chunk.id 复用；native ID 还含媒体 profile。
        新一轮已经生成的向量优先，旧缓存不得覆盖它。
        """
        if not self._pending_vectors:
            return
        pending = self._pending_vectors
        self._pending_vectors = {}
        for chunks in self._chunks.values():
            for chunk in chunks:
                vector = pending.get(chunk.id)
                if vector is not None and chunk.embedding is None:
                    chunk.embedding = vector

    def _load_failed_files(self) -> None:
        """Restore the previous run's failure map (source -> error message).

        纯可观测性：失败文件在下次 sync 时本来就会重试（判据是缺向量而不是这
        份名单），所以文件不存在/损坏/字段缺失一律静默忽略，绝不抛异常。
        """
        if self._failed_cache_path is None:
            return
        try:
            payload = json.loads(self._failed_cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            return
        for source, error in files.items():
            if isinstance(source, str):
                self.failed_files[source] = str(error)

    def _save_failed_files(self) -> None:
        """Persist the failure map so kb_stats stays informative across restarts.

        名单为空时直接删文件，不留空壳。缓存 IO 是尽力而为：这里失败绝不能
        拖垮 sync，所以所有异常都吞掉。
        """
        if self._failed_cache_path is None:
            return
        try:
            if not self.failed_files:
                if self._failed_cache_path.exists():
                    self._failed_cache_path.unlink()
                return
            payload = json.dumps({"version": 1, "files": dict(self.failed_files)}, ensure_ascii=False)
            tmp = self._failed_cache_path.with_suffix(self._failed_cache_path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._failed_cache_path)
        except OSError:
            pass

    def _save_cache(self) -> None:
        """Persist both layers under a single lock; failures degrade gracefully."""
        with self._cache_lock:
            self._save_chunks_cache()
            self._save_vectors_cache()
            self._save_failed_files()

    def _save_chunks_cache(self) -> None:
        if self._chunks_cache_path is None:
            return
        payload: dict[str, tuple[str, list[Chunk]]] = {}
        for source, chunks in self._chunks.items():
            if source not in self._signatures:
                continue
            # The chunks layer must be vector-free: embeddings belong exclusively
            # to the vectors layer, otherwise a model/dimension change would not
            # invalidate vectors while reusing text.
            payload[source] = (self._signatures[source], [self._strip_embedding(chunk) for chunk in chunks])
        try:
            if payload:
                _CacheCodec.dump(self._chunks_cache_path, self._chunks_meta(), payload)
            elif self._chunks_cache_path.exists():
                # 空也必须写：早退等于"删除无法持久化"。把库里的笔记全删光
                # （或全部豁免）后，旧 chunks.bin 原封不动留在磁盘上，
                # 下次启动会把已删除的笔记重新载回索引。
                self._chunks_cache_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _strip_embedding(chunk: Chunk) -> Chunk:
        return Chunk(chunk.id, chunk.content, chunk.source, chunk.title, dict(chunk.metadata), embedding=None)

    def _save_vectors_cache(self) -> None:
        # Disk-backed mode: vectors live in vec.sqlite, never re-written to .bin
        # (the stale .bin stays as the one-time migration source).
        if getattr(self, "_vectors_on_disk", False):
            return
        if self._vectors_cache_path is None:
            return
        vectors: dict[str, array] = {}
        for chunks in self._chunks.values():
            for chunk in chunks:
                if chunk.embedding is not None and len(chunk.embedding):
                    vectors[chunk.id] = chunk.embedding
        try:
            if vectors:
                _VectorsCodec.dump(self._vectors_cache_path, self._vectors_meta(), vectors)
            elif self._vectors_cache_path.exists():
                # 同上：全库删空后旧 .bin 必须一起清掉，否则重启会把陈旧
                # 向量重新挂回来（向量 meta 还可能与新配置不符）。
                self._vectors_cache_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ sync

    def sync(self) -> list[Chunk]:
        with self._sync_lock:
            return self._sync_locked()

    def try_sync_with_guard(self, timeout: float = 1.5) -> bool:
        """带超时的同步尝试，兼顾冷启动防假死真守护 (F-03/04)。

        原则：
        1. 锁被后台持有（正在构建）时，在 timeout 内无法获取锁，直接返回 False；
        2. 锁空闲时，如果检测到尚未完成首次同步（last_sync is None 且配置了重度 embedding）：
           禁止前台主线程阻塞承担几十秒的 embedding 计算，后台启动异步构建并立即返回 False；
        3. 正常增量状态下，在持有锁时快速执行增量对账并返回 True。
        """
        if self.last_sync is None and self.config.embedding.mode == "external":
            if not self._sync_lock.locked():
                threading.Thread(target=self._run_sync_quietly, daemon=True, name="vault-init").start()
            return False

        acquired = self._sync_lock.acquire(timeout=timeout)
        if not acquired:
            return False
        try:
            if self.last_sync is None and self.config.embedding.mode == "external":
                return False
            self._sync_locked()
            return True
        finally:
            self._sync_lock.release()

    def _effective_margin_ns(self) -> int:
        """本次库实际使用的可信余量：max(下限, 2 × 探测到的刻度)。

        刻度未知（探测样本不足，见 _probe_mtime_tick_ns 的退化面）时退回下限。
        余量取刻度的 2 倍：归纳推导要求 `seen - mtime > MARGIN > tick`，2 倍是
        给「时间戳因取整而滞后至多一个刻度」留一处余裕。
        """
        tick = getattr(self, "_mtime_tick_ns", None)
        if not tick or tick <= 0:
            return _MTIME_TRUST_MARGIN_NS
        return max(_MTIME_TRUST_MARGIN_NS, 2 * tick)

    def _finalize_mtime_tick_probe(self, samples: Iterable[int]) -> None:
        """本轮扫描结束后收敛刻度探测结果（每进程只需成功一次）。

        只在样本足够时才置 _mtime_tick_probed，否则下一轮继续尝试——库内文件
        从 1 个长到多个时仍能补探。

        时机安全性：本方法在**扫描循环之后**调用，而快速路径只可能在
        _stat_cache 非空时命中（_stat_cache 是进程内存量、不持久化，每进程首次
        sync 必然为空），所以首次 sync 不会在任何文件上使用未探测的余量。
        """
        if self._mtime_tick_probed:
            return
        tick = _probe_mtime_tick_ns(list(samples))
        if tick is None:
            return
        self._mtime_tick_probed = True
        self._mtime_tick_ns = tick
        if tick > _MTIME_TICK_COARSE_NS:
            # 刻度粗于安全下限：固定余量的归纳前提不成立，快速路径整体禁用以求
            # fail-closed——宁可每轮读盘，也不静默返回过期内容。
            self._fast_path_disabled_reason = (
                f"文件系统时间戳刻度约 {tick / 1e6:.0f}ms，粗于安全下限 "
                f"{_MTIME_TICK_COARSE_NS / 1e6:.0f}ms（同刻度内的等长替换会让 "
                f"签名逐位不变），已按 fail-closed 禁用 Fast-Stat 快速路径"
            )

    def _record_confirmation(self, source: str, prev_seen_ns: int | None, verified_at_ns: int) -> None:
        """累计「跨墙钟的内容级确认次数」，供 mtime 停在未来的条目判稳。"""
        count = self._stat_confirmations.get(source, 0) or 1
        if prev_seen_ns is None or verified_at_ns - prev_seen_ns >= _FUTURE_MTIME_MIN_OBSERVATION_GAP_NS:
            count += 1
        self._stat_confirmations[source] = count

    def _note_fast_path_warning(self, source: str, message: str) -> None:
        try:
            self.fast_path_warnings.setdefault(source, message)
        except AttributeError:
            self.fast_path_warnings = {source: message}

    def _fast_path_is_trustworthy(self, source: str, mtime_ns: int) -> bool:
        """签名相等是否足以断定“文件未修改”。

        这正是 Git 的 *racily clean* 问题（git-scm.com/docs/racy-git）：若内容
        变化发生在时间戳粒度的一个刻度之内，签名会双双不变，签名相等就成了假
        证据。等长替换（"old content" -> "new content"，均 18 字节）配上粗粒度
        时间戳就会命中，旧 chunk 静默残留并继续被召回。

        ## 判据

        仅当文件 mtime 严格早于「该签名被内容级验证的时刻」减去安全余量时，才
        信任签名相等：

            mtime_ns < seen_ns - margin

        其中 seen_ns 是上一轮 read + sha256 完成后取的进程时钟读数（与所哈希的
        内容严格对应）；margin 由 _effective_margin_ns() 从**实际文件系统刻度**
        推导，即 max(50ms, 2 × tick)。

        ## 归纳与其真实前提

        设条目登记时已满足 seen_ns - mtime > margin。此后任何一次真实写入都发生
        在 seen_ns 之后，其落盘时间戳 T2 满足 T2 >= 写入时刻 - tick（时间戳只会
        因取整而滞后至多一个刻度）。于是

            T2 >= seen_ns - tick > mtime + margin - tick >= mtime + tick > mtime

        即「登记之后的写入必然推动 mtime 前进」，签名必然失配、走正常 sha256
        路径检出。**但请注意该结论的前提是 margin > tick**——这正是本判据此前
        的失效点：旧实现把余量写死成 50ms，而同文件注释自己写着 FAT32 刻度可达
        2s，前提不成立时（刻度 >= margin）登记后落在同一刻度内的等长替换会让
        mtime 与 size 逐位不变、判据同时为真，**静默漏检**（实测复现）。因此
        余量现在从库内真实时间戳反推（_probe_mtime_tick_ns），并在探测到刻度粗
        于 _MTIME_TICK_COARSE_NS 时**整体禁用快速路径**（fail-closed）——宁可
        每轮读盘也不漏检。禁用的代价是丢掉零读盘优化，正确性优先。

        登记时刻与 mtime 靠得太近（< margin，含 Windows 实测约 3% 的「时间戳
        超前于进程时钟」取整碰撞，见 CI 诊断 racycount=1/30）的条目一律不信任，
        下一轮强制读盘复核；复核会把 seen_ns 推得更晚，随着真实时间流逝终能满
        足余量、固化为准可信条目。

        代价：刚被写过的文件在其 mtime 变旧到 margin 之前，每轮 sync 都会多付
        一次读盘复核。方向是安全的（宁可多读不可漏检），且一旦超过余量即恢复
        零读盘。

        ## 签名已升级为 (mtime_ns, size, st_ctime_ns)

        上一版签名只有 (mtime_ns, size)，于是「外部显式回拨 mtime」这条路径完
        全落在判据之外：只要回拨到与登记值逐位相同，签名相等与判据为真可以同时
        成立，等长替换被漏检。**实测在完全自然的稳态下即可复现**（条目
        seen-mtime = 152.5ms，即「上次写完之后隔了 150ms 又 sync 过」这种日常
        状态），因为条目端条件 `seen - mtime > margin` 对任何 mtime 已变旧超过
        margin 的文件几乎恒真——所以「窗口很窄」的说法只对攻击者需要命中原
        mtime 这一点成立，对条目端并不成立（旧的 docstring 在此处写过「窗口很
        窄…已实测」，该表述已被实测证伪，见 docs/Changelog_developer.md C29）。

        现在签名含 st_ctime_ns：POSIX 下 ctime 是 inode 元数据变更时间，由内核
        维护，os.utime / rsync --times / tar / 快照还原**无法**把它改回去，于是
        回拨 mtime 必然改变 ctime → 签名失配 → 正常 sha256 路径检出。

        残余风险（如实申报）：**Windows 上 st_ctime 是创建时间，没有鉴别力**，
        上述回拨路径在 Windows 侧仍然存在；Windows 侧只有 (mtime_ns, size) 两个
        有效通道，与 Git 对「mtime 回拨到与缓存完全相同」的盲区同类。粗刻度带
        来的同刻度漏检已由上面的刻度探测堵死，回拨漏检在 POSIX 侧已堵死，剩下
        的就是 Windows + 显式回拨这一个组合面，需要更强的通道（如持久化内容摘
        要或 USN 日志）才能进一步收敛，本轮不做。另一个方向安全的副作用：POSIX
        下任何元数据变更（chmod / rename 等）都会使 ctime 变化、令条目多付一次
        读盘复核——只多读盘，不影响正确性。

        ## mtime 停在未来：不永久惩罚

        外部时基偏移（网络盘/共享盘时钟超前、备份还原、手工 touch）会让 mtime
        长期大于 seen_ns，上面的余量归纳永远不成立。旧实现因此让该文件**永久**
        失去零读盘快速路径（实测连续三轮都读盘）。现在把这种情况识别为独立状态：
        若该签名已在**跨墙钟**的多次内容级复核（间隔 >=
        _FUTURE_MTIME_MIN_OBSERVATION_GAP_NS）中始终未变，且次数超过
        _FUTURE_MTIME_RECHECK_LIMIT，则接受它稳定、恢复零读盘，并在
        fast_path_warnings 里留一条告警。这是「复核次数上限 + 告警」，不是无声
        豁免：上限内仍然每轮精确校验。

        ## 与 Git 的差异

        Git 用「索引文件自身的 mtime」当锚点（同为文件系统时间戳，天然同时基），
        本实现用进程时钟 + 余量，因为 cache.enabled=False（AppConfig 默认值）时
        不落任何盘上锚点，而正确性修复必须覆盖默认配置。
        """
        if getattr(self, "_fast_path_disabled_reason", None):
            # 探测到粗刻度：不对任何文件使用快速路径（fail-closed）。
            return False
        seen_ns = self._stat_seen_ns.get(source)
        if seen_ns is None:
            return False
        if mtime_ns < seen_ns - self._effective_margin_ns():
            return True
        if mtime_ns > seen_ns:
            confirmations = getattr(self, "_stat_confirmations", {}).get(source, 0)
            if confirmations > _FUTURE_MTIME_RECHECK_LIMIT:
                self._note_fast_path_warning(
                    source,
                    f"mtime 长期晚于本机墙钟（seen-mtime={-(mtime_ns - seen_ns) / 1e6:.0f}ms），"
                    f"经 {confirmations} 次跨墙钟复核签名未变，已按稳定条目接受",
                )
                return True
        return False

    def _sync_locked(self) -> list[Chunk]:
        self._sync_state = "scanning"
        self._sync_progress = {
            "phase": "scanning",
            "files_done": 0,
            "files_total": 0,
            "chunks_done": 0,
            "chunks_total": 0,
        }
        try:
            return self._sync_locked_impl()
        finally:
            self._sync_state = "idle"
            self._sync_progress["phase"] = "idle"

    def _sync_locked_impl(self) -> list[Chunk]:
        return _sync_engine.run_sync(self)

    def _ensure_disk_vectors_migrated(self) -> None:
        _sync_engine.ensure_disk_vectors_migrated(self)

    def _flush_vectors_to_disk(self) -> None:
        _sync_engine.flush_vectors_to_disk(self)
    def _fts_upsert(self, source: str, chunks: list[Chunk]) -> None:
        if self._fts is None:
            return
        try:
            self._fts.upsert_source(source, chunks)
        except Exception:
            # A broken FTS index must never take the sync down; degrade to off.
            try:
                self._fts.close()
            except Exception:
                pass
            self._fts = None

    def _fts_ensure_populated(self) -> None:
        """Populate FTS from in-memory chunks when the row count is stale.

        Covers the warm-cache upgrade (no changed files -> sync writes nothing)
        and crash-mid-build states. Idempotent via delete-by-source upserts.
        """
        if self._fts is None:
            return
        try:
            total = len(self.all_chunks())
            if total and self._fts.count() != total:
                for source, chunks in self._chunks.items():
                    self._fts_upsert(source, chunks)
        except Exception:
            self._fts = None

    def _fts_delete(self, source: str) -> None:
        if self._fts is not None:
            self._fts.delete_source(source)

    def _chunk_has_vector(self, chunk: Chunk) -> bool:
        return _sync_engine.chunk_has_vector(self, chunk)

    def _stamp_embedding_keys(self) -> None:
        return _sync_engine._stamp_embedding_keys(self)

    def _reuse_vectors_by_content_hash(self) -> int:
        return _sync_engine.reuse_vectors_by_content_hash(self)

    def _embed_missing(self) -> bool:
        return _sync_engine.embed_missing(self)

    def _embedding_changed_state(
        self,
        pending_chunks: list[Chunk],
        failed_before: dict[str, str],
        reused: int = 0,
    ) -> bool:
        return _sync_engine.embedding_changed_state(self, pending_chunks, failed_before, reused)

    @staticmethod
    def _embed_one_file(source: str, chunks: list[Chunk], provider: EmbeddingProvider) -> None:
        _sync_engine.embed_one_file(source, chunks, provider)
    def _load_ignore_patterns(self) -> list[str]:
        patterns = list(self.config.exclude_patterns)
        if self.config.cache.placement == "vault" and self.config.cache.subdir:
            patterns.append(self.config.cache.subdir + "/")

        ignore_file_path = self.vault_path / self.config.ignore_file
        if ignore_file_path.exists() and ignore_file_path.is_file():
            try:
                for line in ignore_file_path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        patterns.append(stripped)
            except Exception:
                pass
        return patterns

    def _ignore_matcher(self) -> IgnoreMatcher:
        return IgnoreMatcher(self._load_ignore_patterns())

    def _markdown_files(self) -> Iterable[Path]:
        return _scanning.scandir_indexable_files(self.vault_path, self._ignore_matcher(), _INDEXABLE_TEXT_EXTS)

    @staticmethod
    def _ignored_name(name: str) -> bool:
        return _scanning.ignored_name(name)

    def _source(self, path: Path) -> str:
        return _scanning.source_rel(self.vault_path, path)
    def _chunk_file(self, source: str, text: str, mtime: float | None = None,
                    virtual_identity: dict[str, str] | None = None) -> list[Chunk]:
        # 必须传**捕获的**切块配置（`_chunking_config`，可能来自旧库 meta 保留），
        # 而不是现场读 self.config.chunking：否则旧库的 mode/参数在一次
        # `[chunking] mode` 未被显式声明时被本机默认值覆盖，等于静默换算法。
        return _chunking.chunk_file(
            source,
            text,
            self.config,
            mtime=mtime,
            inject_image_notes_fn=_inject_image_notes,
            chunking_config=self._chunking_config,
            virtual_identity=virtual_identity,
        )

    @staticmethod
    def _frontmatter(lines: list[str]) -> tuple[int, list[str], dict[str, Any]]:
        return _chunking.frontmatter(lines)

    def _is_frontmatter_exempt(self, tags: list[str], properties: dict[str, Any]) -> tuple[bool, str | None]:
        return _chunking.is_frontmatter_exempt(
            tags, properties, self.config.exclude_frontmatter_keys, self.config.exclude_tags
        )

    @staticmethod
    def _strip_ignored_blocks(body: list[str]) -> tuple[list[str], bool]:
        return _chunking.strip_ignored_blocks(body)

    @staticmethod
    def _clean_heading(heading: str) -> str:
        return _chunking.clean_heading(heading)

    def _title(self, source: str, body: list[str]) -> str:
        return _chunking.extract_title(source, body)

    def _make_chunks(
        self,
        source: str,
        title: str,
        tags: list[str],
        sections: list[tuple[str, int, list[str]]],
        mtime: float | None = None,
        source_pdf: str | None = None,
    ) -> list[Chunk]:
        return _chunking.make_chunks(
            source,
            title,
            tags,
            sections,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            mtime=mtime,
            source_pdf=source_pdf,
        )

    @staticmethod
    def _overlap_tail(lines: list[str], overlap: int) -> tuple[list[str], int]:
        return _chunking.overlap_tail(lines, overlap)

    @staticmethod
    def _new_chunk(
        source: str,
        title: str,
        heading: str,
        start: int,
        end: int,
        index: int,
        tags: list[str],
        lines: list[str],
        mtime: float | None = None,
        source_pdf: str | None = None,
    ) -> Chunk:
        return _chunking.new_chunk(
            source,
            title,
            heading,
            start,
            end,
            index,
            tags,
            lines,
            mtime=mtime,
            source_pdf=source_pdf,
        )

    def _derived_profile_key(self) -> str:
        # §20.7A：派生映射必须按「捕获的 space + chunker profile」分代，不能只按
        # model/dimension —— 同一模型换切块代际（384/64/hard768 vs 旧字符参数）
        # 会产出完全不同的 chunk 邻接，混在一个 derived generation 里就是错的映射。
        return (f"{self._cache_key()}:{self._embedding_profile.fingerprint}:"
                f"{self._chunker_fingerprint()}")

    def _chunker_fingerprint(self) -> str:
        """实际驱动切块的 profile 指纹（含 legacy 字符参数），不是 cache 代际标记 `chunker: 7`。"""
        from ._indexer.token_chunking import chunker_fingerprint
        return chunker_fingerprint(self._chunking_config)

    def resolve_virtual_source(self, source: str, *, revision_id: str = "") -> dict[str, Any]:
        """发布前核验库归属/当前 revision/物理源 SHA；返回地址事实（含 markdown）。"""
        return _reading.resolve_virtual_source(self, source, revision_id=revision_id)

    def _ingest_mirror_prefix(self) -> str:
        """旧镜像目录前缀（库内相对 posix，带尾斜杠），用于**逐条来源精确排除**。

        只排除「已由文档库承载同一 source」的具体镜像路径；绝不整目录忽略
        `.mortis-parsed/`——未迁移镜像与用户自建内容必须继续可见（§20.1）。
        """
        name = str(getattr(self.config.ingest, "output_dirname", ".mortis-parsed") or "").strip("/\\ ")
        if not name or any(part in ("", ".", "..") for part in name.split("/")):
            name = ".mortis-parsed"
        return name + "/"

    def _vector_route_allowed(self) -> bool:
        try:
            store = self._existing_document_store()
            generation = store.get_derived_generation(self._derived_profile_key()) if store else None
        except Exception:
            return False
        return generation is None or generation.status not in {"stale", "failed"}

    def _filter_visible_chunks(self, chunks: list[Chunk]) -> list[Chunk]:
        try:
            store = self._existing_document_store()
            documents = {doc.source: doc for doc in store.list_documents()} if store else {}
        except Exception:
            documents = {}
            chunks = [chunk for chunk in chunks if not chunk.metadata.get("revision_id")]
        matcher = self._ignore_matcher()
        return [chunk for chunk in chunks
                if not matcher.is_ignored(chunk.source)[0]
                and (chunk.source not in documents or documents[chunk.source].visibility == "active")
                and (not chunk.metadata.get("revision_id") or chunk.source in documents
                     and documents[chunk.source].active_revision == chunk.metadata["revision_id"])]

    def all_chunks(self) -> list[Chunk]:
        """全部 chunk 的快照。

        刻意不加锁：sync() 会持 _sync_lock 跑完整个索引 + embedding（大库是
        分钟级），读路径若等这把锁，所有搜索都会被一次全量重建阻塞。这里改用
        乐观快照——用 .get() 容忍并发 pop（KeyError），遇到 "dictionary changed
        size during iteration" 退避重试。PR 新增的 30s 无条件兜底 sync 把并发
        窗口从"仅文件变动时"扩大到"每 30s 必有"，此前这两种异常会直接变成
        MCP -32000。
        """
        for attempt in range(4):
            try:
                return self._filter_visible_chunks([
                    chunk
                    for source in sorted(self._chunks)
                    for chunk in self._chunks.get(source, [])
                ])
            except RuntimeError:
                if attempt == 3:
                    raise
                time.sleep(0.01 * (attempt + 1))
        return []

    def search(
        self,
        query: str,
        top_k: int = 10,
        use_rerank: bool = False,
        query_vector: Iterable[float] | None = None,
        filters: SearchFilter | None = None,
        dedupe: bool = True,
        *,
        exact_terms: list[str] | None = None,
        skip_semantic: bool = False,
    ) -> list[Chunk]:
        return _search.search_single_vault(
            self,
            query=query,
            top_k=top_k,
            use_rerank=use_rerank,
            query_vector=query_vector,
            filters=filters,
            dedupe=dedupe,
            exact_terms=exact_terms,
            skip_semantic=skip_semantic,
        )

    @staticmethod
    def _fts_query(self_or_query: Any, query: str | None = None) -> str | None:
        if query is None and isinstance(self_or_query, str):
            actual = self_or_query
        elif query is not None:
            actual = query
        else:
            actual = str(self_or_query or "")
        return _fts_query_fn(actual)

    def _hybrid_rank(
        self,
        query: str,
        all_chunks: list[Chunk],
        lexical: dict[str, float],
        semantic_snapshot: dict[str, float],
        path_prefix: str = "",
    ) -> list[Chunk]:
        return _hybrid_rank_fn(
            query,
            all_chunks,
            lexical,
            semantic_snapshot,
            self._fts,
            self.config.rrf_per_route,
            path_prefix=path_prefix,
        )

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        return _query_tokens_fn(query)

    def _semantic_rank(self, query_vector: Iterable[float], chunks: list[Chunk]) -> list[Chunk]:
        return _semantic_rank_fn(query_vector, chunks)

    @staticmethod
    def _cosine(left: array, right: array) -> float:
        return _cosine_fn(left, right)

    def read(self, source: str, start_line: int | None = None, end_line: int | None = None) -> str:
        res = self._read_result(source, start_line=start_line, end_line=end_line, max_chars=None)
        return res.content

    def _read_result(
        self,
        source: str,
        start_line: int | None = None,
        end_line: int | None = None,
        *,
        heading: str | None = None,
        start_char: int = 0,
        max_chars: int | None = None,
        expected_sha256: str | None = None,
        chunk_id_hint: str | None = None,
        allow_stale: bool = False,
        expected_revision_id: str | None = None,
        expected_render_sha256: str | None = None,
        media_refs_offset: int = 0,
    ) -> _reading.ReadResult:
        if type(allow_stale) is not bool:
            raise ValueError("allow_stale must be a boolean")
        if type(media_refs_offset) is not int or media_refs_offset < 0:
            raise ValueError("media_refs_offset must be an integer >= 0")
        virtual = bool(expected_revision_id) or Path(source).suffix.lower() not in self._READABLE_SUFFIXES
        if not virtual:
            store = self._existing_document_store()
            active = store.get_active(source, include_hidden=True) if store is not None else None
            if store is not None and active is None:
                key = _scanning.source_compare_key(source)
                matches = [doc.source for doc in store.list_documents() if doc.active_revision
                           and _scanning.source_compare_key(doc.source) == key]
                if len(matches) > 1:
                    raise ValueError("multiple virtual sources refer to the same filesystem path")
                if matches:
                    active = store.get_active(matches[0], include_hidden=True)
                    if active is not None:
                        source = matches[0]
            virtual = active is not None
        if virtual:
            return _reading.read_virtual_result(
                self, source, start_line, end_line, heading=heading,
                start_char=start_char, max_chars=max_chars, allow_stale=allow_stale,
                expected_revision_id=expected_revision_id,
                expected_sha256=expected_sha256,
                expected_render_sha256=expected_render_sha256,
                chunk_id_hint=chunk_id_hint,
            )
        path = self._safe_path(source)
        return _reading.read_file_result(
            path,
            source,
            start_line=start_line,
            end_line=end_line,
            heading=heading,
            start_char=start_char,
            max_chars=max_chars,
            expected_sha256=expected_sha256,
            chunk_id_hint=chunk_id_hint,
        )

    def list_files(self) -> list[dict[str, Any]]:
        visible = {chunk.source for chunk in self.all_chunks()}
        try:
            store = self._existing_document_store()
            hidden = {doc.source for doc in store.list_documents()
                      if doc.visibility != "active"} if store else set()
        except Exception:
            hidden = {source for source, chunks in self._chunks.items()
                      if any(chunk.metadata.get("revision_id") for chunk in chunks)}
        matcher = self._ignore_matcher()
        return [{"source": source, "title": chunks[0].title if chunks else Path(source).stem,
                 "chunks": len(chunks)} for source, chunks in sorted(self._chunks.items())
                if source in visible or not chunks and source not in hidden
                and not matcher.is_ignored(source)[0]]

    def stats(self) -> dict[str, Any]:
        exempt_count = 0
        unsupported_counts: dict[str, int] = {}
        if self.vault_path.exists():
            matcher = self._ignore_matcher()
            stack = [self.vault_path]
            while stack:
                directory = stack.pop()
                try:
                    entries = list(os.scandir(directory))
                except OSError:
                    continue
                for entry in entries:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    path = Path(entry.path)
                    if is_dir:
                        # 跳过系统级/隐藏目录及缓存目录，但允许进入普通用户目录统计豁免文件
                        if (
                            entry.name.startswith(".")
                            or self._ignored_name(entry.name)
                            or entry.name == "node_modules"
                            or (
                                self.config.cache.placement == "vault"
                                and self.config.cache.subdir
                                and entry.name == self.config.cache.subdir
                            )
                        ):
                            continue
                        stack.append(path)
                        continue
                    if self._ignored_name(entry.name):
                        continue
                    suffix = path.suffix.lower()
                    source = self._source(path)
                    if matcher.is_ignored(source, is_dir=False)[0]:
                        if suffix in _INDEXABLE_TEXT_EXTS:
                            exempt_count += 1
                        continue
                    if suffix in _INDEXABLE_TEXT_EXTS:
                        if suffix in {".md", ".markdown"}:
                            try:
                                raw = path.read_bytes()
                                lines = raw.decode("utf-8-sig", errors="ignore").splitlines()
                                _, tags, properties = self._frontmatter(lines)
                                if self._is_frontmatter_exempt(tags, properties)[0]:
                                    exempt_count += 1
                            except Exception:
                                pass
                    elif suffix not in INGEST_EXTS and not entry.name.startswith((".", "~")):
                        ext = suffix.lstrip(".")
                        if ext:
                            unsupported_counts[ext] = unsupported_counts.get(ext, 0) + 1
        # F7.2：报告可选加速依赖的存在性。使用 find_spec 仅探测元数据，
        # 绝不真实 import（避免提前加载 C 扩展、抢占 GIL 或污染轻量测试环境）。
        import importlib.util as _ilu

        def _has_spec(name: str) -> bool:
            try:
                return _ilu.find_spec(name) is not None
            except Exception:
                return False

        return {
            "files": len(self._chunks),
            "chunks": len(self.all_chunks()),
            "exempt_files": exempt_count,
            "skipped_unsupported": unsupported_counts,
            "failed_files": dict(self.failed_files),
            "last_sync": self.last_sync,
            "embedding": {"mode": self.config.embedding.mode, "model": self.config.embedding.model, "dimension": self.config.embedding.dimension},
            "reranker_enabled": self.reranker_provider is not None,
            "cache_enabled": self._chunks_cache_path is not None or self._vectors_cache_path is not None,
            "cache_key": self._cache_key(),
            "cache_namespace": self.config.cache.namespace,
            "use_hybrid": self.config.use_hybrid,
            "fts_enabled": self._fts is not None and self._fts.available,
            "vector_backend": getattr(self._vector_backend, "name", self.config.vector.backend),
            "accel": {
                "numpy": _has_spec("numpy"),
                "sqlite_vec": _has_spec("sqlite_vec"),
            },
        }

    def _iter_vault_text_files(self) -> list[str]:
        return _exemptions.iter_vault_text_files(self)

    def get_exemptions(self) -> dict[str, Any]:
        return _exemptions.get_exemptions(self)

    def _prune_ignored_sources(self, matcher: IgnoreMatcher) -> list[str]:
        return _exemptions._prune_ignored_sources(self, matcher)

    def add_exemption_pattern(self, pattern: str) -> dict[str, Any]:
        return _exemptions.add_exemption_pattern(self, pattern)

    def remove_exemption_pattern(self, pattern: str) -> dict[str, Any]:
        return _exemptions.remove_exemption_pattern(self, pattern)

    def check_exemption(self, source: str) -> dict[str, Any]:
        return _exemptions.check_exemption(self, source)

    def set_file_exemption(self, source: str, exempt: bool = True, method: str = "frontmatter") -> dict[str, Any]:
        return _exemptions.set_file_exemption(self, source, exempt=exempt, method=method)

    def purge_cache(self) -> bool:
        """Delete this vault's on-disk cache files (used by kb_remove).

        Returns True when both cache files are gone afterwards. Safe to call
        when caching is disabled (returns False, nothing to purge).

        v0.9.0（C92）：这里只删**派生**文件。文档库（doc_store/）是解析事实，
        普通清理与 kb_rebuild 一律不碰它——清除解析资产需要独立的
        `purge_documents` 显式授权并提示重新解析费用（§20.3）。
        """
        removed = True
        with self._cache_lock:
            for cache_file in (self._chunks_cache_path, self._vectors_cache_path, self._failed_cache_path):
                if cache_file is None:
                    continue
                try:
                    if cache_file.exists():
                        cache_file.unlink()
                except OSError:
                    removed = False
            self._chunks_cache_path = None
            self._vectors_cache_path = None
            self._failed_cache_path = None
            # 缓存整体丢弃，失败名单也随之作废（它只是缓存的附属观测数据）。
            self.failed_files.clear()
            self._stat_cache.clear()
            self._stat_seen_ns.clear()
            self._stat_confirmations.clear()
            self.fast_path_warnings.clear()
        # FTS index + optional sqlite-vec backend share the cache lifecycle.
        if self._fts is not None:
            try:
                self._fts.close()
            except Exception:
                pass
            self._fts = None
        if self._fts_cache_path is not None:
            try:
                if self._fts_cache_path.exists():
                    self._fts_cache_path.unlink()
            except OSError:
                removed = False
        try:
            self._vector_backend.purge()
        except Exception:
            pass
        return removed

    def rebuild(self) -> list[Chunk]:
        """Drop both cache layers and the in-memory index, then rebuild from scratch.

        v0.9.0（C92）：重建的是**派生**层。文档库（doc_store/）与账本保留，
        因此重建不会重新调用 MinerU 计费解析（§12.6「rebuild 只重建派生」）。
        """
        with self._cache_lock:
            for cache_file in (
                self._chunks_cache_path,
                self._vectors_cache_path,
                self._fts_cache_path,
                self._failed_cache_path,
            ):
                if cache_file is not None:
                    try:
                        if cache_file.exists():
                            cache_file.unlink()
                    except OSError:
                        pass
            if self._fts is not None:
                try:
                    self._fts.close()
                except Exception:
                    pass
                self._fts = None
        with self._sync_lock:
            self._chunks.clear()
            self._signatures.clear()
            self._stat_cache.clear()
            self._stat_seen_ns.clear()
            self._stat_confirmations.clear()
            self.fast_path_warnings.clear()
            self.failed_files.clear()
            self._disk_vectors.clear()
            if self._vectors_on_disk:
                try:
                    self._vector_backend.purge()
                except Exception:
                    pass
            result = self._sync_locked()
            # _sync_locked's FTS hooks are no-ops while _fts is None; recreate the
            # index instance so the rebuild actually repopulates it.
            if self.config.use_hybrid and self._fts_cache_path is not None:
                try:
                    self._fts = FtsIndex(self._fts_cache_path)
                    if not self._fts.available:
                        self._fts = None
                except Exception:
                    self._fts = None
                if self._fts is not None:
                    for source, chunks in self._chunks.items():
                        self._fts_upsert(source, chunks)
            return result

    _READABLE_SUFFIXES = {".md", ".markdown", ".txt"}

    # ------------------------------------------------------------------ snapshot

    def export_snapshot(self, out_path: str | Path) -> dict[str, Any]:
        return _snapshot.export_snapshot(self, out_path)

    def _export_snapshot_locked(self, out_path: str | Path) -> dict[str, Any]:
        return _snapshot._export_snapshot_locked(self, out_path)

    def import_snapshot(self, snapshot: str | Path, force: bool = False,
                        trust_parsed_documents: bool = False, replace: bool = False,
                        confirm_replace: bool = False) -> dict[str, Any]:
        return _snapshot.import_snapshot(
            self, snapshot, force=force, trust_parsed_documents=trust_parsed_documents,
            replace=replace, confirm_replace=confirm_replace,
        )

    def _import_snapshot_locked(
        self,
        src: Path,
        zf: zipfile.ZipFile,
        vectors_member: str | None,
        skip_vectors: bool,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return _snapshot._import_snapshot_locked(
            self, src, zf, vectors_member, skip_vectors, warnings=warnings
        )

    def _import_chunks_member(self, zf: zipfile.ZipFile) -> tuple[int, int]:
        return _snapshot._import_chunks_member(self, zf)

    def _import_vectors_bin_member(self, zf: zipfile.ZipFile) -> int:
        return _snapshot._import_vectors_bin_member(self, zf)

    def _import_vectors_sqlite_member(self, zf: zipfile.ZipFile) -> int:
        return _snapshot._import_vectors_sqlite_member(self, zf)

    def _decode_member(self, zf: zipfile.ZipFile, member: str, loader: Callable[[Path], Any]) -> Any:
        return _snapshot._decode_member(self, zf, member, loader)

    def _replace_live_file(self, target: Path, staged: Path, *, close_fts: bool) -> None:
        return _snapshot._replace_live_file(self, target, staged, close_fts=close_fts)

    def _stream_member(self, zf: zipfile.ZipFile, member: str, dest: Path | Any) -> int:
        return _snapshot._stream_member(zf, member, dest)

    def _validate_vector_sqlite(self, path: Path, dimension: int | None) -> None:
        return _snapshot._validate_vector_sqlite(path, dimension)

    def _recreate_fts(self) -> None:
        return _snapshot._recreate_fts(self)

    def _safe_path(self, source: str) -> Path:
        # kb_read 只能读 Markdown 与纯文本文件：拒绝任意扩展名，防止把私钥/配置等任意
        # 文件当文本读出（组合 vault_path 注入 = 任意文件读取）。
        if Path(source).suffix.lower() not in self._READABLE_SUFFIXES:
            raise ValueError(f"source must be a Markdown or plain-text file (.md/.markdown/.txt): {source!r}")
        candidate = (self.vault_path / source.replace("\\", "/")).resolve()
        root = self.vault_path.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("source must stay inside the vault")
        return candidate

    def start_watching(self, interval: float = 0.25, debounce_seconds: float | None = None) -> None:
        return _watch.start_watching(self, interval=interval, debounce_seconds=debounce_seconds)

    def _start_fs_scheduler(self) -> None:
        return _watch._start_fs_scheduler(self)

    def _fs_scheduler_loop(self) -> None:
        return _watch._fs_scheduler_loop(self)

    def _on_fs_events(self, events: list[tuple[int, str]] | None) -> None:
        return _watch._on_fs_events(self, events)

    def _fs_event_matters(self, rel: str) -> bool:
        return _watch._fs_event_matters(self, rel)

    def _run_sync_quietly(self) -> None:
        return _watch._run_sync_quietly(self)

    def _native_watch_loop(self, interval: float, debounce: float) -> None:
        return _watch._native_watch_loop(self, interval, debounce)

    def _watch_loop(self, interval: float, debounce: float) -> None:
        return _watch._watch_loop(self, interval, debounce)

    def _quick_signatures(self) -> dict[str, tuple[int, int]]:
        return _watch._quick_signatures(self)

    def stop_watching(self) -> None:
        return _watch.stop_watching(self)

    def request_refresh(self, *, immediate: bool = False, for_read: bool = False) -> bool:
        return _watch.request_refresh(self, immediate=immediate, for_read=for_read)

    def refresh_status(self) -> dict[str, Any]:
        return _watch.refresh_status(self)

    def index_state(self) -> dict[str, Any]:
        """additive `index_state=empty/rebuilding/unverified/ready` + `next_action`（E04-b）。"""
        return _watch.index_state(self)

    def request_ingest_scan(self) -> bool:
        return _watch.request_ingest_scan(self)

    def _ingest_scan_loop(self) -> None:
        return _watch._ingest_scan_loop(self)

