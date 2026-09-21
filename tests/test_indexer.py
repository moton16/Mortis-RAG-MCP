from __future__ import annotations

import os
import time
from pathlib import Path

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def test_markdown_chunks_include_heading_lines_index_and_tags(tmp_path):
    note = tmp_path / "中文笔记.md"
    note.write_text(
        "---\ntags: [python, rag]\n---\n# 标题\n第一段\n第二段\n\n## 小节\n第三段\n",
        encoding="utf-8",
    )
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8)))

    chunks = indexer.sync()

    assert len(chunks) >= 2
    first = chunks[0]
    assert first.source == "中文笔记.md"
    assert first.title == "标题"
    assert first.metadata["heading"] == "标题"
    assert first.metadata["start_line"] == 4
    assert first.metadata["end_line"] >= first.metadata["start_line"]
    assert first.metadata["chunk_index"] == 0
    assert first.metadata["tags"] == ["python", "rag"]
    assert first.id


def test_incremental_add_modify_delete_and_rename(tmp_path):
    old = tmp_path / "old.md"
    old.write_text("# Old\nold content", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))

    indexer.sync()
    assert any(chunk.source == "old.md" and "old content" in chunk.content for chunk in indexer.search("old"))

    time.sleep(0.02)  # 保证在 Windows NTFS mtime 精度内产生可区分的时间戳变动
    old.write_text("# New\nnew content updated", encoding="utf-8")
    indexer.sync()
    # 修改后旧内容必须从索引移除（增量更新），而非仍然可召回。
    assert not any("old content" in chunk.content for chunk in indexer.all_chunks())
    assert any("new content updated" in chunk.content for chunk in indexer.search("new"))

    renamed = tmp_path / "重命名.md"
    old.rename(renamed)
    indexer.sync()
    assert not any(chunk.source == "old.md" for chunk in indexer.all_chunks())
    assert any(chunk.source == "重命名.md" for chunk in indexer.all_chunks())

    renamed.unlink()
    indexer.sync()
    assert not indexer.all_chunks()


def test_incremental_evicts_stale_when_mtime_does_not_advance(tmp_path):
    """等长内容替换 + 时间戳未推进时，增量同步必须仍然淘汰旧 chunk。

    回归测试：快速路径曾用 (mtime_ns, size) 相等短路 sha256，但文件时间戳并非
    真纳秒（NTFS 实测刻度约 3ms，FAT32 达 2s）。等长替换（"old content" ->
    "new content"，均 18 字节）若落在同一刻度内，mtime_ns 与 size 双双不变，
    文件被误判为未修改，旧 chunk 静默残留并继续被召回（Windows CI 稳定复现；
    ext4 纳秒精度掩盖了它）。

    可信判据（_fast_path_is_trustworthy）要求 mtime 严格早于「内容级验证时刻
    seen_ns 减去安全余量」才信任签名。真实场景里，racily clean 状态就是「签名
    匹配，但 mtime 距 seen_ns 不足余量」——等长替换发生在时间戳同一刻度内时，
    磁盘读到的 (mtime_ns, size) 与登记签名逐位相同，唯有可信度判据能拦截。

    构造方式：先完成一次正常 sync，把 _stat_seen_ns["old.md"] 压到与笔记 mtime
    相同（模拟「验证时刻与写入时刻落在同一刻度」的 racily clean 状态，即余量
    条件不成立），再执行等长内容替换并把 mtime 恢复原值。

    换道说明（C29 起签名升级为三元组）：POSIX 下 os.utime 会推进 ctime，因此
    本用例在 Linux 上由「ctime 签名失配」这条通道检出，在 Windows 上（st_ctime
    是创建时间、无鉴别力）才回落到可信度判据。两条通道任一生效都算通过；本用例
    的断言只钉死「变化必须被检出」这个结果，不断言是哪条通道检出的。

    本用例是这条路径的**唯一守卫**：上游 test_incremental_add_modify_delete_and_rename
    自 799d4d6 起已改为「sleep 0.02 + 不等长内容」绕开该组合——缺陷当时并未消失，
    只是那个用例不再踩到它。「等长 + 同刻度」这一组只剩这里覆盖，请勿误以为上游
    那个用例仍在守这条路径，也不要为了让它变绿而弱化本用例。
    """
    import os

    note = tmp_path / "old.md"
    note.write_text("# Old\nold content", encoding="utf-8")
    indexer = MarkdownIndexer(
        tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4))
    )
    indexer.sync()
    assert any("old content" in chunk.content for chunk in indexer.all_chunks())
    assert "old.md" in indexer._stat_seen_ns

    # 把可信时刻压到与 mtime 相同：mtime < seen - margin 显然为假，复现
    # 「时间戳粒度碰撞导致签名不可信」的 racily clean 状态。
    mtime_ns = int(note.stat().st_mtime_ns)
    indexer._stat_seen_ns["old.md"] = mtime_ns
    assert indexer._fast_path_is_trustworthy("old.md", mtime_ns) is False, (
        "本用例前提：该条目必须被判定为不可信（racily clean），才具备鉴别力"
    )

    before = note.stat()
    note.write_text("# New\nnew content", encoding="utf-8")
    # 等长内容 + mtime 回拨：新一轮 (mtime_ns, size) 与登记签名逐位相同。
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = note.stat()
    assert after.st_size == before.st_size, "本用例前提：内容等长"
    assert after.st_mtime_ns == before.st_mtime_ns, "本用例前提：时间戳未推进"
    cached = indexer._stat_cache["old.md"]
    assert cached[:2] == (
        int(after.st_mtime_ns),
        int(after.st_size),
    ), "本用例前提：快速路径的 mtime+size 那两位逐位相同（Windows 下三元组整体相同）"

    indexer.sync()
    assert not any("old content" in chunk.content for chunk in indexer.all_chunks())
    assert any("new content" in chunk.content for chunk in indexer.all_chunks())

    # 复核检出后，条目按本轮验证时刻重新登记；等它老化超过余量后应恢复可信
    # （零读盘快速路径），验证「不可信」只是临时状态而非永久惩罚。
    seen_now = indexer._stat_seen_ns["old.md"]
    indexer._stat_seen_ns["old.md"] = seen_now + 2 * 50_000_000
    assert indexer._fast_path_is_trustworthy(
        "old.md", int(note.stat().st_mtime_ns)
    ) is True, "老化超过余量后条目应恢复可信"


