"""E03-b：迁移不得覆盖现代/隐藏/归属不明已提交事实（NEW）。

真实 SQLite 第二连接读回；提交前重核物理源 SHA/stat 与 change_seq CAS。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout
from mortis_rag_mcp.ingest.migration import migrate_legacy_mirrors

MODERN = "# Modern\n\nmodern parse output\n"
LEGACY_BODY = "# Legacy\n\nlegacy parse output\n"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    return cfg


def _store(tmp_path: Path, vault: Path, cfg: AppConfig) -> DocumentStore:
    store = DocumentStore(resolve_storage_layout(cfg, vault), cfg)
    store.open(write=True)
    return store


def _fixture(tmp_path: Path) -> tuple[Path, AppConfig, DocumentStore, str]:
    vault = tmp_path / "vault"
    (vault / "papers").mkdir(parents=True)
    (vault / ".mortis-parsed" / "papers").mkdir(parents=True)
    (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 payload")
    sha = hashlib.sha256((vault / "papers" / "paper.pdf").read_bytes()).hexdigest()
    (vault / ".mortis-parsed" / "papers" / "paper.md").write_text(
        "---\n"
        "source_pdf: papers/paper.pdf\n"
        f"source_sha256: {sha}\n"
        "parsed_by: mineru-v4\n"
        "---\n" + LEGACY_BODY,
        encoding="utf-8",
    )
    (vault / ".mortis-parsed" / ".ingest_state.json").write_text(
        json.dumps({"jobs": {"j1": {"source": "papers/paper.pdf", "state": "done", "sha256": sha}}}),
        encoding="utf-8",
    )
    cfg = _cfg(tmp_path)
    store = _store(tmp_path, vault, cfg)
    return vault, cfg, store, sha


def _commit(store: DocumentStore, vault: Path, body: str, parser: str,
            source: str = "papers/paper.pdf") -> str:
    physical = vault / source
    staged = store.stage_revision(
        source=source,
        source_sha256=hashlib.sha256(physical.read_bytes()).hexdigest(),
        render_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        parser_fingerprint=parser,
        markdown=body,
        capabilities={"coverage": "full"},
    )
    store.commit_revision(staged.revision_id,
                          source_sha256=hashlib.sha256(physical.read_bytes()).hexdigest())
    return staged.revision_id


def test_migration_does_not_overwrite_modern_result(tmp_path: Path):
    vault, cfg, store, _sha = _fixture(tmp_path)
    try:
        modern_rev = _commit(store, vault, MODERN, "mineru-v4:current")
        result = migrate_legacy_mirrors(store, apply=True)
        assert result["migrated"] == 0
        item = result["items"][0]
        assert item["status"] == "pending_manual" and "modern" in item["reason"]
        # 第二连接读回：现代事实完好无损
        other = _store(tmp_path, vault, cfg)
        try:
            active = other.get_active("papers/paper.pdf")
            assert active is not None and active.active_revision == modern_rev
            assert active.revision.parser_fingerprint == "mineru-v4:current"
        finally:
            other.close()
        assert (vault / ".mortis-parsed" / "papers" / "paper.md").is_file()
    finally:
        store.close()


def test_migration_does_not_overwrite_hidden_or_unverified_fact(tmp_path: Path):
    vault, cfg, store, _sha = _fixture(tmp_path)
    try:
        rev = _commit(store, vault, MODERN, "mineru-v4:current")
        store.set_visibility("papers/paper.pdf", "exempt")
        result = migrate_legacy_mirrors(store, apply=True)
        assert result["migrated"] == 0
        assert result["items"][0]["status"] == "pending_manual"
        assert "visibility" in result["items"][0]["reason"]
        # get_active 默认看不到 exempt —— 隐藏事实依然存在且未被覆盖
        assert store.get_active("papers/paper.pdf") is None
        hidden = store.get_active("papers/paper.pdf", include_hidden=True)
        assert hidden is not None and hidden.active_revision == rev
    finally:
        store.close()


def test_dry_run_reports_pending_manual_instead_of_ready(tmp_path: Path):
    vault, _cfg, store, _sha = _fixture(tmp_path)
    try:
        _commit(store, vault, MODERN, "mineru-v4:current")
        dry = migrate_legacy_mirrors(store, apply=False)
        assert dry["migrated"] == 0
        assert dry["items"][0]["status"] == "pending_manual"
        assert dry["excluded_sources"] == []
    finally:
        store.close()


def test_source_changed_after_preflight_is_refused(tmp_path: Path):
    """预检通过后、提交前物理源被改 → 重新核 SHA 失败，不发布。"""
    vault, cfg, store, _sha = _fixture(tmp_path)
    try:
        real_stage = store.stage_revision

        def _stage(*args, **kwargs):
            (vault / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4 CHANGED")
            return real_stage(*args, **kwargs)

        store.stage_revision = _stage  # type: ignore[method-assign]
        result = migrate_legacy_mirrors(store, apply=True)
        assert result["migrated"] == 0
        assert "changed after preflight" in result["items"][0]["reason"]
        other = _store(tmp_path, vault, cfg)
        try:
            assert other.get_active("papers/paper.pdf") is None
        finally:
            other.close()
    finally:
        store.close()


def test_cas_conflict_does_not_publish(tmp_path: Path):
    """CAS 抢占（change_seq 已在锁外变化）→ 明确失败，不留下半截事实。"""
    vault, cfg, store, _sha = _fixture(tmp_path)
    try:
        store.change_seq = lambda: 99999  # type: ignore[method-assign]
        result = migrate_legacy_mirrors(store, apply=True)
        assert result["migrated"] == 0
        assert "publish failed" in result["items"][0]["reason"]
        other = _store(tmp_path, vault, cfg)
        try:
            assert other.get_active("papers/paper.pdf") is None
        finally:
            other.close()
    finally:
        store.close()


def test_idempotent_apply_keeps_media_and_mirror_facts(tmp_path: Path):
    vault, _cfg, store, _sha = _fixture(tmp_path)
    try:
        first = migrate_legacy_mirrors(store, apply=True)
        assert first["migrated"] == 1
        active = store.get_active("papers/paper.pdf")
        assert active is not None
        second = migrate_legacy_mirrors(store, apply=True)
        assert second["migrated"] == 0
        assert [item["status"] for item in second["items"]] == ["already_migrated"]
        again = store.get_active("papers/paper.pdf")
        assert again is not None and again.active_revision == active.active_revision
        assert again.revision.source_sha256 == _sha
    finally:
        store.close()
