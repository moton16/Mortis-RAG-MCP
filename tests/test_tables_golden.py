"""split_table_into_chunks 字节级金标准测试（v0.8.0 Phase 1 门禁，用户裁决 D8）。

契约：精简后的实现必须与 v0.7.3 原实现（tests/golden/legacy_split_table_impl.py
逐字快照）在全部语料上产出**完全一致**的 ``(start, end, chunk_lines)`` 列表。

任何差异 = chunk 正文变化 = chunk.id 变化 = 存量用户全库重新切块 + 全量重嵌
（付费 embedding）——直接 FAIL，禁止合并。
"""
from __future__ import annotations

from mortis_rag_mcp.ingest.tables import split_table_into_chunks
from tests.golden.legacy_split_table_impl import legacy_split_table_into_chunks

# 语料覆盖 split_table_into_chunks 的全部分支：
# 常规表头表 / 极小预算（100 下限）/ 单行多 row（D5）/ 单行多 row 超预算 /
# 超长 td 切片（DOTALL 路径）/ 无 td 超长行（等距切块路径）/ 单行内多 <tr> /
# thead+tbody 结构 / 空表（无 items 兜底）/ 无行内容 / 表头中断条件 / 未闭合表。
_CASES: list[tuple[str, list[str], int]] = [
    (
        "simple_header",
        [
            "<table>",
            "<tr><th>名前</th><th>値</th></tr>",
            "<tr><td>a</td><td>1</td></tr>",
            "<tr><td>b</td><td>2</td></tr>",
            "</table>",
        ],
        1200,
    ),
    (
        "small_budget_floor100",
        [
            "<table>",
            "<tr><th>h1</th><th>h2</th></tr>",
            "<tr><td>row-one-cell</td><td>x</td></tr>",
            "<tr><td>row-two-cell</td><td>y</td></tr>",
            "<tr><td>row-three</td><td>z</td></tr>",
            "</table>",
        ],
        80,
    ),
    (
        "single_line_multirow",
        ["<table><tr><td>a1</td></tr><tr><td>b2</td></tr><tr><td>c3</td></tr></table>"],
        1200,
    ),
    (
        "single_line_multirow_overlong",
        [
            "<table><tr><td>" + "x" * 200 + "</td></tr><tr><td>" + "y" * 200 + "</td></tr></table>"
        ],
        300,
    ),
    (
        "overlong_td_sliced",
        [
            "<table>",
            "<tr><th>hdr</th></tr>",
            "<tr><td>" + "长" * 1500 + "</td></tr>",
            "</table>",
        ],
        1200,
    ),
    (
        "overlong_row_no_td",
        ["<table>", "<tr><td>plain " + "word " * 400 + "</td></tr>", "</table>"],
        1200,
    ),
    (
        "multi_tr_in_one_line",
        [
            "<table>",
            "<tr><td>r1</td></tr><tr><td>r2</td></tr>",
            "<tr><td>r3</td></tr>",
            "</table>",
        ],
        200,
    ),
    (
        "thead_tbody",
        [
            "<table>",
            "<thead>",
            "<tr><th>colA</th><th>colB</th></tr>",
            "</thead>",
            "<tbody>",
            "<tr><td>1</td><td>2</td></tr>",
            "<tr><td>3</td><td>4</td></tr>",
            "</tbody>",
            "</table>",
        ],
        150,
    ),
    ("empty_table", ["<table>", "</table>"], 1200),
    ("no_row_items", ["<table>", "junk text", "</table>"], 1200),
    (
        "header_repeat_continuation",
        [
            "<table>",
            "<tr><th>A</th><th>B</th></tr>",
            "<tr><td>1</td><td>2</td></tr>",
            "<tr><td>3</td><td>4</td></tr>",
            "<tr><td>5</td><td>6</td></tr>",
            "</table>",
        ],
        120,
    ),
    ("unclosed_table", ["<table>", "<tr><td>u1</td></tr>", "<tr><td>u2"], 1200),
]


def test_split_table_matches_v073_legacy_byte_identical():
    for name, lines, chunk_size in _CASES:
        current = split_table_into_chunks(lines, chunk_size)
        legacy = legacy_split_table_into_chunks(lines, chunk_size)
        assert current == legacy, (
            f"golden mismatch in case {name!r}: 精简实现与 v0.7.3 原实现输出不一致，"
            f"将导致存量 chunk.id 变化与全库重嵌。\n"
            f"current={current!r}\nlegacy={legacy!r}"
        )


def test_split_table_structural_invariants():
    """包装不变量：每个非空片段都以 <table> 开头、</table> 结尾；行号不越界。"""
    for name, lines, chunk_size in _CASES:
        chunks = split_table_into_chunks(lines, chunk_size)
        assert chunks, f"case {name}: no chunks produced"
        for sub_s, sub_e, content in chunks:
            assert 0 <= sub_s <= sub_e <= max(0, len(lines) - 1), (
                f"case {name}: 行号区间越界 ({sub_s}, {sub_e})"
            )
            if len(content) > 2:
                assert content[0] == "<table>" and content[-1] == "</table>", (
                    f"case {name}: 片段缺少合法 HTML 包裹"
                )


def test_split_table_unclosed_tail_row_is_preserved():
    """未闭合表的最后一行（D2 修复引入的兜底）不得被静默丢弃。"""
    lines = ["<table>", "<tr><td>u1</td></tr>", "<tr><td>u2"]
    chunks = split_table_into_chunks(lines, 1200)
    legacy = legacy_split_table_into_chunks(lines, 1200)
    assert chunks == legacy
    joined = "\n".join(chunks[-1][2])
    assert "u2" in joined
