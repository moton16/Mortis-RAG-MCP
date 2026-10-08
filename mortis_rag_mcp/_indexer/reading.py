from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
import time
from typing import Any, Protocol

from ..doc_store import normalize_source_path


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
    source_kind: str = "physical"
    revision_id: str | None = None
    original_sha256: str | None = None
    render_sha256: str | None = None
    line_basis: str = "physical"
    quality: str | None = None
    coverage: Any = None
    source_changed: bool = False


class ReadFileNotFoundError(FileNotFoundError, ValueError):
    """文件不存在异常，同时继承 FileNotFoundError 与 ValueError。

    既保持旧测试对 FileNotFoundError 的捕获兼容，又满足 ValueError fail-closed
    错误协议，且不泄露物理文件的绝对路径。
    """
    pass


from .chunking import _HEADING_RE, _FENCE_START_RE, clean_heading, _is_chapter_heading, frontmatter
from ..ingest.tables import iter_table_blocks


def scan_headings(lines: list[str]) -> list[tuple[str, int, int]]:
    """扫描文本行列表中的物理标题，返回 (title, level, 1-based start_line) 列表。

    跳过 frontmatter、代码围栏 (``` 或 ~~~) 以及 HTML 表格块 (<table>...</table>) 内部的假标题。
    不注入图片，不修改原始行号。
    支持 ATX 标题 (#..######) 以及中文/Chapter 章节标题（视为 level 1）。
    """
    headings: list[tuple[str, int, int]] = []
    fm_end, _, _ = frontmatter(lines)
    # review R5：表格配对只在 frontmatter 之后的正文上做——frontmatter 里的
    # title:"<table>" 之类表格字样会污染配对深度，导致正文 HTML 表格内部的
    # # 假标题没被屏蔽（章节被静默截短）。表格行号整体偏移回物理源文件。
    tbl_offset = fm_end + 1  # frontmatter() 无前置元数据时返回 -1 → 偏移 0
    tbl_blocks = iter_table_blocks(lines[tbl_offset:])
    tbl_lines = {tbl_offset + idx for s, e in tbl_blocks for idx in range(s, e + 1)}

    fence_char: str | None = None
    fence_len: int = 0

    for i, line in enumerate(lines):
        if i <= fm_end:
            continue

        # 代码围栏状态维护（需使用相同字符且长度>=起始围栏方可闭合）
        m = _FENCE_START_RE.match(line)
        if m:
            token = m.group(1)
            char, length = token[0], len(token)
            if fence_char is None:
                fence_char = char
                fence_len = length
                continue
            elif char == fence_char and length >= fence_len:
                fence_char = None
                fence_len = 0
                continue

        if fence_char is not None:
            continue

        if i in tbl_lines:
            continue

        # 优先 ATX 标题
        hm = _HEADING_RE.match(line)
        if hm:
            hashes = hm.group(1)
            raw_title = hm.group(2)
            title = clean_heading(raw_title)
            headings.append((title, len(hashes), i + 1))
            continue

        # 章节标题（如 第1章 标题、Chapter 1 Title）
        is_ch, ch_title = _is_chapter_heading(line)
        if is_ch:
            headings.append((ch_title, 1, i + 1))

    return headings


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

    return slice_text_result(
        text, source, source_sha256, start_line, end_line,
        heading=heading, start_char=start_char, max_chars=max_chars,
    )


