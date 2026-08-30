from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import struct
import threading
import time
import zlib
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import AppConfig
from . import fsnotify
from .fsnotify import WindowsDirectoryWatcher, watcher_available
from .fts import FtsIndex
from .providers import EmbeddingProvider, RerankerProvider, create_embedding_provider, create_reranker_provider
from .vector import create_vector_backend

_RRF_K = 60

# native 监听回调的防抖延迟上限：编辑器保存风暴（事件持续不断）会让防抖定时器
# 一直顺延，超过这个窗口就必须同步一次，不能让事件流饿死同步。
_FS_MAX_DEBOUNCE_WAIT = 5.0

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_ASCII_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_BLOCK_IGNORE_START = re.compile(r"^\s*<!--\s*(?:rag-ignore|rag:ignore|no-rag|norag)\s*-->", re.IGNORECASE)
_BLOCK_IGNORE_END = re.compile(r"^\s*<!--\s*(?:/rag-ignore|/rag:ignore|/no-rag|/norag|end-rag-ignore)\s*-->", re.IGNORECASE)

# 图片注入（inject_image_captions）：标准 Markdown 图片与 Obsidian wiki 图片嵌入。
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"([^\"]*)\")?\s*\)")
_WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
# wiki 嵌入只有在图片扩展名时才当图片处理：![[另一篇笔记]] 不是图片。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}

_CACHE_MAGIC = b"VMCPC"
_CACHE_VERSION = 1

# Embeddings are stored as float32 arrays (4 bytes/dim) instead of Python lists
# to keep memory sane: 6157 chunks x 4096 dims would otherwise cost ~800MB.
_EMB_DTYPE = "f"


