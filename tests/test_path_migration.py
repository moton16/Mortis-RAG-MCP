from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from mortis_rag_mcp.config import (
    load_config,
    resolve_api_key,
    resolve_config_path,
    resolve_default_cache_dir,
)
from mortis_rag_mcp.registry import registry_path, user_config_dir


def test_user_config_dir_new_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    new_dir = tmp_path / ".mortis_rag_mcp"
    new_dir.mkdir()
    res = user_config_dir()
    assert res == new_dir
    assert res.exists()


def test_user_config_dir_atomic_migration_from_old(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_dir = tmp_path / ".vault_mcp"
    old_dir.mkdir()
    (old_dir / "vaults.toml").write_text("test_data = 1\n", encoding="utf-8")

    res = user_config_dir()
    new_dir = tmp_path / ".mortis_rag_mcp"
    assert res == new_dir
    assert new_dir.exists()
    assert not old_dir.exists()
    assert (new_dir / "vaults.toml").read_text(encoding="utf-8") == "test_data = 1\n"


def test_user_config_dir_rename_oserror_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_dir = tmp_path / ".vault_mcp"
    old_dir.mkdir()

    with patch.object(Path, "rename", side_effect=OSError("Permission denied or cross-device")):
        res = user_config_dir()
        assert res == old_dir
        assert old_dir.exists()


def test_user_config_dir_both_exist_keeps_new_without_touching_old(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    new_dir = tmp_path / ".mortis_rag_mcp"
    old_dir = tmp_path / ".vault_mcp"
    new_dir.mkdir()
    old_dir.mkdir()
    (new_dir / "marker.txt").write_text("new", encoding="utf-8")
    (old_dir / "marker.txt").write_text("old", encoding="utf-8")

    res = user_config_dir()
    assert res == new_dir
    # Old must remain untouched (never deleted)
    assert old_dir.exists()
    assert (old_dir / "marker.txt").read_text(encoding="utf-8") == "old"


def test_resolve_api_key_and_registry_env_priority(tmp_path, monkeypatch):
    # 1. Explicit argument overrides everything
    monkeypatch.setenv("MORTIS_RAG_API_KEY", "key_new")
    monkeypatch.setenv("VAULT_MCP_API_KEY", "key_old")
    assert resolve_api_key("explicit_key") == "explicit_key"

    # 2. MORTIS_RAG_API_KEY > VAULT_MCP_API_KEY
    assert resolve_api_key() == "key_new"

    # 3. Fallback to VAULT_MCP_API_KEY
    monkeypatch.delenv("MORTIS_RAG_API_KEY", raising=False)
    assert resolve_api_key() == "key_old"

    # 4. Fallback to empty string
    monkeypatch.delenv("VAULT_MCP_API_KEY", raising=False)
    assert resolve_api_key() == ""

    # Registry path env priority: MORTIS_RAG_REGISTRY > VAULT_MCP_REGISTRY
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    reg_new = tmp_path / "custom_new_vaults.toml"
    reg_old = tmp_path / "custom_old_vaults.toml"
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", str(reg_new))
    monkeypatch.setenv("VAULT_MCP_REGISTRY", str(reg_old))
    assert registry_path() == reg_new

    monkeypatch.delenv("MORTIS_RAG_REGISTRY", raising=False)
    assert registry_path() == reg_old

    monkeypatch.delenv("VAULT_MCP_REGISTRY", raising=False)
    assert registry_path() == (tmp_path / ".mortis_rag_mcp" / "vaults.toml")


def test_resolve_config_path_and_cache_dir_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # 1. Cache dir: old exists -> atomic migration to new
    old_cache = tmp_path / ".vault_mcp_cache"
    old_cache.mkdir()
    res_cache = resolve_default_cache_dir()
    new_cache = tmp_path / ".mortis_rag_mcp_cache"
    assert res_cache == str(new_cache)
    assert new_cache.exists()
    assert not old_cache.exists()

    # Cache dir: rename error fallback
    old_cache2 = tmp_path / ".vault_mcp_cache"
    old_cache2.mkdir()
    # remove new_cache so migration is attempted
    new_cache.rmdir()
    with patch.object(Path, "rename", side_effect=OSError("locked")):
        assert resolve_default_cache_dir() == str(old_cache2)

    # 2. Config path resolution chain:
    # Explicit file
    explicit_cfg = tmp_path / "custom.toml"
    explicit_cfg.write_text("", encoding="utf-8")
    assert resolve_config_path(str(explicit_cfg)) == explicit_cfg

    # MORTIS_RAG_CONFIG > VAULT_MCP_CONFIG
    cfg_new = tmp_path / "env_new.toml"
    cfg_new.write_text("", encoding="utf-8")
    cfg_old = tmp_path / "env_old.toml"
    cfg_old.write_text("", encoding="utf-8")
    monkeypatch.setenv("MORTIS_RAG_CONFIG", str(cfg_new))
    monkeypatch.setenv("VAULT_MCP_CONFIG", str(cfg_old))
    assert resolve_config_path() == cfg_new

    monkeypatch.delenv("MORTIS_RAG_CONFIG", raising=False)
    assert resolve_config_path() == cfg_old

    monkeypatch.delenv("VAULT_MCP_CONFIG", raising=False)

    # Fallback to ~/.mortis_rag_mcp/config.toml
    home_new_cfg = tmp_path / ".mortis_rag_mcp" / "config.toml"
    home_new_cfg.parent.mkdir(parents=True, exist_ok=True)
    home_new_cfg.write_text("", encoding="utf-8")
    assert resolve_config_path() == home_new_cfg

    # If home new config missing, fallback to ~/.vault_mcp/config.toml
    home_new_cfg.unlink()
    home_old_cfg = tmp_path / ".vault_mcp" / "config.toml"
    home_old_cfg.parent.mkdir(parents=True, exist_ok=True)
    home_old_cfg.write_text("", encoding="utf-8")
    assert resolve_config_path() == home_old_cfg

    # If both missing, returns None
    home_old_cfg.unlink()
    assert resolve_config_path() is None


def test_user_config_dir_rename_race_fallback_to_new_if_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_dir = tmp_path / ".vault_mcp"
    new_dir = tmp_path / ".mortis_rag_mcp"
    old_dir.mkdir()

    # Simulate race: rename raises OSError, but concurrent process has already created new_dir
    def fake_rename(self, target):
        new_dir.mkdir(exist_ok=True)
        raise OSError("WinError 183: Cannot create a file when that file already exists")

    with patch.object(Path, "rename", fake_rename):
        res = user_config_dir()
        assert res == new_dir
        assert new_dir.exists()


def test_resolve_default_cache_dir_rename_race_fallback_to_new_if_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_cache = tmp_path / ".vault_mcp_cache"
    new_cache = tmp_path / ".mortis_rag_mcp_cache"
    old_cache.mkdir()

    def fake_rename(self, target):
        new_cache.mkdir(exist_ok=True)
        raise OSError("WinError 183: Cannot create a file when that file already exists")

    with patch.object(Path, "rename", fake_rename):
        res = resolve_default_cache_dir()
        assert res == str(new_cache)
        assert new_cache.exists()


def test_registry_path_migrates_old_file_when_new_dir_already_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_dir = tmp_path / ".vault_mcp"
    new_dir = tmp_path / ".mortis_rag_mcp"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "vaults.toml").write_text("version = 4\n[[vaults]]\npath = 'test'\nname = 'v1'\n", encoding="utf-8")

    # When new_dir already exists without vaults.toml, registry_path() must migrate old vaults.toml
    target = registry_path()
    assert target == (new_dir / "vaults.toml")
    assert target.exists()
    assert not (old_dir / "vaults.toml").exists()
    assert "name = 'v1'" in target.read_text(encoding="utf-8")


def test_registry_path_rename_error_fallback_to_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old_dir = tmp_path / ".vault_mcp"
    new_dir = tmp_path / ".mortis_rag_mcp"
    old_dir.mkdir()
    new_dir.mkdir()
    old_file = old_dir / "vaults.toml"
    old_file.write_text("version = 4\n", encoding="utf-8")

    with patch.object(Path, "rename", side_effect=OSError("locked")):
        target = registry_path()
        assert target == old_file
        assert target.exists()


