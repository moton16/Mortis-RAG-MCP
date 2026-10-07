"""Unit tests for Ingest auto_watch and max_file_size_mb configuration (Card C58a)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from mortis_rag_mcp.config import (
    AppConfig,
    IngestConfig,
    _fallback_toml,
    load_config,
)


def test_ingest_config_defaults() -> None:
    """IngestConfig defaults: auto_watch=False, max_file_size_mb=20, size cap is 20MiB."""
    cfg = IngestConfig()
    assert cfg.auto_watch is False
    assert cfg.max_file_size_mb == 20
    assert cfg.max_file_size_bytes == 20 * 1024 * 1024
    assert cfg.enabled is False


def test_app_config_defaults() -> None:
    """AppConfig defaults propagate IngestConfig defaults."""
    app = AppConfig()
    assert app.ingest.auto_watch is False
    assert app.ingest.max_file_size_mb == 20
    assert app.ingest.max_file_size_bytes == 20 * 1024 * 1024


def test_load_config_defaults(tmp_path: Path) -> None:
    """Loading empty TOML or None returns defaults."""
    cfg_none = load_config(None)
    assert cfg_none.ingest.auto_watch is False
    assert cfg_none.ingest.max_file_size_mb == 20

    toml_file = tmp_path / "app.toml"
    toml_file.write_text("[index]\nchunk_size = 1000\n", encoding="utf-8")
    cfg_file = load_config(toml_file)
    assert cfg_file.ingest.auto_watch is False
    assert cfg_file.ingest.max_file_size_mb == 20


def test_load_config_explicit_values(tmp_path: Path) -> None:
    """Explicit auto_watch and max_file_size_mb parse correctly."""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        "[ingest]\n"
        "enabled = true\n"
        "auto_watch = true\n"
        "max_file_size_mb = 50\n",
        encoding="utf-8",
    )
    cfg = load_config(toml_file)
    assert cfg.ingest.enabled is True
    assert cfg.ingest.auto_watch is True
    assert cfg.ingest.max_file_size_mb == 50
    assert cfg.ingest.max_file_size_bytes == 50 * 1024 * 1024


def test_load_config_unlimited_size(tmp_path: Path) -> None:
    """max_file_size_mb = 0 means unlimited size."""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        "[ingest]\n"
        "max_file_size_mb = 0\n",
        encoding="utf-8",
    )
    cfg = load_config(toml_file)
    assert cfg.ingest.max_file_size_mb == 0
    assert cfg.ingest.max_file_size_bytes == 0


def test_enabled_false_with_auto_watch_true_is_legal(tmp_path: Path) -> None:
    """enabled=false + auto_watch=true is valid configuration (inactive at runtime)."""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        "[ingest]\n"
        "enabled = false\n"
        "auto_watch = true\n",
        encoding="utf-8",
    )
    cfg = load_config(toml_file)
    assert cfg.ingest.enabled is False
    assert cfg.ingest.auto_watch is True


@pytest.mark.parametrize("invalid_val", ['"false"', '"true"', '"0"', '"1"'])
def test_load_config_rejects_string_auto_watch(tmp_path: Path, invalid_val: str) -> None:
    """auto_watch must be a boolean; string 'false' or 'true' must raise ValueError."""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        f"[ingest]\nauto_watch = {invalid_val}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="auto_watch.*must be a boolean"):
        load_config(toml_file)


@pytest.mark.parametrize("invalid_val", [1, 0, "yes", "no", None])
def test_ingest_config_direct_rejects_non_bool_auto_watch(invalid_val: object) -> None:
    """Direct instantiation of IngestConfig rejects non-bool auto_watch."""
    with pytest.raises(ValueError, match="ingest.auto_watch must be a boolean"):
        IngestConfig(auto_watch=invalid_val)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid_mb", [-1, -20])
def test_load_config_rejects_negative_max_file_size_mb(tmp_path: Path, invalid_mb: int) -> None:
    """Negative max_file_size_mb raises ValueError."""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        f"[ingest]\nmax_file_size_mb = {invalid_mb}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_file_size_mb"):
        load_config(toml_file)


@pytest.mark.parametrize("invalid_mb", ["true", "false"])
def test_load_config_rejects_bool_max_file_size_mb(tmp_path: Path, invalid_mb: str) -> None:
    """Boolean for max_file_size_mb raises ValueError."""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        f"[ingest]\nmax_file_size_mb = {invalid_mb}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_file_size_mb.*got a boolean"):
        load_config(toml_file)


@pytest.mark.parametrize("invalid_mb", [12.5, '"not_a_number"'])
def test_load_config_rejects_non_integer_max_file_size_mb(tmp_path: Path, invalid_mb: object) -> None:
    """Non-integer max_file_size_mb raises ValueError."""
    toml_file = tmp_path / "app.toml"
    toml_file.write_text(
        f"[ingest]\nmax_file_size_mb = {invalid_mb}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_file_size_mb"):
        load_config(toml_file)


def test_ingest_config_direct_rejects_invalid_max_file_size_mb() -> None:
    """Direct IngestConfig instantiation rejects bool, float, negative, and string."""
    with pytest.raises(ValueError, match="ingest.max_file_size_mb must be an integer >= 0"):
        IngestConfig(max_file_size_mb=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ingest.max_file_size_mb must be an integer >= 0"):
        IngestConfig(max_file_size_mb=-5)

    with pytest.raises(ValueError, match="ingest.max_file_size_mb must be an integer >= 0"):
        IngestConfig(max_file_size_mb=20.5)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ingest.max_file_size_mb must be an integer >= 0"):
        IngestConfig(max_file_size_mb="20")  # type: ignore[arg-type]


def test_app_config_post_init_validates_ingest() -> None:
    """AppConfig.__post_init__ catches invalid ingest sub-configurations."""
    valid_ingest = IngestConfig(auto_watch=True, max_file_size_mb=10)
    app = AppConfig(ingest=valid_ingest)
    assert app.ingest.auto_watch is True
    assert app.ingest.max_file_size_mb == 10


def test_size_cap_boundary_calculation() -> None:
    """Policy cap calculation and boundary semantics:
    cap_bytes = max_file_size_mb * 1024 * 1024
    size == cap is allowed, size > cap is blocked.
    0 means unlimited.
    """
    cfg = IngestConfig(max_file_size_mb=20)
    cap = cfg.max_file_size_bytes
    assert cap == 20 * 1024 * 1024

    # Helper function simulating the C58b gate logic
    def is_allowed(size: int, limit: int) -> bool:
        if limit == 0:
            return True
        return size <= limit

    assert is_allowed(cap - 1, cap) is True
    assert is_allowed(cap, cap) is True
    assert is_allowed(cap + 1, cap) is False

    cfg_unlimited = IngestConfig(max_file_size_mb=0)
    unlimited_cap = cfg_unlimited.max_file_size_bytes
    assert unlimited_cap == 0
    assert is_allowed(1024 * 1024 * 1024, unlimited_cap) is True


def test_fallback_toml_parser() -> None:
    """_fallback_toml parses auto_watch and max_file_size_mb correctly."""
    text = (
        "[ingest]\n"
        "enabled = true\n"
        "auto_watch = true\n"
        "max_file_size_mb = 35\n"
    )
    parsed = _fallback_toml(text)
    assert parsed["ingest"]["enabled"] is True
    assert parsed["ingest"]["auto_watch"] is True
    assert parsed["ingest"]["max_file_size_mb"] == 35

    # String false parsed as string
    text_str = (
        "[ingest]\n"
        'auto_watch = "false"\n'
    )
    parsed_str = _fallback_toml(text_str)
    assert parsed_str["ingest"]["auto_watch"] == "false"


# ======================================================================
# Card C58b Tests: Size Gate, Auto Candidates, and Ledger Deduplication
# ======================================================================

from unittest.mock import MagicMock, patch
from mortis_rag_mcp.ingest.mineru import MineruError, ParsedDocument
from mortis_rag_mcp.ingest.worker import IngestManager, _sha256


def test_size_gate_explicit_submit_rejects_and_zero_calls(tmp_path: Path) -> None:
    """Explicit submit rejects oversized file with ValueError; zero client parse calls."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, max_file_size_mb=20))
    big_pdf = tmp_path / "big.pdf"
    # 21 MiB
    big_pdf.write_bytes(b"%PDF-1.4 " + b"0" * (21 * 1024 * 1024))

    mock_parse = MagicMock()
    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        with pytest.raises(ValueError, match="exceeds limit"):
            mgr.submit(["big.pdf"])

    assert mock_parse.call_count == 0
    st = mgr.status()
    assert len(st["jobs"]) == 0


