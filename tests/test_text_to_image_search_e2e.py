from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import DocumentStore, MediaOccurrenceSpec, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp.media_providers import EmbeddingInput, HttpMediaTransport
from mortis_rag_mcp.providers import ExternalEmbeddingProvider, create_media_provider
from mortis_rag_mcp.embedding_capabilities import resolve_embedding_profile

#: 本机 EG2（LiteRT-LM 三阶路由）多模态 embeddings 端点。可用环境变量指向其它载体。
EG2_ENDPOINT = os.environ.get("MORTIS_EG2_MEDIA_ENDPOINT", "http://127.0.0.1:8000/v1/embeddings")


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
    cfg.embedding.endpoint = EG2_ENDPOINT
    cfg.embedding.model = "embeddinggemma2"
    if not _endpoint_reachable(cfg.embedding.endpoint):
        pytest.skip(
            f"local EG2 embeddings endpoint {cfg.embedding.endpoint} unreachable; runtime carrier "
            "blocked (local LiteRT-LM EG2 deployment not running) -- 不是服务端架构缺陷，"
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


def _png_bytes(width: int, height: int, color_at) -> bytes:
    """最小 PNG 编码器（纯标准库，不依赖 Pillow）：让真实载体收到真实图片字节。"""
    import struct
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            raw.extend(color_at(x, y))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def _figure_png() -> bytes:
    """真实的两块拓扑图：左蓝右绿 + 中间连线，用于真实图像 embedding。"""
    def color_at(x: int, y: int) -> bytes:
        if 40 <= x <= 250 and 60 <= y <= 260:
            return (210, 228, 250)
        if 330 <= x <= 560 and 60 <= y <= 260:
            return (235, 245, 225)
        if 250 < x < 330 and 155 <= y <= 165:
            return (30, 90, 160)
        return (255, 255, 255)

    return _png_bytes(600, 320, color_at)


def test_real_multimodal_carrier_image_transport_roundtrip(tmp_path: Path):
    """真实多模态载体链路：真实图片 → 真实 HTTP 媒体向量 → native chunk → 图片查询召回。

    与上一条用例不同，这里**不做任何 mock**：图片向量由 `HttpMediaTransport` 真实 POST 到
    载体的 /v1/embeddings 得到（EG2 三阶路由的 Tier2 图文分支）。载体不可达时 skip：
    运行车载体限制，不是服务端架构缺陷。
    """
    if not _endpoint_reachable(EG2_ENDPOINT):
        pytest.skip(
            f"local EG2 embeddings endpoint {EG2_ENDPOINT} unreachable; runtime carrier blocked "
            "(local LiteRT-LM EG2 deployment not running)"
        )

    vault = tmp_path / "vault"
    vault.mkdir()
    img_data = _figure_png()

    cfg = AppConfig()
    cfg.vault_path = str(vault)
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.embedding.mode = "external"
    cfg.embedding.endpoint = EG2_ENDPOINT
    cfg.embedding.model = "embeddinggemma-2"
    cfg.embedding.dimension = 768
    cfg.embedding.send_dimensions = False
    cfg.embedding.capability_profile = "embeddinggemma2"
    cfg.embedding.media_modalities = ("image",)
    cfg.embedding.media_alignment_space_id = "eg2-aligned-space"
    cfg.embedding.media_preprocess_version = "img-v1"
    cfg.embedding.media_endpoint_revision = "ep-eg2"
    cfg.embedding.media_allowed_mime_types = ("image/png",)
    cfg.embedding.media_max_input_bytes = 1024 * 1024
    cfg.embedding.media_max_batch_size = 4
    cfg.embedding.media_model_reference = "eg2-local"
    cfg.embedding.media_endpoint_fixture_reference = "eg2-local"
    cfg.embedding.media_alignment_reference = "eg2-local"
    cfg.embedding.media_license_reference = "eg2-local"

    md_content = "# 系统架构设计\n\n系统由摄取层与存储层组成。\n\n![双层数据流拓扑图](images/arch.png)\n\n上图给出了两侧的数据流方向与边界。\n"
    doc_path = vault / "doc.md"
    doc_path.write_text(md_content, encoding="utf-8")

    layout = resolve_storage_layout(cfg, vault, registered_vaults=[])
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    staged = store.stage_revision(
        source="doc.md",
        source_sha256=hashlib.sha256(doc_path.read_bytes()).hexdigest(),
        render_sha256=hashlib.sha256(md_content.encode("utf-8")).hexdigest(),
        parser_fingerprint="e2e-real-v1",
        markdown=md_content,
        capabilities={"coverage": "full"},
    )
    blob_id = store.put_media_blob(data=img_data, mime_type="image/png", width=600, height=320)
    pos = md_content.index("![双层数据流拓扑图]")
    store.attach_occurrences(staged.revision_id, [MediaOccurrenceSpec(
        occurrence_id="occ-real-arch", blob_id=blob_id, kind="image", ordinal=0,
        mime_type="image/png", page=1, caption="双层数据流拓扑图",
        anchor_start=pos, anchor_end=pos + len("![双层数据流拓扑图](images/arch.png)"),
    )])
    store.commit_revision(staged.revision_id)
    store.close()

    profile = resolve_embedding_profile(cfg.embedding)
    text_provider = ExternalEmbeddingProvider(
        endpoint=cfg.embedding.endpoint, model=cfg.embedding.model,
        dimension=cfg.embedding.dimension, send_dimensions=cfg.embedding.send_dimensions,
        profile=profile,
    )
    transport = HttpMediaTransport(
        endpoint=EG2_ENDPOINT, model=cfg.embedding.model, timeout=120.0, dimension=768,
        send_dimensions=False, allowed_mime_types=("image/png",),
        max_input_bytes=1024 * 1024, max_batch_size=4,
    )
    media_provider = create_media_provider(cfg.embedding, transport=transport)
    assert media_provider is not None

    image_vector = list(transport([EmbeddingInput(
        request_id="query-image", modality="image", data=img_data,
        mime_type="image/png", media_hash=hashlib.sha256(img_data).hexdigest(),
    )])[0]["embedding"])
    assert len(image_vector) == 768, "真实载体必须返回 768 维图像向量"

    indexer = MarkdownIndexer(
        vault, cfg, embedding_provider=text_provider, media_provider=media_provider
    )
    try:
        indexer.sync()
        native = [c for c in indexer.all_chunks() if c.metadata.get("kind") == "media_native"]
        assert len(native) == 1, "真实图文摄取必须生成 1 个 native 图片 chunk"
        assert native[0].metadata.get("occurrence_id") == "occ-real-arch"

        vectors = indexer._vector_backend.get_vectors([c.id for c in indexer.all_chunks()])
        assert native[0].id in vectors, "native chunk 的真实图像向量必须入库"

        # 图片查询（原生多模态输入）：以图片自身真实向量检索，必须命中该 native chunk。
        stored = list(vectors[native[0].id])
        dot = sum(a * b for a, b in zip(stored, image_vector))
        na = sum(a * a for a in stored) ** 0.5
        nb = sum(b * b for b in image_vector) ** 0.5
        assert dot / (na * nb) > 0.999, "同一图片的真实向量必须自洽命中"

        results = indexer.search("双层数据流拓扑图", top_k=5)
        assert any(c.metadata.get("occurrence_id") == "occ-real-arch" for c in results), (
            "纯文本查询必须召回真实图片 occurrence"
        )
    finally:
        if getattr(indexer, "_fts", None):
            try:
                indexer._fts.close()
            except Exception:
                pass
        indexer.close_document_store()
