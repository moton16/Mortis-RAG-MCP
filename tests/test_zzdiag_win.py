"""临时诊断：把关键时序量塞进断言消息，确保 CI 日志一定能看到。用完即删。"""
import time
from pathlib import Path
from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def test_diag_timings(tmp_path):
    note = tmp_path / "fast_stat_test.md"
    note.write_text("# Fast Stat\nInitial content for testing fast stat.", encoding="utf-8")
    ix = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))

    # 反复跑，统计 seen 与 mtime 的相对次序，量化时序碰撞概率
    stats = []
    for i in range(30):
        n2 = tmp_path / f"f{i}.md"
        n2.write_text("# X\ncontent", encoding="utf-8")
        ix2 = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
        ix2.sync()
        m = n2.stat().st_mtime_ns
        s = ix2._stat_seen_ns.get(f"f{i}.md")
        stats.append("NA" if s is None else str(m < s))

    ix.sync()
    m = note.stat().st_mtime_ns
    seen = ix._stat_seen_ns["fast_stat_test.md"]
    calls = []
    orig = Path.read_bytes
    Path.read_bytes = lambda self: (calls.append(str(self)), orig(self))[1]
    try:
        ix.sync()
    finally:
        Path.read_bytes = orig

    raise AssertionError(
        f"DIAG mtime={m} seen={seen} seen-mtime={seen-m} "
        f"trust={m < seen} reads2nd={len(calls)} "
        f"racycount={stats.count('False')}/{len(stats)} pattern={''.join('T' if x=='True' else 'F' if x=='False' else '?' for x in stats)}"
    )
