"""Chunk / SearchFilter 数据模型与纯内存处理原语（v0.8.0 自 indexer.py 逐字提取）。

兼容契约（P2 闸门 ``tests/test_cache_codec_roundtrip.py`` 锁定）：
- ``Chunk`` 字段序 ``id, content, source, title, metadata, score, embedding``
  即磁盘兼容契约——``_CacheCodec.load`` 按**位置序**重建 Chunk，字段重排 =
  存量缓存静默错位，严禁变更；
- 本模块是 ``_indexer`` 私有包的自底向上基座：只依赖标准库，严禁反向导入
  ``indexer.py`` Facade 或任何兄弟模块。
"""
from __future__ import annotations

import os
from array import array
from dataclasses import dataclass, field
from typing import Any, Callable

# Embeddings are stored as float32 arrays (4 bytes/dim) instead of Python lists
# to keep memory sane: 6157 chunks x 4096 dims would otherwise cost ~800MB.
_EMB_DTYPE = "f"


def _candidate_terms(tokens: list[str]) -> list[str]:
    """把查询词元展开成可用于定位的候选词：英文单词原样，中文按 2-gram 滑窗。"""
    out: list[str] = []
    for token in tokens or []:
        t = token.strip()
        if not t:
            continue
        if "\u4e00" <= t[0] <= "\u9fff":
            if len(t) == 1:
                out.append(t)
                continue
            out.extend(t[i:i + 2] for i in range(len(t) - 1))
        elif len(t) >= 2:
            out.append(t)
    return out


def _extract_snippet(content: str, query_tokens: list[str] | None = None, max_len: int = 150) -> str:
    """从 chunk 正文中提取围绕查询关键词的高光摘要片段（约 100~150 字符）。"""
    if not content:
        return ""
    clean_text = " ".join(content.split())
    if len(clean_text) <= max_len:
        return clean_text

    tokens = query_tokens or []
    # 寻找首个命中的高价值查询词（长度 >= 2，或单字中文）
    match_idx = -1
    for token in tokens:
        t = token.strip()
        if not t or (len(t) < 2 and not ("\u4e00" <= t <= "\u9fff")):
            continue
        idx = clean_text.lower().find(t.lower())
        if idx != -1:
            match_idx = idx
            break

    if match_idx == -1:
        candidates = _candidate_terms(tokens)
        best_term = None
        best_count = 0
        clean_text_lower = clean_text.lower()
        for term in candidates:
            cnt = clean_text_lower.count(term.lower())
            if cnt > best_count:
                best_count = cnt
                best_term = term
        if best_term is not None and best_count > 0:
            match_idx = clean_text_lower.find(best_term.lower())

    if match_idx == -1:
        # 未直接定位到词元，取开头内容
        return clean_text[:max_len].rstrip() + "..."

    # 以匹配词为中心，向两侧各扩展
    half = max_len // 2
    start = max(0, match_idx - half)
    end = min(len(clean_text), start + max_len)
    if end - start < max_len:
        start = max(0, end - max_len)

    snippet = clean_text[start:end].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean_text) else ""
    return f"{prefix}{snippet}{suffix}"


@dataclass
class Chunk:
    id: str
    content: str
    source: str
    title: str
    metadata: dict[str, Any]
    score: float = 0.0
    embedding: array | None = field(default=None, repr=False)

    def to_dict(self, preview: bool = False, query_tokens: list[str] | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "score": self.score,
            "source": self.source,
            "title": self.title,
            "heading": self.metadata.get("heading", self.title),
            "start_line": self.metadata.get("start_line", 1),
            "end_line": self.metadata.get("end_line", 1),
            "metadata": (
                {k: self.metadata[k] for k in ("tags", "mtime") if k in self.metadata}
                if preview
                else dict(self.metadata)
            ),
        }
        if "source_pdf" in self.metadata:
            d["source_pdf"] = self.metadata["source_pdf"]

        if preview:
            d["snippet"] = _extract_snippet(self.content, query_tokens)
            d["char_count"] = len(self.content)
        else:
            d["content"] = self.content
        return d


