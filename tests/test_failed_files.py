from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.providers import ProviderError


class FlakyProvider:
    """可编程失败的 embedding provider：fail=True 时每次调用都抛 ProviderError。"""

    def __init__(self, dimension: int = 8, fail: bool = True) -> None:
        self.calls = 0
        self.dimension = dimension
        self.fail = fail

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise ProviderError("429 rate limit exceeded")
        return [[float(index % 7) / 7.0 for index in range(self.dimension)] for _ in texts]


def _config(tmp_path: Path, **cache_kwargs: Any) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="external", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, **cache_kwargs),
    )


def _failed_json(tmp_path: Path) -> Path | None:
    """缓存 namespace 目录下的 failed.json，不存在时返回 None。"""
    matches = sorted((tmp_path / "cache" / "default").glob("*.failed.json"))
    return matches[0] if matches else None


def _failed_payload(tmp_path: Path) -> dict[str, Any] | None:
    path = _failed_json(tmp_path)
    return None if path is None else json.loads(path.read_text(encoding="utf-8"))


def test_failed_embedding_is_persisted_and_restored_by_a_new_instance(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    provider = FlakyProvider()
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider)

    indexer.sync()

    assert "a.md" in indexer.failed_files
    payload = _failed_payload(tmp_path)
    assert payload is not None
    assert payload["version"] == 1
    assert payload["files"]["a.md"] == "429 rate limit exceeded"

    # 模拟重启：同一个缓存目录新建实例，不 sync 也该看得到上一轮的失败。
    revived = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=FlakyProvider())
    assert revived.stats()["failed_files"] == {"a.md": "429 rate limit exceeded"}


def test_successful_retry_clears_the_persisted_failure(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    provider = FlakyProvider()
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider)
    indexer.sync()
    assert _failed_json(tmp_path) is not None

    # 文件本身没变（签名未变），但缺向量 -> 下一次 sync 天然重试。
    provider.fail = False
    revived = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider)
    revived.sync()

    assert revived.failed_files == {}
    assert revived.all_chunks()
    assert all(len(chunk.embedding) for chunk in revived.all_chunks())
    # 名单清空后不留空壳文件。
    assert _failed_json(tmp_path) is None


def test_undecodable_file_is_recorded_and_cleared_once_fixed(tmp_path):
    bad = tmp_path / "乱码.md"
    bad.write_bytes("# 中文标题\n这是 GBK 编码的内容".encode("gbk"))
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=FlakyProvider(fail=False))

    indexer.sync()

    assert "乱码.md" in indexer.failed_files
    payload = _failed_payload(tmp_path)
    assert payload is not None and "乱码.md" in payload["files"]

    bad.write_text("# 中文标题\n这是 UTF-8 编码的内容", encoding="utf-8")
    indexer.sync()

    assert "乱码.md" not in indexer.failed_files
    assert any(chunk.source == "乱码.md" for chunk in indexer.all_chunks())
    assert _failed_json(tmp_path) is None


def test_rebuild_drops_the_persisted_failures(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    provider = FlakyProvider()
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=provider)
    indexer.sync()
    assert _failed_json(tmp_path) is not None

    provider.fail = False
    indexer.rebuild()

    assert indexer.failed_files == {}
    assert _failed_json(tmp_path) is None


def test_purge_cache_drops_the_persisted_failures(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=FlakyProvider())
    indexer.sync()
    assert _failed_json(tmp_path) is not None

    assert indexer.purge_cache() is True

    assert indexer.failed_files == {}
    assert _failed_json(tmp_path) is None


def test_corrupt_failed_json_is_silently_ignored(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    indexer = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=FlakyProvider())
    indexer.sync()
    failed_path = _failed_json(tmp_path)
    assert failed_path is not None
    failed_path.write_text("{ not json at all", encoding="utf-8")

    # 坏文件只是少了可观测性，不能拖垮启动、也不该抛异常。
    revived = MarkdownIndexer(tmp_path, _config(tmp_path), embedding_provider=FlakyProvider(fail=False))
    assert revived.stats()["failed_files"] == {}
    revived.sync()
    assert revived.failed_files == {}
