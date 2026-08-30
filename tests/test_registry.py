from __future__ import annotations

import time

from vault_mcp.registry import VaultEntry, VaultRegistry, normalize_vault_key


def _entry(path, name=None, at=None):
    return VaultEntry(path=str(path), name=name or "x", registered_at=at if at is not None else time.time())


def test_registry_roundtrip_keeps_windows_backslashes(tmp_path):
    reg = VaultRegistry(tmp_path / "vaults.toml")
    reg.add("C:\\Users\\somebody\\Notes\\Work", "Work")
    reg.add("D:/knowledge/research")

    loaded = VaultRegistry(tmp_path / "vaults.toml").load()
    paths = [entry.path for entry in loaded]
    assert paths == ["C:\\Users\\somebody\\Notes\\Work", "D:\\knowledge\\research"]
    assert loaded[0].name == "Work"


def test_registry_rejects_case_insensitive_duplicates(tmp_path):
    reg = VaultRegistry(tmp_path / "vaults.toml")
    vault = tmp_path / "Vault"
    vault.mkdir()
    reg.add(vault)
    # Windows normcase folds any casing/spelling onto one registry key.
    other_spelling = str(vault).swapcase()
    if normalize_vault_key(other_spelling) == normalize_vault_key(str(vault)):
        try:
            reg.add(other_spelling)
            raised = False
        except ValueError:
            raised = True
        assert raised
    else:
        reg.add(other_spelling)  # non-Windows: distinct keys are allowed


def test_registry_add_detects_existing_entry(tmp_path):
    reg = VaultRegistry(tmp_path / "vaults.toml")
    vault = tmp_path / "notes"
    vault.mkdir()
    reg.add(vault)
    try:
        reg.add(vault)
        raised = False
    except ValueError as exc:
        raised = True
        assert "already registered" in str(exc)
    assert raised


def test_registry_corrupt_file_loads_empty(tmp_path):
    reg_path = tmp_path / "vaults.toml"
    reg_path.write_text("not [valid toml", encoding="utf-8")
    assert VaultRegistry(reg_path).load() == []


def test_registry_remove_unknown_raises(tmp_path):
    reg = VaultRegistry(tmp_path / "vaults.toml")
    try:
        reg.remove(tmp_path / "ghost")
        raised = False
    except ValueError as exc:
        raised = True
        assert "not registered" in str(exc)
    assert raised


def test_registry_get_uses_normcase(tmp_path):
    reg = VaultRegistry(tmp_path / "vaults.toml")
    vault = tmp_path / "MixedCase"
    vault.mkdir()
    entry = reg.add(vault)
    other_spelling = str(vault).swapcase()
    assert reg.get(other_spelling) is not None
    assert reg.get(entry.path).path == entry.path


def test_registry_memory_entries_survive_without_file(tmp_path):
    reg = VaultRegistry(tmp_path / "never_written.toml")
    reg.add(tmp_path / "session", persist=False)
    assert not reg.path.exists()
    assert len(reg.load()) == 1


def test_registry_save_uses_atomic_replace(tmp_path):
    reg = VaultRegistry(tmp_path / "vaults.toml")
    reg.save([_entry(tmp_path / "a")])
    # No leftover tmp files from the write.
    assert list(tmp_path.glob("*.tmp")) == []
    assert reg.load()[0].path == str(tmp_path / "a")