class IgnoreMatcher:
    """Gitignore-style pattern matcher for vault exclusion rules."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.rules: list[tuple[bool, str, bool]] = []
        for raw in patterns:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            is_neg = False
            if line.startswith("!"):
                is_neg = True
                line = line[1:].strip()
            if not line:
                continue
            is_dir_only = line.endswith("/")
            if is_dir_only:
                line = line.rstrip("/")
            self.rules.append((is_neg, line, is_dir_only))

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> tuple[bool, str | None]:
        rel_posix = rel_path.replace("\\", "/").strip("/")
        if not rel_posix:
            return False, None

        parts = rel_posix.split("/")
        filename = parts[-1]

        matched = False
        matched_rule: str | None = None

        for is_neg, pat, is_dir_only in self.rules:
            hit = False
            norm_pat = pat.replace("\\", "/").rstrip("/")
            pat_lower = norm_pat.lower()
            rel_lower = rel_posix.lower()
            fn_lower = filename.lower()

            if "/" not in norm_pat:
                if fnmatch.fnmatchcase(fn_lower, pat_lower):
                    if not is_dir_only or is_dir:
                        hit = True
                if not hit:
                    for part in parts[:-1]:
                        if fnmatch.fnmatchcase(part.lower(), pat_lower):
                            hit = True
                            break
            else:
                clean_pat = norm_pat.lstrip("/")
                clean_pat_lower = clean_pat.lower()
                if fnmatch.fnmatchcase(rel_lower, clean_pat_lower):
                    hit = True
                elif is_dir_only and (rel_lower == clean_pat_lower or rel_lower.startswith(clean_pat_lower + "/")):
                    hit = True
                elif not is_dir_only and rel_lower.startswith(clean_pat_lower + "/"):
                    hit = True
                elif "**" in clean_pat_lower and fnmatch.fnmatchcase(rel_lower, clean_pat_lower):
                    hit = True

            if hit:
                if is_neg:
                    matched = False
                    matched_rule = None
                else:
                    matched = True
                    matched_rule = norm_pat + ("/" if is_dir_only else "")

        return matched, matched_rule


@dataclass
class Chunk:
    id: str
    content: str
    source: str
    title: str
    metadata: dict[str, Any]
    score: float = 0.0
    embedding: array | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "score": self.score,
            "source": self.source,
            "title": self.title,
            "metadata": dict(self.metadata),
        }


@dataclass
class SearchFilter:
    """kb_search 的过滤条件与分页参数（全部可选）。

    过滤统一在融合排序之后做（FTS 那一路的 path_prefix 只是减少候选量的 SQL
    层下推），所以即使 FTS 索引缺失、或查询太短走不了 BM25，结果集依然正确——
    下推只影响速度，不影响语义。
    """

    path_prefix: str = ""
    tags: list[str] | None = None
    mtime_after: float | None = None
    mtime_before: float | None = None
    offset: int = 0
    limit: int | None = None

    def matches(self, chunk: Chunk) -> bool:
        """chunk 是否满足全部已设置的条件（未设置的条件一律放行）。"""
        if self.path_prefix and not chunk.source.startswith(self.path_prefix):
            return False
        if self.tags:
            wanted = {str(tag).lower().lstrip("#") for tag in self.tags if str(tag).strip()}
            if wanted:
                # 清洗规则与 _is_frontmatter_exempt 保持一致：小写 + 去 # 前缀。
                have = {str(tag).lower().lstrip("#") for tag in chunk.metadata.get("tags") or []}
                if not (wanted & have):
                    return False
        # 老缓存产生的 chunk 没有 mtime 字段，视为"时间未知"直接放行，
        # 否则一次过滤条件就会让整个历史索引检索不到。
        mtime = chunk.metadata.get("mtime")
        if mtime is not None:
            if self.mtime_after is not None and mtime < self.mtime_after:
                return False
            if self.mtime_before is not None and mtime > self.mtime_before:
                return False
        return True

    def page_slice(self, default_limit: int) -> tuple[int, int]:
        """分页区间 [start, end)：limit 未设置时回落到调用方的 top_k。"""
        start = max(0, int(self.offset))
        limit = self.limit if self.limit is not None else default_limit
        return start, start + max(0, int(limit))


def _pack_str(buf: bytearray, text: str) -> None:
    data = text.encode("utf-8")
    buf += struct.pack("<I", len(data))
    buf += data


def _pack_u32(buf: bytearray, value: int) -> None:
    buf += struct.pack("<I", value)


def _to_emb(vectors: Iterable[float]) -> array:
    return array(_EMB_DTYPE, vectors)


def dedupe_by_content_hash(items: list[Any], chunk_of: Callable[[Any], Chunk] | None = None) -> list[Any]:
    """按 content_hash 保序去重：同一份内容只留排在最前面的那条。

    items 默认就是 Chunk 列表；跨库 fan-out 传的是 (vault_entry, chunk) 元组，
    用 chunk_of 把 chunk 取出来即可。没有 content_hash 的老 chunk 一律保留——
    宁可多返回一条，也不能把内容未知的 chunk 误判成重复删掉。
    """
    extract = chunk_of if chunk_of is not None else (lambda item: item)
    seen: set[str] = set()
    kept: list[Any] = []
    for item in items:
        digest = extract(item).metadata.get("content_hash")
        if digest is None:
            kept.append(item)
            continue
        if digest in seen:
            continue
        seen.add(digest)
        kept.append(item)
    return kept


def _image_note(alt: str, caption: str, path: str) -> str:
    """一张图片对应的一行注入文本：`[图片: alt 图注 (文件名)]`。"""
    name = Path(path.replace("\\", "/")).stem
    descriptor = " ".join(part for part in (alt, caption) if part)
    return f"[图片: {descriptor} ({name})]" if descriptor else f"[图片: {name}]"


def _image_notes_for_line(line: str) -> list[str]:
    """提取一行里的所有图片，返回要插入的注入行（没有图片则返回空列表）。"""
    notes: list[str] = []
    for match in _MD_IMAGE_RE.finditer(line):
        alt = match.group(1).strip()
        path = match.group(2)
        title = (match.group(3) or "").strip()
        notes.append(_image_note(alt, title, path))
    for match in _WIKI_IMAGE_RE.finditer(line):
        path = match.group(1).strip()
        caption = (match.group(2) or "").strip()
        if Path(path.replace("\\", "/")).suffix.lower() not in _IMAGE_EXTS:
            continue
        notes.append(_image_note("", caption, path))
    return notes


def _inject_image_notes(lines: list[str]) -> list[str]:
    """把图片的 alt / 图注变成可检索的正文行（纯函数，不修改输入）。

    Markdown 里的图片只有路径引用，alt 与图注（Obsidian 的 ``![[path|图注]]``）
    原本不会进入 chunk 正文，语义检索不到「这张图讲了什么」。本函数在每张图片
    所在行之后插入一行 ``[图片: alt 图注 (文件名)]``。

    代价：chunk content 变了 → chunk.id 变了 → 全库重新 embedding。所以由
    ``config.inject_image_captions`` 门控，默认必须关闭。代码块（``` / ~~~）
    里的图片语法只是示例文本，不注入。
    """
    result: list[str] = []
    in_fence = False
    for line in lines:
        result.append(line)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        result.extend(_image_notes_for_line(line))
    return result


class _CacheCodec:
    """Compact binary cache format: signatures + chunks + float32 embeddings (zlib)."""

    @staticmethod
    def dump(path: Path, meta: dict[str, Any], files: dict[str, tuple[str, list[Chunk]]]) -> None:
        buf = bytearray()
        buf += _CACHE_MAGIC
        buf += struct.pack("<B", _CACHE_VERSION)
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        _pack_u32(buf, len(meta_bytes))
        buf += meta_bytes
        _pack_u32(buf, len(files))
        for source in sorted(files):
            signature, chunks = files[source]
            _pack_str(buf, source)
            _pack_str(buf, signature)
            _pack_u32(buf, len(chunks))
            for chunk in chunks:
                _pack_str(buf, chunk.id)
                _pack_str(buf, chunk.content)
                _pack_str(buf, chunk.title)
                _pack_str(buf, json.dumps(chunk.metadata, ensure_ascii=False))
                emb = chunk.embedding
                if emb is not None and len(emb):
                    _pack_u32(buf, len(emb))
                    buf += emb.tobytes()
                else:
                    _pack_u32(buf, 0)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(zlib.compress(bytes(buf), 6))
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> tuple[dict[str, Any], dict[str, tuple[str, list[Chunk]]]] | None:
        if not path.exists():
            return None
        try:
            raw = zlib.decompress(path.read_bytes())
        except (OSError, zlib.error):
            return None
        if raw[: len(_CACHE_MAGIC)] != _CACHE_MAGIC:
            return None
        pos = len(_CACHE_MAGIC)
        version = raw[pos]
        pos += 1
        if version != _CACHE_VERSION:
            return None
        try:
            (meta_len,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            meta = json.loads(raw[pos : pos + meta_len].decode("utf-8"))
            pos += meta_len
            (file_count,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            files: dict[str, tuple[str, list[Chunk]]] = {}
            for _ in range(file_count):
                (s_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                source = raw[pos : pos + s_len].decode("utf-8")
                pos += s_len
                (sig_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                signature = raw[pos : pos + sig_len].decode("utf-8")
                pos += sig_len
                (chunk_count,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                chunks: list[Chunk] = []
                for _ in range(chunk_count):
                    (c_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    chunk_id = raw[pos : pos + c_len].decode("utf-8")
                    pos += c_len
                    (content_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    content = raw[pos : pos + content_len].decode("utf-8")
                    pos += content_len
                    (title_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    title = raw[pos : pos + title_len].decode("utf-8")
                    pos += title_len
                    (meta_len2,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    metadata = json.loads(raw[pos : pos + meta_len2].decode("utf-8"))
                    pos += meta_len2
                    (emb_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    embedding: array | None = None
                    if emb_len:
                        embedding = array(_EMB_DTYPE)
                        embedding.frombytes(raw[pos : pos + emb_len * 4])
                        pos += emb_len * 4
                    chunks.append(Chunk(chunk_id, content, source, title, metadata, embedding=embedding))
                files[source] = (signature, chunks)
            return meta, files
        except (struct.error, UnicodeDecodeError, json.JSONDecodeError, ValueError, OverflowError):
            return None


class _VectorsCodec:
    """Vector-layer cache: chunk id -> float32 embedding (zlib).

    Kept separate from the chunks layer so embedding model/dimension changes
    only invalidate vectors while the text chunks stay reusable.
    """

    _MAGIC = b"VMCPV"
    _VERSION = 1

    @staticmethod
    def dump(path: Path, meta: dict[str, Any], vectors: dict[str, array]) -> None:
        buf = bytearray()
        buf += _VectorsCodec._MAGIC
        buf += struct.pack("<B", _VectorsCodec._VERSION)
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        _pack_u32(buf, len(meta_bytes))
        buf += meta_bytes
        _pack_u32(buf, len(vectors))
        for chunk_id in sorted(vectors):
            embedding = vectors[chunk_id]
            _pack_str(buf, chunk_id)
            _pack_u32(buf, len(embedding))
            if len(embedding):
                buf += embedding.tobytes()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(zlib.compress(bytes(buf), 6))
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> tuple[dict[str, Any], dict[str, array]] | None:
        if not path.exists():
            return None
        try:
            raw = zlib.decompress(path.read_bytes())
        except (OSError, zlib.error):
            return None
        if raw[: len(_VectorsCodec._MAGIC)] != _VectorsCodec._MAGIC:
            return None
        pos = len(_VectorsCodec._MAGIC)
        version = raw[pos]
        pos += 1
        if version != _VectorsCodec._VERSION:
            return None
        try:
            (meta_len,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            meta = json.loads(raw[pos : pos + meta_len].decode("utf-8"))
            pos += meta_len
            (count,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            vectors: dict[str, array] = {}
            for _ in range(count):
                (cid_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                chunk_id = raw[pos : pos + cid_len].decode("utf-8")
                pos += cid_len
                (emb_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                embedding: array | None = None
                if emb_len:
                    embedding = array(_EMB_DTYPE)
                    embedding.frombytes(raw[pos : pos + emb_len * 4])
                    pos += emb_len * 4
                vectors[chunk_id] = embedding
            return meta, vectors
        except (struct.error, UnicodeDecodeError, json.JSONDecodeError, ValueError, OverflowError):
            return None


def rerank_chunks(query: str, ranked: list[Chunk], reranker_provider: Any, cap: int = 60) -> list[Chunk]:
    """Reorder `ranked` with the reranker provider (module-level so the server
    can rerank a merged multi-vault candidate pool with a single API call).

    Returns the reordered list; on provider failure the input order is kept.
    """
    if not ranked:
        return ranked
    try:
        candidates = ranked[:cap]  # cap rerank payload; never send the whole corpus
        if hasattr(reranker_provider, "rerank_or_none"):
            reranked = reranker_provider.rerank_or_none(query, [chunk.content for chunk in candidates])
        else:
            reranked = reranker_provider.rerank(query, [chunk.content for chunk in candidates])
    except Exception:
        return ranked
    if not reranked:
        return ranked
    positions = {int(item["index"]): item for item in reranked if "index" in item}
    ordered = [candidates[index] for index in positions if 0 <= index < len(candidates)]
    ordered += [chunk for index, chunk in enumerate(candidates) if index not in positions]
    for index, item in positions.items():
        if 0 <= index < len(candidates) and "relevance_score" in item:
            candidates[index].score = float(item["relevance_score"])
    return ordered + ranked[len(candidates):]


class MarkdownIndexer:
    def __init__(
        self,
        vault_path: str | Path,
        config: AppConfig | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        reranker_provider: RerankerProvider | None = None,
    ) -> None:
        self.vault_path = Path(vault_path).expanduser()
        self.config = config or AppConfig(vault_path=str(self.vault_path))
        self.embedding_provider = embedding_provider or create_embedding_provider(self.config.embedding)
        self.reranker_provider = reranker_provider
        if reranker_provider is None:
            try:
                self.reranker_provider = create_reranker_provider(self.config.reranker)
            except Exception:
                self.reranker_provider = None
        self._chunks: dict[str, list[Chunk]] = {}
        self._signatures: dict[str, str] = {}
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
        self._fs_debounce_lock = threading.Lock()
        self._fs_debounce_timer: threading.Timer | None = None
        self._fs_pending_since: float | None = None
        self._fs_debounce_seconds: float = 0.5
        self._sync_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._chunks_cache_path: Path | None = None
        self._vectors_cache_path: Path | None = None
        self._fts_cache_path: Path | None = None
        self._vectors_db_path: Path | None = None
        self._failed_cache_path: Path | None = None
        self._fts: FtsIndex | None = None
        self._vector_backend: Any = None
        if self.config.cache.enabled and self.config.cache.dir:
            try:
                self._init_cache_paths()
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
        if not self._vectors_on_disk and self.config.vector.backend == "sqlite_vec":
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
        """
        if self.config.cache.id:
            return hashlib.sha256(self.config.cache.id.encode("utf-8")).hexdigest()[:16]
        raw = os.fspath(self.vault_path.resolve())
        normalized = os.path.normcase(os.path.realpath(raw))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _init_cache_paths(self) -> None:
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
        self._vectors_cache_path = vectors_dir / f"vault_{key}.{model_hash}.{self.config.embedding.dimension}.vec.bin"
        self._fts_cache_path = fts_dir / f"vault_{key}.fts.sqlite"
        # sqlite-vec backend keeps its own db (never shares the FTS file).
        self._vectors_db_path = vectors_dir / f"vault_{key}.vec.sqlite"
        # 失败名单：与 chunks/vectors/fts 平级的纯可观测性文件，进程重启后
        # 让 kb_stats 仍能报出上一轮的失败原因。
        self._failed_cache_path = base / f"vault_{key}.failed.json"
        self._load_chunks_cache()
        self._load_failed_files()
        # With the disk-backed sqlite_vec backend, vectors are not loaded into
        # RAM (that's the memory win); the disk store is migrated/flushed by
        # _ensure_disk_vectors_migrated() on first sync.
        if self.config.vector.backend != "sqlite_vec":
            self._load_vectors_cache()
        self._sweep_stale_cache()

    def _sweep_stale_cache(self) -> None:
        """Delete cache files older than cache.max_age_days (0 disables)."""
        max_age = self.config.cache.max_age_days
        if max_age <= 0 or self._cache_root() is None:
            return
        cutoff = time.time() - max_age * 86400
        root = self._cache_root()
        for pattern in ("*.bin", "*.sqlite"):
            for cache_file in root.rglob(pattern):
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

    # ------------------------------------------------------------------ cache

    def _chunks_meta(self) -> dict[str, Any]:
        # chunker 是文本层的手工失效开关：chunk 元数据每多一个字段就 +1，
        # 让老缓存重建一次。这里刻意不碰向量层——向量按 chunk.id（sha1 of
        # source+index+content）匹配，content 不变则 id 不变，所以 bump 之后
        # 老向量全部命中，不会触发任何重新 embedding（前提是这批向量要能活到
        # 重建之后，见 _load_vectors_cache 的 _pending_vectors 兜底）。
        return {
            "key": self._cache_key(),
            "chunk_size": self.config.chunk_size,
            "chunk_overlap": self.config.chunk_overlap,
            "chunker": 4,
        }

    def _vectors_meta(self) -> dict[str, Any]:
        return {
            "key": self._cache_key(),
            "embedding_mode": self.config.embedding.mode,
            "embedding_model": self.config.embedding.model,
            "dimension": self.config.embedding.dimension,
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
        if meta != self._chunks_meta():
            return
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

        命中与否只看 chunk.id，与文本层是否重建无关：内容没变的 chunk id 一定
        不变，向量也就一定是有效的。
        """
        if not self._pending_vectors:
            return
        pending = self._pending_vectors
        self._pending_vectors = {}
        for chunks in self._chunks.values():
            for chunk in chunks:
                vector = pending.get(chunk.id)
                if vector is not None:
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
        if not payload:
            return
        try:
            _CacheCodec.dump(self._chunks_cache_path, self._chunks_meta(), payload)
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
        if not vectors:
            return
        try:
            _VectorsCodec.dump(self._vectors_cache_path, self._vectors_meta(), vectors)
        except OSError:
            pass

    # ------------------------------------------------------------------ sync

    def sync(self) -> list[Chunk]:
        with self._sync_lock:
            return self._sync_locked()

    def _sync_locked(self) -> list[Chunk]:
        self.vault_path.mkdir(parents=True, exist_ok=True)
        failed_before = dict(self.failed_files)
        found: set[str] = set()
        changed: list[tuple[str, str, list[Chunk]]] = []
        for path in self._markdown_files():
            source = self._source(path)
            found.add(source)
            try:
                raw = path.read_bytes()
                signature = hashlib.sha256(raw).hexdigest()
                if self._signatures.get(source) == signature:
                    continue
                text = raw.decode("utf-8-sig")
                # 顺手复用上面 read_bytes 已经打开的目录项做一次 stat，记录文件
                # 修改时间供 mtime 过滤用：签名未变的文件不会走到这里，所以这个
                # mtime 语义上是"内容最后一次变化的时间"，而不是每次 touch 都更新。
                mtime: float | None = None
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = None
                chunks = self._chunk_file(source, text, mtime)
                changed.append((source, signature, chunks))
            except Exception as exc:
                self.failed_files[source] = str(exc)
                self._chunks.pop(source, None)
                self._signatures.pop(source, None)
                self._fts_delete(source)

        # Text layer: changed files update the index even if embedding fails
        # afterwards, so lexical search still works without vectors.
        for source, signature, chunks in changed:
            old_chunks = self._chunks.get(source)
            self._chunks[source] = chunks
            self._signatures[source] = signature
            self.failed_files.pop(source, None)
            self._fts_upsert(source, chunks)
            # Disk-backed mode: re-chunking a file orphans its old vector ids.
            if self._vectors_on_disk and old_chunks:
                old_ids = [chunk.id for chunk in old_chunks]
                try:
                    self._vector_backend.delete_vectors(old_ids)
                    self._disk_vectors.difference_update(old_ids)
                except Exception:
                    pass

        # Disk-backed mode: on first sync (or after a crash) make sure the
        # vector store matches the in-memory chunk set before embedding.
        self._ensure_disk_vectors_migrated()
        # 文本层刚被重建过（chunker 版本提升 / 缓存损坏）时，把初始化阶段
        # 暂存的老向量挂回去，避免整个库重新 embedding。
        self._attach_pending_vectors()

        # Vector layer: embed every chunk that lacks a vector. When the vectors
        # cache was invalidated (model/dimension change) this re-embeds the whole
        # corpus while reusing the text chunks; when only a few files changed it
        # embeds just those chunks.
        embed_did_work = self._embed_missing()
        # Disk-backed mode: persist newly embedded vectors and release RAM.
        self._flush_vectors_to_disk()

        removed: set[str] = set(self._chunks) - found
        for source in removed:
            removed_ids = [chunk.id for chunk in self._chunks.get(source, [])]
            self._chunks.pop(source, None)
            self._signatures.pop(source, None)
            self.failed_files.pop(source, None)
            self._fts_delete(source)
            if removed_ids:
                try:
                    self._vector_backend.delete_vectors(removed_ids)
                    self._disk_vectors.difference_update(removed_ids)
                except Exception:
                    pass
        self.last_sync = time.time()
        # 什么都没变时跳过缓存重写：原生监听（[cache] placement = "vault"）下，
        # 每次写缓存都会再次触发文件事件，无变化也重写等于自激的同步死循环。
        if changed or removed or embed_did_work or self.failed_files != failed_before:
            self._save_cache()
        return self.all_chunks()

    def _ensure_disk_vectors_migrated(self) -> None:
        """Reconcile the disk vector store with in-memory chunks.

        Rebuilds the RAM bookkeeping set from the disk store, and performs a
        one-time migration from the legacy .vec.bin when the disk store is
        empty (so switching backends never triggers a full re-embed).
        """
        if not self._vectors_on_disk or self._disk_vectors:
            return
        try:
            stored = set(self._vector_backend.list_ids())
            self._disk_vectors = stored
            if not stored and self._chunks:
                # Migrate the legacy .bin into the disk store once.
                if self._vectors_cache_path is not None and self._vectors_cache_path.exists():
                    self._load_vectors_cache()
                self._flush_vectors_to_disk()
        except Exception:
            pass

    def _flush_vectors_to_disk(self) -> None:
        """Upsert all in-RAM embeddings into the disk store, then drop them
        from Chunk to release resident memory. On failure keep them in RAM."""
        if not self._vectors_on_disk:
            return
        vectors = {
            chunk.id: chunk.embedding
            for chunks in self._chunks.values()
            for chunk in chunks
            if chunk.embedding is not None and len(chunk.embedding)
        }
        if not vectors:
            return
        try:
            self._vector_backend.upsert_vectors(vectors)
            self._disk_vectors.update(vectors)
            for chunk in self.all_chunks():
                if chunk.id in vectors:
                    chunk.embedding = None
        except Exception:
            pass

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
        """这个 chunk 已经有可用向量了吗？

        磁盘后端看是否落盘；刚复用/刚算出来、还留在 RAM 里等 flush 的也算有
        （flush 会按它自己的 chunk.id 落盘，所以复用的向量最终会以副本形式
        各存一份——这是刻意的，磁盘省的是 RAM 而不是磁盘）。
        """
        if self._vectors_on_disk:
            return chunk.id in self._disk_vectors or chunk.embedding is not None
        return chunk.embedding is not None and len(chunk.embedding) > 0

    def _reuse_vectors_by_content_hash(self) -> int:
        """把已有向量按 content_hash 复用到内容相同但还没有向量的 chunk 上。

        典型场景：库里有一份整目录的备份（教材/ 与 教材_Raw_Backup/），两处
        正文逐字相同，但 chunk.id 因为 source 不同而不一样——与其把同一段文本
        送进 embedding API 两次，不如直接复用已经算出来的向量。返回复用条数。

        只读使用向量，所以多个 chunk 可以安全共享同一个 array 对象。
        """
        missing: list[Chunk] = []
        donors: dict[str, Chunk] = {}
        for chunks in self._chunks.values():
            for chunk in chunks:
                digest = chunk.metadata.get("content_hash")
                if not digest:
                    continue
                if self._chunk_has_vector(chunk):
                    donors.setdefault(digest, chunk)
                else:
                    missing.append(chunk)
        if not missing or not donors:
            return 0

        needed = {chunk.metadata.get("content_hash") for chunk in missing}
        reusable: dict[str, Any] = {}
        if self._vectors_on_disk:
            # 磁盘后端：chunk.embedding 在 flush 后是 None，得从 vec0 表读回来。
            by_id = {chunk.id: digest for digest, chunk in donors.items() if digest in needed}
            for chunk_id, vector in self._vector_backend.get_vectors(by_id).items():
                digest = by_id.get(chunk_id)
                if digest is not None:
                    reusable[digest] = vector
        else:
            for digest, chunk in donors.items():
                if digest in needed and chunk.embedding is not None and len(chunk.embedding):
                    reusable[digest] = chunk.embedding
        if not reusable:
            return 0

        reused = 0
        for chunk in missing:
            vector = reusable.get(chunk.metadata.get("content_hash"))
            if vector is None:
                continue
            chunk.embedding = vector
            reused += 1
        return reused

    def _embed_missing(self) -> bool:
        """Embed every chunk that has no vector yet.

        Returns True when there was embedding work to do (or failures to record),
        so the caller knows whether the cache files need rewriting. When the
        vectors cache was invalidated (model/dimension change) this re-embeds the
        whole corpus while reusing the text chunks; when only a few files changed
        it embeds just those chunks.

        注意：失败文件的重试是天然的——这里的判据是"缺向量"（memory 后端看
        chunk.embedding is None，磁盘后端看 chunk.id not in _disk_vectors），
        而不是看 failed_files 字典，所以这里不需要任何额外重试逻辑；把
        failed_files 持久化到磁盘只是为了跨进程重启的可观测性。
        """
        # 先做一轮内容哈希复用：已经算过的内容不再花钱重算一次。
        self._reuse_vectors_by_content_hash()

        pending: dict[str, list[Chunk]] = {}
        for source, chunks in self._chunks.items():
            missing = [chunk for chunk in chunks if not self._chunk_has_vector(chunk)]
            if missing:
                pending[source] = missing
        if not pending:
            return False

        if self.config.embedding.mode != "external":
            for source, chunks in pending.items():
                try:
                    vectors = self.embedding_provider.embed([chunk.content for chunk in chunks])
                    for chunk, vector in zip(chunks, vectors):
                        chunk.embedding = _to_emb(vector)
                except Exception as exc:
                    self.failed_files[source] = str(exc)
                else:
                    # 补向量成功即撤销旧失败记录，否则持久化文件会永久撒谎。
                    self.failed_files.pop(source, None)
            return True

        max_workers = self.config.cache.embedding_max_workers
        tasks = list(pending.items())
        if max_workers <= 1 or len(tasks) <= 1:
            for source, chunks in tasks:
                try:
                    self._embed_one_file(source, chunks, self.embedding_provider)
                except Exception as exc:
                    self.failed_files[source] = str(exc)
                else:
                    self.failed_files.pop(source, None)
            return True

        failures: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="vault-emb") as pool:
            future_map = {
                pool.submit(self._embed_one_file, source, chunks, self.embedding_provider): source
                for source, chunks in tasks
            }
            for future in as_completed(future_map):
                source = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    failures[source] = str(exc)
        for source in pending:
            if source in failures:
                self.failed_files[source] = failures[source]
            else:
                self.failed_files.pop(source, None)
        return True

    @staticmethod
    def _embed_one_file(source: str, chunks: list[Chunk], provider: EmbeddingProvider) -> None:
        # 同一个文件里也可能出现逐字重复的段落（复制粘贴、模板套话），按内容
        # 哈希去重后只请求一次，回填时同 hash 的 chunk 共用同一个向量。
        # 老 chunk 没有 content_hash 时退回用正文算一个，行为与去重前一致。
        contents: dict[str, str] = {}
        order: list[str] = []
        keys: list[str] = []
        for chunk in chunks:
            digest = chunk.metadata.get("content_hash")
            if not digest:
                digest = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()[:16]
            keys.append(digest)
            if digest not in contents:
                contents[digest] = chunk.content
                order.append(digest)
        vectors = provider.embed([contents[digest] for digest in order])
        # 按哈希回填而不是按位置，避免 provider 少返回向量时整批错位。
        by_hash = dict(zip(order, vectors))
        for chunk, digest in zip(chunks, keys):
            vector = by_hash.get(digest)
            if vector is not None:
                chunk.embedding = _to_emb(vector)

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
        if not self.vault_path.exists():
            return []
        matcher = self._ignore_matcher()
        paths: list[Path] = []
        for path in self.vault_path.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            if self._ignored_name(path.name):
                continue
            source = self._source(path)
            ignored, _ = matcher.is_ignored(source, is_dir=False)
            if ignored:
                continue
            paths.append(path)
        return sorted(paths, key=lambda item: self._source(item))

    @staticmethod
    def _ignored_name(name: str) -> bool:
        lower = name.lower()
        return name.startswith("~") or lower.endswith((".tmp.md", ".swp.md", ".swo.md"))

    def _source(self, path: Path) -> str:
        return path.relative_to(self.vault_path).as_posix()

    def _chunk_file(self, source: str, text: str, mtime: float | None = None) -> list[Chunk]:
        lines = text.splitlines()
        frontmatter_end, tags, properties = self._frontmatter(lines)
        is_fm_exempt, _ = self._is_frontmatter_exempt(tags, properties)
        if is_fm_exempt:
            return []
        body_start = frontmatter_end + 1
        body = lines[body_start:]
        body, _ = self._strip_ignored_blocks(body)
        if self.config.inject_image_captions:
            body = _inject_image_notes(body)
        title = self._title(source, body)
        sections: list[tuple[str, int, list[str]]] = []
        current_heading = title
        current_start = body_start + 1
        current_lines: list[str] = []
        for offset, line in enumerate(body):
            line_number = body_start + offset + 1
            match = _HEADING_RE.match(line)
            if match:
                if any(l.strip() for l in current_lines):
                    sections.append((current_heading, current_start, current_lines))
                current_heading = self._clean_heading(match.group(2))
                current_start = line_number
                current_lines = [line]
            else:
                if not current_lines:
                    if line.strip():
                        current_start = line_number
                        current_lines.append(line)
                else:
                    current_lines.append(line)
        if any(l.strip() for l in current_lines):
            sections.append((current_heading, current_start, current_lines))
        if not sections and body:
            if any(line.strip() for line in body):
                sections = [(title, body_start + 1, body)]
        return self._make_chunks(source, title, tags, sections, mtime)

    @staticmethod
    def _frontmatter(lines: list[str]) -> tuple[int, list[str], dict[str, Any]]:
        if len(lines) < 2 or lines[0].strip() != "---":
            return -1, [], {}
        end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), -1)
        if end < 0:
            return -1, [], {}
        tags: list[str] = []
        properties: dict[str, Any] = {}
        current_list_key: str | None = None

        for line in lines[1:end]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") and current_list_key:
                val = stripped[2:].strip().strip("'\"")
                if isinstance(properties.get(current_list_key), list):
                    properties[current_list_key].append(val)
                continue
            current_list_key = None
            if ":" in line:
                key, raw_val = line.split(":", 1)
                key = key.strip().lower()
                val = raw_val.strip()
                if not val:
                    properties[key] = []
                    current_list_key = key
                    continue
                if val.startswith("[") and val.endswith("]"):
                    items = [item.strip().strip("'\"") for item in val[1:-1].split(",") if item.strip()]
                    properties[key] = items
                elif val.lower() in {"true", "yes", "on"}:
                    properties[key] = True
                elif val.lower() in {"false", "no", "off"}:
                    properties[key] = False
                else:
                    properties[key] = val.strip("'\"")

        raw_tags = properties.get("tags")
        if isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags if str(t).strip()]
        elif isinstance(raw_tags, str) and raw_tags:
            tags = [item.strip().strip("'\"") for item in raw_tags.split(",") if item.strip()]

        return end, tags, properties

    def _is_frontmatter_exempt(self, tags: list[str], properties: dict[str, Any]) -> tuple[bool, str | None]:
        if "rag" in properties:
            val = properties["rag"]
            if val is False or (isinstance(val, str) and val.lower() in {"false", "no", "off", "0"}):
                return True, "frontmatter property 'rag: false'"

        for key in self.config.exclude_frontmatter_keys:
            key_lower = key.lower()
            if key_lower in properties:
                val = properties[key_lower]
                if val is True or (isinstance(val, str) and val.lower() in {"true", "yes", "on", "1"}):
                    return True, f"frontmatter property '{key}: true'"

        exclude_tags_lower = {t.lower().lstrip("#") for t in self.config.exclude_tags}
        for tag in tags:
            tag_clean = tag.lower().lstrip("#")
            if tag_clean in exclude_tags_lower:
                return True, f"frontmatter tag '{tag}'"

        return False, None

    @staticmethod
    def _strip_ignored_blocks(body: list[str]) -> tuple[list[str], bool]:
        cleaned: list[str] = []
        in_ignore = False
        has_ignores = False

        for line in body:
            if not in_ignore:
                if _BLOCK_IGNORE_START.search(line):
                    in_ignore = True
                    has_ignores = True
                    cleaned.append("")
                    if _BLOCK_IGNORE_END.search(line):
                        in_ignore = False
                else:
                    cleaned.append(line)
            else:
                cleaned.append("")
                if _BLOCK_IGNORE_END.search(line):
                    in_ignore = False

        return cleaned, has_ignores

    @staticmethod
    def _clean_heading(heading: str) -> str:
        return re.sub(r"\s+#*$", "", heading).strip()

    def _title(self, source: str, body: list[str]) -> str:
        for line in body:
            match = _HEADING_RE.match(line)
            if match and len(match.group(1)) == 1:
                return self._clean_heading(match.group(2))
        for line in body:
            match = _HEADING_RE.match(line)
            if match:
                return self._clean_heading(match.group(2))
        return Path(source).stem

    def _make_chunks(
        self,
        source: str,
        title: str,
        tags: list[str],
        sections: list[tuple[str, int, list[str]]],
        mtime: float | None = None,
    ) -> list[Chunk]:
        result: list[Chunk] = []
        chunk_index = 0
        overlap = self.config.chunk_overlap
        for heading, start, lines in sections:
            current: list[str] = []
            current_start = start
            current_length = 0
            carry: list[str] = []
            carry_length = 0
            for offset, line in enumerate(lines):
                if current and current_length + len(line) + 1 > self.config.chunk_size:
                    result.append(self._new_chunk(source, title, heading, current_start, start + offset - 1, chunk_index, tags, current, mtime))
                    chunk_index += 1
                    carry, carry_length = self._overlap_tail(current, overlap)
                    current = list(carry)
                    current_start = start + offset - len(carry)
                    current_length = carry_length
                current.append(line)
                current_length += len(line) + 1
            if current:
                result.append(self._new_chunk(source, title, heading, current_start, start + len(lines) - 1, chunk_index, tags, current, mtime))
                chunk_index += 1
        return result

    @staticmethod
    def _overlap_tail(lines: list[str], overlap: int) -> tuple[list[str], int]:
        """Return the trailing lines whose total length covers ``overlap`` chars."""
        if overlap <= 0:
            return [], 0
        tail: list[str] = []
        length = 0
        for line in reversed(lines):
            tail.append(line)
            length += len(line) + 1
            if length >= overlap:
                break
        return list(reversed(tail)), length

    @staticmethod
    def _new_chunk(source: str, title: str, heading: str, start: int, end: int, index: int, tags: list[str], lines: list[str], mtime: float | None = None) -> Chunk:
        content = "\n".join(lines).strip()
        identifier = hashlib.sha1(f"{source}\0{index}\0{content}".encode("utf-8")).hexdigest()
        return Chunk(identifier, content, source, title, {
            "heading": heading,
            "start_line": start,
            "end_line": max(start, end),
            "chunk_index": index,
            "tags": list(tags),
            # epoch 秒，供 kb_search 的 mtime_after / mtime_before 过滤；
            # 老缓存里没有这个字段，SearchFilter 会放行而不是判为不匹配。
            "mtime": mtime,
            # chunk 正文的 sha256 前 16 位：内容完全相同的 chunk（重复备份、
            # 复制粘贴的段落）共享同一个哈希，embedding 与检索去重都靠它。
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        })

    def all_chunks(self) -> list[Chunk]:
        return [chunk for source in sorted(self._chunks) for chunk in self._chunks[source]]

    def search(self, query: str, top_k: int = 10, use_rerank: bool = False, query_vector: Iterable[float] | None = None, filters: SearchFilter | None = None, dedupe: bool = True) -> list[Chunk]:
        query = query.strip()
        all_chunks = self.all_chunks()
        if not query:
            ranked = list(all_chunks)
            if dedupe:
                ranked = dedupe_by_content_hash(ranked)
            if filters is None:
                return ranked[: max(0, top_k)]
            ranked = [chunk for chunk in ranked if filters.matches(chunk)]
            start, end = filters.page_slice(top_k)
            return ranked[start:end]

        query_tokens = self._query_tokens(query)

        # Lexical scores are a soft signal, never a hard gate: every chunk gets a
        # score so semantic recall always has the full corpus to work with.
        lexical: dict[str, float] = {}
        for chunk in all_chunks:
            haystack = chunk.content.lower()
            token_hits = sum(haystack.count(token) for token in query_tokens)
            exact_boost = 1 if query.lower() in haystack else 0
            lexical[chunk.id] = float(token_hits + exact_boost * 10)

        # Semantic route: raw cosine, snapshotted BEFORE any lexical fusion so
        # both the hybrid (RRF) and legacy paths can use it independently.
        # Queries always go through the vector backend (memory brute-force or
        # disk-backed sqlite-vec KNN).
        semantic_chunks: list[Chunk] = []
        semantic_snapshot: dict[str, float] = {}
        if self.config.embedding.mode == "external":
            try:
                # Callers doing multi-vault fan-out embed the query once and
                # pass it in, so N vaults cost one embed call instead of N.
                if query_vector is None:
                    query_vector = self.embedding_provider.embed([query])[0]
                # Rerank candidate cap + per-route RRF width both come from
                # config so callers can trade recall vs API payload size.
                vec_limit = max(top_k, self.config.rrf_per_route, self.config.rerank_cap)
                pairs = self._vector_backend.query(query_vector, vec_limit)
                semantic_snapshot = dict(pairs)
                for chunk in all_chunks:
                    if chunk.id in semantic_snapshot:
                        chunk.score = semantic_snapshot[chunk.id]
                        semantic_chunks.append(chunk)
            except Exception:
                pass

        hybrid = self.config.use_hybrid and self._fts is not None and self._fts.available
        if hybrid:
            # path_prefix 下推到 FTS 的 SQL 层只是减少候选量（source 是 UNINDEXED
            # 列，可直接进 WHERE）；过滤的正确性由下面的统一后过滤保证。
            prefix = filters.path_prefix if filters is not None else ""
            ranked = self._hybrid_rank(query, all_chunks, lexical, semantic_snapshot, path_prefix=prefix)
        else:
            ranked = []
        if not ranked:
            # Legacy path, unchanged: cosine dominates, lexical breaks ties.
            if semantic_chunks:
                lex_max = max(lexical[chunk.id] for chunk in semantic_chunks) or 1.0
                for chunk in semantic_chunks:
                    chunk.score = chunk.score + (lexical[chunk.id] / lex_max) * 0.2
                ranked = semantic_chunks
            else:
                ranked = [chunk for chunk in all_chunks if lexical[chunk.id] > 0]
                for chunk in ranked:
                    chunk.score = lexical[chunk.id]

        ranked.sort(key=lambda chunk: (-chunk.score, chunk.source, chunk.metadata["chunk_index"]))

        # 过滤放在 rerank 之前：rerank 是要花钱/花时间的配额，不能浪费在马上
        # 会被过滤掉的条目上。
        if filters is not None:
            ranked = [chunk for chunk in ranked if filters.matches(chunk)]

        if use_rerank and self.reranker_provider and ranked:
            ranked = rerank_chunks(query, ranked, self.reranker_provider, cap=self.config.rerank_cap)

        # 内容完全相同的 chunk（重复备份）只留排在最前面的那条，否则同一段
        # 内容会占掉 top_k 里的好几格，把其它库/其它文件的结果挤出去。
        if dedupe:
            ranked = dedupe_by_content_hash(ranked)

        if filters is None:
            return ranked[: max(0, top_k)]
        start, end = filters.page_slice(top_k)
        return ranked[start:end]

    def _fts_query(self, query: str) -> str | None:
        """Build an FTS5 MATCH expression from tokens of length >= 3.

        Returns None when no such token survives (e.g. a 2-char CJK query like
        "银狼"), so the caller skips the BM25 route and the bigram lexical route
        covers the query instead. Trigram cannot match <3-char queries.
        """
        terms: list[str] = []
        for piece in _WORD_RE.findall(query):
            for word in _ASCII_RE.findall(piece):
                if len(word) >= 3:
                    terms.append('"' + word.lower().replace('"', '""') + '"')
            for cjk in _CJK_RE.findall(piece):
                if len(cjk) >= 3:
                    terms.append('"' + cjk.replace('"', '""') + '"')
        if not terms:
            return None
        return " AND ".join(terms)

    def _hybrid_rank(
        self,
        query: str,
        all_chunks: list[Chunk],
        lexical: dict[str, float],
        semantic_snapshot: dict[str, float],
        path_prefix: str = "",
    ) -> list[Chunk]:
        """Three-route RRF fusion: FTS5 BM25 + vector cosine + bigram lexical.

        chunk.score becomes the RRF value (comparable across vaults), then the
        caller sorts and reranks as usual. Any single route failing or empty is
        simply absent — search never throws.
        """
        by_id = {chunk.id: chunk for chunk in all_chunks}
        routes: list[list[str]] = []

        # Route A: FTS5 BM25 (trigram). Skipped when the query has no >=3-char token.
        fts_sql = self._fts_query(query)
        if fts_sql is not None and self._fts is not None:
            try:
                routes.append([chunk_id for chunk_id, _score in self._fts.search(fts_sql, self.config.rrf_per_route, path_prefix)])
            except Exception:
                pass

        # Route B: vector cosine, raw and descending.
        if semantic_snapshot:
            ordered = sorted(semantic_snapshot.items(), key=lambda item: -item[1])
            routes.append([chunk_id for chunk_id, _score in ordered[: self.config.rrf_per_route]])

        # Route C: bigram lexical soft scores, descending, score > 0 only.
        lexical_ordered = sorted(
            ((chunk_id, score) for chunk_id, score in lexical.items() if score > 0),
            key=lambda item: -item[1],
        )
        if lexical_ordered:
            routes.append([chunk_id for chunk_id, _score in lexical_ordered[: self.config.rrf_per_route]])

        if not routes:
            return []

        fused: dict[str, float] = {}
        for route in routes:
            for rank, chunk_id in enumerate(route, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)

        ranked = [by_id[chunk_id] for chunk_id in fused if chunk_id in by_id]
        for chunk in ranked:
            chunk.score = fused[chunk.id]
        ranked.sort(key=lambda chunk: (-chunk.score, chunk.source, chunk.metadata["chunk_index"]))
        return ranked

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        """Tokenize for lexical scoring: ASCII words verbatim, CJK text as bigrams.

        The previous regex grabbed a whole CJK run as one token, which made
        lexical scoring behave like exact-substring matching for Chinese. Bigrams
        keep named entities (卡芙卡 -> 卡芙/芙卡) matchable without any tokenizer
        dependency.
        """
        tokens: list[str] = []
        for piece in _WORD_RE.findall(query):
            for word in _ASCII_RE.findall(piece):
                tokens.append(word.lower())
            for cjk in _CJK_RE.findall(piece):
                chars = list(cjk)
                if len(chars) == 1:
                    tokens.append(chars[0])
                else:
                    tokens.extend("".join(chars[i:i + 2]) for i in range(len(chars) - 1))
        return tokens

    def _semantic_rank(self, query_vector: Iterable[float], chunks: list[Chunk]) -> list[Chunk]:
        """Batch cosine similarity. numpy when available, scalar loop fallback."""
        embedded = [chunk for chunk in chunks if chunk.embedding is not None and len(chunk.embedding)]
        if not embedded:
            return []
        try:
            import numpy as np
            matrix = np.asarray([chunk.embedding for chunk in embedded], dtype=np.float32)
            vector = np.asarray(query_vector, dtype=np.float32)
            denominator = np.linalg.norm(matrix, axis=1) * np.linalg.norm(vector)
            if denominator.size == 0 or float(np.linalg.norm(vector)) == 0.0:
                return []
            similarities = (matrix @ vector) / (denominator + 1e-9)
            for chunk, similarity in zip(embedded, similarities.tolist()):
                chunk.score = float(similarity)
            return embedded
        except Exception:
            for chunk in embedded:
                chunk.score = self._cosine(query_vector, chunk.embedding)
            return embedded

    @staticmethod
    def _cosine(left: array, right: array) -> float:
        size = min(len(left), len(right))
        if not size:
            return 0.0
        dot = 0.0
        left_norm = 0.0
        right_norm = 0.0
        for index in range(size):
            lv = left[index]
            rv = right[index]
            dot += lv * rv
            left_norm += lv * lv
            right_norm += rv * rv
        return dot / ((left_norm ** 0.5) * (right_norm ** 0.5)) if left_norm and right_norm else 0.0

    def read(self, source: str, start_line: int | None = None, end_line: int | None = None) -> str:
        path = self._safe_path(source)
        lines = path.read_text(encoding="utf-8").splitlines()
        start = 1 if start_line is None else max(1, start_line)
        end = len(lines) if end_line is None else min(len(lines), end_line)
        if start > end:
            return ""
        return "\n".join(lines[start - 1:end])

    def list_files(self) -> list[dict[str, Any]]:
        return [{"source": source, "title": chunks[0].title if chunks else Path(source).stem, "chunks": len(chunks)} for source, chunks in sorted(self._chunks.items())]

    def stats(self) -> dict[str, Any]:
        exempt_count = 0
        if self.vault_path.exists():
            matcher = self._ignore_matcher()
            for path in self.vault_path.rglob("*.md"):
                if not path.is_file() or self._ignored_name(path.name):
                    continue
                source = self._source(path)
                if matcher.is_ignored(source, is_dir=False)[0]:
                    exempt_count += 1
                else:
                    try:
                        raw = path.read_bytes()
                        lines = raw.decode("utf-8-sig", errors="ignore").splitlines()
                        _, tags, properties = self._frontmatter(lines)
                        if self._is_frontmatter_exempt(tags, properties)[0]:
                            exempt_count += 1
                    except Exception:
                        pass
        return {
            "files": len(self._chunks),
            "chunks": len(self.all_chunks()),
            "exempt_files": exempt_count,
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
        }

    def get_exemptions(self) -> dict[str, Any]:
        ignore_file_path = self.vault_path / self.config.ignore_file
        vaultignore_rules: list[str] = []
        if ignore_file_path.exists() and ignore_file_path.is_file():
            try:
                for line in ignore_file_path.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        vaultignore_rules.append(s)
            except Exception:
                pass

        all_md_files: list[str] = []
        exempt_files: list[dict[str, str]] = []
        if self.vault_path.exists():
            for path in sorted(self.vault_path.rglob("*.md")):
                if not path.is_file() or self._ignored_name(path.name):
                    continue
                source = self._source(path)
                all_md_files.append(source)
                check_res = self.check_exemption(source)
                if check_res["is_exempt"]:
                    exempt_files.append({"source": source, "reason": check_res["reason"]})

        return {
            "vault_path": str(self.vault_path.resolve()),
            "ignore_file": self.config.ignore_file,
            "vaultignore_rules": vaultignore_rules,
            "config_exclude_patterns": list(self.config.exclude_patterns),
            "exclude_tags": list(self.config.exclude_tags),
            "exclude_frontmatter_keys": list(self.config.exclude_frontmatter_keys),
            "total_md_files": len(all_md_files),
            "indexed_files": len(self._chunks),
            "exempt_files_count": len(exempt_files),
            "exempt_files_sample": [item["source"] for item in exempt_files[:50]],
        }

    def add_exemption_pattern(self, pattern: str) -> dict[str, Any]:
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        ignore_file_path = self.vault_path / self.config.ignore_file
        lines: list[str] = []
        if ignore_file_path.exists():
            try:
                lines = ignore_file_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                lines = []

        if pattern not in [l.strip() for l in lines]:
            lines.append(pattern)
            ignore_file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.sync()
        return {
            "success": True,
            "action": "add_pattern",
            "pattern": pattern,
            "vaultignore_path": str(ignore_file_path.resolve()),
            "total_rules": len([l for l in lines if l.strip() and not l.strip().startswith("#")]),
        }

    def remove_exemption_pattern(self, pattern: str) -> dict[str, Any]:
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        ignore_file_path = self.vault_path / self.config.ignore_file
        if not ignore_file_path.exists():
            return {"success": True, "action": "remove_pattern", "pattern": pattern, "removed": False}

        lines = ignore_file_path.read_text(encoding="utf-8").splitlines()
        new_lines = [l for l in lines if l.strip() != pattern]
        removed = len(new_lines) < len(lines)
        if removed:
            ignore_file_path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
            self.sync()

        return {
            "success": True,
            "action": "remove_pattern",
            "pattern": pattern,
            "removed": removed,
            "remaining_rules": len([l for l in new_lines if l.strip() and not l.strip().startswith("#")]),
        }

    def check_exemption(self, source: str) -> dict[str, Any]:
        source_posix = source.replace("\\", "/").strip("/")
        matcher = self._ignore_matcher()
        ignored, rule = matcher.is_ignored(source_posix, is_dir=False)
        if ignored:
            return {
                "source": source_posix,
                "is_exempt": True,
                "reason": f"matched ignore rule '{rule}'",
                "has_block_ignores": False,
                "indexed_chunks": 0,
            }

        target_path = self.vault_path / source_posix
        if not target_path.exists() or not target_path.is_file():
            return {
                "source": source_posix,
                "is_exempt": False,
                "reason": "file not found on disk",
                "has_block_ignores": False,
                "indexed_chunks": len(self._chunks.get(source_posix, [])),
            }

        try:
            raw = target_path.read_bytes()
            text = raw.decode("utf-8-sig")
            lines = text.splitlines()
            frontmatter_end, tags, properties = self._frontmatter(lines)
            is_fm_exempt, fm_reason = self._is_frontmatter_exempt(tags, properties)
            if is_fm_exempt:
                return {
                    "source": source_posix,
                    "is_exempt": True,
                    "reason": fm_reason or "frontmatter",
                    "has_block_ignores": False,
                    "indexed_chunks": 0,
                }

            body = lines[frontmatter_end + 1:]
            _, has_block_ignores = self._strip_ignored_blocks(body)
            return {
                "source": source_posix,
                "is_exempt": False,
                "reason": "none (actively indexed)",
                "has_block_ignores": has_block_ignores,
                "indexed_chunks": len(self._chunks.get(source_posix, [])),
            }
        except Exception as exc:
            return {
                "source": source_posix,
                "is_exempt": False,
                "reason": f"error reading file: {exc}",
                "has_block_ignores": False,
                "indexed_chunks": 0,
            }

    def set_file_exemption(self, source: str, exempt: bool = True, method: str = "frontmatter") -> dict[str, Any]:
        source_posix = source.replace("\\", "/").strip("/")
        if method == "ignore_file":
            if exempt:
                return self.add_exemption_pattern(source_posix)
            else:
                return self.remove_exemption_pattern(source_posix)

        path = self._safe_path(source_posix)
        if not path.exists():
            raise ValueError(f"file not found: {source_posix}")

        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()

        if exempt:
            if len(lines) >= 2 and lines[0].strip() == "---":
                end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
                if end > 0:
                    fm_lines = lines[1:end]
                    rag_found = False
                    new_fm_lines = []
                    for line in fm_lines:
                        if line.strip().lower().startswith("rag:"):
                            new_fm_lines.append("rag: false")
                            rag_found = True
                        else:
                            new_fm_lines.append(line)
                    if not rag_found:
                        new_fm_lines.insert(0, "rag: false")
                    new_lines = ["---"] + new_fm_lines + ["---"] + lines[end + 1:]
                else:
                    new_lines = ["---", "rag: false", "---", ""] + lines
            else:
                new_lines = ["---", "rag: false", "---", ""] + lines
        else:
            if len(lines) >= 2 and lines[0].strip() == "---":
                end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
                if end > 0:
                    fm_lines = lines[1:end]
                    new_fm_lines = []
                    for line in fm_lines:
                        stripped = line.strip().lower()
                        if any(stripped.startswith(k + ":") for k in ["rag", "rag_exclude", "rag_ignore", "no_rag"]):
                            continue
                        new_fm_lines.append(line)
                    if any(l.strip() for l in new_fm_lines):
                        new_lines = ["---"] + new_fm_lines + ["---"] + lines[end + 1:]
                    else:
                        rest = lines[end + 1:]
                        while rest and not rest[0].strip():
                            rest = rest[1:]
                        new_lines = rest
                else:
                    new_lines = lines
            else:
                new_lines = lines

        path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
        self.sync()
        return {
            "success": True,
            "action": "exempt_file" if exempt else "unexempt_file",
            "source": source_posix,
            "method": "frontmatter",
            "is_exempt": exempt,
        }

    def purge_cache(self) -> bool:
        """Delete this vault's on-disk cache files (used by kb_unregister).

        Returns True when both cache files are gone afterwards. Safe to call
        when caching is disabled (returns False, nothing to purge).
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
        """Drop both cache layers and the in-memory index, then rebuild from scratch."""
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

    _READABLE_SUFFIXES = {".md", ".markdown"}

    def _safe_path(self, source: str) -> Path:
        # kb_read 只能读 Markdown：拒绝任意扩展名，防止把私钥/配置等任意
        # 文件当文本读出（组合 vault_path 注入 = 任意文件读取）。
        if Path(source).suffix.lower() not in self._READABLE_SUFFIXES:
            raise ValueError(f"source must be a Markdown file: {source!r}")
        candidate = (self.vault_path / source.replace("\\", "/")).resolve()
        root = self.vault_path.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("source must stay inside the vault")
        return candidate

    def start_watching(self, interval: float = 0.25, debounce_seconds: float | None = None) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            return
        if self._fs_watcher is not None and self._fs_watcher.is_alive():
            return
        debounce = self.config.debounce_seconds if debounce_seconds is None else debounce_seconds
        self._fs_debounce_seconds = debounce
        self.sync()
        self._watch_stop.clear()
        method = self.config.watch_method
        # auto：平台支持就用原生；native：优先原生（比如想在非 Windows 上显式
        # 表达意图）；两者启动失败都静默退回轮询，监听永不因此失效。
        if method in {"auto", "native"} and (method == "native" or watcher_available()):
            watcher = WindowsDirectoryWatcher(self.vault_path, self._on_fs_events)
            if watcher.start():
                self._fs_watcher = watcher
                self._watch_thread = threading.Thread(
                    target=self._native_watch_loop, args=(interval, debounce), daemon=True, name="vault-watch-native"
                )
                self._watch_thread.start()
                return
            self._fs_watcher = None
        self._watch_thread = threading.Thread(target=self._watch_loop, args=(interval, debounce), daemon=True)
        self._watch_thread.start()

    def _on_fs_events(self, events: list[tuple[int, str]] | None) -> None:
        """原生监听的回调：把「库里有动静」翻译成一次防抖后的全量 sync。

        事件里带哪些路径完全不重要——正确性由 sync() 的全量 sha256 对账兜底，
        这里不解析、不增量处理。events=None（内核缓冲区溢出，具体改动不可知）
        也走同一条路，反正 sync 本来就是全量的。
        """
        with self._fs_debounce_lock:
            now = time.monotonic()
            if self._fs_pending_since is None:
                self._fs_pending_since = now
            # 防抖：事件安静 debounce 秒后才同步；事件持续到达就不断顺延定时器，
            # 但受 _FS_MAX_DEBOUNCE_WAIT 封顶（延迟归零，尽快同步一次）。
            delay = self._fs_debounce_seconds
            if now - self._fs_pending_since >= _FS_MAX_DEBOUNCE_WAIT:
                delay = 0.0
            if self._fs_debounce_timer is not None:
                self._fs_debounce_timer.cancel()
            timer = threading.Timer(delay, self._fs_fire_sync)
            timer.daemon = True
            self._fs_debounce_timer = timer
        timer.start()

    def _fs_fire_sync(self) -> None:
        with self._fs_debounce_lock:
            self._fs_debounce_timer = None
            self._fs_pending_since = None
        if self._watch_stop.is_set():
            return
        try:
            self.sync()
        except Exception:
            pass

    def _native_watch_loop(self, interval: float, debounce: float) -> None:
        """原生监听生效期间的兜底循环，职责有二：

        1. 低频（watch_fallback_interval，默认 30s）全量对账，覆盖原生事件可能
           丢失的极端情况——sync 是全量 sha256 对账，多跑只是白花一点 IO；
        2. 盯住 watcher 线程存活性：一旦它退出（句柄失效等），退回全速轮询，
           监听永不静默失效。
        """
        fallback_interval = self.config.watch_fallback_interval
        while not self._watch_stop.is_set():
            if self._fs_watcher is None or not self._fs_watcher.is_alive():
                break
            if self._watch_stop.wait(fallback_interval if fallback_interval > 0 else interval):
                return
            if self._fs_watcher is None or not self._fs_watcher.is_alive():
                break
            if fallback_interval > 0:
                try:
                    self.sync()
                except Exception:
                    pass
        if self._watch_stop.is_set():
            return
        # 降级：原生线程已退出，退回 0.25s 全速轮询（0.4.1 行为）。
        watcher = self._fs_watcher
        self._fs_watcher = None
        if watcher is not None:
            watcher.stop()
        self._watch_loop(interval, debounce)

    def _watch_loop(self, interval: float, debounce: float) -> None:
        pending_since: float | None = None
        # 基线必须取在 sync 之前：先 sync 后取基线的话，「sync 完成到取基线之间」
        # 落盘的改动会被当成已同步而从此丢失——线程刚启动时这个窗口最大（主线程
        # 往往在 watcher 线程第一次扫描前就写完了文件）。先取基线再 sync，两者
        # 之间出现的改动由随后的 sync 补上，之后的改动才由轮询发现。
        previous = self._quick_signatures()
        self.sync()
        while not self._watch_stop.wait(interval):
            current = self._quick_signatures()
            if current != previous:
                pending_since = pending_since or time.monotonic()
                if time.monotonic() - pending_since >= debounce:
                    self.sync()
                    previous = self._quick_signatures()
                    pending_since = None
            else:
                pending_since = None

    def _quick_signatures(self) -> dict[str, tuple[int, int]]:
        return {self._source(path): (path.stat().st_mtime_ns, path.stat().st_size) for path in self._markdown_files()}

    def stop_watching(self) -> None:
        self._watch_stop.set()
        with self._fs_debounce_lock:
            timer, self._fs_debounce_timer = self._fs_debounce_timer, None
            self._fs_pending_since = None
        if timer is not None:
            timer.cancel()
        watcher, self._fs_watcher = self._fs_watcher, None
        if watcher is not None:
            watcher.stop()
        if self._watch_thread:
            self._watch_thread.join(timeout=2)
            self._watch_thread = None
