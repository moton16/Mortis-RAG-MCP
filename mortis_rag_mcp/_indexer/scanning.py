"""目录扫描 / IgnoreMatcher / Fast-Stat 刻度探测（v0.8.0 自 indexer.py 逐字提取）。

模块边界：
- 只依赖标准库，严禁反向导入 ``mortis_rag_mcp.indexer`` Facade；
- ``_probe_mtime_tick_ns`` 的**调用接缝**保留在 Facade（indexer.py 的包装
  函数在调用时解析模块全局，测试补丁 ``indexer._probe_mtime_tick_ns`` 仍然
  生效——D2 决策；回归测试 ``tests/test_facade_seam.py`` 锁定）；
- Fast-Stat 判据族（``_effective_margin_ns`` / ``_fast_path_is_trustworthy``
  等）留守 Facade：它们操作实例状态（``_stat_seen_ns`` 等）且是
  ``tests/test_indexer.py`` 的实例级锚点。
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import math
import os
from pathlib import Path
from typing import Iterable

# ---- Fast-Stat 可信判据常量（原 indexer.py 常量区逐字迁移，Facade re-export 保持导入面） ----

# Fast-Stat 可信判据的安全余量**下限**（纳秒）。文件时间戳并非真纳秒：Windows
# 系统定时器最坏 15.6ms 才更新一次（实测 NTFS 刻度约 3ms，FAT32 达 2s），且
# 时间戳可能因取整而超前于进程时钟（Windows CI 实测约 3% 概率，见 CI 诊断
# racycount=1/30）。
#
# 注意这里是下限而非实际取值：实际余量由 _effective_margin_ns() 从**库所在
# 文件系统探测到的真实刻度**推导（margin = max(下限, 2 × 刻度)）。固定 50ms
# 曾在粗粒度文件系统上失守——当刻度 > 余量时，「登记之后发生的写入必然推动
# mtime 前进」的归纳前提不成立（论证与反例见 _fast_path_is_trustworthy）。
_MTIME_TRUST_MARGIN_NS = 50_000_000

# 时间戳刻度探测（用于推导余量）。刻度估计取库内文件 mtime_ns 的最大公约数：
# 真实刻度 T 的任何正整数倍都必然整除所有时间戳，故 gcd 只会**高估**刻度、
# 绝不会低估；高估的方向是把余量调大（更保守、只多读盘），低估才会漏检，而
# gcd 在数学上不可能低估。见 _probe_mtime_tick_ns()。
_MTIME_TICK_PROBE_MIN_SAMPLES = 2   # 至少两个样本才有差值可比
_MTIME_TICK_PROBE_MAX_SAMPLES = 64  # 单进程内最多保留的探测样本
_MTIME_TICK_COARSE_NS = 50_000_000  # 刻度粗于此时直接禁用快速路径（fail-closed）

# mtime 停在未来的条目（网络盘/共享盘时钟超前、备份还原、手工 touch）的复核
# 上限：跨墙钟复核这么多次后签名仍未变，就接受它稳定并恢复零读盘，而不是因
# 外部时钟偏移永久惩罚它（每轮重读+重哈希）。见 _fast_path_is_trustworthy。
_FUTURE_MTIME_RECHECK_LIMIT = 2
# 「跨墙钟」的最小间隔：两次内容级验证的进程时钟读数至少差这么多才算两次独立
# 观测，否则同一毫秒内的连续 sync 会把复核计数刷满。
_FUTURE_MTIME_MIN_OBSERVATION_GAP_NS = 1_000_000


class IgnoreMatcher:
    """Gitignore-style pattern matcher for vault exclusion rules."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self.rules: list[tuple[bool, str, bool]] = []
        for raw in patterns:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            is_neg = False
            if line.startswith("!"):
                is_neg = True
                line = line[1:].strip()
            if not line:
                continue
            is_dir_only = line.endswith("/")
            if is_dir_only:
                line = line.rstrip("/")
            self.rules.append((is_neg, line, is_dir_only))

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> tuple[bool, str | None]:
        rel_posix = rel_path.replace("\\", "/").strip("/")
        if not rel_posix:
            return False, None

        parts = rel_posix.split("/")
        filename = parts[-1]

        matched = False
        matched_rule: str | None = None

        for is_neg, pat, is_dir_only in self.rules:
            hit = False
            norm_pat = pat.replace("\\", "/").rstrip("/")
            pat_lower = norm_pat.lower()
            rel_lower = rel_posix.lower()
            fn_lower = filename.lower()

            if "/" not in norm_pat:
                if fnmatch.fnmatchcase(fn_lower, pat_lower):
                    if not is_dir_only or is_dir:
                        hit = True
                if not hit:
                    for part in parts[:-1]:
                        if fnmatch.fnmatchcase(part.lower(), pat_lower):
                            hit = True
                            break
            else:
                clean_pat = norm_pat.lstrip("/")
                clean_pat_lower = clean_pat.lower()
                if fnmatch.fnmatchcase(rel_lower, clean_pat_lower):
                    hit = True
                elif is_dir_only and (rel_lower == clean_pat_lower or rel_lower.startswith(clean_pat_lower + "/")):
                    hit = True
                elif not is_dir_only and rel_lower.startswith(clean_pat_lower + "/"):
                    hit = True
                elif "**" in clean_pat_lower and fnmatch.fnmatchcase(rel_lower, clean_pat_lower):
                    hit = True

            if hit:
                if is_neg:
                    matched = False
                    matched_rule = None
                else:
                    matched = True
                    matched_rule = norm_pat + ("/" if is_dir_only else "")

        return matched, matched_rule