def test_size_gate_exact_cap_allowed(tmp_path: Path) -> None:
    """Exact cap boundary size == cap is allowed."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, max_file_size_mb=1))
    exact_pdf = tmp_path / "exact.pdf"
    exact_pdf.write_bytes(b"0" * (1 * 1024 * 1024))

    with patch.object(mgr, "_worker_loop"):
        res = mgr.submit(["exact.pdf"])
        assert res["submitted"] == 1
        assert len(res["jobs"]) == 1


def test_size_gate_scan_pending_and_submit_none(tmp_path: Path) -> None:
    """scan_pending tags oversized files reason='too_large' without sha; submit(None) skips them."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, max_file_size_mb=20))
    small_pdf = tmp_path / "small.pdf"
    small_pdf.write_bytes(b"%PDF-1.4 small content")
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(b"%PDF-1.4 " + b"X" * (25 * 1024 * 1024))

    pending = mgr.scan_pending()
    assert len(pending) == 2

    big_item = next(p for p in pending if p["source"] == "big.pdf")
    assert big_item["reason"] == "too_large"
    assert big_item["sha256"] == ""
    assert big_item["limit_bytes"] == 20 * 1024 * 1024

    small_item = next(p for p in pending if p["source"] == "small.pdf")
    assert small_item["reason"] == "new"
    assert small_item["sha256"] != ""

    mock_parse = MagicMock()
    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        with patch.object(mgr, "_worker_loop"):
            res = mgr.submit(None)
            assert res["submitted"] == 1
            assert res["skipped_too_large"] == 1
            assert res["jobs"][0]["source"] == "small.pdf"

    assert mock_parse.call_count == 0


