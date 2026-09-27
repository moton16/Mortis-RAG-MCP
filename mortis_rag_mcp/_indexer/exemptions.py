from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any, TYPE_CHECKING

from .chunking import _INDEXABLE_TEXT_EXTS
from .scanning import IgnoreMatcher

if TYPE_CHECKING:
    from ..indexer import MarkdownIndexer


def iter_vault_text_files(owner: MarkdownIndexer) -> list[str]:
    """按 scandir 剪枝遍历收集库内全部可索引文本文件（.md / .txt）。"""
    if not owner.vault_path.exists():
        return []
    found: list[str] = []
    stack = [owner.vault_path]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                path = Path(entry.path)
                if is_dir:
                    if (
                        entry.name.startswith(".")
                        or owner._ignored_name(entry.name)
                        or entry.name == "node_modules"
                        or (
                            owner.config.cache.placement == "vault"
                            and owner.config.cache.subdir
                            and entry.name == owner.config.cache.subdir
                        )
                    ):
                        continue
                    stack.append(path)
                    continue
                if owner._ignored_name(entry.name):
                    continue
                suffix = path.suffix.lower()
                if suffix in _INDEXABLE_TEXT_EXTS:
                    found.append(owner._source(path))
    found.sort()
    return found


def get_exemptions(owner: MarkdownIndexer) -> dict[str, Any]:
    ignore_file_path = owner.vault_path / owner.config.ignore_file
    vaultignore_rules: list[str] = []
    if ignore_file_path.exists() and ignore_file_path.is_file():
        try:
            for line in ignore_file_path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    vaultignore_rules.append(s)
        except Exception:
            pass

    all_text_files: list[str] = []
    all_md_files: list[str] = []
    exempt_files: list[dict[str, str]] = []
    for source in iter_vault_text_files(owner):
        all_text_files.append(source)
        if source.lower().endswith((".md", ".markdown")):
            all_md_files.append(source)
        check_res = check_exemption(owner, source)
        if check_res["is_exempt"]:
            exempt_files.append({"source": source, "reason": check_res["reason"]})

    return {
        "vault_path": str(owner.vault_path.resolve()),
        "ignore_file": owner.config.ignore_file,
        "vaultignore_rules": vaultignore_rules,
        "config_exclude_patterns": list(owner.config.exclude_patterns),
        "exclude_tags": list(owner.config.exclude_tags),
        "exclude_frontmatter_keys": list(owner.config.exclude_frontmatter_keys),
        "total_md_files": len(all_md_files),
        "total_text_files": len(all_text_files),
        "indexed_files": len(owner._chunks),
        "exempt_files_count": len(exempt_files),
        "exempt_files_sample": [item["source"] for item in exempt_files[:50]],
    }


def _prune_ignored_sources(owner: MarkdownIndexer, matcher: IgnoreMatcher) -> list[str]:
    """按豁免规则即时剪除内存态（含 0.7.1 全部时序观测状态）。"""
    pruned: list[str] = []
    removed_ids: list[str] = []
    with owner._sync_lock:
        for source in list(owner._chunks.keys()):
            if not matcher.is_ignored(source, is_dir=False)[0]:
                continue
            for c in owner._chunks.pop(source, []):
                removed_ids.append(c.id)
            owner._signatures.pop(source, None)
            owner._stat_cache.pop(source, None)
            owner._stat_seen_ns.pop(source, None)
            owner._stat_confirmations.pop(source, None)
            if hasattr(owner, "fast_path_warnings"):
                owner.fast_path_warnings.pop(source, None)
            owner._fts_delete(source)
            pruned.append(source)
        if removed_ids:
            try:
                owner._vector_backend.delete_vectors(removed_ids)
                owner._disk_vectors.difference_update(removed_ids)
            except Exception:
                pass
        for failed_source in list(owner.failed_files.keys()):
            if matcher.is_ignored(failed_source, is_dir=False)[0]:
                owner.failed_files.pop(failed_source, None)
        if pruned or removed_ids:
            owner._save_cache()
    return pruned


def add_exemption_pattern(owner: MarkdownIndexer, pattern: str) -> dict[str, Any]:
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    ignore_file_path = owner.vault_path / owner.config.ignore_file
    lines: list[str] = []
    if ignore_file_path.exists():
        try:
            lines = ignore_file_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

    if pattern not in [l.strip() for l in lines]:
        lines.append(pattern)
        ignore_file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 1. 内存即时极速剪枝与资源联动清理 (F-02)
    matcher = IgnoreMatcher([pattern])
    pruned_sources = _prune_ignored_sources(owner, matcher)

    # 2. 启动异步平滑对账（若有未同步事件，不阻塞前台返回）
    threading.Thread(target=owner._run_sync_quietly, daemon=True, name="exempt-sync").start()

    return {
        "success": True,
        "action": "add_pattern",
        "pattern": pattern,
        "vaultignore_path": str(ignore_file_path.resolve()),
        "pruned_files_count": len(pruned_sources),
        "total_rules": len([l for l in lines if l.strip() and not l.strip().startswith("#")]),
        "sync": "background",
    }


