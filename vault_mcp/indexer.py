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
from typing import Any, Iterable

from .config import AppConfig
from .fts import FtsIndex
from .providers import EmbeddingProvider, RerankerProvider, create_embedding_provider, create_reranker_provider
from .vector import create_vector_backend

_RRF_K = 60
_RRF_PER_ROUTE = 40

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_ASCII_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_BLOCK_IGNORE_START = re.compile(r"^\s*<!--\s*(?:rag-ignore|rag:ignore|no-rag|norag)\s*-->", re.IGNORECASE)
_BLOCK_IGNORE_END = re.compile(r"^\s*<!--\s*(?:/rag-ignore|/rag:ignore|/no-rag|/norag|end-rag-ignore)\s*-->", re.IGNORECASE)

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


def _pack_str(buf: bytearray, text: str) -> None:
    data = text.encode("utf-8")
    buf += struct.pack("<I", len(data))
    buf += data


def _pack_u32(buf: bytearray, value: int) -> None:
    buf += struct.pack("<I", value)


def _to_emb(vectors: Iterable[float]) -> array:
    return array(_EMB_DTYPE, vectors)


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
        self.failed_files: dict[str, str] = {}
        self.last_sync: float | None = None
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._chunks_cache_path: Path | None = None
        self._vectors_cache_path: Path | None = None
        self._fts_cache_path: Path | None = None
        self._fts: FtsIndex | None = None
        self._vector_backend: Any = None
        if self.config.cache.enabled and self.config.cache.dir:
            try:
                self._init_cache_paths()
            except OSError:
                self._chunks_cache_path = None
                self._vectors_cache_path = None
                self._fts_cache_path = None
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
        # sqlite_vec only when configured AND importable, else falls back to memory.
        self._vector_backend = create_vector_backend(self.config.vector, self, self._fts_cache_path)

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
        self._load_chunks_cache()
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
        return {
            "key": self._cache_key(),
            "chunk_size": self.config.chunk_size,
            "chunk_overlap": self.config.chunk_overlap,
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
        for chunks in self._chunks.values():
            for chunk in chunks:
                vector = vectors.get(chunk.id)
                if vector is not None:
                    chunk.embedding = vector

    def _save_cache(self) -> None:
        """Persist both layers under a single lock; failures degrade gracefully."""
        with self._cache_lock:
            self._save_chunks_cache()
            self._save_vectors_cache()

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
                chunks = self._chunk_file(source, text)
                changed.append((source, signature, chunks))
            except Exception as exc:
                self.failed_files[source] = str(exc)
                self._chunks.pop(source, None)
                self._signatures.pop(source, None)
                self._fts_delete(source)

        # Text layer: changed files update the index even if embedding fails
        # afterwards, so lexical search still works without vectors.
        for source, signature, chunks in changed:
            self._chunks[source] = chunks
            self._signatures[source] = signature
            self.failed_files.pop(source, None)
            self._fts_upsert(source, chunks)

        # Vector layer: embed every chunk that lacks a vector. When the vectors
        # cache was invalidated (model/dimension change) this re-embeds the whole
        # corpus while reusing the text chunks; when only a few files changed it
        # embeds just those chunks.
        self._embed_missing()

        for source in set(self._chunks) - found:
            removed_ids = [chunk.id for chunk in self._chunks.get(source, [])]
            self._chunks.pop(source, None)
            self._signatures.pop(source, None)
            self.failed_files.pop(source, None)
            self._fts_delete(source)
            if removed_ids:
                try:
                    self._vector_backend.delete_vectors(removed_ids)
                except Exception:
                    pass
        self.last_sync = time.time()
        self._save_cache()
        return self.all_chunks()

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

    def _embed_missing(self) -> set[str]:
        """Embed every chunk that has no vector yet.

        Returns the set of sources whose chunks all got vectors. When the vectors
        cache was invalidated (model/dimension change) this re-embeds the whole
        corpus while reusing the text chunks; when only a few files changed it
        embeds just those chunks.
        """
        pending: dict[str, list[Chunk]] = {}
        for source, chunks in self._chunks.items():
            missing = [chunk for chunk in chunks if chunk.embedding is None or not len(chunk.embedding)]
            if missing:
                pending[source] = missing
        if not pending:
            return set(self._chunks)

        if self.config.embedding.mode != "external":
            for source, chunks in pending.items():
                try:
                    vectors = self.embedding_provider.embed([chunk.content for chunk in chunks])
                    for chunk, vector in zip(chunks, vectors):
                        chunk.embedding = _to_emb(vector)
                except Exception as exc:
                    self.failed_files[source] = str(exc)
            return set(self._chunks)

        max_workers = self.config.cache.embedding_max_workers
        tasks = list(pending.items())
        if max_workers <= 1 or len(tasks) <= 1:
            for source, chunks in tasks:
                try:
                    self._embed_one_file(source, chunks, self.embedding_provider)
                except Exception as exc:
                    self.failed_files[source] = str(exc)
            return set(self._chunks)

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
        for source, exc in failures.items():
            self.failed_files[source] = exc
        return set(self._chunks)

    @staticmethod
    def _embed_one_file(source: str, chunks: list[Chunk], provider: EmbeddingProvider) -> None:
        vectors = provider.embed([chunk.content for chunk in chunks])
        for chunk, vector in zip(chunks, vectors):
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

    def _chunk_file(self, source: str, text: str) -> list[Chunk]:
        lines = text.splitlines()
        frontmatter_end, tags, properties = self._frontmatter(lines)
        is_fm_exempt, _ = self._is_frontmatter_exempt(tags, properties)
        if is_fm_exempt:
            return []
        body_start = frontmatter_end + 1
        body = lines[body_start:]
        body, _ = self._strip_ignored_blocks(body)
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
        return self._make_chunks(source, title, tags, sections)

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
                    result.append(self._new_chunk(source, title, heading, current_start, start + offset - 1, chunk_index, tags, current))
                    chunk_index += 1
                    carry, carry_length = self._overlap_tail(current, overlap)
                    current = list(carry)
                    current_start = start + offset - len(carry)
                    current_length = carry_length
                current.append(line)
                current_length += len(line) + 1
            if current:
                result.append(self._new_chunk(source, title, heading, current_start, start + len(lines) - 1, chunk_index, tags, current))
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
    def _new_chunk(source: str, title: str, heading: str, start: int, end: int, index: int, tags: list[str], lines: list[str]) -> Chunk:
        content = "\n".join(lines).strip()
        identifier = hashlib.sha1(f"{source}\0{index}\0{content}".encode("utf-8")).hexdigest()
        return Chunk(identifier, content, source, title, {
            "heading": heading,
            "start_line": start,
            "end_line": max(start, end),
            "chunk_index": index,
            "tags": list(tags),
        })

    def all_chunks(self) -> list[Chunk]:
        return [chunk for source in sorted(self._chunks) for chunk in self._chunks[source]]

    def search(self, query: str, top_k: int = 10, use_rerank: bool = False, query_vector: Iterable[float] | None = None) -> list[Chunk]:
        query = query.strip()
        all_chunks = self.all_chunks()
        if not query:
            return all_chunks[: max(0, top_k)]

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
        semantic_chunks: list[Chunk] = []
        semantic_snapshot: dict[str, float] = {}
        if self.config.embedding.mode == "external":
            try:
                # Callers doing multi-vault fan-out embed the query once and
                # pass it in, so N vaults cost one embed call instead of N.
                if query_vector is None:
                    query_vector = self.embedding_provider.embed([query])[0]
                semantic_chunks = self._semantic_rank(query_vector, all_chunks)
                semantic_snapshot = {chunk.id: chunk.score for chunk in semantic_chunks}
            except Exception:
                pass

        hybrid = self.config.use_hybrid and self._fts is not None and self._fts.available
        if hybrid:
            ranked = self._hybrid_rank(query, all_chunks, lexical, semantic_snapshot)
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

        if use_rerank and self.reranker_provider and ranked:
            ranked = rerank_chunks(query, ranked, self.reranker_provider)
        return ranked[: max(0, top_k)]

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
                routes.append([chunk_id for chunk_id, _score in self._fts.search(fts_sql, _RRF_PER_ROUTE)])
            except Exception:
                pass

        # Route B: vector cosine, raw and descending.
        if semantic_snapshot:
            ordered = sorted(semantic_snapshot.items(), key=lambda item: -item[1])
            routes.append([chunk_id for chunk_id, _score in ordered[:_RRF_PER_ROUTE]])

        # Route C: bigram lexical soft scores, descending, score > 0 only.
        lexical_ordered = sorted(
            ((chunk_id, score) for chunk_id, score in lexical.items() if score > 0),
            key=lambda item: -item[1],
        )
        if lexical_ordered:
            routes.append([chunk_id for chunk_id, _score in lexical_ordered[:_RRF_PER_ROUTE]])

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
            for cache_file in (self._chunks_cache_path, self._vectors_cache_path):
                if cache_file is None:
                    continue
                try:
                    if cache_file.exists():
                        cache_file.unlink()
                except OSError:
                    removed = False
            self._chunks_cache_path = None
            self._vectors_cache_path = None
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
            for cache_file in (self._chunks_cache_path, self._vectors_cache_path, self._fts_cache_path):
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
        debounce = self.config.debounce_seconds if debounce_seconds is None else debounce_seconds
        self.sync()
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, args=(interval, debounce), daemon=True)
        self._watch_thread.start()

    def _watch_loop(self, interval: float, debounce: float) -> None:
        pending_since: float | None = None
        previous = self._quick_signatures()
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
        if self._watch_thread:
            self._watch_thread.join(timeout=2)
            self._watch_thread = None