def _fast_sig_of(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return (int(st.st_mtime_ns), int(st.st_size), int(st.st_ctime_ns))


def test_probe_mtime_tick_ns_infers_granularity_from_samples():
    """刻度探测：只允许高估、不允许低估（低估才会漏检）。

    样本 = 某刻度下的时间戳乘某个整数时，探测结果必须是该刻度的整数倍
    （即 >= 真实刻度）；样本不足两个时返回 None，调用方退回固定余量。
    """
    from mortis_rag_mcp.indexer import _probe_mtime_tick_ns

    coarse = 2_000_000_000  # FAT32 典型刻度 2s
    samples = [coarse * 1000, coarse * 1001, coarse * 1004, coarse * 1011]
    tick = _probe_mtime_tick_ns(samples)
    assert tick is not None
    assert tick % coarse == 0, f"低估了刻度：{tick} 不是 {coarse} 的整数倍"
    assert tick >= coarse

    # 真纳秒精度：gcd 退化为极小值，余量由下限兜住
    fine = _probe_mtime_tick_ns([1_789_654_078_278_941_800, 1_789_654_078_278_941_801])
    assert fine == 1

    # 样本不足 -> None（调用方退回固定余量，不是悄悄用了个假刻度）
    assert _probe_mtime_tick_ns([1_789_654_078_278_941_800]) is None
    assert _probe_mtime_tick_ns([]) is None


def test_fast_path_disabled_on_coarse_timestamps_detects_equal_length_replacement(tmp_path, monkeypatch):
    """粗刻度文件系统（FAT32/exFAT/部分 SMB）：快速路径必须整体禁用（fail-closed）。

    复现路径（本分支修复前会漏检）：条目已老化到可信（seen-mtime > 余量），
    磁盘刻度粗于余量时，登记之后落在同一刻度内的等长替换会让 (mtime, size) 逐位
    不变——签名相等 + 判据为真 -> 直接 continue -> 旧 chunk 静默残留。

    修复方式是把余量从探测到的真实刻度推导，并在刻度粗于安全下限时禁用快速路径。
    断言：探测到 2s 刻度后① 快速路径被标记禁用；② 同刻度等长替换仍被检出。
    """
    from mortis_rag_mcp import indexer as indexer_module

    note = tmp_path / "coarse.md"
    note.write_text("# Coarse\nold content", encoding="utf-8")
    indexer = MarkdownIndexer(
        tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4))
    )
    indexer.sync()
    assert any("old content" in c.content for c in indexer.all_chunks())

    # 模拟「库在某粗刻度文件系统上」：探测返回 2s
    monkeypatch.setattr(indexer_module, "_probe_mtime_tick_ns", lambda samples: 2_000_000_000)
    indexer.sync()
    assert indexer._fast_path_disabled_reason, "探测到粗刻度必须禁用快速路径（fail-closed）"
    assert indexer._mtime_tick_ns == 2_000_000_000

    # 把条目伪造成「已老化到可信」——这正是旧实现里被判为可信、从而漏检的状态
    source = list(indexer._stat_seen_ns)[0]
    indexer._stat_seen_ns[source] = time.time_ns() + 10**12
    assert indexer._fast_path_is_trustworthy(source, int(note.stat().st_mtime_ns)) is False, (
        "快速路径已禁用时，任何条目都不得被信任"
    )

    before = note.stat()
    note.write_text("# Coarse\nnew content", encoding="utf-8")  # 等长
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))   # 同刻度（时间戳未推进）
    after = note.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns

    indexer.sync()
    assert not any("old content" in c.content for c in indexer.all_chunks()), (
        "粗刻度 + 同刻度等长替换被漏检：快速路径没有被正确禁用"
    )
    assert any("new content" in c.content for c in indexer.all_chunks())


