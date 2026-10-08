"""切块算法 / Markdown 解析 / Frontmatter / 图片注释注入 / 表格保护（v0.8.0 自 indexer.py 逐字提取）。

模块边界：
- 纯算法与切块函数集合，严禁运行时反向导入 ``mortis_rag_mcp.indexer`` Facade；
- 依赖上游 ``models.py``（Chunk）与 ``ingest/tables.py``（表格识别与分块）；
- 接缝保持：``_inject_image_notes`` 是历史测试锚点，Facade 保留 re-export，
  并在 ``_chunk_file`` 调用时由 Facade 以参数注入，测试对 Facade 模块属性的
  monkeypatch 依然能被观察到（D2 决策）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from ..ingest.tables import iter_table_blocks, split_table_into_chunks
from .models import Chunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

_BLOCK_IGNORE_START = re.compile(r"^\s*<!--\s*(?:rag-ignore|rag:ignore|no-rag|norag)\s*-->", re.IGNORECASE)
_BLOCK_IGNORE_END = re.compile(r"^\s*<!--\s*(?:/rag-ignore|/rag:ignore|/no-rag|/norag|end-rag-ignore)\s*-->", re.IGNORECASE)

# 图片注入（inject_image_captions）：标准 Markdown 图片与 Obsidian wiki 图片嵌入。
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"([^\"]*)\")?\s*\)")
_WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
_FENCE_START_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_FENCE_RE = _FENCE_START_RE

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
_INDEXABLE_TEXT_EXTS = frozenset({".md", ".txt"})
_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(第[0-9一二三四五六七八九十百千]+[章回节卷]|Chapter\s+[0-9]+)\s*(.*)$",
    re.IGNORECASE,
)


def _is_chapter_heading(line: str) -> tuple[bool, str]:
    """检测单行是否为网络小说/书籍的章节标题。

    门禁规则（F-09）：
    1. 行长门禁：去除首尾空格后长度 <= 60 字符，超长的一律视为正文段落；
    2. 标点门禁：标题不能以句末标点（如 '。'、'！'、'？'、'!'、'?'、'；'、';'、'…'）结尾；
       也不能包含逗号（'，'、','）、分号（'；'、';'）或对话引号（'“'、'”'、'"'）；
    3. 正则匹配：匹配『第X章/回/节/卷 标题』或『Chapter X Title』。
    """
    s = line.strip()
    if not s or len(s) > 60:
        return False, ""
    if s.endswith(("。", "！", "？", "!", "?", "；", ";", "…")):
        return False, ""
    if any(p in s for p in ("，", ",", "；", ";", "“", "”", '"')):
        return False, ""
    m = _CHAPTER_HEADING_RE.match(s)
    if not m:
        return False, ""
    prefix = m.group(1).strip()
    suffix = m.group(2).strip()
    heading_title = f"{prefix} {suffix}".strip() if suffix else prefix
    return True, heading_title


def _image_note(alt: str, caption: str, path: str) -> str:
    """一张图片对应的一行注入文本：`[图片: alt 图注 (文件名)]`。"""
    name = Path(path.replace("\\", "/")).stem
    descriptor = " ".join(part for part in (alt, caption) if part)
    return f"[图片: {descriptor} ({name})]" if descriptor else f"[图片: {name}]"


def _image_notes_for_line(line: str) -> list[str]:
    """提取一行里的所有图片，返回要插入的注入行（没有图片则返回空列表）。"""
    notes: list[str] = []
    for match in _MD_IMAGE_RE.finditer(line):
        alt = match.group(1).strip()
        path = match.group(2)
        title = (match.group(3) or "").strip()
        notes.append(_image_note(alt, title, path))
    for match in _WIKI_IMAGE_RE.finditer(line):
        path = match.group(1).strip()
        caption = (match.group(2) or "").strip()
        if Path(path.replace("\\", "/")).suffix.lower() not in _IMAGE_EXTS:
            continue
        notes.append(_image_note("", caption, path))
    return notes


def _inject_image_notes(lines: list[str]) -> list[str]:
    """把图片的 alt / 图注变成可检索的正文行（纯函数，不修改输入）。

    Markdown 里的图片只有路径引用，alt 与图注（Obsidian 的 ``![[path|图注]]``）
    原本不会进入 chunk 正文，语义检索不到「这张图讲了什么」。本函数在每张图片
    所在行之后插入一行 ``[图片: alt 图注 (文件名)]``。

    代价：chunk content 变了 → chunk.id 变了 → 全库重新 embedding。所以由
    ``config.inject_image_captions`` 门控，默认必须关闭。代码块（``` / ~~~）
    里的图片语法只是示例文本，不注入。
    """
    result: list[str] = []
    fence_char: str | None = None
    fence_len: int = 0
    for line in lines:
        result.append(line)
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
        result.extend(_image_notes_for_line(line))
    return result


def frontmatter(lines: list[str]) -> tuple[int, list[str], dict[str, Any]]:
    if len(lines) < 2 or lines[0].strip() != "---":
        return -1, [], {}
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), -1)
    if end < 0:
        return -1, [], {}
    tags: list[str] = []
    properties: dict[str, Any] = {}
    current_list_key: str | None = None

    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list_key:
            val = stripped[2:].strip().strip("'\"")
            if isinstance(properties.get(current_list_key), list):
                properties[current_list_key].append(val)
            continue
        current_list_key = None
        if ":" in line:
            key, raw_val = line.split(":", 1)
            key = key.strip().lower()
            val = raw_val.strip()
            if not val:
                properties[key] = []
                current_list_key = key
                continue
            if val.startswith("[") and val.endswith("]"):
                items = [item.strip().strip("'\"") for item in val[1:-1].split(",") if item.strip()]
                properties[key] = items
            elif val.lower() in {"true", "yes", "on"}:
                properties[key] = True
            elif val.lower() in {"false", "no", "off"}:
                properties[key] = False
            else:
                properties[key] = val.strip("'\"")

    raw_tags = properties.get("tags")
    if isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags if str(t).strip()]
    elif isinstance(raw_tags, str) and raw_tags:
        tags = [item.strip().strip("'\"") for item in raw_tags.split(",") if item.strip()]

    return end, tags, properties


def is_frontmatter_exempt(
    tags: list[str],
    properties: dict[str, Any],
    exclude_frontmatter_keys: Iterable[str] = (),
    exclude_tags: Iterable[str] = (),
) -> tuple[bool, str | None]:
    if "rag" in properties:
        val = properties["rag"]
        if val is False or (isinstance(val, str) and val.lower() in {"false", "no", "off", "0"}):
            return True, "frontmatter property 'rag: false'"

    for key in exclude_frontmatter_keys:
        key_lower = key.lower()
        if key_lower in properties:
            val = properties[key_lower]
            if val is True or (isinstance(val, str) and val.lower() in {"true", "yes", "on", "1"}):
                return True, f"frontmatter property '{key}: true'"

    exclude_tags_lower = {t.lower().lstrip("#") for t in exclude_tags}
    for tag in tags:
        tag_clean = tag.lower().lstrip("#")
        if tag_clean in exclude_tags_lower:
            return True, f"frontmatter tag '{tag}'"

    return False, None


def strip_ignored_blocks(body: list[str]) -> tuple[list[str], bool]:
    cleaned: list[str] = []
    in_ignore = False
    has_ignores = False

    for line in body:
        if not in_ignore:
            if _BLOCK_IGNORE_START.search(line):
                in_ignore = True
                has_ignores = True
                cleaned.append("")
                if _BLOCK_IGNORE_END.search(line):
                    in_ignore = False
            else:
                cleaned.append(line)
        else:
            cleaned.append("")
            if _BLOCK_IGNORE_END.search(line):
                in_ignore = False

    return cleaned, has_ignores


def clean_heading(heading: str) -> str:
    return re.sub(r"\s+#*$", "", heading).strip()


def extract_title(source: str, body: list[str]) -> str:
    fence_char: str | None = None
    fence_len: int = 0
    for line in body:
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
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return clean_heading(match.group(2))

    fence_char = None
    fence_len = 0
    for line in body:
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
        match = _HEADING_RE.match(line)
        if match:
            return clean_heading(match.group(2))
    return Path(source).stem


def overlap_tail(lines: list[str], overlap: int) -> tuple[list[str], int]:
    """Return the trailing lines whose total length covers ``overlap`` chars."""
    if overlap <= 0:
        return [], 0
    tail: list[str] = []
    length = 0
    for line in reversed(lines):
        tail.append(line)
        length += len(line) + 1
        if length >= overlap:
            break
    return list(reversed(tail)), length


def new_chunk(
    source: str,
    title: str,
    heading: str,
    start: int,
    end: int,
    index: int,
    tags: list[str],
    lines: list[str],
    mtime: float | None = None,
    source_pdf: str | None = None,
    aliases: list[str] | None = None,
) -> Chunk:
    content = "\n".join(lines).strip()
    identifier = hashlib.sha1(f"{source}\0{index}\0{content}".encode("utf-8")).hexdigest()
    meta = {
        "heading": heading,
        "start_line": start,
        "end_line": max(start, end),
        "chunk_index": index,
        "tags": list(tags),
        "mtime": mtime,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
    }
    if source_pdf:
        meta["source_pdf"] = source_pdf
    # 仅在 aliases 非空时写入 metadata，避免无别名笔记产生冗余 key 导致元数据膨胀与 v073 金测漂移
    if aliases:
        seen_aliases: set[str] = set()
        clean_aliases: list[str] = []
        for a in aliases:
            if a is None or isinstance(a, bool):
                continue
            s = str(a).strip().strip("'\"").strip()
            if s and s not in seen_aliases:
                seen_aliases.add(s)
                clean_aliases.append(s)
        if clean_aliases:
            meta["aliases"] = clean_aliases
    return Chunk(identifier, content, source, title, meta)


def make_chunks(
    source: str,
    title: str,
    tags: list[str],
    sections: list[tuple[str, int, list[str]]],
    chunk_size: int,
    chunk_overlap: int,
    mtime: float | None = None,
    source_pdf: str | None = None,
    aliases: list[str] | None = None,
    *,
    chunking: Any = None,
    source_text: str | None = None,
    virtual_identity: dict[str, str] | None = None,
) -> list[Chunk]:
    from .token_chunking import chunking_profile, make_token_chunks

    if chunking_profile(chunking)["mode"] == "estimated_tokens":
        return make_token_chunks(
            source, title, tags, sections, profile=chunking, new_chunk_fn=new_chunk,
            mtime=mtime, source_pdf=source_pdf, aliases=aliases,
            source_text=source_text, virtual_identity=virtual_identity,
        )
    result: list[Chunk] = []
    chunk_index = 0
    overlap = chunk_overlap
    for heading, start, lines in sections:
        table_blocks = iter_table_blocks(lines)
        table_map = {s: e for s, e in table_blocks}
        offset = 0
        current: list[str] = []
        current_start = start
        current_length = 0
        carry: list[str] = []
        carry_length = 0

        while offset < len(lines):
            if offset in table_map:
                # 遇到表格开始：先将此前积攒的正文文本 flush 为 chunk
                if current:
                    result.append(
                        new_chunk(
                            source,
                            title,
                            heading,
                            current_start,
                            start + offset - 1,
                            chunk_index,
                            tags,
                            current,
                            mtime,
                            source_pdf=source_pdf,
                            aliases=aliases,
                        )
                    )
                    chunk_index += 1
                    current = []
                    current_length = 0
                    carry = []
                    carry_length = 0

                s = offset
                e = table_map[s]
                tbl_lines = lines[s : e + 1]
                tbl_chars = sum(len(l) + 1 for l in tbl_lines)

                if tbl_chars <= chunk_size:
                    # 整个表格未超预算：作为一个完整原子 chunk
                    result.append(
                        new_chunk(
                            source,
                            title,
                            heading,
                            start + s,
                            start + e,
                            chunk_index,
                            tags,
                            tbl_lines,
                            mtime,
                            source_pdf=source_pdf,
                            aliases=aliases,
                        )
                    )
                    chunk_index += 1
                else:
                    # 表格超预算：使用 split_table_into_chunks 做保真且受限的分片 (D3, D4a, D4b, D5)
                    tbl_chunks = split_table_into_chunks(tbl_lines, chunk_size)
                    for sub_s, sub_e, chunk_lines in tbl_chunks:
                        result.append(
                            new_chunk(
                                source,
                                title,
                                heading,
                                start + s + sub_s,
                                start + s + sub_e,
                                chunk_index,
                                tags,
                                chunk_lines,
                                mtime,
                                source_pdf=source_pdf,
                                aliases=aliases,
                            )
                        )
                        chunk_index += 1

                # 表格处理完毕，直接跳到表格末尾下一行，绝对不在 lines 里插入额外虚行，行号绝不漂移 (D4c)
                offset = e + 1
                current_start = start + offset
                continue

            line = lines[offset]
            # 非表格行超长按字符切块
            if len(line) > chunk_size:
                if current:
                    result.append(
                        new_chunk(
                            source,
                            title,
                            heading,
                            current_start,
                            start + offset - 1,
                            chunk_index,
                            tags,
                            current,
                            mtime,
                            source_pdf=source_pdf,
                            aliases=aliases,
                        )
                    )
                    chunk_index += 1
                    carry, carry_length = overlap_tail(current, overlap)
                    current = list(carry)
                    current_start = start + offset - len(carry)
                    current_length = carry_length
                for piece_start in range(0, len(line), chunk_size):
                    piece = line[piece_start : piece_start + chunk_size]
                    if current:
                        result.append(
                            new_chunk(
                                source,
                                title,
                                heading,
                                current_start,
                                start + offset - 1,
                                chunk_index,
                                tags,
                                current,
                                mtime,
                                source_pdf=source_pdf,
                                aliases=aliases,
                            )
                        )
                        chunk_index += 1
                    current = [piece]
                    current_length = len(piece) + 1
                    current_start = start + offset
                offset += 1
                continue

            if current and current_length + len(line) + 1 > chunk_size:
                result.append(
                    new_chunk(
                        source,
                        title,
                        heading,
                        current_start,
                        start + offset - 1,
                        chunk_index,
                        tags,
                        current,
                        mtime,
                        source_pdf=source_pdf,
                        aliases=aliases,
                    )
                )
                chunk_index += 1
                carry, carry_length = overlap_tail(current, overlap)
                current = list(carry)
                current_start = start + offset - len(carry)
                current_length = carry_length

            current.append(line)
            current_length += len(line) + 1
            offset += 1

        if current:
            result.append(
                new_chunk(
                    source,
                    title,
                    heading,
                    current_start,
                    start + len(lines) - 1,
                    chunk_index,
                    tags,
                    current,
                    mtime,
                    source_pdf=source_pdf,
                    aliases=aliases,
                )
            )
            chunk_index += 1
    if virtual_identity is not None:
        from .token_chunking import chunker_fingerprint, virtual_chunk_id

        fingerprint = chunker_fingerprint(chunking)
        for chunk in result:
            chunk.id = virtual_chunk_id(chunk, virtual_identity, fingerprint)
            chunk.metadata.update(dict(virtual_identity))
            chunk.metadata["chunker_fingerprint"] = fingerprint
    return result


def chunk_file(
    source: str,
    text: str,
    config: Any,
    mtime: float | None = None,
    inject_image_notes_fn: Callable[[list[str]], list[str]] | None = None,
    *,
    chunking_config: Any = None,
    virtual_identity: dict[str, str] | None = None,
) -> list[Chunk]:
    if inject_image_notes_fn is None:
        inject_image_notes_fn = _inject_image_notes
    lines = text.splitlines()
    frontmatter_end, tags, properties = frontmatter(lines)
    is_fm_exempt, _ = is_frontmatter_exempt(
        tags,
        properties,
        exclude_frontmatter_keys=getattr(config, "exclude_frontmatter_keys", ()),
        exclude_tags=getattr(config, "exclude_tags", ()),
    )
    if is_fm_exempt:
        return []
    raw_aliases = properties.get("aliases")
    aliases: list[str] = []
    seen_aliases: set[str] = set()
    if isinstance(raw_aliases, list):
        for a in raw_aliases:
            if a is None or isinstance(a, bool):
                continue
            s = str(a).strip().strip("'\"").strip()
            if s and s not in seen_aliases:
                seen_aliases.add(s)
                aliases.append(s)
    elif isinstance(raw_aliases, str):
        # 兼容中文逗号（中文笔记常见）与英文逗号分隔，去空去重
        parts = raw_aliases.replace("，", ",").split(",")
        for item in parts:
            s = item.strip().strip("'\"").strip()
            if s and s not in seen_aliases:
                seen_aliases.add(s)
                aliases.append(s)

    body_start = frontmatter_end + 1
    body = lines[body_start:]
    body, _ = strip_ignored_blocks(body)
    if getattr(config, "inject_image_captions", False):
        body = inject_image_notes_fn(body)
    title = extract_title(source, body)
    sections: list[tuple[str, int, list[str]]] = []
    current_heading = title
    current_start = body_start + 1
    current_lines: list[str] = []
    fence_char: str | None = None
    fence_len: int = 0
    body_table_blocks = iter_table_blocks(body)
    in_table_lines = {i for s, e in body_table_blocks for i in range(s, e + 1)}
    for offset, line in enumerate(body):
        line_number = body_start + offset + 1
        m = _FENCE_START_RE.match(line)
        if m:
            token = m.group(1)
            char, length = token[0], len(token)
            if fence_char is None:
                fence_char = char
                fence_len = length
            elif char == fence_char and length >= fence_len:
                fence_char = None
                fence_len = 0
        in_fence = fence_char is not None
        in_table = offset in in_table_lines
        heading_text: str | None = None
        if not in_fence and not in_table:
            match = _HEADING_RE.match(line)
            if match:
                heading_text = clean_heading(match.group(2))
            else:
                is_ch, ch_title = _is_chapter_heading(line)
                if is_ch:
                    heading_text = ch_title

        if heading_text is not None:
            if any(l.strip() for l in current_lines):
                sections.append((current_heading, current_start, current_lines))
            current_heading = heading_text
            current_start = line_number
            current_lines = [line]
        else:
            if not current_lines:
                if line.strip():
                    current_start = line_number
                    current_lines.append(line)
            else:
                current_lines.append(line)
    if any(l.strip() for l in current_lines):
        sections.append((current_heading, current_start, current_lines))
    if not sections and body:
        if any(line.strip() for line in body):
            sections = [(title, body_start + 1, body)]
    return make_chunks(
        source=source,
        title=title,
        tags=tags,
        sections=sections,
        chunk_size=getattr(config, "chunk_size", 800),
        chunk_overlap=getattr(config, "chunk_overlap", 120),
        mtime=mtime,
        source_pdf=properties.get("source_pdf"),
        aliases=aliases if aliases else None,
        chunking=chunking_config if chunking_config is not None else getattr(config, "chunking", None),
        source_text=text,
        virtual_identity=virtual_identity,
    )


# 模块内部别名兼容
_frontmatter = frontmatter
_is_frontmatter_exempt = is_frontmatter_exempt
_strip_ignored_blocks = strip_ignored_blocks
_clean_heading = clean_heading
_title = extract_title
_overlap_tail = overlap_tail
_new_chunk = new_chunk
_make_chunks = make_chunks
_chunk_file = chunk_file
