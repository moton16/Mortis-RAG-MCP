import hashlib
import json
import sqlite3

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import DocumentStore, ControlStore, SnapshotInvalid, StoreConflict, resolve_storage_layout, validate_backup
from mortis_rag_mcp.ingest.migration import migrate_legacy_mirrors


def make_store(tmp_path, monkeypatch):
    monkeypatch.setattr("mortis_rag_mcp.doc_store.registered_vault_paths", lambda: [])
    root = tmp_path / "vault"
    root.mkdir()
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    layout = resolve_storage_layout(cfg, root, registered_vaults=[])
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    source = root / "paper.pdf"
    source.write_bytes(b"original")
    sha = hashlib.sha256(b"original").hexdigest()
    staged = store.stage_revision(source="paper.pdf", source_sha256=sha,
        render_sha256=hashlib.sha256(b"parsed").hexdigest(), parser_fingerprint="fixture", markdown="parsed")
    store.commit_revision(staged.revision_id, source_sha256=sha)
    return store


def test_import_isolated_and_cas_and_retained(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    original = store.generation_id
    backup = store.backup_to(tmp_path / "backup.sqlite")
    prepared = store.prepare_import(backup)
    assert store.generation_id == original
    store.publish_import(prepared)
    assert store.get_active("paper.pdf") is None
    assert original != store.generation_id
    store.restore_generation(original)
    assert store.get_active("paper.pdf").revision.parsed_markdown == "parsed"
    with pytest.raises(StoreConflict):
        store.publish_import(prepared)
    store.close()


def test_trusted_matching_source_and_stale_connection(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    other = DocumentStore(store.layout, store.config)
    other.open()
    old = other.generation_id
    backup = store.backup_to(tmp_path / "backup.sqlite")
    prepared = store.prepare_import(backup, trust_parsed_documents=True)
    store.publish_import(prepared, trusted=True)
    assert other.get_active("paper.pdf").revision.parsed_markdown == "parsed"
    assert other.generation_id != old
    other.close()
    store.close()


def test_malicious_schema_refused_and_pin(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    backup = store.backup_to(tmp_path / "backup.sqlite")
    conn = sqlite3.connect(backup)
    conn.execute("CREATE VIEW evil AS SELECT * FROM documents")
    conn.commit()
    conn.close()
    with pytest.raises(SnapshotInvalid):
        validate_backup(backup)
    ctrl = ControlStore(store.layout)
    ctrl.open(create=False)
    with store.pin_generation() as generation:
        assert ctrl.generation_is_pinned(generation)
    assert not ctrl.generation_is_pinned(generation)
    ctrl.close()
    store.close()


def test_migration_dry_run_no_control_or_vault_write(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    before = sorted(p.relative_to(store.layout.vault_path).as_posix() for p in store.layout.vault_path.rglob("*"))
    assert migrate_legacy_mirrors(store)["migrated"] == 0
    assert before == sorted(p.relative_to(store.layout.vault_path).as_posix() for p in store.layout.vault_path.rglob("*"))
    store.close()


def _write_legacy_mirror(root, body, *, extra_fields="", sha=None, assets=None):
    sha = sha or hashlib.sha256(b"original").hexdigest()
    mirrors = root / ".mortis-parsed"
    mirrors.mkdir(parents=True, exist_ok=True)
    (mirrors / "paper.md").write_text(
        f"---\nsource_pdf: \"paper.pdf\"{extra_fields}\nsource_sha256: \"{sha}\"\nparsed_by: \"vlm\"\n---\n{body}",
        encoding="utf-8",
    )
    for rel, payload in (assets or {}).items():
        target = mirrors / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (mirrors / ".ingest_state.json").write_text(
        json.dumps({"jobs": {"j1": {"source": "paper.pdf", "state": "done", "sha256": sha}}}),
        encoding="utf-8",
    )


def test_migration_apply_is_idempotent_and_source_precise(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    _write_legacy_mirror(store.layout.vault_path, "旧解析正文。\n")

    first = migrate_legacy_mirrors(store, apply=True)
    assert first["migrated"] == 1
    # 来源精确排除：只列出已迁移的具体镜像，不是整目录忽略。
    assert first["excluded_sources"] == [".mortis-parsed/paper.md"]

    second = migrate_legacy_mirrors(store, apply=True)
    assert second["migrated"] == 0
    assert second["excluded_sources"] == [".mortis-parsed/paper.md"]
    store.close()


def test_migration_unsafe_source_is_pending_manual(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    mirrors = store.layout.vault_path / ".mortis-parsed"
    mirrors.mkdir(parents=True, exist_ok=True)
    (mirrors / "bad.md").write_text(
        f'---\nsource_pdf: "/etc/passwd"\nsource_sha256: "{"a" * 64}"\nparsed_by: "vlm"\n---\n正文\n',
        encoding="utf-8",
    )
    result = migrate_legacy_mirrors(store, apply=True)
    assert result["migrated"] == 0
    assert result["items"][0]["status"] == "pending_manual"
    store.close()


def test_migration_rejects_unprovable_asset_magic(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    _write_legacy_mirror(
        store.layout.vault_path,
        "![x](paper.assets/evil.png)\n",
        assets={"paper.assets/evil.png": b"not an image"},
    )
    result = migrate_legacy_mirrors(store, apply=True)
    assert result["migrated"] == 0
    assert result["items"][0]["status"] == "pending_manual"
    store.close()
