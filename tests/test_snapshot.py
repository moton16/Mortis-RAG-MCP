"""C8：kb_export / kb_import 索引快照。

核心验收：把库 A 的快照导入到「另一台机器」（不同 vault 路径 + 不同缓存目录）
的库 B 后，B 的下一次 sync 对 embedding provider 的调用次数为 0。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.providers import StaticEmbeddingProvider
from mortis_rag_mcp.server import VaultMcpServer


class CountingProvider:
    def __init__(self, dimension: int = 8) -> None:
        self.calls = 0
        self.embedded: list[str] = []
        self.dimension = dimension

    def embed(self, texts):
        self.calls += 1
        self.embedded.extend(texts)
        return [[float(index % 7) / 7.0 for index in range(self.dimension)] for _ in texts]


def _config(tmp_path: Path, dimension: int = 8, **cache_kwargs) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=dimension),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=True, **cache_kwargs),
    )


def _write_notes(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "a.md").write_text("# 甲\n\n第一份笔记的内容。\n", encoding="utf-8")
    (vault / "sub").mkdir(parents=True, exist_ok=True)
    (vault / "sub" / "b.md").write_text("# 乙\n\n子目录里的第二份笔记。\n", encoding="utf-8")


def test_roundtrip_to_a_fresh_machine_costs_zero_embeddings(tmp_path):
    """换机迁移零重嵌：不同 vault 路径 + 不同缓存目录的全新实例导入后 0 次调用。"""
    vault_a = tmp_path / "机器A" / "vault"
    _write_notes(vault_a)
    indexer_a = MarkdownIndexer(vault_a, _config(tmp_path / "A"), embedding_provider=CountingProvider())
    indexer_a.sync()
    assert len(indexer_a.all_chunks()) >= 2

    snapshot = tmp_path / "机器A" / "vault-snapshot.zip"
    result = indexer_a.export_snapshot(snapshot)
    assert result["exported"] is True
    assert result["files"] == 2
    assert snapshot.is_file()

    # 「另一台机器」：vault 路径不同（cache key 由路径派生）、缓存目录全新。
    vault_b = tmp_path / "机器B" / "notes"
    _write_notes(vault_b)
    provider_b = CountingProvider()
    indexer_b = MarkdownIndexer(vault_b, _config(tmp_path / "B"), embedding_provider=provider_b)

    imported = indexer_b.import_snapshot(snapshot)
    assert imported["imported"] is True
    assert imported["files"] == 2
    assert imported["vectors_imported"] is True

    # 核心验收：导入后 sync 一次都不碰 embedding API。
    indexer_b.sync()
    assert provider_b.calls == 0
    assert provider_b.embedded == []

    # 内容与源库一致（按 chunk id 与正文对比）。
    assert {c.id for c in indexer_b.all_chunks()} == {c.id for c in indexer_a.all_chunks()}
    assert {c.content for c in indexer_b.all_chunks()} == {c.content for c in indexer_a.all_chunks()}
    # 检索在导入后的库上开箱即用。
    hits = indexer_b.search("第二份笔记", top_k=3, use_rerank=False)
    assert hits and hits[0].source == "sub/b.md"


def test_export_reflects_unsaved_state_and_rejects_empty_index(tmp_path):
    vault = tmp_path / "vault"
    _write_notes(vault)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())

    # 空索引不能导出（导出的会是一份毒快照）。
    with __import__("pytest").raises(ValueError, match="index is empty"):
        indexer.export_snapshot(tmp_path / "empty.zip")

    indexer.sync()
    # 新文件只 sync 过一次，快照必须包含它（export 前强制落盘当前内存态）。
    (vault / "c.md").write_text("# 丙\n\n增量内容。\n", encoding="utf-8")
    indexer.sync()
    snapshot = tmp_path / "snap.zip"
    indexer.export_snapshot(snapshot)
    assert indexer.stats()["files"] == 3

    fresh = MarkdownIndexer(vault, _config(tmp_path / "fresh"), embedding_provider=CountingProvider())
    fresh.import_snapshot(snapshot)
    assert fresh.stats()["files"] == 3


def test_model_dimension_mismatch_requires_force(tmp_path):
    vault = tmp_path / "vault"
    _write_notes(vault)
    indexer_a = MarkdownIndexer(vault, _config(tmp_path / "A", dimension=8), embedding_provider=CountingProvider(8))
    indexer_a.sync()
    snapshot = tmp_path / "snap.zip"
    indexer_a.export_snapshot(snapshot)

    provider_b = CountingProvider(4)
    vault_b = tmp_path / "B" / "vault"
    _write_notes(vault_b)
    indexer_b = MarkdownIndexer(vault_b, _config(tmp_path / "B", dimension=4), embedding_provider=provider_b)

    # 维度不一致：直接拒绝。
    try:
        indexer_b.import_snapshot(snapshot)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "force" in str(exc)

    # force=true：只导入文本层，向量作废，本地重嵌（文本层仍被复用：只嵌入、
    # 不重建 chunk）。
    imported = indexer_b.import_snapshot(snapshot, force=True)
    assert imported["vectors_imported"] is False
    indexer_b.sync()
    assert provider_b.calls >= 1
    assert len(indexer_b.all_chunks()) == len(indexer_a.all_chunks())


def test_import_rejects_unexpected_members_and_corrupt_payloads(tmp_path):
    vault = tmp_path / "vault"
    _write_notes(vault)
    indexer = MarkdownIndexer(vault, _config(tmp_path), embedding_provider=CountingProvider())
    indexer.sync()

    # 白名单外的成员（含路径穿越名）一律拒绝，且不落地任何文件。
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "vault-mcp-snapshot", "format_version": 1}))
        zf.writestr("chunks.bin", "whatever")
        zf.writestr("../escaped.txt", "boom")
    try:
        indexer.import_snapshot(evil)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "unexpected members" in str(exc)
    assert not (tmp_path / "escaped.txt").exists()

    # manifest 损坏 / 格式不对。
    bad_format = tmp_path / "bad_format.zip"
    with zipfile.ZipFile(bad_format, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "someone-elses-backup", "format_version": 1}))
        zf.writestr("chunks.bin", "x")
    try:
        indexer.import_snapshot(bad_format)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not a vault-mcp-snapshot" in str(exc)

    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "vault-mcp-snapshot", "format_version": 1}))
        zf.writestr("chunks.bin", b"\x00\x01not-a-cache-file")
    try:
        indexer.import_snapshot(corrupt)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "corrupt" in str(exc)

    # 快照文件不存在。
    try:
        indexer.import_snapshot(tmp_path / "missing.zip")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not found" in str(exc)


def test_import_requires_enabled_cache(tmp_path):
    vault = tmp_path / "vault"
    _write_notes(vault)
    no_cache = AppConfig(
        embedding=EmbeddingConfig(mode="static", dimension=8),
        cache=CacheConfig(dir=str(tmp_path / "cache"), enabled=False),
    )
    indexer = MarkdownIndexer(vault, no_cache, embedding_provider=CountingProvider())
    try:
        indexer.export_snapshot(tmp_path / "x.zip")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "cache is disabled" in str(exc)
    try:
        indexer.import_snapshot(tmp_path / "x.zip")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "cache is disabled" in str(exc)


def test_server_kb_export_import_roundtrip(tmp_path, monkeypatch):
    """MCP 工具链路：注册 -> 导出 -> 换目录注册 -> 导入 -> 检索可用。"""
    vault_a = tmp_path / "库A"
    _write_notes(vault_a)
    config = tmp_path / "app.toml"
    config.write_text(
        'mode = "static"\n[cache]\nenabled = true\ndir = "%s"\n' % (tmp_path / "cache").as_posix(),
        encoding="utf-8",
    )
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(tmp_path / "vaults.toml"))
    server = VaultMcpServer(config)

    server.call_tool("kb_init", {"path": str(vault_a)})
    snapshot = (tmp_path / "snap.zip").as_posix()
    exported = json.loads(server.call_tool("kb_export", {"out_path": snapshot, "vault_path": str(vault_a)})["content"][0]["text"])
    assert exported["exported"] is True

    vault_b = tmp_path / "库B"
    _write_notes(vault_b)
    server.call_tool("kb_init", {"path": str(vault_b)})
    imported = json.loads(server.call_tool("kb_import", {"snapshot": snapshot, "vault_path": str(vault_b)})["content"][0]["text"])
    assert imported["imported"] is True

    result = server.call_tool("kb_search", {"query": "第二份笔记", "vault_path": str(vault_b)})
    chunks = json.loads(result["content"][0]["text"])["chunks"]
    assert chunks and chunks[0]["source"] == "sub/b.md"