def test_fast_path_signature_includes_ctime_so_metadata_channel_is_not_blind(tmp_path, monkeypatch):
    """签名三元组必须含 st_ctime_ns：它是「mtime 被显式回拨」路径的鉴别通道。

    在 POSIX 上 ctime 由内核维护、os.utime 改不回去，因此回拨 mtime 会让签名失配。
    本用例用打桩 stat 复刻「内容与 mtime/size 都逐位相同、只有 ctime 变了」这一
    通道可用的场景，断言快速路径失配并读到新内容——即该通道确实参与签名比对。
    """
    note = tmp_path / "ctime.md"
    note.write_text("# Ctime\nold content", encoding="utf-8")
    indexer = MarkdownIndexer(
        tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4))
    )
    indexer.sync()
    source = list(indexer._stat_cache)[0]

    # 签名是三元组，第三位是 ctime
    cached = indexer._stat_cache[source]
    assert len(cached) == 3, "签名必须是 (mtime_ns, size, ctime_ns) 三元组"
    assert cached[2] == int(note.stat().st_ctime_ns)

    # 模拟「内容等长替换 + mtime 回拨到原值 + ctime 被内核推进」（POSIX 语义）
    before = note.stat()
    note.write_text("# Ctime\nnew content", encoding="utf-8")
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    real_stat = Path.stat

    class _FakeStat:
        def __init__(self, inner, ctime_ns):
            self._inner = inner
            self.st_ctime_ns = ctime_ns

        def __getattr__(self, item):
            return getattr(self._inner, item)

    def fake_stat(self, *args, **kwargs):
        inner = real_stat(self, *args, **kwargs)
        if self.name == "ctime.md":
            return _FakeStat(inner, inner.st_ctime_ns + 1)
        return inner

    monkeypatch.setattr(Path, "stat", fake_stat)
    indexer.sync()
    monkeypatch.undo()

    assert _fast_sig_of(note)[:2] == (
        int(before.st_mtime_ns),
        int(before.st_size),
    ), "本用例前提：mtime 与 size 逐位未变（回拨成功）"
    assert not any("old content" in c.content for c in indexer.all_chunks()), (
        "ctime 变了却没检出：签名没有把 ctime 纳入比对"
    )
    assert any("new content" in c.content for c in indexer.all_chunks())


