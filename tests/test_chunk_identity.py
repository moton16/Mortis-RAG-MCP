"""C98：切块身份、虚拟 ID 与「禁用嵌入」跳过（不碰网络）。"""
from __future__ import annotations

import json

import pytest

from mortis_rag_mcp._indexer.models import Chunk
from mortis_rag_mcp._indexer.token_chunking import (
    chunker_fingerprint,
    embedding_key,
    virtual_chunk_id,
)
from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def test_legacy_fingerprint_covers_char_params():
    base = {"mode": "legacy_chars", "legacy_chunk_size": 800, "legacy_chunk_overlap": 120}
    assert chunker_fingerprint(base) == chunker_fingerprint(dict(base))
    changed = dict(base, legacy_chunk_size=1200)
    assert chunker_fingerprint(base) != chunker_fingerprint(changed), \
        "legacy 字符参数必须参与身份指纹，否则改 chunk_size 会被误判为同一代"
    # 没有提供字符参数时（纯算法调用）不引入额外字段
    assert chunker_fingerprint({"mode": "legacy_chars"}) == chunker_fingerprint({"mode": "legacy_chars"})


def test_virtual_chunk_id_is_revision_scoped_and_stable():
    chunk = Chunk("x", "content", "doc.pdf", "title", {"chunk_index": 0})
    fingerprint = chunker_fingerprint({"mode": "legacy_chars"})
    a = virtual_chunk_id(chunk, {"store_uuid": "s", "doc_id": "d", "revision_id": "rev-a"}, fingerprint)
    same = virtual_chunk_id(chunk, {"store_uuid": "s", "doc_id": "d", "revision_id": "rev-a"}, fingerprint)
    b = virtual_chunk_id(chunk, {"store_uuid": "s", "doc_id": "d", "revision_id": "rev-b"}, fingerprint)
    back_to_a = virtual_chunk_id(chunk, {"store_uuid": "s", "doc_id": "d", "revision_id": "rev-a"}, fingerprint)
    assert a == same == back_to_a
    assert a != b, "同文新 revision 不得复用旧虚拟地址（A→B→A 也不能复活旧 id）"
    with pytest.raises(ValueError):
        virtual_chunk_id(chunk, {"store_uuid": "", "doc_id": "d", "revision_id": "r"}, fingerprint)


def test_embedding_key_binds_space_and_media(tmp_path):
    text = "same exact input"
    assert embedding_key(text, "space-1") == embedding_key(text, "space-1")
    assert embedding_key(text, "space-1") != embedding_key(text, "space-2")
    assert embedding_key(text, "space-1") != embedding_key(text, "space-1", media_hashes=["m1"])


def _external_config(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    cfg.embedding.mode = "external"
    cfg.embedding.endpoint = "https://example.invalid/v1/embeddings"
    cfg.embedding.model = "bge-m3"
    cfg.embedding.dimension = 1024
    cfg.embedding.api_key = "k"
    cfg.embedding.send_dimensions = False
    return cfg


def test_indexer_fingerprint_drives_virtual_identity(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, _external_config(tmp_path))
    try:
        assert indexer._chunker_fingerprint() == chunker_fingerprint(indexer._chunking_config)
        key = indexer._derived_profile_key()
        assert indexer._chunker_fingerprint() in key
        assert indexer._embedding_profile.fingerprint in key
    finally:
        indexer.close_document_store()


def test_document_template_flows_into_exact_input_and_key(tmp_path):
    from dataclasses import replace

    from mortis_rag_mcp._indexer.sync_engine import _document_input, _document_provider

    vault = tmp_path / "vault"
    vault.mkdir()
    indexer = MarkdownIndexer(vault, _external_config(tmp_path))
    try:
        chunk = Chunk("cid", "raw content", "a.md", "t", {"chunk_index": 0})
        indexer._chunks["a.md"] = [chunk]
        indexer._stamp_embedding_keys()
        raw_key = chunk.metadata["embedding_key"]
        assert _document_input(indexer, "raw content") == "raw content"
        assert _document_provider(indexer)._provider is indexer.embedding_provider

        indexer._embedding_profile = replace(indexer._embedding_profile,
                                             document_template="passage: {text}")
        assert _document_input(indexer, "raw content") == "passage: raw content"

        class _Provider:
            def __init__(self) -> None:
                self.seen: list[str] = []

            def embed(self, texts):
                self.seen.extend(texts)
                return [[1.0] + [0.0] * 1023 for _ in texts]

        provider = _Provider()
        indexer.embedding_provider = provider
        _document_provider(indexer).embed(["abc"])
        assert provider.seen == ["passage: abc"], "模板必须作用在真正发送的输入上"

        # embedding_key 必须描述**实际输入**：模板变化 → key 变化（旧向量不复用）
        indexer._stamp_embedding_keys()
        assert chunk.metadata["embedding_key"] != raw_key
        # E05/R2: no asymmetric silent fallback; bad templates are rejected.
        indexer._embedding_profile = replace(indexer._embedding_profile,
                                             document_template="{missing_slot}")
        with pytest.raises(KeyError):
            _document_input(indexer, "raw content")
    finally:
        indexer.close_document_store()


def test_embedding_disabled_chunks_are_never_requested(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append(json.loads(request.data.decode("utf-8")))
        raise AssertionError("embedding_disabled chunk 不得触发任何外部请求")

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fake_urlopen)
    indexer = MarkdownIndexer(vault, _external_config(tmp_path))
    try:
        chunk = Chunk("cid", "x" * 10, "big.pdf", "title",
                      {"chunk_index": 0, "start_line": 1, "end_line": 1,
                       "embedding_disabled": True, "oversize": True})
        indexer._chunks["big.pdf"] = [chunk]
        assert indexer._embed_missing() is False
        assert calls == []
        assert "big.pdf" not in indexer.failed_files
        # oversize chunk 也不得当捐赠者
        assert indexer._reuse_vectors_by_content_hash() == 0
    finally:
        indexer.close_document_store()