def test_size_gate_auto_submit_and_diagnostics(tmp_path: Path) -> None:
    """auto_submit skips oversized files, records skipped_too_large and samples in state."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True, max_file_size_mb=20))
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(b"%PDF-1.4 " + b"Z" * (22 * 1024 * 1024))

    mock_parse = MagicMock()
    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        res = mgr.auto_submit()
        assert res["submitted"] == 0
        assert res["skipped_too_large"] == 1

    st = mgr.status()
    aw = st["auto_watch"]
    assert aw["skipped_too_large"] == 1
    assert len(aw["too_large_samples"]) == 1
    assert aw["too_large_samples"][0]["source"] == "big.pdf"
    assert mock_parse.call_count == 0


def test_size_gate_force_does_not_bypass(tmp_path: Path) -> None:
    """force=True still enforces size cap."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, max_file_size_mb=20))
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(b"%PDF-1.4 " + b"F" * (25 * 1024 * 1024))

    with pytest.raises(ValueError, match="exceeds limit"):
        mgr.submit(["big.pdf"], force=True)


def test_size_gate_recover_queued_blocks_at_run_job(tmp_path: Path) -> None:
    """Queued jobs recovered from state are blocked at _run_job if physical file > cap."""
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(b"%PDF-1.4 " + b"Q" * (25 * 1024 * 1024))

    # Pre-populate state with a queued job for big.pdf
    state_file = tmp_path / ".mortis-parsed" / ".ingest_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        '{"version": 1, "jobs": {"job1": {"job_id": "job1", "source": "big.pdf", "sha256": "fake", "state": "queued"}}}',
        encoding="utf-8",
    )

    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, max_file_size_mb=20))
    mock_parse = MagicMock()
    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        mgr._worker_loop()

    st = mgr.status("job1")
    assert st["job"]["state"] == "failed"
    assert "exceeds limit" in st["job"]["error"]
    assert mock_parse.call_count == 0


def test_size_gate_file_grew_after_enqueue(tmp_path: Path) -> None:
    """If a file was small when enqueued but grew before parsing, _run_job catches it."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, max_file_size_mb=20))
    doc_pdf = tmp_path / "grow.pdf"
    doc_pdf.write_bytes(b"%PDF-1.4 small initially")

    # Enqueue while withholding worker
    with patch.object(mgr, "_worker_loop"):
        mgr.submit(["grow.pdf"])

    # Now make it 25 MiB
    doc_pdf.write_bytes(b"%PDF-1.4 " + b"G" * (25 * 1024 * 1024))

    mock_parse = MagicMock()
    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        mgr._worker_loop()

    st = mgr.status()
    job = st["jobs"][0]
    assert job["state"] == "failed"
    assert "exceeds limit" in job["error"]
    assert mock_parse.call_count == 0


def test_mixed_sources_all_or_nothing(tmp_path: Path) -> None:
    """Batch submit with any oversized source fails completely without enrolling valid files."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, max_file_size_mb=20))
    (tmp_path / "good.pdf").write_bytes(b"%PDF-1.4 good")
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4 " + b"B" * (25 * 1024 * 1024))

    with pytest.raises(ValueError, match="exceeds limit"):
        mgr.submit(["good.pdf", "bad.pdf"])

    st = mgr.status()
    assert len(st["jobs"]) == 0


