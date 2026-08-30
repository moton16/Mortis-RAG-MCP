"""Vector storage abstraction seam.

Two backends behind one Protocol:

- `memory` (default): vectors live on Chunk.embedding in RAM, persisted in the
  .vec.bin cache exactly as before. Zero behavior change.
- `sqlite_vec` (optional, opt-in): vectors live on disk in a vec0 (sqlite-vec)
  table inside a per-vault sqlite file. Chunk.embedding is NOT retained in RAM
  (`on_disk = True`), which is what actually cuts resident memory: at 13k
  chunks x 1024 dims that is ~55MB freed. Activated only when the user sets
  `[vector] backend = "sqlite_vec"` AND `sqlite_vec` is importable; any
  failure falls back to the memory backend.

The indexer drives both through the same Protocol, so swapping is a config
change, not a code change.
"""

from __future__ import annotations

import sqlite3
from array import array
from typing import Any, Iterable, Protocol, runtime_checkable

from .config import VectorConfig


@runtime_checkable
class VectorBackend(Protocol):
    name: str
    # True when vectors are NOT held in Chunk.embedding (disk-backed storage).
    on_disk: bool

    def query(self, query_vector: Iterable[float], limit: int | None = None) -> list[tuple[str, float]]:
        """Return [(chunk_id, cosine_similarity)] best-first (descending).
        limit=None means "all available"."""
        ...

    def upsert_vectors(self, vectors: dict[str, Any]) -> None:
        """Store/replace vectors keyed by chunk_id. None-safe."""
        ...

    def get_vectors(self, chunk_ids: Iterable[str]) -> dict[str, Any]:
        """Read back stored vectors: {chunk_id: array('f')}.

        Used by the indexer's content-hash reuse round, which needs the vector of
        an already-embedded chunk to hand to a duplicate chunk with a different
        id. Missing/failed ids are simply absent from the result — never raise.
        """
        ...

    def delete_vectors(self, chunk_ids: Iterable[str]) -> None:
        """Remove vectors for the given chunk ids (missing ids are ignored)."""
        ...

    def purge(self) -> None:
        """Drop all stored vectors (cache purge / unregister)."""
        ...

    def list_ids(self) -> list[str]:
        """All chunk ids currently stored (for disk-side bookkeeping)."""
        ...


class MemoryVectorBackend:
    """Wraps the existing in-memory brute-force semantic ranking.

    Vectors live on Chunk.embedding and are persisted in the .vec.bin cache by
    the indexer; upsert/delete/purge are therefore no-ops here — the in-memory
    chunk state IS the storage.
    """

    name = "memory"
    on_disk = False

    def __init__(self, indexer: Any) -> None:
        self._indexer = indexer

    def query(self, query_vector: Iterable[float], limit: int | None = None) -> list[tuple[str, float]]:
        ranked = self._indexer._semantic_rank(query_vector, self._indexer.all_chunks())
        ranked.sort(key=lambda chunk: -chunk.score)
        if limit is None:
            return [(chunk.id, chunk.score) for chunk in ranked]
        return [(chunk.id, chunk.score) for chunk in ranked[: max(0, limit)]]

    def upsert_vectors(self, vectors: dict[str, Any]) -> None:
        return None

    def get_vectors(self, chunk_ids: Iterable[str]) -> dict[str, Any]:
        wanted = {str(chunk_id) for chunk_id in chunk_ids}
        out: dict[str, Any] = {}
        if not wanted:
            return out
        for chunk in self._indexer.all_chunks():
            if chunk.id in wanted and chunk.embedding is not None and len(chunk.embedding):
                out[chunk.id] = chunk.embedding
        return out

    def delete_vectors(self, chunk_ids: Iterable[str]) -> None:
        return None

    def purge(self) -> None:
        return None

    def list_ids(self) -> list[str]:
        return []


