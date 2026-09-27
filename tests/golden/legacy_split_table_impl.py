"""v0.7.3 ``split_table_into_chunks`` 逐字快照（金标准 oracle）。

来源：mortis_rag_mcp/ingest/tables.py @ v0.7.3（commit 1c9ec8f）。
本文件是冻结的历史实现副本，供 Phase 1 精简重构做字节级等价对照。
**禁止修改本文件内容**——它代表"存量用户已固化的切块行为"。
"""
from __future__ import annotations

import re

_TABLE_OPEN_TAG = re.compile(r"<table(?:\s+[^>]*)?>", re.IGNORECASE)
_TABLE_CLOSE_TAG = re.compile(r"</table\s*>", re.IGNORECASE)


def legacy_split_table_into_chunks(tbl_lines: list[str], chunk_size: int) -> list[tuple[int, int, list[str]]]:
    """把表格行拆分为不超过 chunk_size 预算的合法片段，并返回每个片段对应的相对行号区间 (sub_s, sub_e, chunk_lines)。
    保证：
    - 每个片段都是带有 <table></table> 包裹的合法 HTML 片段
    - 后序片段保留表头 (D4b)
    - 预算装箱，即使单行/单元格超长也不会产生 10x chunk (D3, D5)
    - 相对行号区间精准映射回原文件物理行号，彻底杜绝行号漂移 (D4c)
    """
    header_rows: list[str] = []
    for l in tbl_lines:
        cleaned = _TABLE_OPEN_TAG.sub("", l)
        cleaned = _TABLE_CLOSE_TAG.sub("", cleaned)
        if "<th" in l.lower() or "<thead" in l.lower():
            if cleaned.strip():
                header_rows.append(cleaned.strip())
            if "</tr>" in l.lower() or "</thead>" in l.lower():
                if header_rows:
                    break

    items: list[tuple[int, str]] = []
    if len(tbl_lines) == 1 and ("</tr>" in tbl_lines[0].lower() or "<tr" in tbl_lines[0].lower()):
        # 单行多 row 表格 (D5)
        line = tbl_lines[0]
        cleaned = _TABLE_OPEN_TAG.sub("", line)
        cleaned = _TABLE_CLOSE_TAG.sub("", cleaned)
        raw_rows = re.split(r"(?<=</tr>)", cleaned, flags=re.IGNORECASE)
        for r in raw_rows:
            if r.strip():
                items.append((0, r.strip()))
    else:
        for idx, line in enumerate(tbl_lines):
            cleaned = _TABLE_OPEN_TAG.sub("", line)
            cleaned = _TABLE_CLOSE_TAG.sub("", cleaned)
            if cleaned.strip():
                if cleaned.lower().count("<tr") > 1 and "</tr>" in cleaned.lower():
                    raw_rows = re.split(r"(?<=</tr>)", cleaned, flags=re.IGNORECASE)
                    for r in raw_rows:
                        if r.strip():
                            items.append((idx, r.strip()))
                else:
                    items.append((idx, cleaned.strip()))

    if not items:
        return [(0, max(0, len(tbl_lines) - 1), ["<table>", "</table>"])]

    overhead = len("<table>\n\n</table>")
    header_overhead = sum(len(h) + 1 for h in header_rows)

    chunks: list[tuple[int, int, list[str]]] = []
    current_rows: list[str] = []
    current_sub_start = items[0][0]
    current_sub_end = items[0][0]
    current_chars = 0

    def flush():
        nonlocal current_rows, current_chars, current_sub_start, current_sub_end
        if not current_rows:
            return
        rows_to_add = []
        if chunks and header_rows and not any("<th" in r.lower() for r in current_rows):
            rows_to_add.extend(header_rows)
        rows_to_add.extend(current_rows)
        chunk_content = ["<table>", *rows_to_add, "</table>"]
        chunks.append((current_sub_start, current_sub_end, chunk_content))
        current_rows = []
        current_chars = 0

    for line_no, row_str in items:
        is_continuation = bool(chunks)
        budget = chunk_size - overhead - (header_overhead if is_continuation else 0)
        budget = max(100, budget)

        if len(row_str) <= budget:
            if current_rows and (current_chars + len(row_str) + 1 > budget):
                flush()
                current_sub_start = line_no
            current_rows.append(row_str)
            current_chars += len(row_str) + 1
            current_sub_end = line_no
        else:
            if current_rows:
                flush()
            td_match = re.search(r"(<td[^>]*>)(.*?)(</td>)", row_str, flags=re.DOTALL | re.IGNORECASE)
            if td_match:
                td_open, td_content, td_close = td_match.groups()
                slice_budget = budget - len(f"<tr>{td_open}{td_close}</tr>\n") - 10
                slice_budget = max(50, slice_budget)
                for p_start in range(0, len(td_content), slice_budget):
                    piece = td_content[p_start : p_start + slice_budget]
                    piece_row = f"<tr>{td_open}{piece}{td_close}</tr>"
                    rows_to_add = []
                    if chunks and header_rows:
                        rows_to_add.extend(header_rows)
                    rows_to_add.append(piece_row)
                    chunks.append((line_no, line_no, ["<table>", *rows_to_add, "</table>"]))
            else:
                for p_start in range(0, len(row_str), budget):
                    piece = row_str[p_start : p_start + budget]
                    rows_to_add = []
                    if chunks and header_rows:
                        rows_to_add.extend(header_rows)
                    rows_to_add.append(piece)
                    chunks.append((line_no, line_no, ["<table>", *rows_to_add, "</table>"]))
            current_sub_start = line_no
            current_sub_end = line_no

    if current_rows:
        flush()

    return chunks
