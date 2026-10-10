"""T02：doc_store schema/API 集成测试（真实 SQLite，不 mock 存储）。

计划依据 §12.3（schema/quota）、§12.4（锁序/不 fail-open）、§12.5（发布 CAS）、
§11.2（home 真实归属）、§17.4（cache.enabled=false 拒虚拟摄取）。

宿主隔离：每个用例用 `tmp_path` 显式构造 AppConfig 并显式传 `registered_vaults=[]`，
绝不读真实 `~/.mortis_rag_mcp` 注册表，不联网。
"""

from __future__ import annotations

import sqlite3
import threading
import types

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import (
    ControlStore,
    DocumentStore,
    LockUnavailable,
    MediaSpec,
    StoreBindingMismatch,
    StoreConflict,
    StoreQuotaExceeded,
    SourcePathError,
    VirtualStorageDisabled,
    normalize_source_path,
    resolve_storage_layout,
)


# --------------------------------------------------------------------- helpers


def make_config(
    tmp_path,
    *,
    enabled: bool = True,
    placement: str = "home",
    cache_dir=None,
    cache_id: str = "",
) -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(cache_dir if cache_dir is not None else tmp_path / "cache")
    cfg.cache.enabled = enabled
    cfg.cache.placement = placement
    cfg.cache.id = cache_id
    return cfg


def make_layout(cfg, vault):
    return resolve_storage_layout(cfg, vault, registered_vaults=[])


def open_store(layout, cfg) -> DocumentStore:
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    return store


def make_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def stage(store: DocumentStore, source: str, *, markdown: str = "# doc", page_map=None) -> str:
    staged = store.stage_revision(
        source=source,
        source_sha256="a" * 64,
        render_sha256="b" * 64,
        parser_fingerprint="fp-1",
        markdown=markdown,
        page_map=page_map,
    )
    return staged.revision_id


# --------------------------------------------------------------------- tests


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """写门禁会读「已注册库」：把注册表钉到用例临时目录，不读宿主 ~/.mortis_rag_mcp。

    （宿主注册表默认落点的隔离缺口属 v0.8.2 的 DCGFH-F；本 Lane 的读写路径必须
    自带隔离，不能新增对真实用户配置的依赖。）
    """
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(tmp_path / "vaults.toml"))
    monkeypatch.delenv("VAULT_MCP_REGISTRY", raising=False)


def test_schema_created_and_reopen_stable(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)

    store = open_store(layout, cfg)
    meta = store.store_meta()
    assert meta.schema_version == 1
    assert meta.store_uuid
    assert meta.generation_id == "g0001"
    first_uuid = meta.store_uuid
    store.close()

    # 同线程重开验证「重启」语义：store_uuid 稳定，schema 不重建。
    store2 = DocumentStore(layout, cfg)
    store2.open()
    assert store2.store_meta().store_uuid == first_uuid
    assert store2.store_meta().schema_version == 1
    store2.close()


def test_shared_blob_across_docs_not_misdeleted(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)

    shared = MediaSpec(occurrence_id="fig", kind="image", mime_type="image/png", data=b"SHARED-BYTES", page=1)
    unique = MediaSpec(occurrence_id="fig2", kind="image", mime_type="image/png", data=b"UNIQUE-BYTES", page=2)

    # docA / docB 共享同一 blob（相同内容）。
    for source in ("a.pdf", "b.pdf"):
        rid = stage(store, source)
        store.append_media(rid, [shared])
        store.commit_revision(rid, source_sha256="a" * 64)
    assert store.store_meta().blob_count == 1

    # docC 引用共享 blob + 一个独占 blob，然后丢弃候选 → 独占 blob 应被 GC。
    rid_c = stage(store, "c.pdf")
    store.append_media(rid_c, [shared, unique])
    assert store.discard_staged(rid_c) is True

    removed = store.gc_unreferenced()
    assert removed == 1
    assert store.store_meta().blob_count == 1  # 共享 blob 因 docA/docB 仍被引用而保留
    store.close()


def test_foreign_keys_enforced(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)

    conn = store._open_conn(store.generation_id)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO media_occurrences (revision_id, occurrence_id, blob_id, kind, ordinal) "
            "VALUES ('missing-rev', 'o1', 'missing-blob', 'image', 0)"
        )
    conn.rollback()
    store.close()


def test_quota_exceeded_rejects_stage_without_partial_data(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    # 极小 quota：0.001 MiB ≈ 1048 字节。
    shim = types.SimpleNamespace(cache=cfg.cache, doc_store=types.SimpleNamespace(max_size_mb=0.001))
    layout = resolve_storage_layout(shim, vault, registered_vaults=[])
    store = DocumentStore(layout, shim)
    store.open(write=True)

    with pytest.raises(StoreQuotaExceeded):
        stage(store, "big.txt", markdown="x" * 4096)
    # 不产生半截数据。
    assert store.store_meta().document_count == 0
    assert store.store_meta().revision_count == 0
    store.close()


def test_two_writers_only_one_holds_mutation_lock(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)

    outcome: list[str] = []
    started = threading.Event()

    def worker() -> None:
        started.set()
        try:
            with store.mutation(timeout=0.3):
                outcome.append("acquired")
        except LockUnavailable:
            outcome.append("locked")

    with store.mutation():
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(2.0)
        thread.join(5.0)

    assert outcome == ["locked"]
    store.close()


def test_append_media_enforces_quota_before_any_insert(tmp_path):
    """媒体必须计入 quota，且在任何插入之前裁决（审核实测发现 append 原无检查）。

    `stage_revision` 只按 markdown 记账，若 append 不查 quota，"按配额合法"的
    文档仍能靠媒体把文档库撑爆——这就是一条 fail-open 路径。
    """
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    cfg.doc_store.max_size_mb = 1  # 1 MiB
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)
    rev = stage(store, "big.pdf", markdown="# big")

    with pytest.raises(StoreQuotaExceeded):
        store.append_media(rev, [MediaSpec("fig", "image", "image/png", b"x" * (1024 * 1024))])

    # 一点都没写进去（不是"先写后报错"）
    assert store.store_meta().blob_count == 0
    store.close()


