"""User-level vault registry.

0.3.0 起 vault-mcp 不再绑定任何硬编码路径：用户（或 AI 助手）通过 kb_init 把
任意文件夹注册为知识库，注册表持久化在 ~/.vault_mcp/vaults.toml，跨重启、
跨设备可用。分发给他人时，对方只需要跑一次 kb_init 即可锚定自己的文件夹。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import read_toml_file

_thread_local = threading.local()


@contextlib.contextmanager
def _process_file_lock(lock_path: Path):
    """Advisory cross-process file lock using msvcrt (Windows) or fcntl (POSIX).
    Supports reentrancy within the same thread.
    """
    depth = getattr(_thread_local, "lock_depth", 0)
    if depth > 0:
        _thread_local.lock_depth = depth + 1
        try:
            yield
        finally:
            _thread_local.lock_depth = depth
        return

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_path, "a+b")
    except OSError:
        yield
        return

    locked = False
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                pass

        _thread_local.lock_depth = 1
        try:
            yield
        finally:
            _thread_local.lock_depth = 0
            if locked:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
    finally:
        try:
            f.close()
        except OSError:
            pass

# v3（0.6.0）：vaults 条目新增 solo 布尔字段（老文件没有该键 → False）。
REGISTRY_VERSION = 3


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
    weight: float = 1.0  # 跨库检索时该库分数的放大系数：>1 表示更偏好这个库
    solo: bool = False   # solo 库不参与跨库 fan-out，仅在显式指定 vault_path 时被检索


class VaultRegistry:
    """Persistent list of registered vault folders (atomic TOML file)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else registry_path()
        # Session-only entries used when the registry file can't be written
        # (e.g. read-only home dir): vault keeps working until the process exits.
        self._memory_entries: list[VaultEntry] = []
        # add/remove/set_weight 都是 load→改→save 的读改写序列，而后台启动线程
        # 会并发 load()。锁只保证进程内一致（跨进程文件锁不在本轮范围）。
        self._lock = threading.RLock()
        self._lock_path = self.path.with_suffix(".lock")

    # ------------------------------------------------------------------ io

    def load(self) -> list[VaultEntry]:
        """Read the registry; a missing or corrupt file yields an empty list.
        Session-only (memory) entries are appended at the end."""
        file_entries: list[VaultEntry] = []
        if self.path.is_file():
            data: dict[str, Any] | None = None
            for attempt in range(4):
                try:
                    data = read_toml_file(self.path)
                    break
                except OSError:
                    if attempt == 3:
                        break
                    time.sleep(0.015 * (attempt + 1))
                except Exception:
                    break
            if data is not None and isinstance(data, dict):
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
                    # weight 是 0.4.2 新增字段：老 toml 里没有，回退默认 1.0（不放大）。
                    try:
                        weight = float(raw.get("weight", 1.0))
                    except (TypeError, ValueError):
                        weight = 1.0
                    # solo 是 v3 新增字段：老 toml 里没有 → False；脏值（字符串）
                    # 按 server 层同款布尔容错解析，解析不出一律 False。
                    solo_raw = raw.get("solo", False)
                    if isinstance(solo_raw, str):
                        solo = solo_raw.strip().lower() in {"1", "true", "yes", "on"}
                    else:
                        solo = bool(solo_raw)
                    file_entries.append(
                        VaultEntry(
                            path=path_value,
                            name=str(raw.get("name", "")) or Path(path_value).name,
                            registered_at=registered_at,
                            weight=weight,
                            solo=solo,
                        )
                    )
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
            # 字符串值一律用 json.dumps 序列化（TOML basic string 兼容，能转义
            # 引号/反斜杠/换行）。此前 name 用 f-string 裸拼：name 来自 LLM 可控
            # 的 kb_init 参数，一个 `"` 或换行就让整个 vaults.toml 不可解析，
            # load() 把它当成空表，下一次 save 静默覆盖掉所有其它库的注册。
            # （Windows 反斜杠在 json 转义后仍可被 tomllib 正确解析。）
            lines.append(f"path = {json.dumps(entry.path, ensure_ascii=False)}")
            lines.append(f"name = {json.dumps(entry.name, ensure_ascii=False)}")
            lines.append(f"registered_at = {entry.registered_at!r}")
            lines.append(f"weight = {entry.weight}")
            # TOML 布尔字面量必须小写。
            lines.append(f"solo = {str(entry.solo).lower()}")
            lines.append("")
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text("\n".join(lines), encoding="utf-8")
            tmp.replace(self.path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ mutation

    def add(
        self,
        path: str | os.PathLike[str],
        name: str | None = None,
        *,
        persist: bool = True,
        weight: float = 1.0,
        solo: bool = False,
    ) -> VaultEntry:
        with self._lock, _process_file_lock(self._lock_path):
            resolved = str(Path(path).expanduser().resolve())
            entries = self.load()
            for entry in entries:
                if normalize_vault_key(entry.path) == normalize_vault_key(resolved):
                    raise ValueError(f"vault already registered as '{entry.name}': {entry.path}")
            entry = VaultEntry(
                path=resolved,
                name=name or Path(resolved).name,
                registered_at=time.time(),
                weight=float(weight),
                solo=bool(solo),
            )
            entries.append(entry)
            if persist:
                self.save(entries)
            else:
                self._memory_entries.append(entry)
            return entry

    def remove(self, path: str | os.PathLike[str]) -> VaultEntry:
        with self._lock, _process_file_lock(self._lock_path):
            target = normalize_vault_key(path)
            entries = self.load()
            for index, entry in enumerate(entries):
                if normalize_vault_key(entry.path) == target:
                    entries.pop(index)
                    self.save(entries)
                    return entry
            raise ValueError(f"vault not registered: {path}")

    def set_weight(self, path: str | os.PathLike[str], weight: float) -> VaultEntry:
        """调整单个库的检索权重（跨库 fan-out 时该库分数的放大系数）。"""
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            raise ValueError(f"invalid weight: {weight}")
        if not 0 < weight <= 100:
            raise ValueError(f"weight must be in (0, 100]: {weight}")
        with self._lock, _process_file_lock(self._lock_path):
            target = normalize_vault_key(path)
            entries = self.load()
            for entry in entries:
                if normalize_vault_key(entry.path) == target:
                    entry.weight = weight
                    self.save(entries)
                    return entry
            raise ValueError(f"vault not registered: {path}")

    def set_solo(self, path: str | os.PathLike[str], solo: bool) -> VaultEntry:
        """设置/取消单个库的 solo 标志（不参与跨库 fan-out，仅显式检索）。

        只改注册表布尔位：索引、缓存、watcher 全部不动，调用方无需重建任何东西。
        """
        with self._lock, _process_file_lock(self._lock_path):
            target = normalize_vault_key(path)
            entries = self.load()
            for entry in entries:
                if normalize_vault_key(entry.path) == target:
                    entry.solo = bool(solo)
                    self.save(entries)
                    return entry
            raise ValueError(f"vault not registered: {path}")

    def get(self, path: str | os.PathLike[str]) -> VaultEntry | None:
        target = normalize_vault_key(path)
        for entry in self.load():
            if normalize_vault_key(entry.path) == target:
                return entry
        return None
