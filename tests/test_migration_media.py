"""C97：旧镜像迁移的 dry-run/apply、幂等与「资产真正入库」（不联网）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout
from mortis_rag_mcp.ingest.migration import MIGRATION_VERSION, migrate_legacy_mirrors

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
BODY = "# Legacy\n\n![fig](paper.assets/fig.png)\n\nparsed body\n"


def _fixture(tmp_path: Path):
    vault = tmp_path / "vault"
    mirrors = vault / ".mortis-parsed" / "papers"
    (mirrors / "paper.assets").mkdir(parents=True)
    (vault / "papers").mkdir(parents=True, exist_ok=True)
    source = vault / "papers" / "paper.pdf"
    source.write_bytes(b"%PDF-1.4 payload")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    (mirrors / "paper.assets" / "fig.png").write_bytes(PNG)
    (mirrors / "paper.md").write_text(
        "---\n"
        f"source_pdf: papers/paper.pdf\n"
        f"source_sha256: {sha}\n"
        "parsed_by: mineru-v4\n"
        "---\n" + BODY,
        encoding="utf-8",
    )
    (vault / ".mortis-parsed" / ".ingest_state.json").write_text(
        json.dumps({"jobs": {"j1": {"source": "papers/paper.pdf", "state": "done", "sha256": sha}}}),
        encoding="utf-8",
    )
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    return vault, cfg, sha


def _store(tmp_path, cfg, vault) -> DocumentStore:
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)
    return store


def test_dry_run_changes_nothing_then_apply_imports_media(tmp_path):
    vault, cfg, sha = _fixture(tmp_path)
    store = _store(tmp_path, cfg, vault)
    try:
        dry = migrate_legacy_mirrors(store, apply=False)
        assert dry["migrated"] == 0 and dry["migrated_assets"] == 0
        ready = [item for item in dry["items"] if item["status"] == "ready"]
        assert len(ready) == 1 and ready[0]["media_assets"] == 1
        assert store.get_active("papers/paper.pdf") is None, "dry-run 不得写库"

        applied = migrate_legacy_mirrors(store, apply=True)
        assert applied["migrated"] == 1 and applied["migrated_assets"] == 1
        active = store.get_active("papers/paper.pdf")
        assert active is not None and active.revision.state == "committed"
        assert active.revision.source_sha256 == sha
        assert active.revision.parser_fingerprint == f"{MIGRATION_VERSION}:mineru-v4"

        # 资产真正入库：可读、magic 一致、anchor 来自正文引用位置
        media = store.list_media("papers/paper.pdf", revision_id=active.active_revision)
        assert len(media) == 1
        entry = media[0]
        assert entry["kind"] == "image"
        assert entry["metadata"]["anchor_start"] == BODY.index("![fig]")
        assert entry["metadata"]["legacy_mirror"] == ".mortis-parsed/papers/paper.md"
        blob = store.read_media("papers/paper.pdf", revision_id=active.active_revision,
                                occurrence_id=entry["occurrence_id"], include_data=True)
        assert blob["data"] == PNG and blob["mime_type"] == "image/png"

        # 幂等：重复 apply 不重复建 blob/job
        again = migrate_legacy_mirrors(store, apply=True)
        assert again["migrated"] == 0
        assert [item["status"] for item in again["items"]] == ["already_migrated"]
        assert len(store.list_media("papers/paper.pdf",
                                    revision_id=active.active_revision)) == 1
    finally:
        store.close()


def test_unprovable_mirror_stays_pending_manual(tmp_path):
    vault, cfg, sha = _fixture(tmp_path)
    # 源 SHA 与镜像声明不符 → 无法证明归属，绝不自动"修复"
    (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 CHANGED")
    store = _store(tmp_path, cfg, vault)
    try:
        result = migrate_legacy_mirrors(store, apply=True)
        assert result["migrated"] == 0
        item = result["items"][0]
        assert item["status"] == "pending_manual" and "SHA" in item["reason"]
        assert store.get_active("papers/paper.pdf") is None
        # 未迁移镜像保留（不删除、不改写）
        assert (vault / ".mortis-parsed" / "papers" / "paper.md").is_file()
    finally:
        store.close()


def test_remote_or_missing_asset_is_rejected(tmp_path):
    vault, cfg, sha = _fixture(tmp_path)
    (vault / ".mortis-parsed" / "papers" / "paper.md").write_text(
        "---\n"
        f"source_pdf: papers/paper.pdf\n"
        f"source_sha256: {sha}\n"
        "parsed_by: mineru-v4\n"
        "---\n# Legacy\n\n![fig](https://example.invalid/fig.png)\n",
        encoding="utf-8",
    )
    store = _store(tmp_path, cfg, vault)
    try:
        result = migrate_legacy_mirrors(store, apply=True)
        assert result["migrated"] == 0
        assert "remote/data" in result["items"][0]["reason"]
    finally:
        store.close()