def test_future_mtime_entry_regains_zero_read_after_bounded_rechecks(tmp_path, monkeypatch):
    """mtime 停在未来（时基偏移）：不得永久失去零读盘快速路径，但必须有上限与告警。

    修复前实测：该文件每轮 sync 都被重读+重哈希，连续三轮读盘次数均为 1，永不
    自愈。现在改为「跨墙钟复核次数上限 + 告警」：上限内仍每轮精确校验，超过上限
    且签名始终未变才接受稳定并恢复零读盘，同时在 fast_path_warnings 留痕。
    """
    from mortis_rag_mcp.indexer import _FUTURE_MTIME_RECHECK_LIMIT

    note = tmp_path / "future.md"
    note.write_text("# Future\nfuture mtime", encoding="utf-8")
    indexer = MarkdownIndexer(
        tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4))
    )
    indexer.sync()

    future = time.time_ns() + 2 * 3600 * 10**9
    os.utime(note, ns=(future, future))
    assert _fast_sig_of(note)[0] > time.time_ns()

    reads = []
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda self, *a, **k: (reads.append(str(self)) if self.resolve() == note.resolve() else None, real_read_bytes(self, *a, **k))[1],
    )

    # 上限内：每轮都必须精确校验（宁可多读不可漏检），复核计数跨墙钟累计
    for _ in range(_FUTURE_MTIME_RECHECK_LIMIT):
        time.sleep(0.002)
        indexer.sync()
    assert len(reads) == _FUTURE_MTIME_RECHECK_LIMIT, (
        f"上限内的每轮都必须强制读盘复核，实际读了 {len(reads)} 次"
    )
    baseline = len(reads)

    # 跨墙钟复核够次数后：接受稳定、恢复零读盘
    for _ in range(4):
        time.sleep(0.002)
        indexer.sync()
    assert len(reads) == baseline, (
        f"mtime 在未来的条目仍未恢复零读盘：又多读了 {len(reads) - baseline} 次"
    )
    source = list(indexer._stat_cache)[0]
    assert source in indexer.fast_path_warnings, "接受稳定条目时必须留下告警"
    assert "mtime" in indexer.fast_path_warnings[source]
    assert indexer._stat_confirmations[source] > _FUTURE_MTIME_RECHECK_LIMIT

    # 恢复信任之后仍然要能检出真实改动（不是无声豁免）
    note.write_text("# Future\nfuture mtime CHANGED", encoding="utf-8")
    os.utime(note, ns=(future, future))  # 保持「mtime 仍在未来」这一状态
    assert _fast_sig_of(note)[0] > time.time_ns()
    indexer.sync()
    assert any("CHANGED" in c.content for c in indexer.all_chunks())


def test_search_returns_structured_chunk_fields_and_read_returns_raw_lines(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Heading\nline one\nline two\nline three\n", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    indexer.sync()

    results = indexer.search("line two")
    assert results
    chunk = results[0]
    payload = chunk.to_dict()
    assert payload["source"] == "note.md"
    assert payload["metadata"]["heading"] == "Heading"
    assert payload["metadata"]["start_line"] == 1
    assert payload["metadata"]["end_line"] == 4
    assert payload["id"]
    assert indexer.read("note.md", 2, 3) == "line one\nline two"


def test_ignores_obsidian_temp_and_non_markdown_files(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "ignored.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "draft.tmp.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"ignored")
    (tmp_path / "ok.md").write_text("# OK\nkept", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))

    indexer.sync()

    assert [chunk.source for chunk in indexer.all_chunks()] == ["ok.md"]


def test_search_reranker_failure_keeps_lexical_results(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Heading\nneedle text", encoding="utf-8")

    class BrokenReranker:
        def rerank(self, query, documents):
            raise OSError("offline")

    indexer = MarkdownIndexer(
        tmp_path,
        AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)),
        reranker_provider=BrokenReranker(),
    )
    indexer.sync()

    assert indexer.search("needle", use_rerank=True)[0].source == "note.md"


def test_table_atomic_guard_in_chunker(tmp_path):
    note = tmp_path / "table_note.md"
    note.write_text(
        "# Real Heading\n"
        "Introduction\n"
        "<table>\n"
        "<tr><td># Fake Heading In Table</td></tr>\n"
        "<tr><td>data row 1</td></tr>\n"
        "<tr><td>data row 2</td></tr>\n"
        "</table>\n"
        "After table text\n",
        encoding="utf-8",
    )
    indexer = MarkdownIndexer(tmp_path, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=4)))
    chunks = indexer.sync()

    # The fake heading inside table must NOT become a chunk's heading or title
    headings = {c.metadata.get("heading") for c in chunks}
    assert "# Fake Heading In Table" not in headings
    assert "Fake Heading In Table" not in headings
    assert "Real Heading" in headings

    # The entire table block is preserved intact in chunk content
    table_chunks = [c for c in chunks if "<table>" in c.content]
    assert len(table_chunks) == 1
    assert "</table>" in table_chunks[0].content
    assert "data row 1" in table_chunks[0].content

