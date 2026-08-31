"""B2-B4 关键修复的回归测试（紧凑版，只锁最易回归的行为）。"""

from __future__ import annotations

import json
from array import array
from pathlib import Path
from typing import Any

import pytest

from vault_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from vault_mcp.indexer import MarkdownIndexer
from vault_mcp.providers import StaticEmbeddingProvider


class CountingProvider(StaticEmbeddingProvider):
    def __init__(self, dimension: int = 8) -> None:
        super().__init__(dimension)
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return super().embed(texts)


def _config(tmp_path: Path, **cache_kwargs: Any) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, **cache_kwargs),
    )


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "a.md").write_text("# A\n半导体物理是微电子专业的核心课程", encoding="utf-8")
    (vault / "b.md").write_text("# B\nsecond note", encoding="utf-8")
    return vault


def test_fts_query_uses_cjk_shingles_instead_of_whole_phrase():
    """中文查询不得整段成短语：'半导体物理' 应切成 3 字滑窗 OR 连接。"""
    fts_q = lambda text: MarkdownIndexer._fts_query(None, text)  # noqa: E731
    q = fts_q("半导体物理")
    assert q == '("半导体" OR "导体物" OR "体物理")'

    # 两字词显式跳过（trigram 无法匹配），返回 None 走词法兜底。
    assert fts_q("物理") is None

    # ASCII 词保持原有 AND 短语语义。
    q2 = fts_q("embedding vector")
    assert q2 == '"embedding" AND "vector"'


def test_chunks_meta_invalidates_on_inject_image_captions():
    """翻转 inject_image_captions 必须改变文本层指纹（否则存量库永不重切块）。"""
    indexer = MarkdownIndexer(
        Path("C:/x"), _config(Path("C:/tmp")), embedding_provider=CountingProvider()
    )
    before = indexer._chunks_meta()
    indexer.config.inject_image_captions = True
    after = indexer._chunks_meta()
    assert before["inject_image_captions"] is False
    assert after["inject_image_captions"] is True


def test_vectors_meta_invalidates_on_endpoint_change():
    """换 embedding endpoint 必须改变向量层指纹，否则静默复用他处向量。"""
    indexer = MarkdownIndexer(
        Path("C:/x"), _config(Path("C:/tmp")), embedding_provider=CountingProvider()
    )
    before = indexer._vectors_meta()
    indexer.config.embedding.endpoint = "https://another.host/v1/embeddings"
    after = indexer._vectors_meta()
    assert before["endpoint"] != after["endpoint"]
    assert "endpoint" in before and "send_dimensions" in before


def test_cosine_rejects_dimension_mismatch():
    """维度不一致的余弦必须是 0（此前 min(len) 截断产出垃圾相似度）。"""
    left = array("f", [0.1, 0.2, 0.3])
    right = array("f", [0.1, 0.2])
    assert MarkdownIndexer._cosine(left, right) == 0.0
    assert MarkdownIndexer._cosine(left, array("f", [0.1, 0.2, 0.3])) != 0.0


def test_deleting_all_notes_persists_empty_cache(tmp_path):
    """全库删光后缓存必须落盘删除，重启不得把旧笔记重新载回。"""
    vault = _vault(tmp_path)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    cache_path = indexer._chunks_cache_path
    assert cache_path is not None and cache_path.exists()

    for note in vault.glob("*.md"):
        note.unlink()
    indexer.sync()
    # 空 payload 也要写盘：删除持久化。
    assert not cache_path.exists() or (
        cache_path.exists() and cache_path.stat().st_size > 0
    )

    # 新实例从（已空的）缓存加载后不应看到任何旧笔记。
    fresh = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    assert fresh.all_chunks() == []


def test_kb_export_rejects_relative_path_and_clobber(tmp_path, monkeypatch):
    from vault_mcp.server import VaultMcpServer

    vault = _vault(tmp_path)
    config = tmp_path / "app.toml"
    config.write_text(
        'mode = "static"\n[cache]\nenabled = true\ndir = "%s"\n' % (tmp_path / "cache").as_posix(),
        encoding="utf-8",
    )
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    server = VaultMcpServer(config)
    server.call_tool("kb_init", {"path": str(vault)})

    with pytest.raises(ValueError, match="absolute"):
        server.call_tool("kb_export", {"out_path": "snap.zip", "vault_path": str(vault)})
    with pytest.raises(ValueError, match=r"\.zip"):
        server.call_tool("kb_export", {"out_path": str(tmp_path / "snap.txt"), "vault_path": str(vault)})

    target = tmp_path / "snap.zip"
    target.write_text("already here", encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        server.call_tool("kb_export", {"out_path": str(target), "vault_path": str(vault)})

    result = json.loads(
        server.call_tool(
            "kb_export",
            {"out_path": str(target), "vault_path": str(vault), "overwrite": "true"},
        )["content"][0]["text"]
    )
    assert result["exported"] is True
    for indexer in server._indexers.values():
        indexer.stop_watching()


def test_import_rejects_chunking_mismatch(tmp_path):
    """快照分块参数与本机不同时必须拒绝（否则导入的向量一条都挂不上）。"""
    vault = _vault(tmp_path)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()
    snapshot = tmp_path / "snap.zip"
    indexer.export_snapshot(snapshot)

    # 篡改 manifest 的 chunks_meta，模拟不同 chunk_size 的源机器。
    import zipfile

    with zipfile.ZipFile(snapshot, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        chunks = zf.read("chunks.bin")
        vectors = zf.read("vectors.bin") if "vectors.bin" in zf.namelist() else None
    manifest["chunks_meta"]["chunk_size"] = manifest["chunks_meta"]["chunk_size"] + 100

    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(forged, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("chunks.bin", chunks)
        if vectors is not None:
            zf.writestr("vectors.bin", vectors)

    with pytest.raises(ValueError, match="chunked with different parameters"):
        indexer.import_snapshot(forged)
