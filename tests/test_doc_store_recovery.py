"""T09：doc_store 恢复/损坏/快照校验集成测试（真实 SQLite + 真实重启语义）。

计划依据 §12.6（两阶段对账、撤销与崩溃恢复）、§20.2（SQLite backup 而非主文件 copy）、
§12.3/§23.1（未知更高 schema fail closed，不删文件）。

宿主隔离：`tmp_path` + 显式 AppConfig + `registered_vaults=[]`，绝不读真实注册表。
"""

from __future__ import annotations

import sqlite3

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import (
    ControlStore,
    DocumentStore,
    MediaSpec,
    SnapshotInvalid,
    StoreConflict,
    StoreContractError,
    StoreCorrupt,
    StoreSchemaUnsupported,
    resolve_storage_layout,
    validate_backup,
)


# --------------------------------------------------------------------- helpers


def make_config(tmp_path, *, enabled: bool = True) -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = enabled
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    return cfg


def setup_store(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = make_config(tmp_path)
    layout = resolve_storage_layout(cfg, vault, registered_vaults=[])
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    return cfg, layout, store


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


def docstore_path(layout, generation_id: str):
    return layout.generations_dir / generation_id / "docstore.sqlite"


# --------------------------------------------------------------------- tests


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """写门禁会读「已注册库」：把注册表钉到用例临时目录，不读宿主 ~/.mortis_rag_mcp。"""
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(tmp_path / "vaults.toml"))
    monkeypatch.delenv("VAULT_MCP_REGISTRY", raising=False)