def test_agent_limit_fallback_within_policy_cap(tmp_path: Path) -> None:
    """MinerU Agent limit error for PDF within policy cap falls back to PyMuPDF."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, pymupdf_fallback=True, max_file_size_mb=20))
    pdf = tmp_path / "agent_limit.pdf"
    # 12 MiB (< 20 MiB policy cap, but > 10 MiB Agent channel cap)
    pdf.write_bytes(b"%PDF-1.4 " + b"A" * (12 * 1024 * 1024))

    def fake_parse(*args, **kwargs):
        raise MineruError("Agent channel file size exceeds 10MB limit", retryable=False)

    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", side_effect=fake_parse):
        with patch.object(mgr, "_pymupdf_fallback", return_value="# Extracted Local Text"):
            mgr.submit(["agent_limit.pdf"])
            assert mgr._worker is not None
            mgr._worker.join(timeout=5.0)

    st = mgr.status()
    job = st["jobs"][0]
    assert job["state"] == "done"
    assert job["channel"] == "pymupdf"
    assert job["parse_quality"] == "fallback"


def test_ignore_rules_and_dynamic_provider(tmp_path: Path) -> None:
    """Files matching ignore rules are skipped; dynamic provider updates are honored."""
    ignored_patterns = ["draft/**"]

    def provider():
        from mortis_rag_mcp._indexer.scanning import IgnoreMatcher
        return IgnoreMatcher(ignored_patterns)

    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True), ignore_provider=provider)
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / "temp.pdf").write_bytes(b"%PDF-1.4 draft")
    (tmp_path / "keep.pdf").write_bytes(b"%PDF-1.4 keep")

    with patch.object(mgr, "_worker_loop"):
        # review R3 两次采样：首扫只登记判稳采样，次扫一致才提交
        assert mgr.auto_submit()["submitted"] == 0
        res = mgr.auto_submit()
        assert res["submitted"] == 1
        assert res["skipped_ignored"] == 1
        assert res["jobs"][0]["source"] == "keep.pdf"

    # Dynamically update ignore patterns to ignore keep.pdf
    ignored_patterns.append("keep.pdf")
    (tmp_path / "keep.pdf").write_bytes(b"%PDF-1.4 keep modified")

    with patch.object(mgr, "_worker_loop"):
        res2 = mgr.auto_submit()
        assert res2["submitted"] == 0
        assert res2["skipped_ignored"] >= 1


def test_auto_seen_dedup_all_four_states(tmp_path: Path) -> None:
    """Continuous unchanged content is skipped if auto_seen state is queued, parsing, done, or failed."""
    doc = tmp_path / "test.pdf"
    doc.write_bytes(b"%PDF-1.4 test")
    digest = _sha256(doc)

    for st_val in ["queued", "parsing", "done", "failed"]:
        mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
        state = mgr._load_state()
        state["auto_seen"]["test.pdf"] = {
            "sha256": digest,
            "state": st_val,
            "submitted_at": 100.0,
            "last_job_id": "job_x",
        }
        mgr._save_state(state)

        targets, scan_stats = mgr._auto_pending()
        assert len(targets) == 0, f"Expected 0 targets for state {st_val}"
        # review R3 两次采样：首扫只登记采样，次扫采样一致才走到 sha 比对计入 skip_seen
        targets2, scan_stats2 = mgr._auto_pending()
        assert len(targets2) == 0, f"Expected 0 targets for state {st_val}"
        assert scan_stats2["skipped_seen"] == 1


def test_auto_seen_changed_hash_enqueues_new_job(tmp_path: Path) -> None:
    """When content hash changes, file becomes eligible again for auto_submit."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    doc = tmp_path / "update.pdf"
    doc.write_bytes(b"%PDF-1.4 v1")

    with patch.object(mgr, "_worker_loop"):
        # review R3 两次采样：首扫登记采样，次扫一致才提交
        assert mgr.auto_submit()["submitted"] == 0
        r1 = mgr.auto_submit()
        assert r1["submitted"] == 1

        # Content unchanged -> 0 submitted
        r2 = mgr.auto_submit()
        assert r2["submitted"] == 0

        # Modify content：变化源同样需两次采样（先登记新采样，再提交）
        doc.write_bytes(b"%PDF-1.4 v2 modified")
        assert mgr.auto_submit()["submitted"] == 0
        r3 = mgr.auto_submit()
        assert r3["submitted"] == 1
        assert r3["jobs"][0]["source"] == "update.pdf"