def remove_exemption_pattern(owner: MarkdownIndexer, pattern: str) -> dict[str, Any]:
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    ignore_file_path = owner.vault_path / owner.config.ignore_file
    if not ignore_file_path.exists():
        return {
            "success": True,
            "action": "remove_pattern",
            "pattern": pattern,
            "removed": False,
            "remaining_rules": 0,
            "sync": "noop",
        }

    lines = ignore_file_path.read_text(encoding="utf-8").splitlines()
    new_lines = [l for l in lines if l.strip() != pattern]
    removed = len(new_lines) < len(lines)
    if removed:
        ignore_file_path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
        # I3：与重对账解耦，前台毫秒级返回；新可见文件由后台线程补进索引
        threading.Thread(target=owner._run_sync_quietly, daemon=True, name="exempt-sync").start()
        sync_state = "background"
    else:
        sync_state = "noop"

    return {
        "success": True,
        "action": "remove_pattern",
        "pattern": pattern,
        "removed": removed,
        "remaining_rules": len([l for l in new_lines if l.strip() and not l.strip().startswith("#")]),
        "sync": sync_state,
    }


def check_exemption(owner: MarkdownIndexer, source: str) -> dict[str, Any]:
    source_posix = source.replace("\\", "/").strip("/")
    matcher = owner._ignore_matcher()
    ignored, rule = matcher.is_ignored(source_posix, is_dir=False)
    if ignored:
        return {
            "source": source_posix,
            "is_exempt": True,
            "reason": f"matched ignore rule '{rule}'",
            "has_block_ignores": False,
            "indexed_chunks": 0,
        }

    target_path = owner.vault_path / source_posix
    if not target_path.exists() or not target_path.is_file():
        return {
            "source": source_posix,
            "is_exempt": False,
            "reason": "file not found on disk",
            "has_block_ignores": False,
            "indexed_chunks": len(owner._chunks.get(source_posix, [])),
        }

    try:
        raw = target_path.read_bytes()
        text = raw.decode("utf-8-sig")
        lines = text.splitlines()
        frontmatter_end, tags, properties = owner._frontmatter(lines)
        is_fm_exempt, fm_reason = owner._is_frontmatter_exempt(tags, properties)
        if is_fm_exempt:
            return {
                "source": source_posix,
                "is_exempt": True,
                "reason": fm_reason or "frontmatter",
                "has_block_ignores": False,
                "indexed_chunks": 0,
            }

        body = lines[frontmatter_end + 1:]
        _, has_block_ignores = owner._strip_ignored_blocks(body)
        return {
            "source": source_posix,
            "is_exempt": False,
            "reason": "none (actively indexed)",
            "has_block_ignores": has_block_ignores,
            "indexed_chunks": len(owner._chunks.get(source_posix, [])),
        }
    except Exception as exc:
        return {
            "source": source_posix,
            "is_exempt": False,
            "reason": f"error reading file: {exc}",
            "has_block_ignores": False,
            "indexed_chunks": 0,
        }


def set_file_exemption(
    owner: MarkdownIndexer,
    source: str,
    exempt: bool = True,
    method: str = "frontmatter",
) -> dict[str, Any]:
    source_posix = source.replace("\\", "/").strip("/")
    if method == "ignore_file":
        if exempt:
            return add_exemption_pattern(owner, source_posix)
        else:
            return remove_exemption_pattern(owner, source_posix)

    path = owner._safe_path(source_posix)
    if not path.exists():
        raise ValueError(f"file not found: {source_posix}")

    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    if exempt:
        if len(lines) >= 2 and lines[0].strip() == "---":
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
            if end > 0:
                fm_lines = lines[1:end]
                rag_found = False
                new_fm_lines = []
                for line in fm_lines:
                    if line.strip().lower().startswith("rag:"):
                        new_fm_lines.append("rag: false")
                        rag_found = True
                    else:
                        new_fm_lines.append(line)
                if not rag_found:
                    new_fm_lines.insert(0, "rag: false")
                new_lines = ["---"] + new_fm_lines + ["---"] + lines[end + 1:]
            else:
                new_lines = ["---", "rag: false", "---", ""] + lines
        else:
            new_lines = ["---", "rag: false", "---", ""] + lines
    else:
        if len(lines) >= 2 and lines[0].strip() == "---":
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
            if end > 0:
                fm_lines = lines[1:end]
                new_fm_lines = []
                for line in fm_lines:
                    stripped = line.strip().lower()
                    if any(stripped.startswith(k + ":") for k in ["rag", "rag_exclude", "rag_ignore", "no_rag"]):
                        continue
                    new_fm_lines.append(line)
                if any(l.strip() for l in new_fm_lines):
                    new_lines = ["---"] + new_fm_lines + ["---"] + lines[end + 1:]
                else:
                    rest = lines[end + 1:]
                    while rest and not rest[0].strip():
                        rest = rest[1:]
                    new_lines = rest
            else:
                new_lines = lines
        else:
            new_lines = lines

    path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    if exempt:
        _prune_ignored_sources(owner, IgnoreMatcher([source_posix]))
    threading.Thread(target=owner._run_sync_quietly, daemon=True, name="exempt-sync").start()
    return {
        "success": True,
        "action": "exempt_file" if exempt else "unexempt_file",
        "source": source_posix,
        "method": "frontmatter",
        "is_exempt": exempt,
        "sync": "background",
    }
