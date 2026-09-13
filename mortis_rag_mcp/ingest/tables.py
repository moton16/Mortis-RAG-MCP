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
_SPAN_ATTR = re.compile(r"rowspan\s*=|colspan\s*=", re.IGNORECASE)


def iter_table_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """返回所有 <table>...</table> 块的 (start, end) 行号区间（闭区间）。
    用配对计数而不是单行判断：MinerU 输出的表格可能跨行。"""
    blocks: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, line in enumerate(lines):
        opens = len(_TABLE_OPEN.findall(line))
        closes = len(_TABLE_CLOSE.findall(line))
        if opens and depth == 0:
            start = i
        depth += opens - closes
        if depth <= 0 and start >= 0:
            blocks.append((start, i))
            depth = 0
            start = -1
    if start >= 0:  # 未闭合（解析质量差时）→ 保护到文件尾，宁可不切也不错切
        blocks.append((start, len(lines) - 1))
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
        cells = len(re.findall(r"<t[dh][\s>]", html, re.IGNORECASE))
        if cells > max_cells or _SPAN_ATTR.search(html):
            continue
        pipe = _table_to_pipe(html)
        if pipe is not None:
            lines[start:end + 1] = pipe.splitlines()
    return "\n".join(lines)


def split_large_table(lines: list[str], max_lines: int) -> list[list[str]]:
    """超大表格按 </tr> 边界切片，每片补 <table></table> 包裹，保证每个分片都是合法片段。
    供 chunker 处理 > 2×chunk_size 的巨型表格（教材里的大参数表）。"""
    parts: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if len(current) >= max_lines and "</tr>" in line.lower():
            parts.append(current)
            current = []
    if current:
        parts.append(current)
    if len(parts) <= 1:
        return parts
    wrapped = []
    for part in parts:
        body = [l for l in part if not _TABLE_OPEN.search(l) and not _TABLE_CLOSE.search(l)]
        wrapped.append(["<table>", *body, "</table>"])
    return wrapped
