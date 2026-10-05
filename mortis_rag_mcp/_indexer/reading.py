from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReadResult:
    source_sha256: str
    content: str
    total_lines: int
    effective_start_line: int | None
    effective_end_line: int | None
    content_end_line: int | None
    truncated: bool
    next_start_line: int | None
    next_start_char: int | None


class ReadFileNotFoundError(FileNotFoundError, ValueError):
    """文件不存在异常，同时继承 FileNotFoundError 与 ValueError。

    既保持旧测试对 FileNotFoundError 的捕获兼容，又满足 ValueError fail-closed
    错误协议，且不泄露物理文件的绝对路径。
    """
    pass


def read_file_result(
    path: Path,
    source: str,
    start_line: int | None = None,
    end_line: int | None = None,
    *,
    heading: str | None = None,
    start_char: int = 0,
    max_chars: int | None = None,
    expected_sha256: str | None = None,
    chunk_id_hint: str | None = None,
) -> ReadResult:
    """物理原文读取核心实现。

    快照式单次读取 bytes，统一计算 sha256、物理总行数、有效区间与字符截断游标。
    只依赖标准库与 Path，不运行时反向依赖 Facade。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ReadFileNotFoundError(
            f"无法读取文件 '{source}': [FileNotFoundError]；请检查文件是否存在"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"无法读取文件 '{source}': [{exc.__class__.__name__}]；请检查文件是否存在且具有读取权限"
        ) from exc

    source_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and source_sha256 != expected_sha256:
        cid_prefix = chunk_id_hint or ""
        raise ValueError(
            f"chunk_id {cid_prefix}已过期（stale：物理文件 {source} 内容签名不一致/已修改）；"
            "请重新 kb_search 获取新 id，或改用 source + heading / 行区间读取"
        )

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"文件 '{source}' 编码不是合法的 UTF-8: [{exc.__class__.__name__}]；请将文件转为 UTF-8 编码"
        ) from exc

    lines = text.splitlines()
    total_lines = len(lines)

    if total_lines == 0:
        if start_line is not None or end_line is not None:
            raise ValueError(
                f"start_line/end_line 超出空文件范围 (requested: start={start_line}, end={end_line}, "
                f"actual: 0, source: {source})；文件为空"
            )
        return ReadResult(
            source_sha256=source_sha256,
            content="",
            total_lines=0,
            effective_start_line=None,
            effective_end_line=None,
            content_end_line=None,
            truncated=False,
            next_start_line=None,
            next_start_char=None,
        )

    if start_line is not None:
        if start_line > total_lines:
            raise ValueError(
                f"start_line 超出文件行数范围 (requested: {start_line}, actual: {total_lines}, "
                f"source: {source})；请核对分卷行号或用heading定位"
            )
        effective_start = start_line
    else:
        effective_start = 1

    if end_line is not None:
        if end_line < effective_start:
            raise ValueError(
                f"end_line must be >= start_line: end_line={end_line}, start_line={effective_start}"
            )
        effective_end = min(end_line, total_lines)
    else:
        effective_end = total_lines

    first_line = lines[effective_start - 1]
    if start_char < 0 or start_char > len(first_line):
        raise ValueError(
            f"start_char ({start_char}) 超出行长度范围 [0, {len(first_line)}] (line {effective_start}, source: {source})"
        )

    if effective_start == effective_end:
        full_text = first_line[start_char:]
    else:
        first_part = first_line[start_char:]
        rest_parts = lines[effective_start:effective_end]
        full_text = first_part + "\n" + "\n".join(rest_parts)

    if max_chars is None or len(full_text) <= max_chars:
        return ReadResult(
            source_sha256=source_sha256,
            content=full_text,
            total_lines=total_lines,
            effective_start_line=effective_start,
            effective_end_line=effective_end,
            content_end_line=effective_end,
            truncated=False,
            next_start_line=None,
            next_start_char=None,
        )

    K = max_chars
    content = full_text[:K]
    truncated = True

    seg_len = len(first_line) - start_char
    if K <= seg_len:
        next_start_line = effective_start
        next_start_char = start_char + K
        content_end_line = effective_start
    else:
        consumed = seg_len
        next_start_line = effective_end
        next_start_char = len(lines[effective_end - 1])
        content_end_line = effective_end
        for line_num in range(effective_start + 1, effective_end + 1):
            if K == consumed + 1:
                next_start_line = line_num
                next_start_char = 0
                content_end_line = line_num - 1
                break
            consumed += 1
            line_len = len(lines[line_num - 1])
            if K <= consumed + line_len:
                next_start_line = line_num
                next_start_char = K - consumed
                content_end_line = line_num
                break
            consumed += line_len

    return ReadResult(
        source_sha256=source_sha256,
        content=content,
        total_lines=total_lines,
        effective_start_line=effective_start,
        effective_end_line=effective_end,
        content_end_line=content_end_line,
        truncated=truncated,
        next_start_line=next_start_line,
        next_start_char=next_start_char,
    )