def _probe_mtime_tick_ns(mtime_samples: Iterable[int]) -> int | None:
    """从一批文件 mtime_ns 估计文件系统时间戳的刻度（纳秒）；样本不足返回 None。

    背景：Fast-Stat 快速路径的安全性依赖「余量 > 时间戳刻度」——只有刻度小于
    余量，「登记之后发生的写入必然推动 mtime 前进」的归纳才成立。而刻度是**文件
    系统属性**，写死在代码里的固定余量无法同时适配 NTFS（约 3ms）、ext4（真纳秒）
    与 FAT32/exFAT/部分 SMB 共享（2s）。所以这里从库内真实时间戳反推刻度。

    算法：取全部时间戳的最大公约数，并在「全部值」与「相邻差值」两组中取较小者。
    正确性方向：真实刻度 T 整除所有时间戳，故任何 gcd 都是 T 的正整数倍——**只会
    高估、不会低估**。高估使余量变大 → 更保守（多读盘，正确性安全）；低估才会
    漏检，而 gcd 不可能低估。取两组 gcd 的较小者只是为了压低高估幅度。

    退化面（明确记录）：样本不足两个（库内可索引文件少于 2 个）时无法求差值，
    返回 None，调用方退回固定下限 _MTIME_TRUST_MARGIN_NS——此时粗刻度库仍存在
    该固定余量的失效面，这是本探测唯一的未覆盖面。
    """
    values = sorted({int(v) for v in mtime_samples if isinstance(v, int) and v > 0})
    if len(values) < _MTIME_TICK_PROBE_MIN_SAMPLES:
        return None
    g_value = 0
    for value in values:
        g_value = math.gcd(g_value, value)
    g_delta = 0
    for prev, cur in zip(values, values[1:]):
        g_delta = math.gcd(g_delta, cur - prev)
    candidates = [g for g in (g_value, g_delta) if g > 0]
    return min(candidates) if candidates else None


def ignored_name(name: str) -> bool:
    """编辑器临时/交换文件名排除（原 MarkdownIndexer._ignored_name 逐字迁移）。"""
    lower = name.lower()
    return name.startswith("~") or lower.endswith(
        (".tmp.md", ".swp.md", ".swo.md", ".tmp.markdown", ".swp.markdown", ".swo.markdown",
         ".tmp.txt", ".swp.txt", ".swo.txt")
    )


def source_compare_key(source: str) -> str:
    """Platform filesystem comparison only; never rewrite stored source or IDs."""
    return os.path.normcase(source.replace("\\", "/"))


def source_rel(vault_path: Path, path: Path) -> str:
    """库内相对 posix 路径（原 MarkdownIndexer._source 逐字迁移）。"""
    return path.relative_to(vault_path).as_posix()


@dataclass(frozen=True, slots=True)
class ScanResult:
    found: list[Path]
    inaccessible: frozenset[str]
    policy_pruned: frozenset[str]
    root_available: bool
    complete: bool


def scan_indexable_files(
    vault_path: Path,
    matcher: IgnoreMatcher,
    indexable_exts: frozenset[str],
) -> ScanResult:
    paths: list[Path] = []
    inaccessible: set[str] = set()
    pruned: set[str] = set()
    root_available = True
    stack = [vault_path]
    visited: set[Path] = set()
    while stack:
        directory = stack.pop()
        rel = source_rel(vault_path, directory)
        try:
            real = directory.resolve(strict=True)
            real.relative_to(vault_path.resolve(strict=True))
            if real in visited:
                pruned.add(rel)
                continue
            visited.add(real)
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except (OSError, ValueError):
            inaccessible.add(rel)
            if directory == vault_path:
                root_available = False
            continue
        for entry in entries:
            path = Path(entry.path)
            source = source_rel(vault_path, path)
            try:
                is_dir = entry.is_dir()
                ignored, _ = matcher.is_ignored(source, is_dir=is_dir)
                if ignored or ignored_name(entry.name):
                    pruned.add(source)
                    continue
                if is_dir:
                    stack.append(path)
                elif path.suffix.lower() in indexable_exts:
                    path.resolve(strict=True).relative_to(vault_path.resolve(strict=True))
                    paths.append(path)
            except (OSError, ValueError):
                inaccessible.add(source)
    return ScanResult(
        sorted(paths, key=lambda item: source_rel(vault_path, item)),
        frozenset(inaccessible), frozenset(pruned), root_available,
        root_available and not inaccessible,
    )


def scandir_indexable_files(
    vault_path: Path,
    matcher: IgnoreMatcher,
    indexable_exts: frozenset[str],
) -> list[Path]:
    """手动 scandir 递归 + 目录层剪枝的可索引文件收集（原 _markdown_files 逐字迁移）。

    rglob 不做剪枝，只能在事后过滤，而排除目录（.git/objects 等）里可能有
    数十万对象，轮询模式每 0.25s 全量遍历一次代价巨大——所以在目录层剪枝。
    """
    if not vault_path.exists():
        return []
    paths: list[Path] = []
    stack = [vault_path]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            path = Path(entry.path)
            if is_dir:
                rel_dir = source_rel(vault_path, path)
                ignored, _ = matcher.is_ignored(rel_dir, is_dir=True)
                if ignored or ignored_name(entry.name):
                    continue
                stack.append(path)
                continue
            suffix = path.suffix.lower()
            if suffix in indexable_exts and not ignored_name(entry.name):
                source = source_rel(vault_path, path)
                ignored, _ = matcher.is_ignored(source, is_dir=False)
                if not ignored:
                    paths.append(path)
    return sorted(paths, key=lambda item: source_rel(vault_path, item))