def slice_text_result(
    text: str,
    source: str,
    render_sha256: str,
    start_line: int | None = None,
    end_line: int | None = None,
    *,
    heading: str | None = None,
    start_char: int = 0,
    max_chars: int | None = None,
) -> ReadResult:
    """Slice physical or rendered text using the same line and cursor contract."""
    source_sha256 = render_sha256
    lines = text.splitlines()
    total_lines = len(lines)

    if total_lines == 0:
        if start_line is not None or end_line is not None:
            raise ValueError(
                f"start_line/end_line 超出空文件范围 (requested: start={start_line}, end={end_line}, "
                f"actual: 0, source: {source})；文件为空"
            )
        if heading is not None:
            raise ValueError(
                f"heading 未找到: '{heading.strip()}' (source: {source}, total_lines: 0)；文件为空"
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

    if heading is not None and start_line is None and end_line is None:
        query_heading = heading.strip()
        all_headings = scan_headings(lines)
        matches = [h for h in all_headings if h[0] == query_heading]

        if not matches:
            cand_preview = ", ".join(f"'{h[0]}' (line {h[2]})" for h in all_headings[:5])
            if len(all_headings) > 5:
                cand_preview += f" 等共 {len(all_headings)} 个标题"
            elif not all_headings:
                cand_preview = "（文件中未检测到任何标题）"
            raise ValueError(
                f"heading 未找到: '{query_heading}' (source: {source}, total_lines: {total_lines})；"
                f"候选标题: {cand_preview}；请核对标题文字或改用 start_line/end_line 行号读取"
            )

        if len(matches) > 1:
            lines_str = ", ".join(str(h[2]) for h in matches[:5])
            raise ValueError(
                f"heading '{query_heading}' 在文件 '{source}' 中存在 {len(matches)} 处同名标题 "
                f"(起始行: {lines_str})；存在歧义，请改用 start_line/end_line 明确指定行号读取"
            )

        matched_title, matched_level, matched_start = matches[0]
        next_heading = next((h for h in all_headings if h[2] > matched_start and h[1] <= matched_level), None)
        if next_heading is not None:
            matched_end = next_heading[2] - 1
        else:
            matched_end = total_lines

        effective_start = matched_start
        effective_end = matched_end
    else:
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


class VirtualReadOwner(Protocol):
    vault_path: Path
    config: Any

    def _existing_document_store(self) -> Any: ...

    def _ignore_matcher(self) -> Any: ...


class VirtualReadError(ValueError):
    """Stable fail-closed read error without exposing physical paths."""

    def __init__(self, code: str, source: str) -> None:
        self.code = code
        self.retryable = code in {"UNAVAILABLE", "VERIFICATION_PENDING"}
        super().__init__(f"{code}: virtual source {source!r}")


def _original_sha256(path: Path, source: str, limit: int, deadline: float) -> str:
    digest = hashlib.sha256()
    try:
        before = path.stat()
        if limit and before.st_size > limit:
            raise VirtualReadError("RESOURCE_LIMIT", source)
        consumed = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise VirtualReadError("SOURCE_CHANGED", source)
            while True:
                if time.monotonic() >= deadline:
                    raise VirtualReadError("VERIFICATION_PENDING", source)
                block = stream.read(64 * 1024)
                if not block:
                    break
                consumed += len(block)
                if limit and consumed > limit:
                    raise VirtualReadError("RESOURCE_LIMIT", source)
                digest.update(block)
            after_open = os.fstat(stream.fileno())
        after = path.stat()
    except OSError as exc:
        raise VirtualReadError("UNAVAILABLE", source) from exc
    def identity(stat: os.stat_result) -> tuple[int, int, int, int]:
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    # Windows path stat uses birth time for ctime while handle stat uses change time.
    if not (identity(before) == identity(opened) == identity(after_open) == identity(after)) or (
        before.st_ctime_ns != after.st_ctime_ns or opened.st_ctime_ns != after_open.st_ctime_ns
    ):
        raise VirtualReadError("SOURCE_CHANGED", source)
    if time.monotonic() >= deadline:
        raise VirtualReadError("VERIFICATION_PENDING", source)
    return digest.hexdigest()


def read_virtual_result(
    owner: VirtualReadOwner,
    source: str,
    start_line: int | None = None,
    end_line: int | None = None,
    *,
    heading: str | None = None,
    start_char: int = 0,
    max_chars: int | None = None,
    allow_stale: bool = False,
    expected_revision_id: str | None = None,
    expected_sha256: str | None = None,
    expected_render_sha256: str | None = None,
    chunk_id_hint: str | None = None,
    verification_timeout: float = 1.5,
) -> ReadResult:
    """Read a committed revision only after source and policy verification.

    The caller selects a registered vault and resolves aliases before entering
    this helper. No facade import, sync, parsing or binary UTF-8 read occurs.
    """
    if type(allow_stale) is not bool:
        raise ValueError("allow_stale must be a boolean")
    if verification_timeout <= 0 or not math.isfinite(verification_timeout):
        raise ValueError("verification_timeout must be positive and finite")
    source = normalize_source_path(source, owner.vault_path)
    try:
        ignore_path = owner.vault_path / owner.config.ignore_file
        try:
            ignore_path.stat()
        except FileNotFoundError:
            pass
        else:
            ignore_path.read_text(encoding="utf-8")
        matcher = owner._ignore_matcher()
        if matcher.is_ignored(source)[0]:
            raise VirtualReadError("UNAVAILABLE", source)
        store = owner._existing_document_store()
        active = store.get_active(source, include_hidden=True) if store is not None else None
    except VirtualReadError:
        raise
    except Exception as exc:
        raise VirtualReadError("UNAVAILABLE", source) from exc
    if store is None or active is None or active.visibility != "active" or active.revision.state != "committed":
        raise VirtualReadError("UNAVAILABLE", source)
    revision = active.revision
    is_chunk = chunk_id_hint is not None or expected_revision_id is not None
    if is_chunk and (
        not expected_revision_id or not expected_sha256 or not expected_render_sha256
        or expected_revision_id != revision.revision_id
        or expected_sha256 != revision.source_sha256
        or expected_render_sha256 != revision.render_sha256
    ):
        raise VirtualReadError("STALE", source)
    deadline = time.monotonic() + verification_timeout
    raw_sha = _original_sha256(owner.vault_path / source, source,
                               owner.config.ingest.max_file_size_bytes, deadline)
    changed = raw_sha != revision.source_sha256
    if changed and (is_chunk or not allow_stale):
        raise VirtualReadError("STALE" if is_chunk else "SOURCE_CHANGED", source)
    if hashlib.sha256(revision.parsed_markdown.encode("utf-8")).hexdigest() != revision.render_sha256:
        raise VirtualReadError("UNAVAILABLE", source)
    result = slice_text_result(revision.parsed_markdown, source, revision.render_sha256,
                               start_line, end_line, heading=heading,
                               start_char=start_char, max_chars=max_chars)
    try:
        latest = store.get_active(source, include_hidden=True)
        try:
            ignore_path.stat()
        except FileNotFoundError:
            pass
        else:
            ignore_path.read_text(encoding="utf-8")
        ignored = owner._ignore_matcher().is_ignored(source)[0]
    except Exception as exc:
        raise VirtualReadError("UNAVAILABLE", source) from exc
    normalize_source_path(source, owner.vault_path)
    if latest is None or latest.visibility != "active" or ignored:
        raise VirtualReadError("UNAVAILABLE", source)
    if latest.active_revision != revision.revision_id:
        raise VirtualReadError("STALE" if is_chunk else "SOURCE_CHANGED", source)
    return replace(result, source_kind="virtual", revision_id=revision.revision_id,
                   original_sha256=revision.source_sha256, render_sha256=revision.render_sha256,
                   line_basis="rendered_markdown", quality=revision.quality,
                   coverage=revision.capabilities.get("coverage"), source_changed=changed)


def resolve_virtual_source(owner: VirtualReadOwner, source: str, *,
                           revision_id: str = "", verification_timeout: float = 1.5) -> dict[str, Any]:
    """Verify库归属/当前revision/物理源 SHA 后返回地址事实，不返回正文（§15.1/§15.3）。

    与 `read_virtual_result` 同一 fail-closed 判据：来源被 ignore/非 active/未提交/物理
    源 SHA 与解析事实不符/复核期间 revision 变化，一律抛 `VirtualReadError`（带稳定 code）。
    媒体读取用它对 `source + revision_id` 做发布前核验，绝不按包内签名或调用方自述采信。
    """
    if not isinstance(revision_id, str):
        raise ValueError("revision_id must be a string")
    if verification_timeout <= 0 or not math.isfinite(verification_timeout):
        raise ValueError("verification_timeout must be positive and finite")
    source = normalize_source_path(source, owner.vault_path)
    try:
        ignore_path = owner.vault_path / owner.config.ignore_file
        try:
            ignore_path.stat()
        except FileNotFoundError:
            pass
        else:
            ignore_path.read_text(encoding="utf-8")
        if owner._ignore_matcher().is_ignored(source)[0]:
            raise VirtualReadError("UNAVAILABLE", source)
        store = owner._existing_document_store()
        active = store.get_active(source, include_hidden=True) if store is not None else None
    except VirtualReadError:
        raise
    except Exception as exc:
        raise VirtualReadError("UNAVAILABLE", source) from exc
    if store is None or active is None or active.visibility != "active" or active.revision.state != "committed":
        raise VirtualReadError("UNAVAILABLE", source)
    revision = active.revision
    if revision_id and revision_id != revision.revision_id:
        raise VirtualReadError("STALE", source)
    deadline = time.monotonic() + verification_timeout
    raw_sha = _original_sha256(owner.vault_path / source, source,
                               owner.config.ingest.max_file_size_bytes, deadline)
    if raw_sha != revision.source_sha256:
        raise VirtualReadError("SOURCE_CHANGED", source)
    if hashlib.sha256(revision.parsed_markdown.encode("utf-8")).hexdigest() != revision.render_sha256:
        raise VirtualReadError("UNAVAILABLE", source)
    try:
        latest = store.get_active(source, include_hidden=True)
        ignored = owner._ignore_matcher().is_ignored(source)[0]
    except Exception as exc:
        raise VirtualReadError("UNAVAILABLE", source) from exc
    if latest is None or latest.visibility != "active" or ignored:
        raise VirtualReadError("UNAVAILABLE", source)
    if latest.active_revision != revision.revision_id:
        raise VirtualReadError("STALE", source)
    return {
        "source": source,
        "doc_id": active.doc_id,
        "revision_id": revision.revision_id,
        "source_sha256": revision.source_sha256,
        "render_sha256": revision.render_sha256,
        "line_basis": "rendered_markdown",
        "quality": revision.quality,
        "coverage": revision.capabilities.get("coverage"),
        "total_lines": len(revision.parsed_markdown.splitlines()),
        "markdown": revision.parsed_markdown,
    }
