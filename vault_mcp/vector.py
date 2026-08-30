"""Vector storage abstraction seam.

Default backend is `memory` — the existing numpy brute-force cosine over
Chunk.embedding, with vectors persisted exactly as today (.vec.bin). The seam
exists so a future backend (e.g. sqlite-vec) can slot in without touching the
indexer's search/sync code paths. sqlite-vec support is optional and gated
behind import success; any failure falls back to memory.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

from .config import VectorConfig


@runtime_checkable
class VectorBackend(Protocol):
    name: str

    def query(self, query_vector: Iterable[float], limit: int) -> list[tuple[str, float]]:
        """Return [(chunk_id, cosine_similarity)] best-first (descending)."""
        ...

    def upsert_vectors(self, vectors: dict[str, Any]) -> None:
        """Store/replace vectors keyed by chunk_id. None-safe."""
        ...

    def delete_vectors(self, chunk_ids: Iterable[str]) -> None:
        """Remove vectors for the given chunk ids (missing ids are ignored)."""
        ...

    def purge(self) -> None:
        """Drop all stored vectors (cache purge / unregister)."""
        ...


class MemoryVectorBackend:
    """Wraps the existing in-memory brute-force semantic ranking.

    Vectors already live on Chunk.embedding and are persisted in the .vec.bin
    cache by the indexer; upsert/delete/purge are therefore no-ops here — the
    in-memory chunk state IS the storage.
    """

    name = "memory"

    def __init__(self, indexer: Any) -> None:
        self._indexer = indexer

    def query(self, query_vector: Iterable[float], limit: int) -> list[tuple[str, float]]:
        ranked = self._indexer._semantic_rank(query_vector, self._indexer.all_chunks())
        ranked.sort(key=lambda chunk: -chunk.score)
        return [(chunk.id, chunk.score) for chunk in ranked[: max(0, limit)]]

    def upsert_vectors(self, vectors: dict[str, Any]) -> None:
        return None

    def delete_vectors(self, chunk_ids: Iterable[str]) -> None:
        return None

    def purge(self) -> None:
        return None


class SqliteVecBackend:
    """Optional sqlite-vec backend (NOT default).

    Only active when the user sets [vector] backend = "sqlite_vec" AND the
    `sqlite_vec` package is importable (pip install sqlite-vec). On any failure
    `available` is False and the factory falls back to MemoryVectorBackend.
    Minimal implementation: vec0 table per vault, upsert-by-chunk_id on sync,
    KNN query on search. Not the focus of this round.
    """

    name = "sqlite_vec"

    def __init__(self, indexer: Any, db_path: Any) -> None:
        self._indexer = indexer
        self._db_path = db_path
        self._conn = None
        self.available = False
        try:
            import sqlite_vec  # type: ignore

            self._module = sqlite_vec
            import sqlite3

            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            dim = int(indexer.config.embedding.dimension)
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec0_chunks USING vec0("
                f"embedding float[{dim}] distance_metric=cosine)"
            )
            self._conn = conn
            self.available = True
        except Exception:
            self._conn = None

    def query(self, query_vector: Iterable[float], limit: int) -> list[tuple[str, float]]:
        if not self.available or self._conn is None:
            return []
        try:
            payload = str(list(query_vector)).replace(" ", "")
            rows = self._conn.execute(
                "SELECT rowid, distance FROM vec0_chunks WHERE embedding MATCH ? "
                "AND k = ? ORDER BY distance",
                (payload, max(1, int(limit))),
            ).fetchall()
            # cosine distance -> cosine similarity
            return [(str(rowid), 1.0 - float(distance)) for rowid, distance in rows]
        except Exception:
            return []

    def upsert_vectors(self, vectors: dict[str, Any]) -> None:
        if not self.available or self._conn is None or not vectors:
            return
        try:
            rows = [(chunk_id, str(list(vec)).replace(" ", "")) for chunk_id, vec in vectors.items() if vec is not None]
            if not rows:
                return
            with self._conn:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO vec0_chunks(rowid, embedding) VALUES (?, ?)", rows
                )
        except Exception:
            pass

    def delete_vectors(self, chunk_ids: Iterable[str]) -> None:
        if not self.available or self._conn is None:
            return
        try:
            with self._conn:
                for chunk_id in chunk_ids:
                    self._conn.execute("DELETE FROM vec0_chunks WHERE rowid = ?", (chunk_id,))
        except Exception:
            pass

    def purge(self) -> None:
        if self._conn is not None:
            try:
                with self._conn:
                    self._conn.execute("DELETE FROM vec0_chunks")
            except Exception:
                pass


def create_vector_backend(config: VectorConfig, indexer: Any, db_path: Any = None) -> VectorBackend:
    """Pick backend by config; sqlite_vec failures fall back to memory."""
    if config.backend == "sqlite_vec":
        backend = SqliteVecBackend(indexer, db_path)
        if backend.available:
            return backend
    return MemoryVectorBackend(indexer)
