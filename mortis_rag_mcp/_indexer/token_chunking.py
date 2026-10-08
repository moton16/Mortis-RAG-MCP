"""Deterministic estimated-token chunking; no tokenizer or network dependency."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Mapping
from typing import Any

from ..ingest.tables import iter_table_blocks
from .models import Chunk

ESTIMATOR_VERSION = "unicode-estimate-v1"
STRUCTURE_GUARD_VERSION = "markdown-guard-v1"


def _value(profile: Any, key: str, default: Any) -> Any:
    return profile.get(key, default) if isinstance(profile, Mapping) else getattr(profile, key, default)


def _legacy_chars(profile: Any) -> list[int] | None:
    size = _value(profile, "legacy_chunk_size", _value(profile, "chunk_size", None))
    overlap = _value(profile, "legacy_chunk_overlap", _value(profile, "chunk_overlap", None))
    if size is None or overlap is None:
        return None
    if (isinstance(size, bool) or not isinstance(size, int) or size <= 0
            or isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0):
        raise ValueError("legacy chunk_size/chunk_overlap must be integers (size > 0, overlap >= 0)")
    return [int(size), int(overlap)]


def chunking_profile(profile: Any = None) -> dict[str, Any]:
    mode = _value(profile, "mode", "legacy_chars")
    if mode == "legacy":
        mode = "legacy_chars"
    if mode not in {"legacy_chars", "estimated_tokens"}:
        raise ValueError("unknown chunking mode")
    values = {key: _value(profile, canonical, _value(profile, key, default))
              for key, canonical, default in (("target", "target_tokens", 384),
                                              ("overlap", "overlap_tokens", 64),
                                              ("hard_limit", "hard_limit_tokens", 768))}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"chunking.{key} must be finite numeric")
    if not 0 <= values["overlap"] < values["target"] <= values["hard_limit"]:
        raise ValueError("require 0 <= overlap < target <= hard_limit")
    result = {"mode": mode, **values, "estimator_version": ESTIMATOR_VERSION,
              "structure_guard_version": STRUCTURE_GUARD_VERSION, "proxy_version": "proxy-v1"}
    if mode == "legacy_chars":
        # legacy 字符参数（chunk_size/chunk_overlap）此前完全不在身份指纹里：改这两个
        # 值不会改变 fingerprint → 虚拟 chunk ID、derived generation 与缓存判据都会
        # 误判「同一代」。显式纳入，只有真正没提供参数的 profile（纯算法调用）才省略。
        legacy = _legacy_chars(profile)
        if legacy is not None:
            result["legacy_chars"] = legacy
    return result


def chunker_fingerprint(profile: Any = None) -> str:
    payload = {"chunker": 7, **chunking_profile(profile)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _weights(text: str) -> list[float]:
    weights: list[float] = []
    in_word = False
    for char in text:
        code = ord(char)
        if char.isspace():
            weight = 0.15
        elif (0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or
              0x20000 <= code <= 0x323AF):
            weight = 0.62
        elif char.isascii() and (char.isalnum() or char == "_"):
            weight = 0.0 if in_word else 1.30
        elif unicodedata.category(char).startswith("M"):
            weight = 0.0
        else:
            weight = 1.15
        in_word = char.isascii() and (char.isalnum() or char == "_")
        # Long identifiers cannot become arbitrarily cheap atomic lines.
        if in_word:
            weight = max(weight, 0.25)
        weights.append(weight)
    return weights


def estimate_tokens(text: str) -> float:
    return sum(_weights(text))


def embedding_key(exact_input: str, space_fingerprint: str,
                  template_fingerprint: str = "text-v1", media_hashes=()) -> str:
    payload = [space_fingerprint, template_fingerprint, exact_input, list(media_hashes)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def virtual_chunk_id(chunk: Chunk, identity: Mapping[str, str], fingerprint: str) -> str:
    fields = [identity[name] for name in ("store_uuid", "doc_id", "revision_id")]
    if not all(isinstance(item, str) and item for item in fields):
        raise ValueError("virtual identity requires nonempty store/doc/revision strings")
    payload = [*fields, fingerprint, chunk.metadata["chunk_index"], chunk.content]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def estimate_reembedding_cost(old_chunks, new_chunks, *, space_fingerprint: str,
                              template_fingerprint: str = "text-v1", batch_size: int = 32,
                              price_per_million_chars: float | None = None) -> dict[str, Any]:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    if price_per_million_chars is not None and (isinstance(price_per_million_chars, bool) or
            not isinstance(price_per_million_chars, (int, float)) or
            not math.isfinite(price_per_million_chars) or price_per_million_chars < 0):
        raise ValueError("price must be finite and nonnegative")
    old = list(old_chunks)
    new = list(new_chunks)
    reusable = {c.metadata.get("embedding_key") for c in old if c.embedding is not None}
    pending: dict[str, str] = {}
    reused = 0
    for chunk in new:
        key = embedding_key(chunk.content, space_fingerprint, template_fingerprint,
                            chunk.metadata.get("media_hashes", ()))
        if key in reusable:
            reused += 1
        elif not chunk.metadata.get("embedding_disabled"):
            pending[key] = chunk.content
    chars = sum(map(len, pending.values()))
    return {"old_chunk_count": len(old), "new_chunk_count": len(new),
            "reusable_chunk_count": reused, "embedding_input_count": len(pending),
            "estimated_embed_chars": chars, "estimated_requests": math.ceil(len(pending) / batch_size),
            "estimated_cost": None if price_per_million_chars is None else chars * price_per_million_chars / 1e6,
            "cost_basis": "characters" if price_per_million_chars is not None else "unknown"}


def _pieces(text: str, budget: float):
    prefix = [0.0]
    for weight in _weights(text):
        prefix.append(prefix[-1] + weight)
    start = 0
    while start < len(text):
        end = min(len(text), bisect_right(prefix, prefix[start] + budget) - 1)
        end = max(start + 1, end)
        while end < len(text) and unicodedata.category(text[end]).startswith("M"):
            end += 1
        if end < len(text):
            boundary = max((i + 1 for i in range(start, end) if text[i] in " \t.!?;。！？；"), default=end)
            if boundary > start + (end - start) // 2:
                end = boundary
        yield start, end, text[start:end]
        start = end


# An item contains exact source text and its real line-local half-open span.
def _item(text: str, line: int, start: int = 0, end: int | None = None):
    return (text, {"line": line, "start_char": start, "end_char": len(text) if end is None else end})


def _render(items, prefix: str = "", suffix: str = "") -> str:
    return prefix + "\n".join(item[0] for item in items) + suffix


def make_token_chunks(source, title, tags, sections, *, profile, new_chunk_fn,
                      mtime=None, source_pdf=None, aliases=None, source_text=None,
                      virtual_identity=None) -> list[Chunk]:
    settings = chunking_profile(profile)
    fingerprint = chunker_fingerprint(profile)
    target, overlap, hard = (settings[k] for k in ("target", "overlap", "hard_limit"))
    result: list[Chunk] = []
    # Match parser-preserved lines against the original, not injected virtual line numbers.
    source_map = {}
    if source_text is not None:
        from difflib import SequenceMatcher

        original = source_text.splitlines()
        indexed = [(start + i, line) for _, start, lines in sections for i, line in enumerate(lines)]
        matcher = SequenceMatcher(None, original, [line for _, line in indexed], autojunk=False)
        for block in matcher.get_matching_blocks():
            for i in range(block.size):
                source_map[indexed[block.b + i][0]] = block.a + i + 1

    def emit(heading, items, prefix="", suffix="", kind="", oversize=False):
        content = _render(items, prefix, suffix)
        if not content.strip():
            return
        spans = []
        synthetic = []
        for text, span in items:
            if source_text is None or span["line"] in source_map:
                spans.append({**span, "line": source_map.get(span["line"], span["line"])})
            elif text:
                synthetic.append({"kind": "parser_generated", "text": text, "position": "body"})
        anchor = next((source_map[item[1]["line"]] for item in items if item[1]["line"] in source_map),
                      min(source_map.values(), default=items[0][1]["line"]))
        chunk = new_chunk_fn(source, title, heading, spans[0]["line"] if spans else anchor,
                             spans[-1]["line"] if spans else anchor, len(result), tags, [content],
                             mtime, source_pdf=source_pdf, aliases=aliases)
        if prefix:
            synthetic.append({"kind": kind, "text": prefix, "position": "prefix"})
        if suffix:
            synthetic.append({"kind": kind, "text": suffix, "position": "suffix"})
        # Estimated mode preserves whitespace so every reported span remains literal source text.
        chunk.content = content
        chunk.metadata["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        chunk.id = hashlib.sha1(f"{source}\0{len(result)}\0{content}".encode("utf-8")).hexdigest()
        chunk.metadata.update({"chunking": dict(settings), "chunker_fingerprint": fingerprint,
                               "source_spans": spans, "synthetic_segments": synthetic,
                               "estimated_tokens": estimate_tokens(chunk.content)})
        if oversize or estimate_tokens(chunk.content) > hard:
            chunk.metadata.update({"oversize": True, "embedding_disabled": True})
        if virtual_identity is not None:
            chunk.id = virtual_chunk_id(chunk, virtual_identity, fingerprint)
            chunk.metadata.update(dict(virtual_identity))
        result.append(chunk)

    def grouped(heading, items, *, prefix="", suffix="", kind="", use_overlap=False):
        current = []
        fresh = False
        overhead = estimate_tokens(prefix + suffix)
        for item in items:
            if current and estimate_tokens(_render(current + [item], prefix, suffix)) > target:
                if fresh:
                    emit(heading, current, prefix, suffix, kind)
                carry = []
                if use_overlap:
                    for old in reversed(current):
                        if estimate_tokens(_render([old] + carry)) > overlap:
                            break
                        carry.insert(0, old)
                current = carry
                fresh = False
            if current and estimate_tokens(_render(current + [item], prefix, suffix)) > hard:
                current = []
            if estimate_tokens(_render([item], prefix, suffix)) > hard:
                if current and fresh:
                    emit(heading, current, prefix, suffix, kind)
                current = []
                fresh = False
                # Pathological headers are explicit non-embedding chunks, not an infinite retry.
                if overhead >= hard or kind == "table_header":
                    emit(heading, [item], prefix, suffix, kind, True)
                    continue
                for a, b, text in _pieces(item[0], max(0.01, min(target, hard - overhead - 2))):
                    span = item[1]
                    emit(heading, [_item(text, span["line"], span["start_char"] + a,
                                         span["start_char"] + b)], prefix, suffix, kind)
                continue
            current.append(item)
            fresh = True
        if current and fresh:
            emit(heading, current, prefix, suffix, kind)

    for heading, start, lines in sections:
        tables = dict(iter_table_blocks(lines))
        offset = 0
        ordinary = []
        while offset < len(lines):
            fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", lines[offset])
            if offset not in tables and fence is None:
                ordinary.append(_item(lines[offset], start + offset))
                offset += 1
                continue
            if ordinary:
                grouped(heading, ordinary, use_overlap=True)
                ordinary = []
            if offset in tables:
                end = tables[offset]
                items = [_item(lines[i], start + i) for i in range(offset, end + 1)]
                if estimate_tokens(_render(items)) <= hard:
                    emit(heading, items)
                else:
                    header = items[:2]
                    emit(heading, header, oversize=estimate_tokens(_render(header)) > hard)
                    grouped(heading, items[2:], prefix=_render(header) + "\n", kind="table_header")
                offset = end + 1
                continue
            token = fence.group(1)
            end = offset + 1
            closed = False
            while end < len(lines):
                match = re.match(r"^\s*([`~]+)\s*$", lines[end])
                if match and match.group(1)[0] == token[0] and len(match.group(1)) >= len(token):
                    closed = True
                    break
                end += 1
            stop = end + 1 if closed else len(lines)
            items = [_item(lines[i], start + i) for i in range(offset, stop)]
            if closed and estimate_tokens(_render(items)) <= hard:
                emit(heading, items)
            else:
                # Original delimiters retain source spans; only added delimiters are synthetic.
                emit(heading, items[:1], suffix="\n" + token, kind="code_fence")
                body = items[1:-1] if closed else items[1:]
                grouped(heading, body, prefix=lines[offset] + "\n", suffix="\n" + token,
                        kind="code_fence")
                if closed:
                    emit(heading, items[-1:], prefix=lines[offset] + "\n", kind="code_fence")
            offset = stop
        if ordinary:
            grouped(heading, ordinary, use_overlap=True)
    return result