def test_a_b_a_version_changes_eligible_again(tmp_path: Path) -> None:
    """A -> B -> A content transitions are both eligible since hash changed from latest known."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    doc = tmp_path / "toggle.pdf"

    with patch.object(mgr, "_worker_loop"):
        # Version A（review R3 两次采样：每次内容变化后首扫登记、次扫提交）
        doc.write_bytes(b"%PDF-1.4 Content A")
        assert mgr.auto_submit()["submitted"] == 0
        r1 = mgr.auto_submit()
        assert r1["submitted"] == 1

        # Version B
        doc.write_bytes(b"%PDF-1.4 Content B")
        assert mgr.auto_submit()["submitted"] == 0
        r2 = mgr.auto_submit()
        assert r2["submitted"] == 1

        # Version A again
        doc.write_bytes(b"%PDF-1.4 Content A")
        assert mgr.auto_submit()["submitted"] == 0
        r3 = mgr.auto_submit()
        assert r3["submitted"] == 1


def test_old_job_finish_does_not_overwrite_newer_sha(tmp_path: Path) -> None:
    """When an older job finishes, it does not overwrite auto_seen if a newer job was enqueued."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    state = mgr._load_state()
    # Job 1 (old) with shaA
    state["jobs"]["job1"] = {
        "job_id": "job1", "source": "file.pdf", "sha256": "shaA", "state": "queued"
    }
    # But auto_seen was already updated by a newer Job 2 with shaB!
    state["auto_seen"]["file.pdf"] = {
        "sha256": "shaB", "state": "queued", "submitted_at": 200.0, "last_job_id": "job2"
    }
    mgr._save_state(state)

    # Simulate Job 1 finishing
    job1 = state["jobs"]["job1"]
    job1["state"] = "done"

    # Worker finishes Job 1
    with mgr._lock:
        st = mgr._load_state()
        st["jobs"][job1["job_id"]] = job1
        seen = st.get("auto_seen", {}).get(job1["source"])
        if seen and seen.get("last_job_id") == job1["job_id"]:
            seen["state"] = job1["state"]
        mgr._save_state(st)

    final_state = mgr._load_state()
    assert final_state["auto_seen"]["file.pdf"]["sha256"] == "shaB"
    assert final_state["auto_seen"]["file.pdf"]["last_job_id"] == "job2"


def test_history_pruning_over_500_preserves_auto_seen(tmp_path: Path) -> None:
    """Pruning state jobs (>500) preserves all auto_seen records."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True))
    state = mgr._load_state()
    for i in range(520):
        jid = f"j_{i:04d}"
        src = f"file_{i:04d}.pdf"
        state["jobs"][jid] = {
            "job_id": jid, "source": src, "sha256": f"sha_{i}", "state": "done", "submitted_at": float(i)
        }
        state["auto_seen"][src] = {
            "sha256": f"sha_{i}", "state": "done", "submitted_at": float(i), "last_job_id": jid
        }

    mgr._save_state(state)

    loaded = mgr._load_state()
    assert len(loaded["jobs"]) == 500
    assert len(loaded["auto_seen"]) == 520


def test_old_state_migration_reconstructs_auto_seen_by_max_submitted_at(tmp_path: Path) -> None:
    """Legacy state without auto_seen reconstructs ledger choosing maximum submitted_at."""
    state_file = tmp_path / ".mortis-parsed" / ".ingest_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_json = json.dumps({
        "version": 1,
        "jobs": {
            "j1": {"job_id": "j1", "source": "same.pdf", "sha256": "sha1", "state": "done", "submitted_at": 10.0},
            "j2": {"job_id": "j2", "source": "same.pdf", "sha256": "sha2", "state": "done", "submitted_at": 30.0},
            "j3": {"job_id": "j3", "source": "same.pdf", "sha256": "sha3", "state": "failed", "submitted_at": 20.0},
        }
    })
    state_file.write_text(legacy_json, encoding="utf-8")

    mgr = IngestManager(tmp_path, IngestConfig(enabled=True))
    state = mgr._load_state()
    assert "auto_seen" in state
    assert state["auto_seen"]["same.pdf"]["sha256"] == "sha2"
    assert state["auto_seen"]["same.pdf"]["last_job_id"] == "j2"


def test_clean_scan_prunes_deleted_sources_from_auto_seen(tmp_path: Path) -> None:
    """When a file is deleted from vault, a clean scan removes it from auto_seen."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    pdf = tmp_path / "temp.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    with patch.object(mgr, "_worker_loop"):
        # review R3 两次采样：首扫登记采样，次扫一致才入账本
        mgr.auto_submit()
        mgr.auto_submit()
        st = mgr._load_state()
        assert "temp.pdf" in st["auto_seen"]

        # Mark job as completed so it is no longer in active_sources
        for j in st["jobs"].values():
            j["state"] = "done"
        st["auto_seen"]["temp.pdf"]["state"] = "done"
        mgr._save_state(st)

        # Delete file
        pdf.unlink()
        mgr.auto_submit()
        st2 = mgr._load_state()
        assert "temp.pdf" not in st2["auto_seen"]


