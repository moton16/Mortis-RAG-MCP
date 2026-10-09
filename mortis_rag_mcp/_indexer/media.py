"""Deterministic media proxies and profile-scoped bidirectional links."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import Chunk
from .token_chunking import chunking_profile, estimate_tokens


class MediaStageError(RuntimeError):
    """Retain successful native batches when a later batch fails."""

    def __init__(self, message: str, chunks: list[Chunk]) -> None:
        super().__init__(message)
        self.chunks = chunks


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _global_span(span: Any, markdown: str | None) -> tuple[int, int] | None:
    """把一条 span 归一为相对整篇 markdown 的全局半开区间；无法确定时返回 None（不猜）。

    优先接受已带全局 `char_start/char_end` 的 span；否则按 `line`（1-based）+ 行内
    `start_char/end_char` 换算，公式 `sum(len(l)+1 for l in lines[:line-1]) + start_char`。
    没有 markdown 时不做换算，避免把行内偏移误当全局偏移。
    """
    if not isinstance(span, Mapping):
        return None
    cs, ce = span.get("char_start"), span.get("char_end")
    if _is_int(cs) and _is_int(ce) and 0 <= cs <= ce:
        return cs, ce
    line, start, end = span.get("line"), span.get("start_char"), span.get("end_char")
    if not (_is_int(line) and _is_int(start) and _is_int(end) and 0 <= start <= end and line >= 1):
        return None
    if markdown is None:
        return None
    lines = markdown.splitlines()
    if line > len(lines) or end > len(lines[line - 1]):
        return None
    offset = sum(len(item) + 1 for item in lines[:line - 1])
    return offset + start, offset + end


def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def occurrence_span(markdown: str, occurrence: Any) -> tuple[int, int] | None:
    """Only explicit parser spans or an exact Markdown image target are anchors.

    E08-a 读取优先级：**已核验的 metadata 锚点优先于 anchor 列**。历史上
    `attach_occurrences` 把 width/height 写进了 anchor_start/anchor_end 列，
    「先取列值」会让像素尺寸冒充正文位置；metadata 锚点只在有证据时才写入。
    """
    meta = _get(occurrence, "metadata", {}) or {}
    start, end = meta.get("anchor_start"), meta.get("anchor_end")
    if start is None or end is None:
        start = _get(occurrence, "anchor_start")
        end = _get(occurrence, "anchor_end")
    if (isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and 0 <= start <= end <= len(markdown)):
        return start, end
    # 行局部锚点必须显式换算为全局 offset，绝不把行内偏移当全局偏移。
    local = _global_span({k: meta.get(k) for k in ("line", "start_char", "end_char")}, markdown)
    if local is not None:
        return local
    name = _get(occurrence, "name", "") or meta.get("name", "")
    if name:
        matches = [m for m in re.finditer(r"!\[[^\]]*\]\(([^)\n]+)\)", markdown)
                   if m.group(1).strip() == name]
        ordinal = _get(occurrence, "ordinal", 0)
        if len(matches) == 1:
            return matches[0].span()
        # Repeated targets without parser spans are ambiguous; never guess a page.
    return None


def _context(markdown: str, span: tuple[int, int] | None, limit: int) -> str:
    if span is None:
        return ""
    start, end = span
    headings = list(re.finditer(r"(?m)^#{1,6}[^\n]*", markdown))
    lower = max((m.end() for m in headings if m.end() <= start), default=0)
    upper = min((m.start() for m in headings if m.start() >= end), default=len(markdown))
    return (markdown[max(lower, start - limit):start].strip() + "\n"
            + markdown[end:min(upper, end + limit)].strip()).strip()


def _split_content(content: str, target: float) -> list[str]:
    """按行确定性累积切分，尽量贴近 target；单行超硬上限时整行保留，由调用方标 oversize。"""
    fragments: list[str] = []
    current: list[str] = []
    for line in content.split("\n"):
        candidate = current + [line]
        if current and estimate_tokens("\n".join(candidate)) > target:
            fragments.append("\n".join(current))
            current = [line]
        else:
            current = candidate
    if current:
        fragments.append("\n".join(current))
    return fragments or [content]


def build_media_proxy_chunks(
    markdown: str, occurrences: Sequence[Any], *, source: str, revision_id: str,
    profile_key: str, chunker_fingerprint: str, derived_generation_id: str,
    caption_limit: int = 500, ocr_limit: int = 2000, context_limit: int = 300,
    chunking: Any = None,
) -> list[Chunk]:
    """No provider calls, no source mutation; synthetic lines never impersonate source."""
    settings = chunking_profile(chunking) if chunking is not None else None
    target = settings["target"] if settings else 0.0
    hard = settings["hard_limit"] if settings else 0.0
    chunks = []
    for occurrence in sorted(occurrences, key=lambda o: (_get(o, "ordinal", 0), _get(o, "occurrence_id", ""))):
        meta = _get(occurrence, "metadata", {}) or {}
        if meta.get("decorative_verified") is True:
            continue
        oid = _get(occurrence, "occurrence_id")
        span = occurrence_span(markdown, occurrence)
        caption = _text(_get(occurrence, "caption", ""), max(1, caption_limit))
        ocr = _text(_get(occurrence, "ocr", "") or meta.get("ocr", ""), max(1, ocr_limit))
        context = _context(markdown, span, max(1, min(context_limit, 300)))
        page = _get(occurrence, "page")
        label = "图表" if _get(occurrence, "kind") != "audio" else "音频"
        location = f" (P.{page})" if page is not None else ""
        content = f"[{label}: {caption or '无图注'}{location}]"
        if ocr:
            content += "\nOCR: " + ocr
        if context:
            content += "\nContext: " + context
        content += f"\nMedia: {source} / {revision_id} / {oid}"
        blob_hash = (_get(occurrence, "blob_sha256", "") or _get(occurrence, "sha256", "")
                     or meta.get("sha256", "") or _get(occurrence, "blob_id", ""))
        identity = [source, revision_id, oid, profile_key, chunker_fingerprint, derived_generation_id]
        chunk_id = "media-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        start_line = markdown.count("\n", 0, span[0]) + 1 if span else None
        end_line = markdown.count("\n", 0, max(span[0], span[1] - 1)) + 1 if span else None
        metadata = {
            "source_kind": "virtual", "revision_id": revision_id, "kind": "media_proxy",
            "occurrence_id": oid, "media_occurrence_ids": [oid], "page": page,
            "t_start_ms": _get(occurrence, "t_start_ms"), "t_end_ms": _get(occurrence, "t_end_ms"),
            "ocr_available": bool(ocr), "anchor_available": span is not None,
            "anchor_confidence": "parser" if _get(occurrence, "anchor_start") is not None else "markdown_adjacency" if span else "unavailable",
            "start_line": start_line, "end_line": end_line, "line_basis": "rendered_markdown",
            "source_spans": [] if span is None else [{"char_start": span[0], "char_end": span[1], "start_line": start_line, "end_line": end_line}],
            "synthetic_segments": [{"char_start": 0, "char_end": len(content), "kind": "media_proxy", "occurrence_id": oid}],
            "profile_key": profile_key, "chunker_fingerprint": chunker_fingerprint,
            "derived_generation_id": derived_generation_id, "blob_sha256": blob_hash,
            "embedding_key": hashlib.sha256((content + "\0" + str(blob_hash)).encode()).hexdigest(),
        }
        if settings is None or estimate_tokens(content) <= hard:
            chunks.append(Chunk(chunk_id, content, source, caption or label, metadata))
            continue
        fragments = _split_content(content, target)
        for index, fragment in enumerate(fragments):
            piece = dict(metadata)
            # 锚点只挂在第一条 proxy 片段上；其余片段不得冒充源行。
            if index > 0:
                piece.update({"source_spans": [], "anchor_available": False,
                              "anchor_confidence": "unavailable", "start_line": None, "end_line": None})
            piece["synthetic_segments"] = [{"char_start": 0, "char_end": len(fragment),
                                            "kind": "media_proxy", "occurrence_id": oid}]
            piece["embedding_key"] = hashlib.sha256((fragment + "\0" + str(blob_hash)).encode()).hexdigest()
            piece["fragment_index"] = index
            piece["fragment_count"] = len(fragments)
            if estimate_tokens(fragment) > hard:
                piece.update({"oversize": True, "embedding_disabled": True})
            cid = chunk_id if len(fragments) == 1 else f"{chunk_id}-{index:02d}"
            chunks.append(Chunk(cid, fragment, source, caption or label, piece))
    return chunks


def merge_audio_segments(occurrences: Sequence[Any]) -> list[dict[str, Any]]:
    """把同一父源/revision 的**相邻或重叠**音频片段按毫秒区间合并展示（E08-d 音频出口）。

    只按时间相邻性合并，**绝不**按转录/caption 文本合并——两段音频即使转录逐字相同，
    只要时间区间不相邻就保持独立，避免把不同音频误当成同一段。合并结果保留每个原始
    片段的可读地址（`occurrence_ids`），展示侧据此仍能取回原片段。
    """
    segments: list[dict[str, Any]] = []
    for occurrence in occurrences:
        if _get(occurrence, "kind") != "audio":
            continue
        start = _get(occurrence, "t_start_ms")
        end = _get(occurrence, "t_end_ms")
        oid = _get(occurrence, "occurrence_id")
        if not (_is_int(start) and _is_int(end) and end >= start and oid):
            continue
        segments.append({"t_start_ms": start, "t_end_ms": end, "occurrence_ids": [oid]})
    segments.sort(key=lambda item: (item["t_start_ms"], item["t_end_ms"], item["occurrence_ids"][0]))
    merged: list[dict[str, Any]] = []
    for segment in segments:
        if merged and segment["t_start_ms"] <= merged[-1]["t_end_ms"]:
            last = merged[-1]
            last["t_end_ms"] = max(last["t_end_ms"], segment["t_end_ms"])
            last["occurrence_ids"].extend(segment["occurrence_ids"])
            continue
        merged.append(segment)
    for item in merged:
        item["kind"] = "audio"
        item["occurrence_ids"] = sorted(set(item["occurrence_ids"]))
    return merged


def media_chunk_links(chunks: Sequence[Chunk], occurrences: Sequence[Any], *, revision_id: str,
                      profile_key: str, chunker_fingerprint: str,
                      derived_generation_id: str, markdown: str | None = None) -> list[dict[str, Any]]:
    """Project links for exactly one captured derived profile/generation.

    chunk 的正文 `source_spans` 是逐行 codepoint 范围；提供 `markdown` 时显式换算成
    全局 offset 再判重叠，未提供时不对行局部 span 猜全局位置。
    """
    links = []
    for chunk in chunks:
        if any(chunk.metadata.get(key, value) != value for key, value in (
                ("revision_id", revision_id), ("profile_key", profile_key),
                ("chunker_fingerprint", chunker_fingerprint),
                ("derived_generation_id", derived_generation_id))):
            continue
        ids = set(chunk.metadata.get("media_occurrence_ids", []))
        spans = [span for span in (_global_span(item, markdown)
                                   for item in chunk.metadata.get("source_spans", []))
                 if span is not None]
        for occurrence in occurrences:
            oid = _get(occurrence, "occurrence_id")
            meta = _get(occurrence, "metadata", {}) or {}
            # 与 `occurrence_span` 同口径：metadata 锚点优先（E08-a）。
            start, end = meta.get("anchor_start"), meta.get("anchor_end")
            if start is None or end is None:
                start = _get(occurrence, "anchor_start")
                end = _get(occurrence, "anchor_end")
            if start is None or end is None:
                start, end = None, None
            if start is None:
                anchor = _global_span({k: meta.get(k) for k in ("line", "start_char", "end_char")}, markdown)
                start, end = anchor if anchor is not None else (None, None)
            overlaps = start is not None and end is not None and any(
                span[0] < end and start < span[1] for span in spans)
            if oid in ids or overlaps:
                ids.add(oid)
                links.append({"revision_id": revision_id, "occurrence_id": oid,
                              "profile_key": profile_key, "chunker_fingerprint": chunker_fingerprint,
                              "derived_generation_id": derived_generation_id, "chunk_id": chunk.id,
                              "relation": "proxy" if chunk.metadata.get("kind") == "media_proxy" else "context"})
        chunk.metadata["media_occurrence_ids"] = sorted(ids)
    return links
