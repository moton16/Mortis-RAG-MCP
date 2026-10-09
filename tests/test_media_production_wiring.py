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
            blobs: list[bytes]) -> str:
    path = vault / source
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    staged = store.stage_revision(
        source=source, source_sha256=sha,
        render_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        parser_fingerprint="fake-1", markdown=markdown, capabilities={"coverage": "full"},
    )
    specs = []
    for index, data in enumerate(blobs):
        blob_id = store.put_media_blob(data=data, mime_type="image/png", width=8, height=8)
        anchor = markdown.index("media")
        specs.append(MediaOccurrenceSpec(
            occurrence_id=f"occ-{index}", blob_id=blob_id, kind="image", ordinal=index,
            mime_type="image/png", page=index + 1, caption="same caption",
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