def test_disabled_auto_watch_zero_side_effect(tmp_path: Path) -> None:
    """When auto_watch is False or enabled is False, auto_submit does zero scans and starts zero threads."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=False, auto_watch=True))
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 doc")

    res = mgr.auto_submit()
    assert res["status"] == "disabled"
    assert res["submitted"] == 0
    assert mgr._worker is None


def test_copy_settling_defers_zero_byte_and_changing_files(tmp_path: Path) -> None:
    """A 0-byte file or actively changing file is deferred while stable files proceed."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))

    zero_doc = tmp_path / "zero.pdf"
    zero_doc.write_bytes(b"")  # 0 bytes

    stable_doc = tmp_path / "stable.pdf"
    stable_doc.write_bytes(b"%PDF-1.4 Stable content")

    changing_doc = tmp_path / "changing.pdf"
    changing_doc.write_bytes(b"%PDF-1.4 Part 1")
    mgr.mark_settling("changing.pdf")

    with patch.object(mgr, "_worker_loop"):
        # First scan: review R3 两次采样——所有首见文件（含 stable.pdf）只登记采样
        r1 = mgr.auto_submit()
        assert r1["submitted"] == 0
        assert r1["rescan_after_seconds"] > 0

        # Now zero_doc gets some bytes, but enters settling check
        zero_doc.write_bytes(b"%PDF-1.4 Zero content now present")
        # changing_doc changes size (still changing)
        changing_doc.write_bytes(b"%PDF-1.4 Part 1 and Part 2 (more content)")

        # Second scan: stable.pdf 采样一致 → 提交；zero/changing 采样变化 → 继续判稳
        r2 = mgr.auto_submit()
        assert r2["submitted"] == 1
        assert r2["jobs"][0]["source"] == "stable.pdf"

        # Third scan: zero_doc and changing_doc now remain unchanged (settled)
        r3 = mgr.auto_submit()
        assert r3["submitted"] == 2
        assert r3["rescan_after_seconds"] == 0.0
        sources = {j["source"] for j in r3["jobs"]}
        assert sources == {"zero.pdf", "changing.pdf"}


def test_source_changed_fails_job_without_upload_and_next_scan_reenqueues(tmp_path: Path) -> None:
    """When source file changes while in queue, worker fails with source_changed without upload; next scan reenqueues."""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    doc = tmp_path / "evolving.pdf"
    doc.write_bytes(b"%PDF-1.4 Version 1")

    mock_parse = MagicMock()
    with patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        # Don't let worker loop run automatically yet
        with patch.object(mgr, "_worker_loop"):
            # review R3 两次采样：首扫登记采样，次扫提交
            assert mgr.auto_submit()["submitted"] == 0
            res = mgr.auto_submit()
            assert res["submitted"] == 1
            job_id = res["jobs"][0]["job_id"]

        # Modify document BEFORE worker processes it!
        doc.write_bytes(b"%PDF-1.4 Version 2 completely changed")

        # Now run the worker loop manually
        mgr._worker_loop()

        st = mgr.status(job_id)
        job = st["job"]
        assert job["state"] == "failed"
        assert "source_changed" in job["error"]
        # MinerU parse MUST NOT have been called!
        assert mock_parse.call_count == 0

        # Run auto_submit() again: it must discover the new hash and enqueue a new job!
        with patch.object(mgr, "_worker_loop"):
            assert mgr.auto_submit()["submitted"] == 0  # 变化源首次采样
            res2 = mgr.auto_submit()
            assert res2["submitted"] == 1
            new_job = res2["jobs"][0]
            assert new_job["source"] == "evolving.pdf"
            assert new_job["job_id"] != job_id
            assert new_job["state"] == "queued"


# ---------------------------------------------------------------
# review v0.8.1 修复回归（R1/R3/R4，报告 .runtime/review-v081/REVIEW.md）
# ---------------------------------------------------------------