def test_committed_facts_survive_reopen(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    rid = stage(store, "paper.pdf", markdown="# body", page_map=[1, 2])
    store.commit_revision(rid)
    doc_id = store.get_active("paper.pdf").doc_id
    store.close()

    reopened = DocumentStore(layout, cfg)
    reopened.open()
    active = reopened.get_active("paper.pdf")
    assert active is not None
    assert active.doc_id == doc_id
    assert active.revision.parsed_markdown == "# body"
    assert active.revision.page_map == [1, 2]
    assert reopened.integrity_check() is None
    reopened.close()


def test_staged_invisible_and_recover_cleans_candidates(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    keep_rid = stage(store, "keep.md", markdown="# keep")
    store.commit_revision(keep_rid)
    stage(store, "temp.md", markdown="# temp")  # 未 commit 的候选
    store.close()

    reopened = DocumentStore(layout, cfg)
    reopened.open()
    assert reopened.get_active("keep.md") is not None
    assert reopened.get_active("temp.md") is None
    assert [d.source for d in reopened.iter_visible_documents()] == ["keep.md"]

    assert reopened.recover() == 1  # 丢弃 staged 候选
    assert reopened.get_active("keep.md") is not None  # committed 事实不变
    assert reopened.recover() == 0
    reopened.close()


def test_corrupt_docstore_fail_closed_and_file_preserved(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    rid = stage(store, "a.md")
    store.commit_revision(rid)
    generation_id = store.generation_id
    store.close()

    path = docstore_path(layout, generation_id)
    junk = bytes(range(256)) * 4
    path.write_bytes(junk)
    size_before = path.stat().st_size

    reopened = DocumentStore(layout, cfg)
    with pytest.raises(StoreCorrupt):
        reopened.open()

    # fail closed 且**不删除**文件。
    assert path.exists()
    assert path.stat().st_size == size_before
    assert path.read_bytes() == junk


def test_corrupt_control_fails_closed_not_reported_as_not_found(tmp_path):
    """控制面损坏不得被吞成「这个源没入库」：读路径必须显式失败（§12.6/§23.4）。

    真实缺陷（审核实测）：`_active_generation()` 原先把一切 DocStoreError 都吞成
    空 generation，于是控制面损坏/未知 schema/绑定冲突在**读路径**上表现为
    `get_active() -> None`——调用方会据此误判为未摄取，而不是 fail closed。
    """
    cfg, layout, store = setup_store(tmp_path)
    store.commit_revision(stage(store, "a.md", markdown="# a"))
    store.close()

    junk = b"\x00" * 512
    layout.control_path.write_bytes(junk)

    reopened = DocumentStore(layout, cfg)
    with pytest.raises(StoreCorrupt):
        reopened.open(write=False)
    # fail closed 且不删除用户文件。
    assert layout.control_path.read_bytes() == junk
    reopened.close()


def test_missing_table_gives_stable_code_not_raw_sqlite_error(tmp_path):
    """D3：缺表（外部删表/半截复制）必须给稳定 code，而不是原生 OperationalError。

    读路径原来只在 `_ensure_schema` 处封装错误，之后的 `conn.execute` 一旦撞上
    缺表就抛**没有 code** 的 `sqlite3.OperationalError`；`quota_status` 甚至静默
    返回空状态。违反 §12.6「每条路径保留可判定的 code」。
    """
    cfg, layout, store = setup_store(tmp_path)
    store.commit_revision(stage(store, "a.md", markdown="# a"))
    db = docstore_path(layout, store.generation_id)
    store.close()

    def _drop(table: str) -> None:
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys=OFF")  # 允许删被引用的父表
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
        conn.close()

    def _assert_corrupt(call) -> None:
        with pytest.raises(StoreCorrupt) as excinfo:
            call()
        assert excinfo.value.code == "STORE_CORRUPT"

    _drop("documents")
    reopened = DocumentStore(layout, cfg)
    reopened.open()
    _assert_corrupt(reopened.store_meta)  # 计数查询撞缺表
    _assert_corrupt(lambda: reopened.get_active("a.md"))
    _assert_corrupt(lambda: list(reopened.iter_visible_documents()))
    # change_seq 只读 store_meta：缺 documents 不该假报损坏（稳定 code ≠ 一律报错）
    assert reopened.change_seq() >= 1
    reopened.close()

    _drop("document_revisions")
    reopened2 = DocumentStore(layout, cfg)
    reopened2.open()
    _assert_corrupt(reopened2.quota_status)  # 原实现静默返回空状态
    reopened2.close()


def test_nested_mutation_does_not_roll_back_outer_uncommitted_write(tmp_path):
    """D5：内层 mutation 正常退出不得回滚外层尚未提交的写入（可重入锁允许嵌套）。

    修复「异常后不回滚」时新增的「未显式 commit 就清理」出口必须只作用于**最外层**
    帧，否则嵌套场景会把外层事务一起回滚掉。
    """
    cfg, layout, store = setup_store(tmp_path)
    with store.mutation() as outer:
        outer.execute(
            "INSERT INTO documents (doc_id, source, visibility, policy_version, updated_at) "
            "VALUES ('nested-doc', 'nested.pdf', 'active', '', 0.0)"
        )
        with store.mutation() as inner:
            assert inner is outer
        assert outer.in_transaction, "内层退出后外层事务必须仍然存活"
        assert outer.execute(
            "SELECT COUNT(*) FROM documents WHERE doc_id = 'nested-doc'"
        ).fetchone()[0] == 1
        outer.rollback()  # 清理：本用例只验证嵌套语义，不需要落库

    assert store.get_active("nested.pdf") is None
    store.close()


def test_unknown_higher_docstore_schema_rejected(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    generation_id = store.generation_id
    store.close()

    path = docstore_path(layout, generation_id)
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE store_meta SET schema_version = 999")
    conn.commit()
    conn.close()

    with pytest.raises(StoreSchemaUnsupported):
        DocumentStore(layout, cfg).open()


def test_unknown_higher_control_schema_rejected(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    store.close()

    conn = sqlite3.connect(str(layout.control_path))
    conn.execute("UPDATE control_meta SET schema_version = 999")
    conn.commit()
    conn.close()

    with pytest.raises(StoreSchemaUnsupported):
        ControlStore(layout).open(create=True, write=True)


def test_backup_and_validate_roundtrip(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    rid = stage(store, "origin.pdf", markdown="# origin", page_map=[1])
    store.append_media(rid, [MediaSpec("fig", "image", "image/png", b"PNGDATA")])
    store.commit_revision(rid)

    dest = store.backup_to(tmp_path / "snapshot.sqlite")
    info = validate_backup(dest)
    assert info["schema_version"] == 1
    assert info["integrity"] == "ok"
    assert "documents" in info["tables"]
    assert "media_blobs" in info["tables"]
    store.close()

    # 备份可独立读取 committed 事实。
    conn = sqlite3.connect(str(dest))
    row = conn.execute(
        "SELECT parsed_markdown FROM document_revisions WHERE state = 'committed'"
    ).fetchone()
    conn.close()
    assert row[0] == "# origin"

    # 坏快照显式拒绝。
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database at all")
    with pytest.raises(SnapshotInvalid):
        validate_backup(bad)


def test_gc_removes_only_unreferenced_blobs(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    rid = stage(store, "a.md")
    store.append_media(rid, [MediaSpec("fig", "image", "image/png", b"AAA")])
    store.commit_revision(rid)
    assert store.store_meta().blob_count == 1

    rid2 = stage(store, "b.md")
    store.append_media(rid2, [MediaSpec("fig", "image", "image/png", b"BBB")])
    assert store.store_meta().blob_count == 2
    assert store.discard_staged(rid2) is True

    assert store.gc_unreferenced() == 1  # 只删零引用的 B blob
    assert store.store_meta().blob_count == 1
    store.close()


def test_deleted_not_visible_and_epoch_fences_old_worker(tmp_path):
    cfg, layout, store = setup_store(tmp_path)
    rid = stage(store, "a.md", markdown="# a")
    store.commit_revision(rid)
    assert store.get_active("a.md") is not None

    store.set_visibility("a.md", "deleted")
    assert store.get_active("a.md") is None
    assert list(store.iter_visible_documents()) == []
    # 解析事实保留，等待显式 purge。
    assert store.store_meta().revision_count >= 1

    # 模拟旧 worker：staged 后移库 epoch++，旧候选不能发布（不复活已删源）。
    rid2 = stage(store, "a.md", markdown="# a v2")
    ctrl = ControlStore(layout)
    ctrl.open(create=True, write=True)
    ctrl.bump_epoch()
    ctrl.close()

    with pytest.raises(StoreConflict):
        store.commit_revision(rid2)
    assert store.get_active("a.md") is None
    store.close()


def test_failed_media_batch_rolls_back_before_later_commit(tmp_path):
    """审核复现的 P1：失败批次必须整体回滚，半截候选不得被后续成功提交写实。

    真实缺陷：`mutation()` 体内异常逃逸时，sqlite3 的隐式事务留在**线程复用**的
    连接上，下一次成功写入的 commit() 会把只写了一半的 occurrence/blob 一并落库
    ——候选不再是「全有或全无」，且失败批次的内容可能被读端当作真实媒体复用。
    """
    cfg, layout, store = setup_store(tmp_path)
    rid = stage(store, "a.pdf", markdown="# a")
    duplicate_occurrence = [
        MediaSpec("occ1", "image", "image/png", b"PNG-1"),
        MediaSpec("occ1", "image", "image/png", b"PNG-2"),  # 主键冲突 → 整批拒绝
    ]
    with pytest.raises(StoreContractError):
        store.append_media(rid, duplicate_occurrence)

    stage(store, "b.pdf", markdown="# b")  # 后续一次成功写入会 commit

    conn = sqlite3.connect(str(docstore_path(layout, store.generation_id)))
    occurrences = conn.execute(
        "SELECT COUNT(*) FROM media_occurrences WHERE revision_id = ?", (rid,)
    ).fetchone()[0]
    blobs = conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0]
    conn.close()
    store.close()

    assert (occurrences, blobs) == (0, 0)


def test_unknown_lower_schema_version_is_rejected(tmp_path):
    """低于当前版本的 schema 同样未知：fail closed，不按 v1 猜字段（§23.1）。"""
    cfg, layout, store = setup_store(tmp_path)
    generation_id = store.generation_id
    store.close()

    conn = sqlite3.connect(str(docstore_path(layout, generation_id)))
    conn.execute("UPDATE store_meta SET schema_version = 0")
    conn.commit()
    conn.close()
    with pytest.raises(StoreSchemaUnsupported):
        DocumentStore(layout, cfg).open()

    conn = sqlite3.connect(str(layout.control_path))
    conn.execute("UPDATE control_meta SET schema_version = 0")
    conn.commit()
    conn.close()
    with pytest.raises(StoreSchemaUnsupported):
        ControlStore(layout).open(create=True, write=True)
