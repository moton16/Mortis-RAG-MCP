"""C96：server/indexer 虚拟读取闭环（虚拟已摄取可读、版本寻址、媒体 refs 分页）。

钉住的合同：
- 虚拟 chunk 精读按 chunk 捕获的 revision/source_sha/render_sha 寻址，**不**把
  虚拟签名当物理 SHA，也不因物理文件被改而静默返回错误正文（必须 STALE 拒绝）；
- kb_read 对虚拟源暴露 source_kind/revision_id/render_sha256/line_basis；
- 媒体引用按配置上限分页且不越权（越权/过期只影响 refs，不影响正文）；
- 短名在多个虚拟源间歧义时报错，不猜。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mortis_rag_mcp.doc_store import DocumentStore, MediaOccurrenceSpec, resolve_storage_layout
from mortis_rag_mcp.server import VaultMcpServer

MARKDOWN = "# Virtual Doc\n\nfirst body line\n\n![fig](images/fig.png)\n\nlast body line\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture(autouse=True)
def isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


@pytest.fixture(autouse=True)
def no_background_watch(monkeypatch: pytest.MonkeyPatch):
    """本文件钉的是「读取时的版本/陈旧判定」，不让后台监听线程插进来。

    `_indexer_for` 建库时会顺带 `start_watching()`；而这里的用例直接改写物理源文件
    来制造 STALE 场景，监听线程可在两次断言之间并发重签源文件、改动文档库的可见
    性，于是同一份 allow_stale 读取在快/慢机器上给出不同结果（CI 上 py3.13 这一档
    实测踩到）。合同本身与监听无关，这里把监听惰性化，让判定保持确定性。
    """
    from mortis_rag_mcp.indexer import MarkdownIndexer

    monkeypatch.setattr(MarkdownIndexer, "start_watching", lambda self, *args, **kwargs: None)


def _commit_document(store: DocumentStore, vault: Path, source: str, markdown: str,
                     *, occurrences: int = 0, parser: str = "fake-1") -> str:
    path = vault / source
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    staged = store.stage_revision(
        source=source,
        source_sha256=sha,
        render_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        parser_fingerprint=parser,
        markdown=markdown,
        capabilities={"coverage": "full"},
    )
    if occurrences:
        specs = []
        for index in range(occurrences):
            blob_id = store.put_media_blob(data=PNG, mime_type="image/png", width=4, height=4)
            specs.append(MediaOccurrenceSpec(occurrence_id=f"occ-{index}", blob_id=blob_id,
                                             kind="image", ordinal=index, mime_type="image/png",
                                             page=index + 1))
        store.attach_occurrences(staged.revision_id, specs)
    store.commit_revision(staged.revision_id)
    return staged.revision_id


def build_server(tmp_path: Path, *, documents=("doc.pdf",), occurrences: int = 0):
    vault = tmp_path / "vault"
    vault.mkdir()
    for source in documents:
        (vault / source).write_bytes(b"%PDF-1.4 payload " + source.encode())
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        'mode = "static"\n\n[ingest]\nenabled = false\nstorage = "virtual"\n',
        encoding="utf-8",
    )
    server = VaultMcpServer(config_path)
    server.config.cache.dir = str(tmp_path / "cache")
    server.config.cache.enabled = True
    server.config.cache.placement = "home"
    server.registry.add(str(vault), name="Vault")
    layout = resolve_storage_layout(server.config, vault, registered_vaults=[])
    store = DocumentStore(layout, server.config)
    store.open(write=True)
    revisions = {source: _commit_document(store, vault, source, MARKDOWN, occurrences=occurrences)
                 for source in documents}
    store.close()
    return server, vault, revisions


def _virtual_chunks(indexer):
    return [chunk for chunks in indexer._chunks.values() for chunk in chunks
            if chunk.metadata.get("revision_id")]


def test_virtual_chunk_read_exposes_version_fields_and_is_not_physical_sha(tmp_path: Path):
    server, vault, revisions = build_server(tmp_path)
    try:
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        chunks = _virtual_chunks(indexer)
        assert chunks, "虚拟文档在 sync 后必须可枚举"
        chunk = chunks[0]
        assert chunk.metadata["revision_id"] == revisions["doc.pdf"]
        # 物理文件从未被索引为文本源：其签名是 virtual 前缀的版本标识，不是物理 SHA
        for signature in indexer._signatures.values():
            assert signature.startswith("virtual:")

        result = server._kb_read({"chunk_id": chunk.id, "vault_path": "Vault"})
        assert result["source"] == "doc.pdf"
        assert result["source_kind"] == "virtual"
        assert result["revision_id"] == revisions["doc.pdf"]
        assert result["render_sha256"] == chunk.metadata["render_sha256"]
        assert result["line_basis"] == "rendered_markdown"
        assert "first body line" in result["content"]

        # 物理源被改：chunk 读取必须 STALE 拒绝，绝不返回旧解析正文
        (vault / "doc.pdf").write_bytes(b"%PDF-1.4 TAMPERED")
        with pytest.raises(ValueError):
            server._kb_read({"chunk_id": chunk.id, "vault_path": "Vault"})
        # 源+行区间读取（非 chunk 版本寻址）默认同样拒绝，只有显式 allow_stale
        # 才返回已提交版本，并如实标记 source_changed
        with pytest.raises(ValueError):
            server._kb_read({"source": "doc.pdf", "vault_path": "Vault"})
        stale = server._kb_read({"source": "doc.pdf", "vault_path": "Vault", "allow_stale": True})
        assert stale["source_changed"] is True
        assert "first body line" in stale["content"]
        assert stale["revision_id"] == revisions["doc.pdf"]
    finally:
        server.shutdown()


def test_media_refs_are_bounded_and_paged(tmp_path: Path):
    server, vault, revisions = build_server(tmp_path, occurrences=3)
    try:
        server.config.media.refs_limit = 2
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        revision_id = revisions["doc.pdf"]

        first = server._kb_read({"source": "doc.pdf", "vault_path": "Vault"})
        assert first["revision_id"] == revision_id
        assert [ref["occurrence_id"] for ref in first["media_refs"]] == ["occ-0", "occ-1"]
        assert first["media_refs_offset"] == 0
        assert first["next_media_refs_offset"] == 2

        second = server._kb_read({"source": "doc.pdf", "vault_path": "Vault",
                                  "media_refs_offset": 2})
        assert [ref["occurrence_id"] for ref in second["media_refs"]] == ["occ-2"]
        assert second["next_media_refs_offset"] is None
        # refs 是 compact 摘要：不塞正文/图注长文本
        assert set(second["media_refs"][0]) <= {"occurrence_id", "kind", "page",
                                                "t_start_ms", "t_end_ms"}
    finally:
        server.shutdown()


def test_kb_read_media_closes_loop_with_real_store(tmp_path: Path):
    """C103 闭环：真实 server + 真实 store + 真实 resolver（不 mock 存储）。"""
    import base64
    import json as _json

    server, vault, revisions = build_server(tmp_path, occurrences=1)
    try:
        server._indexer_for({"vault_path": "Vault"}).sync()
        args = {"vault_path": "Vault", "source": "doc.pdf", "revision_id": revisions["doc.pdf"],
                "occurrence_id": "occ-0"}
        meta = server._kb_read_media(args)
        assert "isError" not in meta
        payload = _json.loads(meta["content"][0]["text"])
        assert payload["mime_type"] == "image/png"
        assert payload["host_media_verified"] is False
        assert len(meta["content"]) == 1, "metadata 默认不允许携带 blob"

        inline = server._kb_read_media({**args, "representation": "inline", "variant": "original"})
        assert "isError" not in inline
        assert inline["content"][1]["type"] == "image"
        assert base64.b64decode(inline["content"][1]["data"]) == PNG

        # revision 过期 / 越权 → 稳定 MEDIA_UNAVAILABLE，且不读取正文
        stale = server._kb_read_media({**args, "revision_id": "not-the-revision"})
        assert _json.loads(stale["content"][0]["text"])["code"] == "MEDIA_UNAVAILABLE"
        other = server._kb_read_media({**args, "source": "other.pdf"})
        assert _json.loads(other["content"][0]["text"])["code"] == "MEDIA_UNAVAILABLE"

        # 源被改动 → 地址失效（媒体地址与解析事实绑定，不返回旧图）
        (vault / "doc.pdf").write_bytes(b"%PDF-1.4 TAMPERED")
        changed = server._kb_read_media(args)
        assert _json.loads(changed["content"][0]["text"])["code"] == "MEDIA_UNAVAILABLE"
    finally:
        server.shutdown()


def test_preview_variant_is_persisted_and_reused(tmp_path: Path):
    """C103/C101：生成的 preview 落 `media_variants`，第二次读取命中 stored 分支。"""
    import io
    import json as _json

    pytest.importorskip("PIL")
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buffer, format="PNG")
    real_png = buffer.getvalue()

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
    layout = resolve_storage_layout(server.config, vault, registered_vaults=[])
    store = DocumentStore(layout, server.config)
    store.open(write=True)
    try:
        staged = store.stage_revision(
            source="doc.pdf",
            source_sha256=hashlib.sha256((vault / "doc.pdf").read_bytes()).hexdigest(),
            render_sha256=hashlib.sha256(MARKDOWN.encode("utf-8")).hexdigest(),
            parser_fingerprint="fake-1", markdown=MARKDOWN,
            capabilities={"coverage": "full"})
        blob_id = store.put_media_blob(data=real_png, mime_type="image/png", width=64, height=48)
        store.attach_occurrences(staged.revision_id, [
            MediaOccurrenceSpec(occurrence_id="occ-0", blob_id=blob_id, kind="image",
                                ordinal=0, mime_type="image/png", page=1)])
        store.commit_revision(staged.revision_id)
    finally:
        store.close()

    try:
        server._indexer_for({"vault_path": "Vault"}).sync()
        args = {"vault_path": "Vault", "source": "doc.pdf",
                "revision_id": staged.revision_id, "occurrence_id": "occ-0",
                "representation": "inline"}
        first = server._kb_read_media(args)
        assert "isError" not in first
        assert _json.loads(first["content"][0]["text"])["variant_source"] == "generated"
        second = server._kb_read_media(args)
        assert "isError" not in second
        assert _json.loads(second["content"][0]["text"])["variant_source"] == "stored"
        # 变体是真实持久化事实，不是只在响应里声称
        check = server._indexer_for({"vault_path": "Vault"}).document_store()
        variant = check.read_media("doc.pdf", revision_id=staged.revision_id,
                                   occurrence_id="occ-0", variant_id="preview", include_data=True)
        assert variant["mime_type"] == "image/jpeg" and len(variant["data"]) > 0
    finally:
        server.shutdown()


def test_short_name_ambiguity_across_virtual_sources(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    (vault / "b").mkdir(parents=True)
    (vault / "a" / "report.pdf").write_bytes(b"%PDF-1.4 a")
    (vault / "b" / "report.pdf").write_bytes(b"%PDF-1.4 b")
    config_path = tmp_path / "app.toml"
    config_path.write_text('mode = "static"\n\n[ingest]\nenabled = false\nstorage = "virtual"\n',
                           encoding="utf-8")
    server = VaultMcpServer(config_path)
    server.config.cache.dir = str(tmp_path / "cache")
    server.config.cache.enabled = True
    server.config.cache.placement = "home"
    server.registry.add(str(vault), name="Vault")
    layout = resolve_storage_layout(server.config, vault, registered_vaults=[])
    store = DocumentStore(layout, server.config)
    store.open(write=True)
    for source in ("a/report.pdf", "b/report.pdf"):
        _commit_document(store, vault, source, MARKDOWN)
    store.close()
    try:
        indexer = server._indexer_for({"vault_path": "Vault"})
        indexer.sync()
        assert len(_virtual_chunks(indexer)) >= 2
        with pytest.raises(ValueError) as excinfo:
            server._kb_read({"source": "report", "vault_path": "Vault"})
        assert "ambiguous" in str(excinfo.value)
        # 显式路径仍然可读
        explicit = server._kb_read({"source": "a/report.pdf", "vault_path": "Vault"})
        assert explicit["source_kind"] == "virtual"
    finally:
        server.shutdown()
