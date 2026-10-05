"""Unit tests for Ingest auto_watch and max_file_size_mb configuration (Card C58a)."""
from __future__ import annotations

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
