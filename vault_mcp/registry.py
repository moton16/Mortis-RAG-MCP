"""User-level vault registry.

0.3.0 起 vault-mcp 不再绑定任何硬编码路径：用户（或 AI 助手）通过 kb_init 把
任意文件夹注册为知识库，注册表持久化在 ~/.vault_mcp/vaults.toml，跨重启、
跨设备可用。分发给他人时，对方只需要跑一次 kb_init 即可锚定自己的文件夹。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import read_toml_file

REGISTRY_VERSION = 1


def user_config_dir() -> Path:
    return Path.home() / ".vault_mcp"


def registry_path() -> Path:
    # VAULT_MCP_REGISTRY lets tests (and multi-instance setups) redirect the
    # registry without touching the user's real one.
    override = os.getenv("VAULT_MCP_REGISTRY", "").strip()
    if override:
        return Path(override).expanduser()
    return user_config_dir() / "vaults.toml"


def user_config_path() -> Path:
    return user_config_dir() / "config.toml"


def normalize_vault_key(path: str | os.PathLike[str]) -> str:
    """Stable registry identity: same normalization as indexer._cache_key
    (resolve symlinks, then normcase so Windows casing can't duplicate)."""
    resolved = os.path.realpath(os.path.normcase(str(Path(path).expanduser())))
    return resolved


@dataclass(slots=True)
class VaultEntry:
    path: str            # resolved absolute path at registration time
    name: str            # display name, defaults to the folder name
    registered_at: float  # time.time()


class VaultRegistry:
    """Persistent list of registered vault folders (atomic TOML file)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else registry_path()
        # Session-only entries used when the registry file can't be written
        # (e.g. read-only home dir): vault keeps working until the process exits.
        self._memory_entries: list[VaultEntry] = []

    # ------------------------------------------------------------------ io

    def load(self) -> list[VaultEntry]:
        """Read the registry; a missing or corrupt file yields an empty list.
        Session-only (memory) entries are appended at the end."""
        file_entries: list[VaultEntry] = []
        if self.path.is_file():
            try:
                data: dict[str, Any] = read_toml_file(self.path)
                for raw in data.get("vaults", []):
                    if not isinstance(raw, dict):
                        continue
                    path_value = str(raw.get("path", "")).strip()
                    if not path_value:
                        continue
                    try:
                        registered_at = float(raw.get("registered_at", 0.0))
                    except (TypeError, ValueError):
                        registered_at = 0.0
                    file_entries.append(
                        VaultEntry(
                            path=path_value,
                            name=str(raw.get("name", "")) or Path(path_value).name,
                            registered_at=registered_at,
                        )
                    )
            except Exception:
                file_entries = []
        known = {normalize_vault_key(entry.path) for entry in file_entries}
        for entry in self._memory_entries:
            if normalize_vault_key(entry.path) not in known:
                file_entries.append(entry)
        return file_entries

    def save(self, entries: list[VaultEntry]) -> None:
        """Atomic write (tmp + replace) so a killed process can't corrupt it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"version = {REGISTRY_VERSION}", ""]
        for entry in entries:
            lines.append("[[vaults]]")
            # Literal strings keep Windows backslashes readable and safe.
            lines.append(f"path = '{entry.path}'")
            lines.append(f'name = "{entry.name}"')
            lines.append(f"registered_at = {entry.registered_at!r}")
            lines.append("")
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ mutation

    def add(self, path: str | os.PathLike[str], name: str | None = None, *, persist: bool = True) -> VaultEntry:
        resolved = str(Path(path).expanduser().resolve())
        entries = self.load()
        for entry in entries:
            if normalize_vault_key(entry.path) == normalize_vault_key(resolved):
                raise ValueError(f"vault already registered as '{entry.name}': {entry.path}")
        entry = VaultEntry(
            path=resolved,
            name=name or Path(resolved).name,
            registered_at=time.time(),
        )
        entries.append(entry)
        if persist:
            self.save(entries)
        else:
            self._memory_entries.append(entry)
        return entry

    def remove(self, path: str | os.PathLike[str]) -> VaultEntry:
        target = normalize_vault_key(path)
        entries = self.load()
        for index, entry in enumerate(entries):
            if normalize_vault_key(entry.path) == target:
                entries.pop(index)
                self.save(entries)
                return entry
        raise ValueError(f"vault not registered: {path}")

    def get(self, path: str | os.PathLike[str]) -> VaultEntry | None:
        target = normalize_vault_key(path)
        for entry in self.load():
            if normalize_vault_key(entry.path) == target:
                return entry
        return None