def path_prefix_match(source: str, prefix: str) -> bool:
    """库内相对 posix 路径的前缀匹配（kb_search 的 SearchFilter.path_prefix 与
    kb_list_files 共用同一口径，避免两套前缀语义漂移）。

    - 归一化：反斜杠 → '/'、去尾部 '/'，Windows 下大小写不敏感；
    - D14：穿透 `.mortis-parsed/` 产物目录，让源目录前缀也能召回对应的 PDF 摄取产物；
    - 空 prefix 返回 True（调用方仍应显式判空，避免「没传前缀 = 全选中」被误解）。

    这里没有路径穿越风险：source 恒为库内相对路径，`../` 或绝对路径只会零命中。
    """
    p = str(prefix or "").replace("\\", "/").rstrip("/")
    if not p:
        return True
    src = source
    if os.name == "nt":
        p = p.casefold()
        src = src.casefold()
    if src.startswith(p):
        return True
    for parsed_dir in (".mortis-parsed/", ".mortis-parsed"):
        pdir = parsed_dir.casefold() if os.name == "nt" else parsed_dir
        if src.startswith(pdir) and src[len(pdir):].lstrip("/").startswith(p):
            return True
    return False


@dataclass
class SearchFilter:
    """kb_search 的过滤条件与分页参数（全部可选）。

    过滤统一在融合排序之后做（FTS 那一路的 path_prefix 只是减少候选量的 SQL
    层下推），所以即使 FTS 索引缺失、或查询太短走不了 BM25，结果集依然正确——
    下推只影响速度，不影响语义。
    """

    path_prefix: str = ""
    tags: list[str] | None = None
    mtime_after: float | None = None
    mtime_before: float | None = None
    offset: int = 0
    limit: int | None = None

    def matches(self, chunk: Chunk) -> bool:
        """chunk 是否满足全部已设置的条件（未设置的条件一律放行）。"""
        if self.path_prefix:
            # 归一化 / 大小写 / 产物目录穿透的细节见 path_prefix_match——它同时被
            # kb_list_files 复用，保证「前缀」在前缀过滤与分页列表里是同一套语义。
            if not path_prefix_match(chunk.source, self.path_prefix):
                return False
        if self.tags:
            wanted = {str(tag).lower().lstrip("#") for tag in self.tags if str(tag).strip()}
            if wanted:
                # 清洗规则与 _is_frontmatter_exempt 保持一致：小写 + 去 # 前缀。
                have = {str(tag).lower().lstrip("#") for tag in chunk.metadata.get("tags") or []}
                if not (wanted & have):
                    return False
        # 老缓存产生的 chunk 没有 mtime 字段，视为"时间未知"直接放行，
        # 否则一次过滤条件就会让整个历史索引检索不到。
        mtime = chunk.metadata.get("mtime")
        if mtime is not None:
            if self.mtime_after is not None and mtime < self.mtime_after:
                return False
            if self.mtime_before is not None and mtime > self.mtime_before:
                return False
        return True

    def page_slice(self, default_limit: int) -> tuple[int, int]:
        """分页区间 [start, end)：limit 未设置时回落到调用方的 top_k。"""
        start = max(0, int(self.offset))
        limit = self.limit if self.limit is not None else default_limit
        return start, start + max(0, int(limit))


def dedupe_by_content_hash(items: list[Any], chunk_of: Callable[[Any], Chunk] | None = None) -> list[Any]:
    """按 content_hash 保序去重：同一份内容只留排在最前面的那条。

    items 默认就是 Chunk 列表；跨库 fan-out 传的是 (vault_entry, chunk) 元组，
    用 chunk_of 把 chunk 取出来即可。没有 content_hash 的老 chunk 一律保留——
    宁可多返回一条，也不能把内容未知的 chunk 误判成重复删掉。
    """
    extract = chunk_of if chunk_of is not None else (lambda item: item)
    seen: set[str] = set()
    kept: list[Any] = []
    for item in items:
        digest = extract(item).metadata.get("content_hash")
        if digest is None:
            kept.append(item)
            continue
        if digest in seen:
            continue
        seen.add(digest)
        kept.append(item)
    return kept
