"""E08-b/c：proxy → sync → FTS/vector → media_chunk_links 生产接线与撤销。

真实 server + 真实 DocumentStore + 真实 sync，不使用 fake store 冒充验证。
钉住的合同：
- 已提交 revision 的 occurrence 在 sync 后生成 proxy chunk 并整代落 links；
- links 真实落盘（第二连接可读回），按 profile/generation/revision/occurrence/chunk 绑定；
- 更新/删除/豁免/换 revision 后派生召回撤销且不复活，解析事实与 blob 不自动删；
- media key 含完整 space + 媒体 blob 身份：同 caption 异图不得得到同一向量身份。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mortis_rag_mcp.doc_store import DocumentStore, MediaOccurrenceSpec, resolve_storage_layout
from mortis_rag_mcp.server import VaultMcpServer

MARKDOWN = "# Doc\n\nalpha text before media\n\nbeta text after media\n"
PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 16
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 16


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def _layout(server, vault):
    return resolve_storage_layout(server.config, vault, registered_vaults=[])


def _commit(store: DocumentStore, vault: Path, source: str, markdown: str,
            blobs: list[bytes], mimes: list[str] | None = None,
            kinds: list[str] | None = None, spans: list[tuple[int, int]] | None = None) -> str:
    path = vault / source
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    staged = store.stage_revision(
        source=source, source_sha256=sha,
        render_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        parser_fingerprint="fake-1", markdown=markdown, capabilities={"coverage": "full"},
    )
    mimes = mimes or ["image/png"] * len(blobs)
    kinds = kinds or ["image"] * len(blobs)
    specs = []
    for index, data in enumerate(blobs):
        blob_id = store.put_media_blob(data=data, mime_type=mimes[index], width=8, height=8)
        anchor = markdown.index("media")
        timing = spans[index] if spans else (None, None)
        specs.append(MediaOccurrenceSpec(
            occurrence_id=f"occ-{index}", blob_id=blob_id, kind=kinds[index], ordinal=index,
            mime_type=mimes[index], page=index + 1, caption="same caption",
            t_start_ms=timing[0], t_end_ms=timing[1],
            anchor_start=anchor, anchor_end=anchor + len("media")))
    if specs:
        store.attach_occurrences(staged.revision_id, specs)
    store.commit_revision(staged.revision_id)
    return staged.revision_id


def _build(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "doc.pdf").write_bytes(b"%PDF-1.4 payload")
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n\n[ingest]\nenabled = false\nstorage = "virtual"\n',
                           encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.config.cache.dir = str(tmp_path / "cache")
    server.config.cache.enabled = True
    server.config.cache.placement = "home"
    server.registry.add(str(vault), name="Vault")
    return server, vault


def _second_connection(server, vault) -> DocumentStore:
    store = DocumentStore(_layout(server, vault), server.config)
    store.open(write=False)
    return store


def test_sync_persists_proxy_chunks_and_links_reachable_from_second_connection(tmp_path: Path):
    server, vault = _build(tmp_path)
    setup = DocumentStore(_layout(server, vault), server.config)
    setup.open(write=True)
    revision = _commit(setup, vault, "doc.pdf", MARKDOWN, [PNG_A, PNG_B])
    setup.close()
    try:
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        profile_key = indexer._embedding_profile.fingerprint
        generation = indexer._derived_profile_key()
        chunks = indexer._chunks["doc.pdf"]
        proxy = [chunk for chunk in chunks if chunk.metadata.get("kind") == "media_proxy"]
        assert {chunk.metadata["occurrence_id"] for chunk in proxy} == {"occ-0", "occ-1"}
        # proxy 进入同一检索候选：正文 + proxy 都在 all_chunks 中。
        assert {chunk.id for chunk in proxy} <= {chunk.id for chunk in indexer.all_chunks()}

        reader = _second_connection(server, vault)
        try:
            links = reader.get_media_chunk_links(revision_id=revision, profile_key=profile_key,
                                                 derived_generation_id=generation)
            linked = {(link["occurrence_id"], link["chunk_id"]) for link in links}
            # 每个 proxy chunk 都必须与自己的 occurrence 绑定（不跨 occurrence 串链）。
            for chunk in proxy:
                assert (chunk.metadata["occurrence_id"], chunk.id) in linked
            one = reader.get_media_chunk_links(revision_id=revision, profile_key=profile_key,
                                               derived_generation_id=generation, occurrence_id="occ-0")
            assert all(link["occurrence_id"] == "occ-0" for link in one)
            # 错误 profile 作用域不得共享授权。
            assert reader.get_media_chunk_links(revision_id=revision, profile_key="other",
                                                derived_generation_id=generation) == []
        finally:
            reader.close()
    finally:
        server.shutdown()


def test_same_caption_distinct_blob_never_shares_vector_identity(tmp_path: Path):
    server, vault = _build(tmp_path)
    setup = DocumentStore(_layout(server, vault), server.config)
    setup.open(write=True)
    _commit(setup, vault, "doc.pdf", MARKDOWN, [PNG_A, PNG_B])
    setup.close()
    try:
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        proxy = [c for c in indexer._chunks["doc.pdf"] if c.metadata.get("kind") == "media_proxy"]
        keys = {c.metadata["occurrence_id"]: c.metadata["embedding_key"] for c in proxy}
        assert len(set(keys.values())) == len(keys), "同 caption 异图必须不同向量身份"
        # key 含完整 space：换一个 embedding space 后同内容 key 必须变化。
        original = proxy[0].metadata["embedding_key"]
        import dataclasses
        indexer._embedding_profile = dataclasses.replace(indexer._embedding_profile, model_revision="other")
        indexer._stamp_embedding_keys()
        assert proxy[0].metadata["embedding_key"] != original
    finally:
        server.shutdown()


def test_revision_change_and_removal_revoke_derived_links_without_deleting_facts(tmp_path: Path):
    server, vault = _build(tmp_path)
    setup = DocumentStore(_layout(server, vault), server.config)
    setup.open(write=True)
    first = _commit(setup, vault, "doc.pdf", MARKDOWN, [PNG_A, PNG_B])
    setup.close()
    try:
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        profile_key = indexer._embedding_profile.fingerprint
        generation = indexer._derived_profile_key()

        reader = _second_connection(server, vault)
        try:
            assert reader.get_media_chunk_links(revision_id=first, profile_key=profile_key,
                                                derived_generation_id=generation)
        finally:
            reader.close()

        # 新 revision：媒体被移除 → 旧代派生召回应撤销，但解析事实与 blob 仍在。
        setup = DocumentStore(_layout(server, vault), server.config)
        setup.open(write=True)
        second = _commit(setup, vault, "doc.pdf", MARKDOWN + "\nmore\n", [])
        assert second != first
        still_there = setup.get_active("doc.pdf")
        assert still_there is not None and still_there.revision.revision_id == second
        setup.close()

        indexer.sync()
        reader = _second_connection(server, vault)
        try:
            assert reader.get_media_chunk_links(revision_id=second, profile_key=profile_key,
                                                derived_generation_id=generation) == []
            # 旧 revision 的链接被撤销（不复活）；解析事实仍在。
            assert reader.get_media_chunk_links(revision_id=first, profile_key=profile_key,
                                                derived_generation_id=generation) == []
            assert reader.get_active("doc.pdf").revision.revision_id == second
        finally:
            reader.close()
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- E08-d
MEDIA_TOML = '''mode = "external"
endpoint = "https://example.test/embed"
model = "unknown"
dimension = 2
send_dimensions = false
media_modalities = ["image"]
media_alignment_space_id = "space-1"
media_preprocess_version = "media-v1"
media_endpoint_revision = "ep-1"
media_allowed_mime_types = ["image/png"]
media_max_input_bytes = 4096
media_max_batch_size = 4
media_model_reference = "model-ref"
media_endpoint_fixture_reference = "fixture-ref"
media_alignment_reference = "align-ref"
media_license_reference = "license-ref"

[ingest]
enabled = false
storage = "virtual"
'''


class _FakeTextProvider:
    """dim-2 文本向量；故意不带 `.profile`，走纯文本 profile 校验路径。"""

    def __init__(self, vector):
        self.vector = list(vector)

    def embed(self, texts):
        return [list(self.vector) for _ in texts]


def _media_indexer(tmp_path: Path, *, text_alignment: str = "space-1",
                   media_alignment: str = "space-1", blobs=None, mimes=None):
    from dataclasses import replace as _replace

    from mortis_rag_mcp.config import load_config
    from mortis_rag_mcp.indexer import MarkdownIndexer
    from mortis_rag_mcp.providers import create_media_provider

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "doc.pdf").write_bytes(b"%PDF-1.4 payload")
    config_path = tmp_path / "app.toml"
    config_path.write_text(MEDIA_TOML, encoding="utf-8")
    config = load_config(config_path)
    config.cache.dir = str(tmp_path / "cache")
    config.cache.enabled = True
    config.cache.placement = "home"
    config.embedding = _replace(config.embedding, media_alignment_space_id=text_alignment)
    store = DocumentStore(resolve_storage_layout(config, vault, registered_vaults=[]), config)
    store.open(write=True)
    revision = _commit(store, vault, "doc.pdf", MARKDOWN, blobs or [PNG_A], mimes)
    store.close()
    provider_config = _replace(config.embedding, media_alignment_space_id=media_alignment)
    media_provider = create_media_provider(
        provider_config,
        transport=lambda items: [{"index": i, "embedding": [3, 4]} for i, _ in enumerate(items)])
    indexer = MarkdownIndexer(vault, config, embedding_provider=_FakeTextProvider([1.0, 0.0]),
                              media_provider=media_provider)
    return indexer, vault, revision


def test_native_media_vectors_enter_aligned_query_and_dedupe_occurrence(tmp_path: Path):
    from mortis_rag_mcp._indexer.sync_engine import media_native_route

    indexer, vault, _revision = _media_indexer(tmp_path)
    try:
        assert media_native_route(indexer) is not None
        indexer.sync()
        chunks = indexer._chunks["doc.pdf"]
        native = [c for c in chunks if c.metadata.get("kind") == "media_native"]
        proxy = [c for c in chunks if c.metadata.get("kind") == "media_proxy"]
        assert len(native) == 1 and native[0].embedding is not None
        assert native[0].metadata["media_route"] == "native"
        assert native[0].metadata["alignment_space_id"] == "space-1"
        assert proxy, "缺少原生能力时纯词法 proxy 仍应存在（可降级）"

        # 查询：同一 occurrence 的 proxy 与 native 只保留一条（去双计权）。
        results = indexer.search("alpha", top_k=20)
        per_occurrence = [c for c in results if c.metadata.get("occurrence_id") == "occ-0"]
        assert len(per_occurrence) == 1

        # 去掉词法 proxy 后，原生向量仍能作为独立候选被检索到（证明它真进了向量空间）。
        indexer._chunks["doc.pdf"] = [c for c in chunks if c.metadata.get("kind") != "media_proxy"]
        native_results = [c for c in indexer.search("alpha", top_k=20)
                          if c.metadata.get("kind") == "media_native"]
        assert native_results, "原生媒体向量必须进入同一对齐空间的检索候选"
    finally:
        indexer.close_document_store()


def test_native_route_unavailable_without_declared_alignment(tmp_path: Path):
    from mortis_rag_mcp._indexer.sync_engine import media_native_route

    indexer, vault, _revision = _media_indexer(tmp_path, media_alignment="other-space")
    try:
        # 缺对齐声明（或声明不一致）→ 该 route 明确不可用，绝不用同维向量冒充。
        assert media_native_route(indexer) is None
        indexer.sync()
        kinds = {c.metadata.get("kind") for c in indexer._chunks["doc.pdf"]}
        assert "media_native" not in kinds
        assert "media_proxy" in kinds, "纯词法降级仍必须可用"
    finally:
        indexer.close_document_store()


def test_native_route_skips_unallowed_mime_without_fabrication(tmp_path: Path):
    from mortis_rag_mcp._indexer.sync_engine import media_native_route

    indexer, vault, _revision = _media_indexer(tmp_path, mimes=["image/tiff"])
    try:
        assert media_native_route(indexer) is not None
        indexer.sync()
        chunks = indexer._chunks["doc.pdf"]
        assert not [c for c in chunks if c.metadata.get("kind") == "media_native"]
        assert [c for c in chunks if c.metadata.get("kind") == "media_proxy"]
    finally:
        indexer.close_document_store()


# --------------------------------------------------------------------------- E08-e
def test_kb_read_media_returns_link_context_and_rejects_expired_pin(tmp_path: Path, monkeypatch):
    server, vault = _build(tmp_path)
    setup = DocumentStore(_layout(server, vault), server.config)
    setup.open(write=True)
    revision = _commit(setup, vault, "doc.pdf", MARKDOWN, [PNG_A])
    setup.close()
    try:
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        proxy = [c for c in indexer._chunks["doc.pdf"] if c.metadata.get("kind") == "media_proxy"]
        args = {"vault_path": "Vault", "source": "doc.pdf", "revision_id": revision,
                "occurrence_id": "occ-0"}
        meta = server._kb_read_media(args)
        assert "isError" not in meta
        payload = json.loads(meta["content"][0]["text"])
        assert proxy[0].id in payload["context_chunk_ids"]
        assert "proxy" in payload["context_relations"]

        # 固定版本 handle 到期 → 最终包络前拒绝，绝不返回媒体。
        monkeypatch.setattr(DocumentStore, "validate_generation_pin", lambda self, pin: False)
        expired = server._kb_read_media(args)
        assert json.loads(expired["content"][0]["text"])["code"] == "MEDIA_UNAVAILABLE"
    finally:
        server.shutdown()


def test_media_refs_pagination_binds_revision_without_loss_or_dupes(tmp_path: Path):
    server, vault = _build(tmp_path)
    setup = DocumentStore(_layout(server, vault), server.config)
    setup.open(write=True)
    revision = _commit(setup, vault, "doc.pdf", MARKDOWN, [PNG_A] * 25)
    setup.close()
    try:
        server.config.media.refs_limit = 20
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        seen: list[str] = []
        offset = 0
        for _ in range(5):
            page = server._kb_read({"source": "doc.pdf", "vault_path": "Vault",
                                    "media_refs_offset": offset})
            assert page["media_refs_revision_id"] == revision
            assert "media_refs_text_range" in page
            seen.extend(ref["occurrence_id"] for ref in page["media_refs"])
            nxt = page["next_media_refs_offset"]
            if nxt is None:
                break
            offset = nxt
        assert len(seen) == 25 and len(set(seen)) == 25
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- E08-f
def test_rpc_budget_uses_real_request_id_and_stdio_serialization(tmp_path: Path):
    server, vault = _build(tmp_path)
    setup = DocumentStore(_layout(server, vault), server.config)
    setup.open(write=True)
    revision = _commit(setup, vault, "doc.pdf", MARKDOWN, [PNG_A])
    setup.close()
    try:
        server._indexer_for({"vault_path": "Vault"}).sync()
        request_id = "req-\u4e2d\u6587-1"  # 非 ASCII id：序列化参数与线上一致才有意义
        base_args = {"vault_path": "Vault", "source": "doc.pdf", "revision_id": revision,
                     "occurrence_id": "occ-0", "representation": "inline", "variant": "original"}

        def call(args):
            return server.handle({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                  "params": {"name": "kb_read_media", "arguments": args}})

        ok = call({**base_args, "budget_bytes": 8 * 1024 * 1024})
        assert ok["id"] == request_id and "result" in ok
        success_result = ok["result"]
        assert success_result["content"][1]["type"] == "image"

        rejected = call({**base_args, "budget_bytes": 64})
        payload = json.loads(rejected["result"]["content"][0]["text"])
        assert payload["code"] == "MEDIA_TOO_LARGE"
        assert payload["next_action"]
        # required_bytes 必须等于**线上同参数**序列化的完整 JSON-RPC 包络（含真实 id）。
        expected = len(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": success_result},
                                  ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert payload["required_bytes"] == expected
        # 只裁完整 item：失败时绝不留下半截 base64。
        assert "data" not in rejected["result"]["content"][0]
    finally:
        server.shutdown()


# --------------------------------------------------------- 联合音频出口（E08×E09）
def test_joint_audio_occurrences_flow_through_the_same_sync_path(tmp_path: Path):
    """E09 发布音频 occurrence 后走**同一** sync 路径：不另起 embed、不反向 import indexer。"""
    server, vault = _build(tmp_path)
    setup = DocumentStore(_layout(server, vault), server.config)
    setup.open(write=True)
    revision = _commit(setup, vault, "doc.pdf", MARKDOWN, [PNG_A, PNG_A],
                       mimes=["audio/wav", "audio/wav"], kinds=["audio", "audio"],
                       spans=[(0, 1000), (1000, 2000)])
    setup.close()
    try:
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        chunks = indexer._chunks["doc.pdf"]
        proxy = [c for c in chunks if c.metadata.get("kind") == "media_proxy"]
        assert {c.metadata["occurrence_id"] for c in proxy} == {"occ-0", "occ-1"}
        # 音频 proxy 走同一 FTS/embed 候选（有向量），且**没有**另起的原生 embed 通道。
        assert all(c.metadata.get("embedding_key") for c in proxy)
        assert not [c for c in chunks if c.metadata.get("kind") == "media_native"]
        reader = _second_connection(server, vault)
        try:
            links = reader.list_media_chunk_links(revision_id=revision)
            assert {link["occurrence_id"] for link in links} == {"occ-0", "occ-1"}
        finally:
            reader.close()

        # refs 页暴露合并后的音频区间，并保留原始片段地址。
        page = server._kb_read({"source": "doc.pdf", "vault_path": "Vault"})
        segments = page["media_audio_segments"]
        assert len(segments) == 1 and segments[0]["t_start_ms"] == 0 and segments[0]["t_end_ms"] == 2000
        assert segments[0]["occurrence_ids"] == ["occ-0", "occ-1"]
    finally:
        server.shutdown()


def test_merge_audio_segments_only_by_time_never_by_transcript():
    from mortis_rag_mcp._indexer.media import merge_audio_segments

    def occ(oid, start, end, caption):
        return {"occurrence_id": oid, "kind": "audio", "t_start_ms": start, "t_end_ms": end,
                "caption": caption, "ocr": caption}

    # 相邻/重叠 → 合并；转录相同但时间不相邻 → 必须保持独立。
    merged = merge_audio_segments([
        occ("a", 0, 1000, "same transcript"),
        occ("b", 900, 2000, "same transcript"),
        occ("c", 50000, 51000, "same transcript"),
    ])
    assert [(s["t_start_ms"], s["t_end_ms"], s["occurrence_ids"]) for s in merged] == [
        (0, 2000, ["a", "b"]), (50000, 51000, ["c"])]
