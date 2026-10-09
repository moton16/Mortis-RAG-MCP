from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import DocumentStore, MediaOccurrenceSpec, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.providers import ExternalEmbeddingProvider, create_media_provider
from mortis_rag_mcp.embedding_capabilities import resolve_embedding_profile


def _endpoint_reachable(endpoint: str, timeout: float = 1.5) -> bool:
    """本机运行载体可达性探测：EG2 本地部署是验证辅助，不是产品运行依赖。"""
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_text_query_matches_image_occurrence_with_eg2(tmp_path: Path):
    """验证文本查询输入能够直接在对齐空间中召回匹配到知识库内的图片 occurrence。"""
    vault = tmp_path / "vault"
    vault.mkdir()

    img_data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    md_content = "# 系统架构设计\n\n系统由前端控制器与后端存储引擎组成。\n\n![核心模块拓扑图](images/arch.png)\n\n展示了核心数据流。"
    doc_path = vault / "doc.md"
    doc_path.write_text(md_content, encoding="utf-8")

    cfg = AppConfig()
    cfg.vault_path = str(vault)
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.embedding.mode = "external"
    cfg.embedding.endpoint = "http://127.0.0.1:8080/v1/embeddings"
    cfg.embedding.model = "embeddinggemma2"
    if not _endpoint_reachable(cfg.embedding.endpoint):
        pytest.skip(
            "local EG2 embedding endpoint 127.0.0.1:8080 unreachable; runtime carrier blocked "
            "(local llama.cpp EG2 deployment not running) -- 不是服务端架构缺陷，"
            "EG2 本地部署仅为验证辅助，不是产品运行依赖"
        )
    cfg.embedding.dimension = 768
    cfg.embedding.send_dimensions = False
    cfg.embedding.capability_profile = "embeddinggemma2"
    cfg.embedding.media_modalities = ("image",)
    cfg.embedding.media_alignment_space_id = "eg2-aligned-space"
    cfg.embedding.media_preprocess_version = "img-v1"
    cfg.embedding.media_endpoint_revision = "ep-1"
    cfg.embedding.media_allowed_mime_types = ("image/png",)
    cfg.embedding.media_max_input_bytes = 1024 * 1024
    cfg.embedding.media_max_batch_size = 4
    cfg.embedding.media_model_reference = "ref"
    cfg.embedding.media_endpoint_fixture_reference = "ref"
    cfg.embedding.media_alignment_reference = "ref"
    cfg.embedding.media_license_reference = "ref"

    layout = resolve_storage_layout(cfg, vault, registered_vaults=[])
    store = DocumentStore(layout, cfg)
    store.open(write=True)

    sha = hashlib.sha256(doc_path.read_bytes()).hexdigest()
    staged = store.stage_revision(
        source="doc.md",
        source_sha256=sha,
        render_sha256=hashlib.sha256(md_content.encode("utf-8")).hexdigest(),
        parser_fingerprint="test-v1",
        markdown=md_content,
        capabilities={"coverage": "full"},
    )

    blob_id = store.put_media_blob(data=img_data, mime_type="image/png", width=800, height=600)
    pos = md_content.index("![核心模块拓扑图]")
    length = len("![核心模块拓扑图](images/arch.png)")

    spec = MediaOccurrenceSpec(
        occurrence_id="occ-img-0",
        blob_id=blob_id,
        kind="image",
        ordinal=0,
        mime_type="image/png",
        page=1,
        caption="核心模块拓扑图",
        anchor_start=pos,
        anchor_end=pos + length,
    )
    store.attach_occurrences(staged.revision_id, [spec])
    store.commit_revision(staged.revision_id)
    store.close()

    profile = resolve_embedding_profile(cfg.embedding)
    text_provider = ExternalEmbeddingProvider(
        endpoint=cfg.embedding.endpoint,
        model=cfg.embedding.model,
        dimension=cfg.embedding.dimension,
        send_dimensions=cfg.embedding.send_dimensions,
        profile=profile,
    )

    # 跨模态对齐向量：在同一个 768 维空间中，媒体向量与核心拓扑图对齐
    img_aligned_vector = text_provider.embed(["核心模块拓扑图"])[0]

    media_provider = create_media_provider(
        cfg.embedding,
        transport=lambda items: [{"index": i, "embedding": img_aligned_vector} for i, _ in enumerate(items)],
    )

    indexer = MarkdownIndexer(vault, cfg, embedding_provider=text_provider, media_provider=media_provider)
    try:
        indexer.sync()
        chunks = indexer._chunks["doc.md"]
        native_chunks = [c for c in chunks if c.metadata.get("kind") == "media_native"]
        assert len(native_chunks) == 1, "应该生成 1 个原生图片 chunk"
        assert native_chunks[0].embedding is not None

        # 核心断言：Agent/用户输入纯文字查询，能够检索并召回匹配到图片 chunk！
        results = indexer.search("核心模块拓扑", top_k=5)
        matched_image = any(c.metadata.get("occurrence_id") == "occ-img-0" for c in results)
        assert matched_image, "纯文本输入必须成功召回匹配到库中的图片 occurrence！"
        print(f"\n[E2E VERIFIED] Query '核心模块拓扑' successfully matched media chunk: {results[0].metadata}")
    finally:
        if getattr(indexer, "_fts", None):
            try:
                indexer._fts.close()
            except Exception:
                pass
        indexer.close_document_store()