class SqliteVecBackend:
    """Optional disk-backed sqlite-vec backend (NOT default).

    Vectors are stored in a vec0 virtual table (cosine) inside a per-vault
    sqlite file. rowid is an integer id mapped via a `vec_ids` table so sha1
    chunk ids never collide. `on_disk = True` tells the indexer to skip
    retaining embeddings in RAM.
    """

    name = "sqlite_vec"
    on_disk = True

    def __init__(self, indexer: Any, db_path: Any) -> None:
        self._indexer = indexer
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._serialize = None
        self.available = False
        try:
            import sqlite_vec  # type: ignore

            self._serialize = sqlite_vec.serialize_float32
            dim = int(indexer.config.embedding.dimension)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS vec_ids("
                "vid INTEGER PRIMARY KEY AUTOINCREMENT, chunk_id TEXT NOT NULL UNIQUE)"
            )
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec0_chunks USING vec0("
                f"embedding float[{dim}] distance_metric=cosine)"
            )
            self._conn = conn
            self.available = True
        except Exception:
            self._conn = None

    @property
    def path(self) -> Any:
        return self._db_path

    def _vid_for(self, chunk_id: str) -> int | None:
        row = self._conn.execute("SELECT vid FROM vec_ids WHERE chunk_id = ?", (chunk_id,)).fetchone()
        return int(row[0]) if row else None

    def query(self, query_vector: Iterable[float], limit: int | None = None) -> list[tuple[str, float]]:
        if not self.available or self._conn is None:
            return []
        try:
            payload = self._serialize(query_vector)
            k = max(1, int(limit)) if limit is not None else 1000000
            rows = self._conn.execute(
                "SELECT v.chunk_id, d.distance FROM vec0_chunks d JOIN vec_ids v ON v.vid = d.rowid "
                "WHERE d.embedding MATCH ? AND k = ? ORDER BY d.distance",
                (payload, k),
            ).fetchall()
            # cosine distance -> cosine similarity (unit-normalized float32)
            return [(str(chunk_id), 1.0 - float(distance)) for chunk_id, distance in rows]
        except Exception:
            return []

    def upsert_vectors(self, vectors: dict[str, Any]) -> None:
        if not self.available or self._conn is None or not vectors:
            return
        try:
            rows = [(chunk_id, self._serialize(vec)) for chunk_id, vec in vectors.items() if vec is not None]
            if not rows:
                return
            with self._conn:
                # Batch in three statements instead of N round-trips.
                self._conn.executemany(
                    "INSERT OR IGNORE INTO vec_ids(chunk_id) VALUES (?)",
                    [(chunk_id,) for chunk_id, _ in rows],
                )
                id_map = {
                    str(chunk_id): vid
                    for chunk_id, vid in self._conn.execute(
                        "SELECT chunk_id, vid FROM vec_ids WHERE chunk_id IN (%s)"
                        % ",".join("?" * len(rows)),
                        [chunk_id for chunk_id, _ in rows],
                    ).fetchall()
                }
                self._conn.executemany(
                    "INSERT OR REPLACE INTO vec0_chunks(rowid, embedding) VALUES (?, ?)",
                    [(id_map[chunk_id], payload) for chunk_id, payload in rows if chunk_id in id_map],
                )
        except Exception:
            pass

    def get_vectors(self, chunk_ids: Iterable[str]) -> dict[str, Any]:
        """Read back vectors from the vec0 table (content-hash reuse round).

        A vec0 column selected normally comes back as the raw float32 blob it was
        stored as, so it can be rebuilt with array('f').frombytes. Anything that
        doesn't decode is skipped: the caller just re-embeds those chunks.
        """
        out: dict[str, Any] = {}
        if not self.available or self._conn is None:
            return out
        try:
            for chunk_id in chunk_ids:
                vid = self._vid_for(chunk_id)
                if vid is None:
                    continue
                try:
                    row = self._conn.execute(
                        "SELECT embedding FROM vec0_chunks WHERE rowid = ?", (vid,)
                    ).fetchone()
                    if row is None or row[0] is None:
                        continue
                    vector = array("f")
                    vector.frombytes(row[0])
                    if len(vector):
                        out[str(chunk_id)] = vector
                except Exception:
                    continue
        except Exception:
            return out
        return out

    def delete_vectors(self, chunk_ids: Iterable[str]) -> None:
        if not self.available or self._conn is None:
            return
        try:
            with self._conn:
                for chunk_id in chunk_ids:
                    vid = self._vid_for(chunk_id)
                    if vid is None:
                        continue
                    self._conn.execute("DELETE FROM vec0_chunks WHERE rowid = ?", (vid,))
                    self._conn.execute("DELETE FROM vec_ids WHERE vid = ?", (vid,))
        except Exception:
            pass

    def list_ids(self) -> list[str]:
        if not self.available or self._conn is None:
            return []
        try:
            return [str(row[0]) for row in self._conn.execute("SELECT chunk_id FROM vec_ids").fetchall()]
        except Exception:
            return []

    def count(self) -> int:
        if not self.available or self._conn is None:
            return 0
        try:
            return int(self._conn.execute("SELECT count(*) FROM vec_ids").fetchone()[0])
        except Exception:
            return 0

    def purge(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        try:
            if self._db_path is not None and self._db_path.exists():
                self._db_path.unlink()
        except OSError:
            pass


def create_vector_backend(config: VectorConfig, indexer: Any, db_path: Any = None) -> VectorBackend:
    """Pick backend by config; sqlite_vec failures fall back to memory."""
    if config.backend == "sqlite_vec":
        backend = SqliteVecBackend(indexer, db_path)
        if backend.available:
            return backend
    return MemoryVectorBackend(indexer)
