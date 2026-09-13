"""HTML 表格处理：MinerU 的表格输出是 HTML（rowspan/colspan 只有 HTML 表达得了）。

两项职责：
1) iter_table_blocks：供 chunker 做原子块保护（表格不被从中间切开）。
2) convert_small_tables：可选地把规整小表格转成 markdown pipe（LLM 更易读）；
   含 rowspan/colspan 或大表格保留 HTML——硬转 pipe 会塌掉（第三方 benchmark 实测教训）。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_TABLE_OPEN = re.compile(r"<table[\s>]", re.IGNORECASE)
_TABLE_CLOSE = re.compile(r"</table\s*>", re.IGNORECASE)
_TABLE_OPEN_TAG = re.compile(r"<table(?:\s+[^>]*)?>", re.IGNORECASE)
_TABLE_CLOSE_TAG = re.compile(r"</table\s*>", re.IGNORECASE)
_SPAN_ATTR = re.compile(r"rowspan\s*=|colspan\s*=", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*([`~]{3,})")


def iter_table_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """返回所有 <table>...</table> 块的 (start, end) 行号区间（闭区间）。
    用配对计数而不是单行判断：MinerU 输出的表格可能跨行。"""
    blocks: list[tuple[int, int]] = []
    depth = 0
    start = -1
    fence_token: str | None = None
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if m:
            token = m.group(1)
            if fence_token is None:
                fence_token = token[:3]
            elif token.startswith(fence_token):
                fence_token = None
        if fence_token is not None:
            continue
        opens = len(_TABLE_OPEN.findall(line))
        closes = len(_TABLE_CLOSE.findall(line))
        if opens and depth == 0:
            start = i
        depth += opens - closes
        if depth <= 0 and start >= 0:
            blocks.append((start, i))
            depth = 0
            start = -1
    if start >= 0:
        # 未闭合（解析质量差时）：严禁保护到文件尾（防止整段删除正文与全篇拒绝分块）
        # 最多向后探测至最近的 </tr> 或上限 50 行
        end = start
        limit = min(start + 50, len(lines) - 1)
        for k in range(start, limit + 1):
            line = lines[k]
            if re.match(r"^#{1,6}\s+", line) and k > start:
                break
            if "</tr" in line.lower() or "</td" in line.lower() or "</th" in line.lower():
                end = k
        blocks.append((start, end))
    return blocks


class _TableParser(HTMLParser):
    """把单个 <table> 解析成二维网格（忽略跨行跨列——带 span 的在调用侧已被拦截）。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _table_to_pipe(table_html: str) -> str | None:
    parser = _TableParser()
    parser.feed(table_html)
    rows = parser.rows
    if not rows or not rows[0]:
        return None
    width = max(len(r) for r in rows)
    if any(len(r) != width for r in rows):  # 行宽不齐 → 不转（多半是隐性合并）
        return None
    out = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def convert_small_tables(md_text: str, *, max_cells: int = 60) -> str:
    """把规整小表格转成 pipe；含 span / 超 max_cells / 行宽不齐的保留 HTML 原样。"""
    lines = md_text.splitlines()
    blocks = iter_table_blocks(lines)
    if not blocks:
        return md_text
    for start, end in reversed(blocks):  # 倒序替换，行号不失效
        html = "\n".join(lines[start:end + 1])
        # 未闭合表格绝对不转（防止正文被删除）
        if not _TABLE_CLOSE.search(html):
            continue
        cells = len(re.findall(r"<t[dh][\s>]", html, re.IGNORECASE))
        if cells > max_cells or _SPAN_ATTR.search(html):
            continue
        pipe = _table_to_pipe(html)
        if pipe is not None:
            lines[start:end + 1] = pipe.splitlines()
    return "\n".join(lines)


def split_large_table(lines: list[str], max_lines: int = 10, *, max_chars: int | None = None, repeat_header: bool = False) -> list[list[str]]:
    """超大表格按 </tr> 边界切片，每片补 <table></table> 包裹，保证每个分片都是合法片段。
    供 chunker 处理 > 2×chunk_size 的巨型表格（教材里的大参数表）。
    - D4a: 精确剥离包裹标签，保留同行的 <tr> 内容与正文中的 <table> 字样
    - D4b: repeat_header=True 时后序分片保留表头
    - D3: 支持 max_chars 字符预算装箱
    """
    header_rows: list[str] = []
    if repeat_header:
        for l in lines:
            cleaned = _TABLE_OPEN_TAG.sub("", l)
            cleaned = _TABLE_CLOSE_TAG.sub("", cleaned)
            if "<th" in l.lower() or "<thead" in l.lower():
                if cleaned.strip():
                    header_rows.append(cleaned)
                if "</tr>" in l.lower() or "</thead>" in l.lower():
                    if header_rows:
                        break

    body_lines: list[str] = []
    for l in lines:
        cleaned = _TABLE_OPEN_TAG.sub("", l)
        cleaned = _TABLE_CLOSE_TAG.sub("", cleaned)
        if cleaned.strip():
            body_lines.append(cleaned)

    if not body_lines:
        return [lines]

    parts: list[list[str]] = []
    current: list[str] = []
    current_chars = 0

    for line in body_lines:
        current.append(line)
        current_chars += len(line) + 1
        reached_lines = max_lines > 0 and len(current) >= max_lines
        reached_chars = max_chars is not None and current_chars >= max_chars
        if (reached_lines or reached_chars) and "</tr>" in line.lower():
            parts.append(current)
            current = []
            current_chars = 0

    if current:
        parts.append(current)

    if len(parts) <= 1:
        return [["<table>", *body_lines, "</table>"]]

    wrapped = []
    for idx, part in enumerate(parts):
        part_rows = []
        if repeat_header and idx > 0 and header_rows:
            if not any("<th" in r.lower() for r in part):
                part_rows.extend(header_rows)
        part_rows.extend(part)
        wrapped.append(["<table>", *part_rows, "</table>"])
    return wrapped


def split_table_into_chunks(tbl_lines: list[str], chunk_size: int) -> list[tuple[int, int, list[str]]]:
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