def test_nonpositive_quota_limit_never_disables_the_limit(tmp_path):
    """§17.4「0 不得关闭安全限制」：非正/非法配额一律回落默认上限，不是无限制。"""
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)

    for bad in (0, -1, float("nan"), True):
        cfg.doc_store.max_size_mb = bad
        store = DocumentStore(layout, cfg)
        store.open(write=True)
        assert store.quota_status().limit_bytes == 2048 * 1024 * 1024, bad
        store.close()


def test_staged_invisible_and_commit_publishes(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)

    rid = stage(store, "papers/a.pdf", markdown="# attention", page_map=[1])
    # staged 对读端不可见。
    assert store.get_active("papers/a.pdf") is None
    assert list(store.iter_visible_documents()) == []

    new_seq = store.commit_revision(rid, expected_change_seq=0, source_sha256="a" * 64)
    assert new_seq == 1
    active = store.get_active("papers/a.pdf")
    assert active is not None
    assert active.revision.state == "committed"
    assert active.revision.parsed_markdown == "# attention"
    assert active.revision.page_map == [1]
    assert [d.source for d in store.iter_visible_documents()] == ["papers/a.pdf"]
    store.close()


def test_cas_conflict_on_expected_change_seq(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)

    rid = stage(store, "a.md")
    with pytest.raises(StoreConflict):
        store.commit_revision(rid, expected_change_seq=999)
    store.close()


def test_source_sha_mismatch_on_commit(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)

    rid = stage(store, "a.md")
    with pytest.raises(StoreConflict):
        store.commit_revision(rid, source_sha256="c" * 64)
    store.close()


def test_exempt_commit_not_published(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path)
    layout = make_layout(cfg, vault)
    store = open_store(layout, cfg)

    rid = stage(store, "secret.pdf", markdown="解析事实")
    store.set_visibility("secret.pdf", "exempt")
    store.commit_revision(rid)

    assert store.get_active("secret.pdf") is None
    assert store.get_active("secret.pdf", include_hidden=True) is None
    assert list(store.iter_visible_documents()) == []
    # 解析事实仍保留（committed revision 计数为 1），只是不发布。
    assert store.store_meta().revision_count == 1
    store.close()


def test_normalize_source_path_accepts_and_rejects(tmp_path):
    vault = make_vault(tmp_path)
    assert normalize_source_path("papers/a.pdf", vault) == "papers/a.pdf"
    assert normalize_source_path("a\\b\\c.md", vault) == "a/b/c.md"

    bad = [
        "",
        "/abs/a.pdf",
        "C:/x.pdf",
        "C:\\x.pdf",
        "//server/share/a.pdf",
        "\\\\server\\share\\a.pdf",
        "a/../b.pdf",
        "../a.pdf",
        "a/./b.md",
        "a//b.md",
    ]
    for value in bad:
        with pytest.raises(SourcePathError):
            normalize_source_path(value, vault)


def test_home_cache_inside_vault_blocked(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path, cache_dir=vault / "cache_inside")
    layout = make_layout(cfg, vault)
    assert layout.blocked_reason

    store = DocumentStore(layout, cfg)
    with pytest.raises(VirtualStorageDisabled):
        store.open(write=True)
    with pytest.raises(VirtualStorageDisabled):
        with store.mutation():
            pass


def test_cache_disabled_blocks_virtual_write(tmp_path):
    vault = make_vault(tmp_path)
    cfg = make_config(tmp_path, enabled=False)
    layout = make_layout(cfg, vault)
    assert layout.enabled is False
    assert layout.blocked_reason == ""

    store = DocumentStore(layout, cfg)
    with pytest.raises(VirtualStorageDisabled):
        store.open(write=True)


def test_control_binding_mismatch_rejected_on_write(tmp_path):
    cache_dir = tmp_path / "cache"
    vault_a = tmp_path / "a"
    vault_b = tmp_path / "b"
    vault_a.mkdir()
    vault_b.mkdir()

    cfg_a = make_config(tmp_path, cache_dir=cache_dir, cache_id="shared")
    cfg_b = make_config(tmp_path, cache_dir=cache_dir, cache_id="shared")
    layout_a = make_layout(cfg_a, vault_a)
    layout_b = make_layout(cfg_b, vault_b)
    assert layout_a.control_path == layout_b.control_path  # 同 cache.id、同缓存根

    ctrl = ControlStore(layout_a)
    ctrl.open(create=True, write=True)
    ctrl.close()

    with pytest.raises(StoreBindingMismatch):
        ControlStore(layout_b).open(create=True, write=True)
    # 读模式允许降级打开（不抛）。
    ControlStore(layout_b).open(create=True, write=False)