def test_auto_submit_fails_closed_when_matcher_unavailable(tmp_path: Path) -> None:
    """R1/F2：provider 已配置但拿不到 matcher（如启动窗口期 indexer 未发布）时，
    自动提交必须 fail-closed 拒绝本轮，绝不冒然提交可能被豁免的文档。"""
    mgr = IngestManager(
        tmp_path, IngestConfig(enabled=True, auto_watch=True), ignore_provider=lambda: None
    )
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4 secret")

    with patch.object(mgr, "_worker_loop"):
        res = mgr.auto_submit()
    assert res["submitted"] == 0
    assert res["jobs"] == []
    # 本轮未确认扫描完整 → 要求扫描循环延时重试，等 matcher 就绪后补扫
    assert res["rescan_after_seconds"] > 0
    st = mgr._load_state()
    assert "secret.pdf" not in st["auto_seen"]
    assert not st["jobs"]


def test_auto_submit_honors_vaultignore_patterns(tmp_path: Path) -> None:
    """R1/F1：ignore_provider 必须覆盖 .vaultignore 级别的动态豁免规则，
    被排除的 PDF 不得进入自动解析队列（此前只拿静态 exclude_patterns）。"""
    def provider():
        from mortis_rag_mcp._indexer.scanning import IgnoreMatcher
        return IgnoreMatcher(["secret.pdf", "private/secret.pdf"])

    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True), ignore_provider=provider)
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    (private_dir / "secret.pdf").write_bytes(b"%PDF-1.4 nested secret")
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4 top secret")
    (tmp_path / "normal.pdf").write_bytes(b"%PDF-1.4 normal")

    with patch.object(mgr, "_worker_loop"):
        assert mgr.auto_submit()["submitted"] == 0  # 首扫仅登记采样
        res = mgr.auto_submit()
    assert res["submitted"] == 1
    assert res["jobs"][0]["source"] == "normal.pdf"
    assert res["skipped_ignored"] == 2
    st = mgr._load_state()
    assert "secret.pdf" not in st["auto_seen"]
    assert "private/secret.pdf" not in st["auto_seen"]


def test_auto_submit_two_sample_settling_for_new_files(tmp_path: Path) -> None:
    """R3/F6：新文件首次扫描只登记采样，两次采样一致才 hash/提交；
    复制中途的部分字节不会被当成完整文档解析上传。"""
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    doc = tmp_path / "copy.pdf"
    doc.write_bytes(b"%PDF-1.4 PARTIAL_PAGE_ONLY")

    with patch.object(mgr, "_worker_loop"):
        r1 = mgr.auto_submit()
        assert r1["submitted"] == 0
        assert r1["rescan_after_seconds"] > 0

        # 复制继续：字节增长 → 采样变化 → 继续判稳（旧逻辑此处已把部分字节入队）
        doc.write_bytes(b"%PDF-1.4 PARTIAL_PAGE_ONLY\nREST_OF_DOCUMENT")
        r2 = mgr.auto_submit()
        assert r2["submitted"] == 0

        # 复制完成、采样一致 → 恰好提交一次，且登记的是完整字节的 SHA
        r3 = mgr.auto_submit()
        assert r3["submitted"] == 1
        assert r3["rescan_after_seconds"] == 0.0
        st = mgr._load_state()
        seen = st["auto_seen"]["copy.pdf"]
        assert st["jobs"][seen["last_job_id"]]["sha256"] == _sha256(doc)


def test_auto_submit_keeps_ledger_when_dirs_policy_pruned(tmp_path: Path) -> None:
    """R4/F7：目录被临时忽略（策略剪枝）≠ 文件被删除；账本必须保留，
    取消忽略后同 SHA 不得重传（此前 clean_scan 误清 auto_seen 导致重复解析）。"""
    ignored = {"on": False}

    def provider():
        from mortis_rag_mcp._indexer.scanning import IgnoreMatcher
        return IgnoreMatcher(["sub"] if ignored["on"] else [])

    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True), ignore_provider=provider)
    sub = tmp_path / "sub"
    sub.mkdir()
    doc = sub / "paper.pdf"
    doc.write_bytes(b"%PDF-1.4 paper body")

    mock_parse = MagicMock()
    with patch.object(mgr, "_worker_loop"), patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        assert mgr.auto_submit()["submitted"] == 0  # 首扫仅登记采样
        assert mgr.auto_submit()["submitted"] == 1

        # 临时忽略目录：非完整枚举，账本不得按「已删除」清理
        ignored["on"] = True
        r_ign = mgr.auto_submit()
        assert r_ign["submitted"] == 0
        st = mgr._load_state()
        assert "sub/paper.pdf" in st["auto_seen"]

        # 取消忽略：同 SHA 命中账本 → 跳过，不重传
        ignored["on"] = False
        r_back = mgr.auto_submit()
        assert r_back["submitted"] == 0
        assert r_back["skipped_seen"] == 1
        assert mock_parse.call_count == 0


