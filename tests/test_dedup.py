"""chunk 内容哈希去重：embedding 侧复用向量，检索侧折叠重复条目。

去重只看 chunk 正文（含标题行）的哈希：正文逐字相同才算重复，标题不同的
段落各有各的语义，不会被误折叠。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig, VectorConfig
from mortis_rag_mcp.indexer import MarkdownIndexer

# 两段正文都带这个 token，保证一次查询能命中全部 chunk。
SHARED = "SHARED123"

ORIGINAL = "教材/lesson1.md"
BACKUP = "教材_Raw_Backup/lesson1.md"

# 两个 section 的标题+正文完全一致 -> 两个 chunk 的内容哈希相同。
DUPLICATE_SECTION = f"# 同一章节\n{SHARED} 重复出现的段落内容"


class CountingProvider:
    """记录每次 embed 收到的文本，用来断言重复内容只被送进去一次。"""

    dimension = 8

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        out = []
        for text in texts:
            digest = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            out.append([float((digest >> index) & 1) for index in range(self.dimension)])
        return out

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


def _config(tmp_path: Path, disk: bool = False, workers: int = 1) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", dimension=8),
        vector=VectorConfig(backend="sqlite_vec" if disk else "memory"),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, embedding_max_workers=workers),
    )


def _write(path: Path, body: str, tags: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntags: {tags}\n---\n{body}\n", encoding="utf-8")


def _indexer(tmp_path: Path, provider: CountingProvider, disk: bool = False, workers: int = 1) -> MarkdownIndexer:
    return MarkdownIndexer(tmp_path / "vault", _config(tmp_path, disk, workers), embedding_provider=provider)


def _embedded(chunks) -> int:
    return sum(1 for chunk in chunks if chunk.embedding is not None and len(chunk.embedding))


@pytest.mark.parametrize("workers", [1, 4])
def test_duplicate_sections_in_one_file_embed_once(tmp_path, workers):
    """同一文件里的逐字重复段落只请求一次 embedding（串行与线程池两条路径）。"""
    _write(tmp_path / "vault" / "a.md", f"{DUPLICATE_SECTION}\n\n{DUPLICATE_SECTION}", "[教材]")
    provider = CountingProvider()
    indexer = _indexer(tmp_path, provider, workers=workers)
    indexer.sync()

    chunks = indexer.all_chunks()
    assert len(chunks) == 2
    assert _embedded(chunks) == 2  # 两条 chunk 都拿到了向量
    # 送进 provider 的文本条数 == 唯一内容哈希数（1 条），而不是 chunk 数。
    assert len(provider.texts) == 1

    # 再 sync 一次：签名未变，零新增请求。
    indexer.sync()
    assert len(provider.texts) == 1


def test_identical_files_reuse_existing_vector(tmp_path):
    """后加进来的整份备份复用已有向量，一次 embedding 都不多花。"""
    vault = tmp_path / "vault"
    _write(vault / ORIGINAL, f"# 章节\n{SHARED} 正文内容", "[教材]")
    provider = CountingProvider()
    indexer = _indexer(tmp_path, provider)
    indexer.sync()
    assert len(provider.texts) == 1

    # 备份目录：正文逐字相同，frontmatter 不同（source 不同 -> chunk.id 不同）。
    _write(vault / BACKUP, f"# 章节\n{SHARED} 正文内容", "[备份]")
    indexer.sync()
    assert len(provider.texts) == 1  # 复用，没有新增请求
    chunks = indexer.all_chunks()
    assert len(chunks) == 2
    assert _embedded(chunks) == 2

    # 第三次 sync：全部命中缓存。
    before = len(provider.texts)
    indexer.sync()
    assert len(provider.texts) == before


def test_modifying_one_duplicate_reembeds_only_that_file(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / ORIGINAL, f"# 章节\n{SHARED} 正文内容", "[教材]")
    _write(vault / BACKUP, f"# 章节\n{SHARED} 正文内容", "[备份]")
    provider = CountingProvider()
    indexer = _indexer(tmp_path, provider)
    indexer.sync()
    assert len(provider.calls) == 2  # 首次同步：两个文件各来一次，都还没算过

    backup_before = {chunk.id for chunk in indexer._chunks[BACKUP]}
    provider.calls.clear()

    # 只改原文件：备份文件的内容哈希没变，不该被重算。
    _write(vault / ORIGINAL, f"# 章节\n{SHARED} 正文内容被修改了", "[教材]")
    indexer.sync()
    assert len(provider.texts) == 1
    assert provider.texts[0].endswith("正文内容被修改了")
    assert {chunk.id for chunk in indexer._chunks[BACKUP]} == backup_before
    assert _embedded(indexer.all_chunks()) == 2


def test_search_dedupe_collapses_identical_chunks(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / ORIGINAL, f"# 章节\n{SHARED} 正文内容", "[教材]")
    _write(vault / BACKUP, f"# 章节\n{SHARED} 正文内容", "[备份]")
    provider = CountingProvider()
    indexer = _indexer(tmp_path, provider)
    indexer.sync()
    assert len(indexer.all_chunks()) == 2

    deduped = indexer.search(SHARED, top_k=10, use_rerank=False)
    assert len(deduped) == 1
    # 留下来的是排序最靠前的那条（source 小的那个）。
    assert deduped[0].source == ORIGINAL

    both = indexer.search(SHARED, top_k=10, use_rerank=False, dedupe=False)
    assert len(both) == 2


def test_search_dedupe_keeps_chunks_without_content_hash(tmp_path):
    """老缓存产生的 chunk 没有 content_hash，一律保留而不是被当成重复删掉。"""
    vault = tmp_path / "vault"
    _write(vault / ORIGINAL, f"# 章节\n{SHARED} 正文内容", "[教材]")
    _write(vault / BACKUP, f"# 章节\n{SHARED} 正文内容", "[备份]")
    indexer = _indexer(tmp_path, CountingProvider())
    indexer.sync()
    for chunk in indexer.all_chunks():
        chunk.metadata.pop("content_hash", None)

    assert len(indexer.search(SHARED, top_k=10, use_rerank=False)) == 2


def test_text_layer_rebuild_does_not_reembed(tmp_path):
    """回归：文本层缓存失效（chunker 版本提升）时，向量必须按 id 命中回收。

    否则每次升级都要把整个库重新 embedding 一遍——这正是 bump chunker 想
    避免的代价。
    """
    vault = tmp_path / "vault"
    _write(vault / ORIGINAL, f"# 章节\n{SHARED} 正文内容", "[教材]")
    _write(vault / "other.md", f"# 别的\n{SHARED} 另一篇内容", "[其它]")
    _indexer(tmp_path, CountingProvider()).sync()

    original_meta = MarkdownIndexer._chunks_meta

    def bumped(self):
        meta = original_meta(self)
        meta["chunker"] = meta["chunker"] + 1
        return meta

    MarkdownIndexer._chunks_meta = bumped
    try:
        provider = CountingProvider()
        indexer = _indexer(tmp_path, provider)
        indexer.sync()
    finally:
        MarkdownIndexer._chunks_meta = original_meta

    assert len(indexer.all_chunks()) == 2
    assert _embedded(indexer.all_chunks()) == 2
    assert len(provider.texts) == 0  # 一个都没重算


def test_disk_backend_reuse_and_dedupe(tmp_path):
    """sqlite_vec 后端下重复上面的断言（向量要从 vec0 表读回来才能复用）。"""
    pytest.importorskip("sqlite_vec", reason="pip install sqlite-vec to test the disk backend")
    vault = (tmp_path / "vault")
    _write(vault / ORIGINAL, f"# 章节\n{SHARED} 正文内容", "[教材]")
    provider = CountingProvider()
    indexer = _indexer(tmp_path, provider, disk=True)
    indexer.sync()
    assert indexer.stats()["vector_backend"] == "sqlite_vec"
    assert len(provider.texts) == 1

    _write(vault / BACKUP, f"# 章节\n{SHARED} 正文内容", "[备份]")
    indexer.sync()
    assert len(provider.texts) == 1  # 复用命中，零新增请求
    # 复用的向量按自己的 chunk.id 落盘，磁盘上两个 chunk 都有向量。
    assert indexer._vector_backend.count() == len(indexer.all_chunks())
    assert _embedded(indexer.all_chunks()) == 0  # 磁盘后端不把向量留在 RAM

    assert len(indexer.search(SHARED, top_k=10, use_rerank=False)) == 1
    assert len(indexer.search(SHARED, top_k=10, use_rerank=False, dedupe=False)) == 2
