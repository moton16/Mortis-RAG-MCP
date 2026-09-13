"""临时诊断：在 CI 上打印零读盘用例的关键时序量。用完即删。"""
import time
from pathlib import Path
from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def test_diag_timings(tmp_path):
    note = tmp_path / "fast_stat_test.md"
    note.write_text("# Fast Stat\nInitial content for testing fast stat.", encoding="utf-8")
    ix = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    ix.sync()
    m = note.stat().st_mtime_ns
    seen = ix._stat_seen_ns["fast_stat_test.md"]
    now = time.time_ns()
    print(f"\n[DIAG] mtime={m}")
    print(f"[DIAG] seen ={seen}  (seen-mtime={seen - m})")
    print(f"[DIAG] now  ={now}  (now-mtime={now - m})")
    print(f"[DIAG] trustworthy(mtime<seen)? {m < seen}")
    print(f"[DIAG] stat_cache={ix._stat_cache}")

    calls = []
    orig = Path.read_bytes
    Path.read_bytes = lambda self: (calls.append(str(self)), orig(self))[1]
    try:
        ix.sync()
    finally:
        Path.read_bytes = orig
    print(f"[DIAG] second sync reads={len(calls)}")
    print(f"[DIAG] seen after 2nd={ix._stat_seen_ns.get('fast_stat_test.md')}")
    assert False, "DIAG"