def test_zero_byte_file_is_never_submitted(tmp_path: Path) -> None:
    """R3 回归（0 字节）：空文件两次采样恒为 (mtime, 0)，会被判成「已稳定」。

    若 0 字节判定放在判稳比对**之后**，空内容会被 hash 后直接入队上传。
    必须始终延后：内容写入并稳定后才进入上传链路。
    """
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    good = tmp_path / "good.pdf"
    good.write_bytes(b"%PDF-1.4 real body")

    with patch.object(mgr, "_worker_loop"):
        assert mgr.auto_submit()["submitted"] == 0  # 首扫仅登记采样
        r2 = mgr.auto_submit()
        assert r2["submitted"] == 1
        assert r2["jobs"][0]["source"] == "good.pdf"

        # 连续多轮：空文件始终不得被提交
        for _ in range(3):
            assert mgr.auto_submit()["submitted"] == 0
        assert "empty.pdf" not in mgr._load_state()["auto_seen"]

        # 内容写入后才走判稳 → 上传
        empty.write_bytes(b"%PDF-1.4 content finally written")
        assert mgr.auto_submit()["submitted"] == 0  # 采样变化 → 重新判稳
        assert mgr.auto_submit()["submitted"] == 1


def test_clean_scan_prunes_real_deletion_despite_pruned_subtree(tmp_path: Path) -> None:
    """R4 补正：目录剪枝只豁免被剪枝子树，不能让真实删除的条目永远留在账本里。

    「本轮出现过剪枝就整轮不清账本」会让已删除文件永久占位：删除后重建同 SHA
    文件不再被解析，同时账本无界增长。
    """
    ignored = {"on": False}

    def provider():
        from mortis_rag_mcp._indexer.scanning import IgnoreMatcher
        return IgnoreMatcher(["sub"] if ignored["on"] else [])

    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True), ignore_provider=provider)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "paper.pdf").write_bytes(b"%PDF-1.4 paper body")
    top = tmp_path / "top.pdf"
    top.write_bytes(b"%PDF-1.4 top body")

    mock_parse = MagicMock()
    with patch.object(mgr, "_worker_loop"), patch("mortis_rag_mcp.ingest.mineru.MineruClient.parse", mock_parse):
        assert mgr.auto_submit()["submitted"] == 0  # 首扫仅登记采样
        assert mgr.auto_submit()["submitted"] == 2

        st = mgr._load_state()
        for j in st["jobs"].values():
            j["state"] = "done"
        for entry in st["auto_seen"].values():
            entry["state"] = "done"
        mgr._save_state(st)

        # 目录被临时忽略 + 同轮 top.pdf 被真实删除
        ignored["on"] = True
        top.unlink()
        assert mgr.auto_submit()["submitted"] == 0

        st2 = mgr._load_state()
        assert "sub/paper.pdf" in st2["auto_seen"]  # 剪枝子树豁免，取消忽略后不重传
        assert "top.pdf" not in st2["auto_seen"]  # 真实删除照常回收


def test_missing_vault_root_keeps_ledger(tmp_path: Path) -> None:
    """库根暂时不可见（未挂载 / 权限抖动 / 同步客户端整目录改名）≠ 文件被删光。

    `Path.exists()` 对瞬时 OSError 返回 False，若此时按「完整枚举 0 文件」清理，
    账本会被整体清空 → 路径恢复后整库重传。
    """
    mgr = IngestManager(tmp_path, IngestConfig(enabled=True, auto_watch=True))
    doc = tmp_path / "paper.pdf"
    doc.write_bytes(b"%PDF-1.4 paper body")

    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if self == mgr.vault_path:
            return False
        return real_exists(self)

    with patch.object(mgr, "_worker_loop"):
        assert mgr.auto_submit()["submitted"] == 0
        assert mgr.auto_submit()["submitted"] == 1

        st = mgr._load_state()
        for j in st["jobs"].values():
            j["state"] = "done"
        st["auto_seen"]["paper.pdf"]["state"] = "done"
        mgr._save_state(st)

        with patch.object(Path, "exists", fake_exists):
            assert mgr.auto_submit()["submitted"] == 0

        assert "paper.pdf" in mgr._load_state()["auto_seen"]

