"""Per-vault FTS5 (trigram) index used as one RRF route in hybrid search.

The index is a derived, rebuildable side channel of the authoritative in-memory
chunks: it stores a redundant copy of each chunk's content ONLY so SQLite's FTS5
can run BM25 against it. Chunk objects (metadata/title/embedding) never come
from here — results map back by chunk_id.

Trigram tokenizer handles CJK substring matching well (verified on SQLite 3.50+)
but requires >= 3-char queries; shorter queries simply yield no rows, which the
caller handles by skipping this route (the bigram lexical route covers them).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

# journal_mode=MEMORY + synchronous=OFF: the index is derived and rebuildable,
# durability is irrelevant; this buys a big write-speedup on incremental syncs.
_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  source UNINDEXED,
  chunk_id UNINDEXED,
  content,
  tokenize = 'trigram'
);
"""


class FtsIndex:
    """One sqlite db per vault. Thread-safe via an internal RLock (the watcher
    thread syncs while search threads query)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ conn

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=MEMORY")
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute(_SCHEMA)
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    @property
    def available(self) -> bool:
        """True when the connection opened and the fts5 virtual table exists."""
        with self._lock:
            try:
                self._connect()
                row = self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
                ).fetchone()
                return row is not None
            except Exception:
                return False

    def count(self) -> int:
        with self._lock:
            try:
                return int(self._connect().execute("SELECT count(*) FROM chunks_fts").fetchone()[0])
            except Exception:
                return 0

    # ------------------------------------------------------------- mutation

    def upsert_source(self, source: str, chunks: Iterable[Any]) -> None:
        """Replace all rows of one source (delete-by-source then insert)."""
        rows = [(source, chunk.id, chunk.content) for chunk in chunks]
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute("DELETE FROM chunks_fts WHERE source = ?", (source,))
                conn.executemany(
                    "INSERT INTO chunks_fts(source, chunk_id, content) VALUES (?, ?, ?)", rows
                )

    def delete_source(self, source: str) -> None:
        with self._lock:
            try:
                with self._connect():
                    self._connect().execute("DELETE FROM chunks_fts WHERE source = ?", (source,))
            except Exception:
                pass

    def clear(self) -> None:
        with self._lock:
            try:
                with self._connect():
                    self._connect().execute("DELETE FROM chunks_fts")
            except Exception:
                pass

    # --------------------------------------------------------------- query

    @staticmethod
    def _like_prefix(path_prefix: str) -> str:
        """把目录前缀转成 LIKE 用的模式串：转义 % / _ / \\ 三个通配符。

        LIKE 里没有转义符的话，路径中带 _ 的目录名（如 raw_notes/）会匹配到
        任意字符，过滤结果就比预期宽。
        """
        escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return escaped + "%"

    def search(self, match_sql: str, limit: int, path_prefix: str = "") -> list[tuple[str, float]]:
        """BM25 top-N: returns [(chunk_id, bm25_score)] best-first (bm25 ascending).

        path_prefix 非空时把 source 前缀条件下推到 SQL 层（source 是 UNINDEXED
        列，可直接出现在 WHERE 里，零 schema 变更）。这纯粹是减少候选量的优化，
        正确性由调用方的统一后过滤兜底。
        """
        with self._lock:
            conn = self._connect()
            if path_prefix:
                rows = conn.execute(
                    "SELECT chunk_id, bm25(chunks_fts) FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? AND source LIKE ? ESCAPE '\\' "
                    "ORDER BY bm25(chunks_fts) LIMIT ?",
                    (match_sql, self._like_prefix(path_prefix), max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT chunk_id, bm25(chunks_fts) FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                    (match_sql, max(1, int(limit))),
                ).fetchall()
            return [(str(chunk_id), float(score)) for chunk_id, score in rows]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
