"""摄取任务管理器：按需异步解析 vault 里的 PDF/Office 文档。

硬性设计：
- 默认手动，显式授权后可自动（enabled=True 且 auto_watch=True 时支持 auto_submit）。
- 产物写 <vault>/<output_dirname>/（默认 .mortis-parsed/），镜像源相对路径。
- 状态文件 <output_dirname>/.ingest_state.json（原子写：tmp+replace，与 registry 同款）。
- 幂等：state 里记录源文件 sha256 与 auto_seen 去重账本；未变 → skip；变了 → 重解析覆盖。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from ..config import IngestConfig
except ImportError:
    from dataclasses import dataclass

    @dataclass(slots=True)
    class IngestConfig:
        enabled: bool = False
        api_key: str = ""
        model_version: str = "vlm"
        language: str = "ch"
        is_ocr: bool = False
        enable_formula: bool = True
        enable_table: bool = True
        poll_interval: float = 3.0
        poll_timeout: float = 600.0
        output_dirname: str = ".mortis-parsed"
        pymupdf_fallback: bool = True
        convert_small_tables: bool = True
        table_convert_max_cells: int = 60
        auto_watch: bool = False
        max_file_size_mb: int = 20

        def __post_init__(self) -> None:
            if not isinstance(self.auto_watch, bool):
                raise ValueError(f"ingest.auto_watch must be a boolean, got {self.auto_watch!r}")
            if (
                isinstance(self.max_file_size_mb, bool)
                or not isinstance(self.max_file_size_mb, int)
                or self.max_file_size_mb < 0
            ):
                raise ValueError(f"ingest.max_file_size_mb must be an integer >= 0, got {self.max_file_size_mb!r}")

        @property
        def max_file_size_bytes(self) -> int:
            """Max file size in bytes (1024*1024 per MiB). 0 means unlimited."""
            return self.max_file_size_mb * 1024 * 1024

from .mineru import AGENT_EXTS, MineruClient, MineruError
from .tables import convert_small_tables
from ..registry import _process_file_lock
from .router import DEFAULT_PARSE_BUDGET, ParseBudget, decide_route, upgrade_route_once
from .local import LocalUnsupported, parse_local, parse_local_volumes
from .audio import (AudioUnsupported, audio_profile_fingerprint, audio_settings,
                    inspect_audio, parse_audio)
from .images import (IMAGE_EXTS, IMAGE_ROUTE_UNSUPPORTED, ImageUnsupported, parse_image,
                     validate_image_source)
from .transcription import AUDIO_ADAPTER_UNAVAILABLE

INGEST_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac"}

#: legacy/既有路径不得把音频误送 MinerU（C99/C104 接缝）：稳定错误码 + 明确拒绝，
#: 既不云解析，也不静默跳过当作已解析。只有完成本地格式解析并接入 virtual worker
#: 音频路由的分支（`VirtualIngestWorker` + `parse_audio`）才允许音频进入 audio adapter。
AUDIO_ROUTE_UNSUPPORTED = "AUDIO_ROUTE_UNSUPPORTED"


def _ingest_exts(config: Any) -> set[str]:
    """**扫描面**的后缀集合：显式提交面（`_validate_safe_source`）比它宽。

    图片（`IMAGE_EXTS`）刻意**不在**扫描面里：E09 只做「用户显式提交的图片源」，
    不因为支持图片就打开后台全库图片扫描（那会改变既有 auto_watch 语义）。
    """
    return INGEST_EXTS | AUDIO_EXTS if getattr(config, "audio_enabled", False) else INGEST_EXTS
_STATE_NAME = ".ingest_state.json"
# review R3：扫描后仍有判稳中的文件或扫描不完整时，扫描循环延时重扫的间隔。
_SETTLE_RESCAN_SECONDS = 1.0
_EXCLUDED_DIR_NAMES = {".git", "node_modules", ".venv", ".trash", ".obsidian", ".stversions", ".stfolder", ".DS_Store"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _validate_safe_source(vault_path: Path, rel_path_str: str) -> Path:
    """严格沙箱路径校验 (D1, D12)：
    拒绝绝对路径、.. 穿越；确保解析后位于 vault 内且为存在的文件。

    后缀白名单是**显式提交面**（文档/音频/图片）；扫描面另有 `_ingest_exts`，两者独立。
    """
    rel = Path(rel_path_str)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"path traversal detected or absolute path not allowed: {rel_path_str}")
    resolved = (vault_path / rel).resolve()
    try:
        resolved.relative_to(vault_path)
    except ValueError:
        raise ValueError(f"source path resolves outside vault: {rel_path_str}")
    if not resolved.is_file():
        raise ValueError(f"not an ingestible document: {rel_path_str}")
    if resolved.suffix.lower() not in INGEST_EXTS | AUDIO_EXTS | IMAGE_EXTS:
        raise ValueError(f"not an ingestible document ({resolved.suffix}): {rel_path_str}")
    return resolved


def _is_excluded_dir_name(name: str, out_dirname: str) -> bool:
    if name in _EXCLUDED_DIR_NAMES or name == out_dirname:
        return True
    if name in {".assets", "assets", ".temp", "temp", ".tmp", "tmp"} or name.endswith(".assets"):
        return True
    return False


def _under_pruned(rel_posix: str, pruned_dirs: set[str]) -> bool:
    """rel 是否位于本轮被策略剪枝的目录子树内（review R4 的逐条豁免判据）。"""
    return any(rel_posix.startswith(prefix + "/") for prefix in pruned_dirs)


def _is_path_ignored(matcher: Any, rel_posix: str, is_dir: bool = False) -> bool:
    if matcher is None:
        return False
    if hasattr(matcher, "is_ignored"):
        ignored, _ = matcher.is_ignored(rel_posix, is_dir=is_dir)
        return bool(ignored)
    if callable(matcher):
        try:
            return bool(matcher(rel_posix, is_dir=is_dir))
        except TypeError:
            return bool(matcher(rel_posix))
    return False


class IngestManager:
    def __init__(
        self,
        vault_path: str | Path,
        config: IngestConfig,
        on_job_finished: Callable[[str, Path], None] | None = None,
        ignore_provider: Callable[[], Any] | None = None,
    ):
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.config = config
        out_name = (config.output_dirname or "").strip("/\\ ")
        if not out_name or ".." in Path(out_name).parts or out_name in (".", "/"):
            out_name = ".mortis-parsed"
        self.out_root = self.vault_path / out_name
        self.state_path = self.out_root / _STATE_NAME
        self._lock_path = self.out_root / ".ingest.lock"
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._client: MineruClient | None = None
        self.on_job_finished = on_job_finished
        self.ignore_provider = ignore_provider

        self._stat_samples: dict[str, tuple[float, int]] = {}
        self._settling_files: set[str] = set()

        # 启动时自动恢复僵尸 parsing 任务 (D10a)
        self._recover_zombie_jobs()

    def mark_settling(self, rel: str) -> None:
        """Mark a source as currently being copied/written (forces settle check)."""
        self._settling_files.add(Path(rel).as_posix())

    def stop(self) -> None:
        """等待进行中的单 job 收尾（幂等）。legacy 路径无租约，只能 join 线程。"""
        thread = self._worker
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)


    def _size_limit_bytes(self) -> int:
        cap = getattr(self.config, "max_file_size_bytes", None)
        if cap is not None:
            return int(cap)
        mb = getattr(self.config, "max_file_size_mb", 20)
        return int(mb) * 1024 * 1024

    def _check_file_size(self, size_bytes: int) -> bool:
        limit = self._size_limit_bytes()
        if limit <= 0:
            return True
        return size_bytes <= limit

    def _recover_zombie_jobs(self) -> None:
        if not self.state_path.exists():
            return
        with self._lock, _process_file_lock(self._lock_path):
            state = self._load_state()
            changed = False
            for j in state.get("jobs", {}).values():
                if j.get("state") == "parsing":
                    j["state"] = "queued"
                    changed = True
                    src = j.get("source")
                    if src and src in state.get("auto_seen", {}):
                        seen = state["auto_seen"][src]
                        if seen.get("last_job_id") == j.get("job_id"):
                            seen["state"] = "queued"
            if changed:
                self._save_state(state)

    # ------------------------------------------------------------ state

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {"version": 1, "jobs": {}}

        if not isinstance(state, dict):
            state = {"version": 1, "jobs": {}}
        if "jobs" not in state or not isinstance(state["jobs"], dict):
            state["jobs"] = {}

        if "auto_seen" not in state or not isinstance(state["auto_seen"], dict):
            auto_seen: dict[str, dict] = {}
            for j in state["jobs"].values():
                src = j.get("source")
                if not src:
                    continue
                sub_at = j.get("submitted_at") or 0
                existing = auto_seen.get(src)
                if existing is None or sub_at > (existing.get("submitted_at") or 0):
                    auto_seen[src] = {
                        "sha256": j.get("sha256", ""),
                        "state": j.get("state", "unknown"),
                        "submitted_at": sub_at,
                        "last_job_id": j.get("job_id", ""),
                    }
            state["auto_seen"] = auto_seen

        if "auto_watch" not in state or not isinstance(state["auto_watch"], dict):
            state["auto_watch"] = {
                "last_scan_at": None,
                "last_error": "",
                "submitted": 0,
                "skipped_too_large": 0,
                "skipped_seen": 0,
                "skipped_ignored": 0,
                "too_large_samples": [],
            }
        return state

    def _save_state(self, state: dict) -> None:
        # 清理超期历史记录，最多保留最近 500 个任务 (D16)
        jobs = state.get("jobs", {})
        if len(jobs) > 500:
            sorted_jobs = sorted(jobs.values(), key=lambda j: j.get("submitted_at") or 0)
            to_remove = len(jobs) - 500
            for j in sorted_jobs[:to_remove]:
                if j.get("state") in ("done", "failed"):
                    jobs.pop(j["job_id"], None)
        _atomic_write_json(self.state_path, state)

    # ------------------------------------------------------------ public

    def scan_pending(self) -> list[dict]:
        """列出待解析文档：未解析过的，或源文件 sha256 已变化的。
        使用带剪枝的遍历，排除 .git/node_modules/产物目录 (D8a)。
        超限文件标注 reason='too_large'，不计算哈希。
        """
        state = self._load_state()
        done = {j["source"]: j for j in state["jobs"].values() if j.get("state") == "done"}
        pending: list[dict] = []
        if not self.vault_path.exists():
            return pending

        out_dirname = self.out_root.name
        limit = self._size_limit_bytes()
        entries_to_visit = [self.vault_path]
        while entries_to_visit:
            curr_dir = entries_to_visit.pop()
            try:
                with os.scandir(curr_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if _is_excluded_dir_name(entry.name, out_dirname):
                                    continue
                                entries_to_visit.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                path = Path(entry.path)
                                if path.suffix.lower() not in _ingest_exts(self.config):
                                    continue
                                rel = path.relative_to(self.vault_path).as_posix()
                                st = entry.stat()
                                if not self._check_file_size(st.st_size):
                                    pending.append({
                                        "source": rel,
                                        "sha256": "",
                                        "mtime": st.st_mtime,
                                        "size": st.st_size,
                                        "limit_bytes": limit,
                                        "reason": "too_large",
                                    })
                                    continue
                                old = done.get(rel)
                                if old is not None and old.get("mtime") == st.st_mtime and old.get("size") == st.st_size and old.get("sha256"):
                                    digest = old["sha256"]
                                else:
                                    digest = _sha256(path)
                                if old is None or old.get("sha256") != digest:
                                    pending.append({
                                        "source": rel,
                                        "sha256": digest,
                                        "mtime": st.st_mtime,
                                        "size": st.st_size,
                                        "reason": "new" if old is None else "changed",
                                    })
                        except OSError:
                            continue
            except OSError:
                continue
        return pending

    def _validate_safe_source(self, rel: str) -> Path:
        return _validate_safe_source(self.vault_path, rel)

    def submit(self, sources: list[str] | None = None, force: bool = False,
               caption: str = "") -> dict:
        """提交摄取任务（异步）。sources 为库内相对路径列表；None → scan_pending() 全量。
        支持 force 强制重解析 (D7)；去重 (D11)；参数严格校验 (D1, D12)；尺寸上限统一闸门。

        `caption` 与 virtual 路径同签名（供 `kb_ingest` 统一调用）：legacy 没有 store，
        图片源在这里就被明确拒绝，因此该参数不参与 legacy 行为。
        """
        if not self.config.enabled:
            raise ValueError(
                "ingest disabled: PDF 摄取层默认关闭。请在 config/app.toml 设置 "
                "[ingest] enabled = true 并重启 MCP 服务后重试。"
            )
        if sources is not None:
            if not isinstance(sources, (list, tuple)):
                raise TypeError(f"sources must be a list of relative file path strings, got {type(sources).__name__}")
            for s in sources:
                if not isinstance(s, str):
                    raise TypeError(f"each item in sources must be a string, got {type(s).__name__}")

        if sources is None:
            raw_targets = self.scan_pending()
            targets = []
            skipped_too_large = 0
            for t in raw_targets:
                if t.get("reason") == "too_large":
                    skipped_too_large += 1
                else:
                    targets.append(t)
        else:
            limit = self._size_limit_bytes()
            validated = []
            for rel in sources:
                path = _validate_safe_source(self.vault_path, rel)
                if path.suffix.lower() in AUDIO_EXTS:
                    # legacy 路径没有本地格式解析接线：明确拒绝，绝不把音频送 MinerU。
                    raise ValueError(
                        f"{AUDIO_ROUTE_UNSUPPORTED}: legacy 摄取路径不得把音频送 MinerU（{rel}）；"
                        "请在完成本地格式解析并接入 virtual worker 音频路由后再启用。"
                    )
                if path.suffix.lower() in IMAGE_EXTS:
                    # legacy 路径没有 store 媒体出口：明确拒绝，绝不把图片送 MinerU 猜 OCR。
                    raise ValueError(
                        f"{IMAGE_ROUTE_UNSUPPORTED}: legacy 摄取路径不发布独立图片源（{rel}）；"
                        "图片显式摄取需要 ingest.storage=virtual 的 store 媒体出口。"
                    )
                st = path.stat()
                if not self._check_file_size(st.st_size):
                    raise ValueError(f"file size {st.st_size} bytes exceeds limit {limit} bytes: {rel}")
                validated.append((rel, path, st))
            targets = []
            for rel, path, st in validated:
                targets.append({
                    "source": Path(rel).as_posix(),
                    "sha256": _sha256(path),
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "reason": "explicit",
                })
            skipped_too_large = 0

        with self._lock, _process_file_lock(self._lock_path):
            state = self._load_state()
            active_sources = {
                j["source"]: j for j in state["jobs"].values()
                if j.get("state") in ("queued", "parsing")
            }
            auto_seen = state.setdefault("auto_seen", {})
            jobs = []
            new_job_count = 0
            for t in targets:
                src = t["source"]
                if not force and src in active_sources:
                    jobs.append(active_sources[src])
                    continue
                job_id = uuid.uuid4().hex[:12]
                submitted_at = time.time()
                new_job = {
                    "job_id": job_id, "source": src, "sha256": t["sha256"],
                    "mtime": t.get("mtime"), "size": t.get("size"),
                    "state": "queued", "channel": None, "error": "",
                    "submitted_at": submitted_at, "finished_at": None, "output": None,
                    "parse_quality": None,
                }
                state["jobs"][job_id] = new_job
                active_sources[src] = new_job
                auto_seen[src] = {
                    "sha256": t["sha256"],
                    "state": "queued",
                    "submitted_at": submitted_at,
                    "last_job_id": job_id,
                }
                jobs.append(new_job)
                new_job_count += 1

            self._save_state(state)
            if any(j["state"] == "queued" for j in jobs) and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="ingest-worker")
                self._worker.start()

        return {
            "submitted": new_job_count,
            "new_jobs": new_job_count,
            "jobs": jobs,
            "skipped_too_large": skipped_too_large,
        }

    def status(self, job_id: str | None = None) -> dict:
        with self._lock, _process_file_lock(self._lock_path):
            state = self._load_state()
        if job_id:
            job = state["jobs"].get(job_id)
            if job is None:
                raise ValueError(f"unknown job_id: {job_id}")
            return {"job": job}
        jobs = sorted(state["jobs"].values(), key=lambda j: -(j.get("submitted_at") or 0))
        summary: dict[str, int] = {}
        for j in jobs:
            st = j.get("state", "unknown")
            if st == "done" and j.get("parse_quality") == "fallback":
                st = "done_fallback"
            elif st == "done":
                st = "done_full"
            summary[st] = summary.get(st, 0) + 1
        if "done_full" in summary or "done_fallback" in summary:
            summary["done"] = summary.get("done_full", 0) + summary.get("done_fallback", 0)
        res = {"summary": summary, "jobs": jobs[:20]}
        if "auto_watch" in state:
            res["auto_watch"] = state["auto_watch"]
        return res

    def _auto_pending(self) -> tuple[list[dict], dict[str, Any]]:
        """自动摄取扫描：
        仅在 enabled=True 且 auto_watch=True 时工作。
        对合规文档先校验路径、ignore 规则、尺寸上限，再计算 sha256。
        对比 auto_seen 去重（连续未变版本跳过）。
        """
        scan_stats: dict[str, Any] = {
            "last_scan_at": time.time(),
            "last_error": "",
            "submitted": 0,
            "skipped_too_large": 0,
            "skipped_seen": 0,
            "skipped_ignored": 0,
            "too_large_samples": [],
            "clean_scan": False,
            "scanned_sources": set(),
        }
        if not (self.config.enabled and self.config.auto_watch):
            return [], scan_stats

        if not self.vault_path.exists():
            # 库根不可见（未挂载 / 权限被临时改 / 同步客户端整目录改名）不是「文件被删光」：
            # 绝不能让账本按「完整枚举 0 文件」清空，否则路径恢复后整库重传。
            scan_stats["clean_scan"] = False
            scan_stats["scanned_sources"] = None
            return [], scan_stats

        state = self._load_state()
        auto_seen = state.get("auto_seen", {})

        matcher = None
        if self.ignore_provider is not None:
            matcher = self.ignore_provider()
            if matcher is None:
                # review R1 fail-closed：宿主配置了 ignore_provider 却拿不到匹配器
                # （indexer 尚未发布），无法判定 .vaultignore/exclude 豁免规则；
                # 宁可本轮不摄取，也绝不冒然提交可能被排除的文档。
                scan_stats["clean_scan"] = False
                scan_stats["last_error"] = "ignore matcher unavailable; auto submit skipped"
                return [], scan_stats
        out_dirname = self.out_root.name
        limit = self._size_limit_bytes()

        targets: list[dict] = []
        scanned_sources: set[str] = set()
        pruned_dirs: set[str] = set()
        entries_to_visit = [self.vault_path]
        clean_scan = True

        while entries_to_visit:
            curr_dir = entries_to_visit.pop()
            try:
                with os.scandir(curr_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if _is_excluded_dir_name(entry.name, out_dirname):
                                    continue
                                rel_dir = Path(entry.path).relative_to(self.vault_path).as_posix()
                                if _is_path_ignored(matcher, rel_dir, is_dir=True):
                                    # review R4：目录被策略剪枝 ⇒ 本轮不是完整枚举，
                                    # 其下文件的账本条目不得按「确认删除」清理。
                                    # 记录剪枝子树而非置「整轮不清」标记：清理时逐条
                                    # 豁免，既保住剪枝子树，又让真实删除的条目照常回收。
                                    pruned_dirs.add(rel_dir)
                                    continue
                                entries_to_visit.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                path = Path(entry.path)
                                if path.suffix.lower() not in _ingest_exts(self.config):
                                    continue
                                rel = path.relative_to(self.vault_path).as_posix()
                                scanned_sources.add(rel)

                                if _is_path_ignored(matcher, rel, is_dir=False):
                                    scan_stats["skipped_ignored"] += 1
                                    continue

                                st = entry.stat()
                                if not self._check_file_size(st.st_size):
                                    scan_stats["skipped_too_large"] += 1
                                    if len(scan_stats["too_large_samples"]) < 5:
                                        scan_stats["too_large_samples"].append({
                                            "source": rel,
                                            "size": st.st_size,
                                            "limit_bytes": limit,
                                        })
                                    continue

                                # 0 字节必须在判稳比对**之前**短路：复制刚创建的 0 字节
                                # 文件两次采样恒为 (mtime, 0)，会被判成「已稳定」后直接
                                # hash 上传空内容；内容确实为空的文件同样没有可解析内容。
                                # 与 v0.8.0 语义一致：0 字节一律延后，不进上传链路。
                                if st.st_size == 0:
                                    scan_stats["skipped_settling"] = scan_stats.get("skipped_settling", 0) + 1
                                    self._stat_samples[rel] = (st.st_mtime, 0)
                                    self._settling_files.add(rel)
                                    continue

                                # 判稳（Card C58c / C61 Req 9；review R3）：所有新/变化源
                                # 至少两次间隔采样一致才 hash/提交——首次事件到达时文件
                                # 可能正处于复制中途，非零的部分字节同样会被解析上传。
                                sample = self._stat_samples.get(rel)
                                if sample is not None and (st.st_mtime, st.st_size) == sample:
                                    self._settling_files.discard(rel)
                                else:
                                    self._stat_samples[rel] = (st.st_mtime, st.st_size)
                                    self._settling_files.add(rel)
                                    scan_stats["skipped_settling"] = scan_stats.get("skipped_settling", 0) + 1
                                    continue

                                digest = _sha256(path)
                                seen = auto_seen.get(rel)
                                if seen is not None and seen.get("sha256") == digest:
                                    scan_stats["skipped_seen"] += 1
                                    continue

                                targets.append({
                                    "source": rel,
                                    "sha256": digest,
                                    "mtime": st.st_mtime,
                                    "size": st.st_size,
                                    "reason": "new" if seen is None else "changed",
                                })
                        except OSError as exc:
                            clean_scan = False
                            scan_stats["last_error"] = str(exc)
                            continue
            except OSError as exc:
                clean_scan = False
                scan_stats["last_error"] = str(exc)
                continue

        scan_stats["clean_scan"] = clean_scan
        scan_stats["scanned_sources"] = scanned_sources
        scan_stats["pruned_dirs"] = pruned_dirs
        # 判稳采样清理只对完整枚举的轮次做，并逐条豁免被策略剪枝的子树（review R4）：
        # 「本轮出现过剪枝就整轮不清」会让残留采样项永不回收，`_settling_files`
        # 恒非空 ⇒ rescan_after_seconds 恒为正，扫描循环被钉成 1Hz 全库重扫。
        if clean_scan:
            for gone in [s for s in self._stat_samples if s not in scanned_sources]:
                if _under_pruned(gone, pruned_dirs):
                    continue
                self._stat_samples.pop(gone, None)
                self._settling_files.discard(gone)
        return targets, scan_stats

    def auto_submit(self) -> dict:
        """自动提交待摄取任务（仅在 enabled=True 且 auto_watch=True 时生效）。
        复用单队列与 state 文件锁，原子更新 auto_seen 账本与任务状态。
        """
        if not (self.config.enabled and self.config.auto_watch):
            return {
                "submitted": 0,
                "new_jobs": 0,
                "jobs": [],
                "status": "disabled",
                "skipped_too_large": 0,
                "skipped_seen": 0,
                "skipped_ignored": 0,
                "rescan_after_seconds": 0.0,
            }

        targets, scan_stats = self._auto_pending()

        with self._lock, _process_file_lock(self._lock_path):
            state = self._load_state()
            auto_seen = state.setdefault("auto_seen", {})
            active_sources = {
                j["source"]: j for j in state["jobs"].values()
                if j.get("state") in ("queued", "parsing")
            }

            aw = state.setdefault("auto_watch", {})
            aw["last_scan_at"] = scan_stats.get("last_scan_at", time.time())
            aw["last_error"] = scan_stats.get("last_error", "")
            aw["skipped_too_large"] = scan_stats.get("skipped_too_large", 0)
            aw["skipped_seen"] = scan_stats.get("skipped_seen", 0)
            aw["skipped_ignored"] = scan_stats.get("skipped_ignored", 0)
            aw["too_large_samples"] = scan_stats.get("too_large_samples", [])

            # review R4：只有完整枚举的干净扫描才允许清理账本，并逐条豁免被策略剪枝
            # 子树内的条目——临时排除目录 ≠ 文件被删除，取消排除后同 SHA 不得重传。
            # 逐条判定（而非「本轮有剪枝就整轮不清」）才能让真实删除的条目照常回收。
            if scan_stats.get("clean_scan") and scan_stats.get("scanned_sources") is not None:
                scanned_set = scan_stats["scanned_sources"]
                pruned_dirs = scan_stats.get("pruned_dirs") or set()
                for s in list(auto_seen.keys()):
                    if s in scanned_set or s in active_sources:
                        continue
                    if _under_pruned(s, pruned_dirs):
                        continue
                    auto_seen.pop(s, None)

            jobs = []
            new_job_count = 0
            for t in targets:
                src = t["source"]
                digest = t["sha256"]
                seen = auto_seen.get(src)
                if seen and seen.get("sha256") == digest:
                    continue
                if src in active_sources and active_sources[src].get("sha256") == digest:
                    continue

                job_id = uuid.uuid4().hex[:12]
                submitted_at = time.time()
                new_job = {
                    "job_id": job_id,
                    "source": src,
                    "sha256": digest,
                    "mtime": t.get("mtime"),
                    "size": t.get("size"),
                    "state": "queued",
                    "channel": None,
                    "error": "",
                    "submitted_at": submitted_at,
                    "finished_at": None,
                    "output": None,
                    "parse_quality": None,
                }
                state["jobs"][job_id] = new_job
                active_sources[src] = new_job
                auto_seen[src] = {
                    "sha256": digest,
                    "state": "queued",
                    "submitted_at": submitted_at,
                    "last_job_id": job_id,
                }
                jobs.append(new_job)
                new_job_count += 1

            aw["submitted"] = aw.get("submitted", 0) + new_job_count
            self._save_state(state)

            if any(j["state"] == "queued" for j in jobs) and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="ingest-worker")
                self._worker.start()

        return {
            "submitted": new_job_count,
            "new_jobs": new_job_count,
            "jobs": jobs,
            "skipped_too_large": scan_stats.get("skipped_too_large", 0),
            "skipped_seen": scan_stats.get("skipped_seen", 0),
            "skipped_ignored": scan_stats.get("skipped_ignored", 0),
            # review R3：仍有判稳中的文件或本轮扫描不完整时，请扫描循环延时重扫，
            # 不依赖下一个文件事件（事件被防抖吞并/丢失会让复制中的文件永远滞留）。
            "rescan_after_seconds": (
                _SETTLE_RESCAN_SECONDS
                if (self._settling_files or not scan_stats.get("clean_scan", True))
                else 0.0
            ),
        }

    # ------------------------------------------------------------ worker

    def _client_or_make(self) -> MineruClient:
        if self._client is None:
            c = self.config
            self._client = MineruClient(
                c.api_key, model_version=c.model_version, language=c.language,
                is_ocr=c.is_ocr, enable_formula=c.enable_formula,
                enable_table=c.enable_table,
            )
        return self._client

    def _worker_loop(self) -> None:
        while True:
            with self._lock, _process_file_lock(self._lock_path):
                state = self._load_state()
                job = next((j for j in state["jobs"].values() if j["state"] == "queued"), None)
                if job is None:
                    return
                job["state"] = "parsing"
                seen = state.get("auto_seen", {}).get(job["source"])
                if seen and seen.get("last_job_id") == job["job_id"]:
                    seen["state"] = "parsing"
                self._save_state(state)
            try:
                self._run_job(job)
                if job.get("state") != "failed":
                    job["state"] = "done"
                    job["finished_at"] = time.time()
            except Exception as exc:  # 单 job 失败不拖垮队列
                job["state"] = "failed"
                job["error"] = str(exc)[:500]
                job["finished_at"] = time.time()
            finally:
                with self._lock, _process_file_lock(self._lock_path):
                    state = self._load_state()
                    state["jobs"][job["job_id"]] = job
                    seen = state.get("auto_seen", {}).get(job["source"])
                    if seen and seen.get("last_job_id") == job["job_id"]:
                        seen["state"] = job["state"]
                    self._save_state(state)

    def _run_job(self, job: dict) -> None:
        # D1: run_job 入口再次校验沙箱与尺寸上限
        src = _validate_safe_source(self.vault_path, job["source"])
        if src.suffix.lower() in AUDIO_EXTS:
            # 兜底防线：扫描/恢复可能因 audio_enabled=true 把音频带回 legacy 队列；
            # 明确失败并给出稳定错误码，绝不落到 `_client_or_make().parse()`（MinerU）。
            job["state"] = "failed"
            job["error_code"] = AUDIO_ROUTE_UNSUPPORTED
            job["error"] = (
                f"{AUDIO_ROUTE_UNSUPPORTED}: legacy 摄取路径不得把音频送 MinerU"
                f"（{job['source']}）；请在 virtual worker 音频路由接通后再启用。"
            )
            job["finished_at"] = time.time()
            return
        if src.suffix.lower() in IMAGE_EXTS:
            job["state"] = "failed"
            job["error_code"] = IMAGE_ROUTE_UNSUPPORTED
            job["error"] = (
                f"{IMAGE_ROUTE_UNSUPPORTED}: legacy 摄取路径不发布独立图片源"
                f"（{job['source']}）；请切换到 ingest.storage=virtual。"
            )
            job["finished_at"] = time.time()
            return
        st = src.stat()
        if not self._check_file_size(st.st_size):
            limit = self._size_limit_bytes()
            raise ValueError(f"file size {st.st_size} bytes exceeds limit {limit} bytes: {job['source']}")

        # C58c / C61 Req 10: worker 解析前哈希与入队 hash 不符时标 source_changed，等待下一扫描按新 hash 入队
        current_sha = _sha256(src)
        if current_sha != job.get("sha256"):
            job["state"] = "failed"
            job["error"] = f"source_changed: expected {job.get('sha256', '')[:8]}, got {current_sha[:8]}"
            job["finished_at"] = time.time()
            return

        rel = Path(job["source"])
        out_md = self.out_root / rel.parent / (rel.stem + ".md")
        out_md.parent.mkdir(parents=True, exist_ok=True)
        try:
            parsed = self._client_or_make().parse(
                src, poll_interval=self.config.poll_interval,
                poll_timeout=self.config.poll_timeout,
            )
            job["channel"] = parsed.channel
            job["parse_quality"] = "full" if parsed.channel == "v4" else "light"
            markdown = parsed.markdown
            # v4 zip 图片落盘 assets/ 并把 md 里的 images/ 前缀改写过去
            if parsed.images:
                assets = self.out_root / rel.parent / (rel.stem + ".assets")
                assets.mkdir(parents=True, exist_ok=True)
                for name, blob in parsed.images.items():
                    (assets / Path(name).name).write_bytes(blob)
                # D15: 精准替换，避免污染正文与外链
                markdown = re.sub(
                    r'(!\[.*?\]\()images/',
                    rf'\1{rel.stem}.assets/',
                    markdown
                )
                markdown = re.sub(
                    r'(<img\b[^>]*?\bsrc=["\'])images/',
                    rf'\1{rel.stem}.assets/',
                    markdown,
                    flags=re.IGNORECASE
                )
        except MineruError as exc:
            # D7: 瞬时故障（429、超时、网络错误）保持 failed 并记录 retryable，绝不固化降级
            if exc.retryable:
                job["state"] = "failed"
                job["error"] = f"transient mineru error: {exc}"[:300]
                job["retryable"] = True
                job["finished_at"] = time.time()
                return
            if not self.config.pymupdf_fallback or src.suffix.lower() != ".pdf":
                raise
            markdown = self._pymupdf_fallback(src)
            job["channel"] = "pymupdf"
            job["parse_quality"] = "fallback"
            job["error"] = f"cloud failed, local fallback used: {exc}"[:300]

        if self.config.convert_small_tables:
            markdown = convert_small_tables(
                markdown, max_cells=self.config.table_convert_max_cells)

        header = (
            "---\n"
            f"source_pdf: {json.dumps(job['source'], ensure_ascii=False)}\n"
            f"source_sha256: {job['sha256']}\n"
            f"parsed_by: {job['channel']}\n"
            f"parsed_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
            f"parse_quality: {job['parse_quality']}\n"
            "---\n\n"
        )
        tmp = out_md.with_suffix(".md.tmp")
        try:
            tmp.write_text(header + markdown, encoding="utf-8")
            tmp.replace(out_md)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        job["output"] = out_md.relative_to(self.vault_path).as_posix()
        # D9: 解析完成回调通知上层
        if self.on_job_finished is not None:
            try:
                self.on_job_finished(job["source"], out_md)
            except Exception:
                pass

    @staticmethod
    def _pymupdf_fallback(path: Path) -> str:
        """云端全挂时的本地纯文本兜底（可选依赖；版面/表格/公式无保障）。"""
        if path.suffix.lower() != ".pdf":
            raise MineruError(f"pymupdf fallback only supports PDF, got {path.suffix}")
        try:
            import pymupdf  # type: ignore
        except ImportError as exc:
            try:
                import fitz as pymupdf  # type: ignore
            except ImportError:
                raise MineruError("cloud channels failed and pymupdf not installed") from exc
        doc = pymupdf.open(str(path))
        try:
            return "\n\n".join(page.get_text() for page in doc)
        finally:
            doc.close()


# =====================================================================================
# C94：虚拟（store）摄取路径
# =====================================================================================
"""虚拟路径与 legacy 路径的差别（都在本文件里，便于对照）：

| 维度 | legacy（默认，兼容 .mortis-parsed/*.md） | virtual（C94） |
|---|---|---|
| 账本 | `output_dirname/.ingest_state.json` | `ingest_jobs` / `auto_seen`（按库隔离的 store） |
| 任务归属 | 进程内 `threading.Lock` + 文件锁 | `owner_token` + `lease_until` 的 SQL CAS（跨进程 fencing） |
| 发布 | 原子写 `.md` | `stage_revision` → 两次源核验 → `commit_job_revision`（done 与切 active **同事务**） |
| 媒体 | 落 `*.assets/` 目录 | 逐项 `put_media_blob`（内存有界）→ `attach_occurrences` |
| 计费重试 | 瞬时错误标 retryable 由用户重试 | 非幂等 POST 的网络失败 → `SUBMISSION_UNKNOWN`，**禁止自动重传** |

为什么 legacy 仍是默认：虚拟文档的**读取**适配器属 C96（Lane C）。在 C96 落地前把默认
切成 virtual，已摄取文档会变成「写了但读不出来」。故 `ingest.storage` 默认 `legacy`，
virtual 必须显式配置；这条偏差要在计划卡里显式记录。
"""


class StoreMediaSink:
    """把媒体**逐项**写进 docstore 的 sink（§13.2：直接写 staged store、释放 RAM）。

    解析期间只做第一阶段（`put_media_blob`，每项写完即释放）；revision 建立后再由
    worker 调 `attach_occurrences` 挂出现。未被挂上的 blob 由 `gc_unreferenced()` 回收。
    """

    def __init__(self, store: Any, *, job_id: str = "", owner_token: str = "") -> None:
        self.store = store
        self.job_id = job_id
        self.owner_token = owner_token
        self.items: list[Any] = []

    def add(self, *, name: str, data: bytes, kind: str, ordinal: int, mime_type: str,
            page: int | None = None, bbox: Any = None, caption: str = "",
            ocr: str = "", width: int | None = None, height: int | None = None,
            t_start_ms: int | None = None, t_end_ms: int | None = None,
            anchor_start: int | None = None, anchor_end: int | None = None,
            metadata: dict[str, Any] | None = None) -> str:
        from ..doc_store import MediaOccurrenceSpec

        blob_id = self.store.put_media_blob(
            data=data, mime_type=mime_type, width=width, height=height,
            job_id=self.job_id, owner_token=self.owner_token,
        )
        occurrence_id = f"occ-{ordinal:05d}"
        meta = {"archive_member": name}
        meta.update(metadata or {})
        self.items.append(MediaOccurrenceSpec(
            occurrence_id=occurrence_id,
            blob_id=blob_id,
            kind=kind,
            ordinal=ordinal,
            mime_type=mime_type,
            page=page,
            bbox=bbox,
            caption=caption,
            ocr=ocr,
            width=width,
            height=height,
            t_start_ms=t_start_ms,
            t_end_ms=t_end_ms,
            # E08-a：正文锚点透传（媒体尺寸走 width/height，绝不冒充 anchor）。
            anchor_start=anchor_start,
            anchor_end=anchor_end,
            metadata=meta,
        ))
        return occurrence_id


class VirtualIngestWorker:
    """store 支撑的摄取 worker（C94）：队列/租约/发布 CAS/两次源核验。

    公开面与 `IngestManager` 对齐（`submit` / `status` / `scan_pending` / `auto_submit` /
    `mark_settling`），使 server 侧与既有 hook 不必区分两条路径。
    """

    def __init__(
        self,
        vault_path: str | Path,
        config: Any,
        store_provider: Callable[[], Any],
        on_job_finished: Callable[[str, str], None] | None = None,
        ignore_provider: Callable[[], Any] | None = None,
        parse_budget: ParseBudget | None = None,
        audio_adapter: Any = None,
        chunker_fingerprint_provider: Callable[[], str] | None = None,
        audio_config: Any = None,
        audio_decoder: Any = None,
        media_provider: Any = None,
    ) -> None:
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.config = config
        # §20.7C：进程级**共享**解析预算池。默认注入模块级 `DEFAULT_PARSE_BUDGET`，
        # 绝不每库/每 job 新建一个冒充全局。它是逻辑预算准入（控制同时在解析的受控缓冲），
        # 不是 native 库 RSS 硬保证，也不是跨进程统一限额。
        self.parse_budget = parse_budget or DEFAULT_PARSE_BUDGET
        self.audio_adapter = audio_adapter
        # E09：独立 `AudioConfig`（`AppConfig.audio`）与显式解码组件。未注入时保留旧
        # flat（`audio_segment_seconds` …）读取路径；注入后 `audio.*` 参数才真正生效。
        self.audio_config = audio_config
        self.audio_decoder = audio_decoder
        self.media_provider = media_provider
        # 显式提交的图片标题/说明（`kb_ingest submit` 的可选事实）。发布后该说明已写进
        # occurrence/revision，是持久事实；进程重启发生在发布前则退回文件名（见 `_image_caption`）。
        self._image_captions: dict[str, str] = {}
        # C99 接缝：队列 fingerprint 需纳入真实 chunker/profile 指纹。`IngestConfig`
        # 本身不含 chunking 段，故由上层（server/indexer）注入 provider；未注入时退回
        # 常量占位并在报告中记为待接线。
        self._chunker_fingerprint_provider = chunker_fingerprint_provider
        self._store_provider = store_provider
        self.on_job_finished = on_job_finished
        self.ignore_provider = ignore_provider
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._client: MineruClient | None = None
        self._settling_files: set[str] = set()
        #: 每个音频 job 的已完成片段进度（ordinal → 事实），未持久化时的内存兜底。
        self._audio_progress: dict[str, dict[int, dict[str, Any]]] = {}
        self._lease_seconds = max(300.0, float(getattr(config, "poll_timeout", 600.0)) + 120.0)

    # ---------------------------------------------------------------- helpers

    def _store(self) -> Any:
        store = self._store_provider()
        layout = getattr(store, "layout", None)
        if layout is not None and not layout.writable:
            from ..doc_store import VirtualStorageDisabled

            raise VirtualStorageDisabled(
                f"虚拟摄取不可用：{layout.blocked_reason or '布局不可写'}",
                fix="按 doctor 的提示修正 cache 归属/placement，或把 ingest.storage 设为 legacy。",
            )
        return store

    def _limits(self) -> Any:
        from .models import ResourceLimits

        return ResourceLimits.from_config(self.config)

    def _size_limit_bytes(self) -> int:
        cap = getattr(self.config, "max_file_size_bytes", None)
        if cap is not None:
            return int(cap)
        return int(getattr(self.config, "max_file_size_mb", 20)) * 1024 * 1024

    def _check_file_size(self, size_bytes: int) -> bool:
        limit = self._size_limit_bytes()
        if limit <= 0:
            return True
        return size_bytes <= limit

    def _matcher(self) -> Any:
        if self.ignore_provider is None:
            return None
        matcher = self.ignore_provider()
        if matcher is None:
            raise ValueError(
                "ignore matcher unavailable; 虚拟摄取拒绝在无法判定豁免规则时入队"
            )
        return matcher

    def mark_settling(self, rel: str) -> None:
        self._settling_files.add(Path(rel).as_posix())

    # ------------------------------------------------------------ audio (E09)

    def _audio_settings(self) -> Any:
        """把独立 `AudioConfig` 与 ingest 字段合成 audio 读取视图（旧 flat 兼容）。"""
        return audio_settings(self.config, self.audio_config)

    def _audio_adapter(self) -> Any:
        """声明了转录 adapter 就必须有可验证实现：不静默降级成 metadata_only。"""
        declared = str(getattr(self.audio_config, "adapter", "")
                       or getattr(self.config, "audio_adapter", "") or "").strip()
        if declared and self.audio_adapter is None:
            raise AudioUnsupported(
                f"{AUDIO_ADAPTER_UNAVAILABLE}: audio.adapter={declared!r} 没有可注入的已核验实现"
                "（请求/响应/幂等/额度合同缺失）；拒绝以 metadata_only 静默降级"
            )
        return self.audio_adapter

    def _assert_network_policy(self, source: str) -> None:
        path = _validate_safe_source(self.vault_path, source)
        suffix = path.suffix.lower()
        if suffix in AUDIO_EXTS:
            # 入队前就按**实际格式**分流：非 PCM 缺解码能力时明确拒绝，绝不先按 WAV 猜，
            # 也绝不送 MinerU 文档通道冒音频支持。
            self._audio_adapter()
            inspect_audio(path, self._audio_settings(), decoder=self.audio_decoder)
            return
        if suffix in IMAGE_EXTS:
            # 显式提交的图片源是本地输入适配，不触发云路由；准入在入队前完成。
            validate_image_source(path, self._limits())
            return
        with self.parse_budget.reserve(min(self._limits().memory_budget_bytes, path.stat().st_size * 4), self._stop.is_set):
            decide_route(path, self.config, limits=self._limits())

    def _job_view(self, job: Any, *, channel: str = "", error: str = "",
                  parse_quality: str = "") -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "source": job.source,
            "sha256": job.source_sha256,
            "state": job.state,
            "phase": job.phase,
            "attempts": job.attempts,
            "channel": channel,
            "error": error or (f"{job.error_code}: {job.error_summary}" if job.error_code else job.error_summary),
            "parse_quality": parse_quality,
            "revision_id": job.result_revision,
            "owner_token": bool(job.owner_token),
        }

    # ---------------------------------------------------------------- public

    def scan_pending(self) -> list[dict]:
        """列出待摄取文档（与 auto_seen 账本比对；不写任何状态）。"""
        store = self._store()
        seen = {item["source"]: item for item in store.iter_auto_seen()}
        pending: list[dict] = []
        if not self.vault_path.exists():
            return pending
        matcher = self._matcher()
        limit = self._size_limit_bytes()
        out_dirname = str(getattr(self.config, "output_dirname", ".mortis-parsed"))
        stack = [self.vault_path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if _is_excluded_dir_name(entry.name, out_dirname):
                                    continue
                                rel_dir = Path(entry.path).relative_to(self.vault_path).as_posix()
                                if _is_path_ignored(matcher, rel_dir, is_dir=True):
                                    continue
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                path = Path(entry.path)
                                if path.suffix.lower() not in _ingest_exts(self.config):
                                    continue
                                rel = path.relative_to(self.vault_path).as_posix()
                                if _is_path_ignored(matcher, rel):
                                    continue
                                stat = entry.stat()
                                if not self._check_file_size(stat.st_size):
                                    pending.append({"source": rel, "sha256": "", "mtime": stat.st_mtime,
                                                    "size": stat.st_size, "limit_bytes": limit,
                                                    "reason": "too_large"})
                                    continue
                                digest = _sha256(path)
                                record = seen.get(rel)
                                if record is None or record.get("source_sha256") != digest:
                                    pending.append({
                                        "source": rel,
                                        "sha256": digest,
                                        "mtime": stat.st_mtime,
                                        "size": stat.st_size,
                                        "reason": "new" if record is None else "changed",
                                    })
                        except OSError:
                            continue
            except OSError:
                continue
        return pending

    def submit(self, sources: list[str] | None = None, force: bool = False,
               caption: str = "") -> dict:
        """提交虚拟摄取任务（异步）。校验→入队（store）→ 起 worker。

        `caption`：**显式提交**图片源时的用户标题/说明（仅对 `IMAGE_EXTS` 生效；其他后缀
        忽略）。发布后该说明写进 occurrence/revision，是持久事实；进程在发布前重启则退回
        文件名（不猜内容，也不做 OCR/caption 推断）。
        """
        if not self.config.enabled:
            raise ValueError(
                "ingest disabled: PDF 摄取层默认关闭。请在 config/app.toml 设置 "
                "[ingest] enabled = true 并重启 MCP 服务后重试。"
            )
        if sources is not None:
            if not isinstance(sources, (list, tuple)):
                raise TypeError(f"sources must be a list of relative file path strings, got {type(sources).__name__}")
            for item in sources:
                if not isinstance(item, str):
                    raise TypeError(f"each item in sources must be a string, got {type(item).__name__}")
        store = self._store()
        matcher = self._matcher()
        limit = self._size_limit_bytes()

        if sources is None:
            scanned = self.scan_pending()
            targets = [t for t in scanned if t.get("reason") != "too_large"]
            skipped_too_large = len([t for t in scanned if t.get("reason") == "too_large"])
            skipped_ignored = 0
            # §13.3：local_only / 未授权云路径必须在**入队前**拒绝（显式分支已在
            # 上面的循环里逐条校验）。这里做全批量校验：任一目标越云即整体拒绝，
            # 绝不「先入队、等领取再拦」。
            for target in targets:
                self._assert_network_policy(target["source"])
        else:
            targets = []
            skipped_too_large = 0
            skipped_ignored = 0
            for rel in sources:
                path = _validate_safe_source(self.vault_path, rel)
                if _is_path_ignored(matcher, Path(rel).as_posix()):
                    skipped_ignored += 1
                    continue
                self._assert_network_policy(rel)
                stat = path.stat()
                if not self._check_file_size(stat.st_size):
                    raise ValueError(
                        f"file size {stat.st_size} bytes exceeds limit {limit} bytes: {rel}"
                    )
                rel_posix = Path(rel).as_posix()
                if caption and path.suffix.lower() in IMAGE_EXTS:
                    self._image_captions[rel_posix] = str(caption).strip()
                targets.append({
                    "source": rel_posix,
                    "sha256": _sha256(path),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "reason": "explicit",
                })

        jobs: list[dict] = []
        created = 0
        for target in targets:
            job, is_new = store.enqueue_job(
                source=target["source"],
                source_sha256=target["sha256"],
                parser_fingerprint=self._parser_fingerprint(target["source"]),
                force=force,
            )
            created += 1 if is_new else 0
            jobs.append(self._job_view(job))
        if created:
            self._ensure_worker(store)
        return {
            "submitted": created,
            "new_jobs": created,
            "jobs": jobs,
            "skipped_too_large": skipped_too_large,
            "skipped_ignored": skipped_ignored,
            "storage": "virtual",
        }

    def auto_submit(self) -> dict:
        """自动摄取扫描（virtual）：用**两次源核验**替代 legacy 的双采样判稳。

        为什么不做双采样：virtual 的第二次源核验发生在发布前（hash 变化即废弃候选），
        复制中途的文件必然在发布前被拦下并重新入队；再叠一层采样只会拖慢首摄。
        """
        if not (self.config.enabled and self.config.auto_watch):
            return {"submitted": 0, "new_jobs": 0, "jobs": [], "status": "disabled",
                    "skipped_too_large": 0, "skipped_ignored": 0, "rescan_after_seconds": 0.0}
        store = self._store()
        try:
            matcher = self._matcher()
        except ValueError as exc:
            return {"submitted": 0, "new_jobs": 0, "jobs": [], "status": "fail_closed",
                    "last_error": str(exc), "rescan_after_seconds": _SETTLE_RESCAN_SECONDS,
                    "skipped_too_large": 0, "skipped_ignored": 0}
        pending = self.scan_pending()
        jobs: list[dict] = []
        created = 0
        skipped_seen = 0
        too_large = 0
        for target in pending:
            if target.get("reason") == "too_large":
                too_large += 1
                continue
            if _is_path_ignored(matcher, target["source"]):
                continue
            try:
                self._assert_network_policy(target["source"])
            except ValueError:
                return {"submitted": 0, "new_jobs": 0, "jobs": [], "status": "network_policy_blocked",
                        "rescan_after_seconds": 0.0, "skipped_too_large": too_large,
                        "skipped_ignored": 0}
            job, is_new = store.enqueue_job(
                source=target["source"],
                source_sha256=target["sha256"],
                parser_fingerprint=self._parser_fingerprint(target["source"]),
            )
            if is_new:
                created += 1
                jobs.append(self._job_view(job))
            else:
                skipped_seen += 1
        if created:
            self._ensure_worker(store)
        return {
            "submitted": created,
            "new_jobs": created,
            "jobs": jobs,
            "status": "ok",
            "skipped_too_large": too_large,
            "skipped_seen": skipped_seen,
            "skipped_ignored": 0,
            "rescan_after_seconds": _SETTLE_RESCAN_SECONDS if self._settling_files else 0.0,
            "storage": "virtual",
        }

    def retry(self, job_id: str) -> dict[str, Any]:
        """显式重试既有任务（E06/E02-b）。

        只转发 `DocumentStore.retry_job` 的 failed/cancelled → queued；**结果未知的远端
        任务**（SUBMISSION_UNKNOWN / submission phase）保持原状态，返回 remote 事实与
        next action，绝不强转 queued，也不自动重发。
        """
        from ..doc_store import StoreContractError, StoreConflict

        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("retry requires job_id")
        store = self._store()
        job = store.job_status(job_id)
        if job is None:
            raise ValueError(f"unknown job_id: {job_id}")
        unknown = (str(job.error_code) == "SUBMISSION_UNKNOWN"
                   or str(job.phase) in {"send_intent", "submitted", "polling", "downloaded",
                                         "submission_unknown"})
        if unknown:
            remote_task_id = ""
            for row in store.list_subjobs(job_id):
                if row.ordinal == 0:
                    remote_task_id = row.remote_task_id
                    break
            view = self._job_view(job)
            view.update(retried=False, retryable=False, reason="SUBMISSION_UNKNOWN",
                        remote_task_id=remote_task_id,
                        next_action=("do not resend; query the original remote task for this job "
                                     "and resolve it explicitly (abandon or accept) before retrying"))
            return view
        try:
            updated = store.retry_job(job_id)
        except (StoreContractError, StoreConflict) as exc:
            view = self._job_view(store.job_status(job_id) or job)
            view.update(retried=False, retryable=False, reason=str(exc),
                        next_action="only failed/cancelled jobs can be retried")
            return view
        view = self._job_view(updated)
        view.update(retried=True, retryable=True, reason="",
                    next_action="queued: the worker will pick it up on the next claim")
        self._ensure_worker(store)
        return view

    def status(self, job_id: str | None = None) -> dict:
        store = self._store()
        if job_id:
            job = store.job_status(job_id)
            if job is None:
                raise ValueError(f"unknown job_id: {job_id}")
            return {"job": self._job_view(job)}
        jobs = [self._job_view(job) for job in store.list_jobs(limit=20)]
        summary: dict[str, int] = {}
        for record in store.list_jobs(limit=1000):
            key = record.state
            if key == "done" and record.phase == "committed":
                key = "done"
            summary[key] = summary.get(key, 0) + 1
        return {"summary": summary, "jobs": jobs, "storage": "virtual",
                "queue_depth": store.queue_depth()}

    def cancel(self, job_id: str) -> bool:
        return bool(self._store().cancel_job(job_id))

    def stop(self) -> None:
        self._stop.set()
        thread = self._worker
        if thread is not None:
            thread.join(timeout=5.0)
        self._worker = None

    # ---------------------------------------------------------------- worker

    def _chunker_fingerprint(self) -> str:
        provider = self._chunker_fingerprint_provider
        value = ""
        if provider is not None:
            try:
                value = str(provider() or "")
            except Exception:
                value = ""
        # 未注入时用显式占位，而不是空串：接入真实 chunker 指纹后自然区分代际。
        return value or "chunker-fingerprint-unwired"

    def _parser_fingerprint(self, source: str = "") -> str:
        """入队时的 job 指纹（用于「同源同 SHA 同 parser/profile 合并」，§12.2）。

        通道在领取时才判定，故 `channel="auto"`。**必须**纳入 routing/network_policy
        与 chunker/profile 指纹：否则同内容不同 profile 会被 `enqueue_job` 误合并成
        同一 job。真正的解析事实指纹仍由 `parse_structured()`/`parse_local()` 产出并
        写进 `document_revisions.parser_fingerprint`（本函数不是它）。

        E09：音频任务必须额外纳入**音频处理 profile**（分段/重叠/解码器/转录 adapter）。
        只对音频源追加，文档源的指纹值保持不变——否则已入库文档会被判成「新 profile」
        而在下次 submit 时重复入队（可能真实计费）。
        """
        from .mineru import ADAPTER_VERSION

        cfg = self.config
        parts = [
            "job-v1",
            "adapter=" + str(ADAPTER_VERSION),
            "channel=auto",
            "model=" + str(getattr(cfg, "model_version", "vlm")),
            "language=" + str(getattr(cfg, "language", "ch")),
            "ocr=" + ("1" if getattr(cfg, "is_ocr", False) else "0"),
            "table=" + ("1" if getattr(cfg, "enable_table", True) else "0"),
            "formula=" + ("1" if getattr(cfg, "enable_formula", True) else "0"),
            "routing=" + str(getattr(cfg, "routing", "auto")),
            "network=" + str(getattr(cfg, "network_policy", "configured")),
            "chunker=" + self._chunker_fingerprint(),
        ]
        suffix = Path(source).suffix.lower() if source else ""
        if suffix in AUDIO_EXTS:
            parts.append("audio=" + audio_profile_fingerprint(
                self._audio_settings(), adapter=self.audio_adapter, decoder=self.audio_decoder))
        elif suffix in IMAGE_EXTS:
            parts.append("image=image-source-v1")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------- audio (C104)

    def _audio_resume(self, store: Any, job: Any) -> dict[int, dict[str, Any]]:
        """读回已完成片段的 checkpoint（resume **只补缺失片段**）。

        真实读回面是 `DocumentStore.list_subjobs(job_id)`（E02）。ordinal 0 是父任务的
        远端 submission 阶段，不参与段级恢复；只有 `state == 'done'` 且父源 SHA 未变的
        记录才可复用。**坏记录不再静默回退成首次解析**（那会重复计费）。
        """
        reader = getattr(store, "list_subjobs", None)
        if reader is None:
            return {}
        rows = reader(job.job_id)
        resumed: dict[int, dict[str, Any]] = {}
        job_sha = str(getattr(job, "source_sha256", "") or "")
        for row in rows or []:
            ordinal = int(getattr(row, "ordinal", 0) or 0)
            if ordinal < 1:
                continue  # 父任务远端阶段，不是段级 checkpoint
            if str(getattr(row, "state", "") or "") != "done":
                continue
            raw = getattr(row, "checkpoint", "") or ""
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"audio checkpoint for ordinal {ordinal} is corrupt; refusing to resume "
                    "silently (that would re-run confirmed transcripts)"
                ) from exc
            if not isinstance(payload, dict) or not str(payload.get("sha256") or ""):
                raise ValueError(f"audio checkpoint for ordinal {ordinal} lacks segment evidence")
            parent = str(payload.get("parent_source_sha256") or "")
            if parent and job_sha and parent != job_sha:
                continue  # 父源已变，这条记录不能复用
            resumed[ordinal] = dict(payload)
        return resumed

    def _audio_checkpoint(self, store: Any, job: Any, owner: str, segment: Any,
                          transcript: str, media: dict[str, Any] | None = None) -> None:
        """逐片段记录进度（partial coverage 的事实来源），供重试/恢复只补缺失片段。

        `media` 是该片段已落库媒体的定位（occurrence_id/blob_id/...），E02 恢复要靠它
        把**已确认**的媒体重新挂回新 revision，而不是重新 sink 一份。
        """
        progress = self._audio_progress.setdefault(job.job_id, {})
        item: dict[str, Any] = {
            "ordinal": int(segment.ordinal),
            "t_start_ms": int(segment.t_start_ms),
            "t_end_ms": int(segment.t_end_ms),
            "sha256": segment.input_sha256,
            "text": transcript,
        }
        if media:
            payload = {key: value for key, value in dict(media).items() if value not in (None, "")}
            item["media"] = payload
            if payload.get("blob_id"):
                item["blob_id"] = str(payload["blob_id"])
        progress[int(segment.ordinal)] = item
        self._persist_audio_checkpoint(store, job, owner, progress)

    @staticmethod
    def _persist_audio_checkpoint(store: Any, job: Any, owner: str,
                                  progress: dict[int, dict[str, Any]]) -> None:
        """逐 ordinal 写段级 checkpoint（E02）：完整 JSON、不截断、写前限额拒绝。

        写失败**不再静默吞掉**：吞掉之后进程会继续转录，崩溃恢复时这些片段没有
        记录 → 重复转录与重复 sink（重复计费）。
        """
        recorder = getattr(store, "record_subjob", None)
        if recorder is None:
            raise AttributeError(
                "checkpoint persistence unavailable: the store does not expose record_subjob()"
            )
        for ordinal in sorted(progress):
            item = dict(progress[ordinal])
            payload = dict(item)
            payload["parent_source_sha256"] = str(getattr(job, "source_sha256", "") or "")
            payload["parser_fingerprint"] = str(getattr(job, "parser_fingerprint", "") or "")
            recorder(job.job_id, owner, ordinal=int(ordinal),
                     input_hash=str(item.get("sha256") or ""),
                     range={"kind": "audio_ms", "start": int(item.get("t_start_ms") or 0),
                            "end": int(item.get("t_end_ms") or 0)},
                     state="done", checkpoint=payload)

    @staticmethod
    def _resumed_occurrences(resume_state: dict[int, dict[str, Any]],
                             sink: Any) -> list[Any]:
        """把已确认片段的媒体定位重组成 occurrence（E02：不重复 sink，但必须复挂）。"""
        from ..doc_store import MediaOccurrenceSpec

        known = {str(getattr(item, "occurrence_id", "") or "")
                 for item in getattr(sink, "items", []) or []}
        specs: list[Any] = []
        for ordinal in sorted(resume_state):
            item = resume_state[ordinal]
            media = item.get("media") if isinstance(item, dict) else None
            if not isinstance(media, dict):
                continue
            occurrence_id = str(media.get("occurrence_id") or "")
            blob_id = str(media.get("blob_id") or "")
            if not occurrence_id or not blob_id or occurrence_id in known:
                continue
            specs.append(MediaOccurrenceSpec(
                occurrence_id=occurrence_id, blob_id=blob_id,
                kind=str(media.get("kind") or "audio"), ordinal=int(ordinal),
                mime_type=str(media.get("mime_type") or "audio/wav"),
                caption=str(media.get("caption") or item.get("text") or ""),
                t_start_ms=int(media.get("t_start_ms") or item.get("t_start_ms") or 0),
                t_end_ms=int(media.get("t_end_ms") or item.get("t_end_ms") or 0),
                metadata=dict(media.get("metadata") or {}),
            ))
            known.add(occurrence_id)
        return specs

    def _client_or_make(self) -> MineruClient:
        if self._client is None:
            cfg = self.config
            self._client = MineruClient(
                cfg.api_key, model_version=cfg.model_version, language=cfg.language,
                is_ocr=cfg.is_ocr, enable_formula=cfg.enable_formula,
                enable_table=cfg.enable_table, limits=self._limits(),
            )
        return self._client

    def _ensure_worker(self, store: Any) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._loop, daemon=True,
                                            name="ingest-virtual-worker")
            self._worker.start()

    # ------------------------------------------------------ parse 编排 (E09)

    def _parse_audio_job(self, store: Any, job: Any, owner: str, src: Path, sink: Any) -> Any:
        """音频：resume 只补缺失片段，checkpoint 逐片段持久化（partial coverage 事实）。

        真实 `AudioConfig`/解码组件沿 `_audio_settings()` 贯通到 `parse_audio`：不同分段/
        重叠/解码 profile 会产生不同片段 SHA 与 parser 指纹，因此不会误复用旧片段或旧任务。
        """
        resume_state = self._audio_resume(store, job)
        result = parse_audio(
            src, self._audio_settings(), sink=sink, adapter=self._audio_adapter(),
            cancelled=self._stop.is_set, resume=resume_state, decoder=self.audio_decoder,
            checkpoint=lambda segment, transcript, media=None: self._audio_checkpoint(
                store, job, owner, segment, transcript, media))
        # 已确认片段的媒体必须重新挂回本次 revision（不重新 sink）。
        if resume_state:
            sink.items.extend(self._resumed_occurrences(resume_state, sink))
        return result

    def _parse_image_job(self, job: Any, src: Path, sink: Any) -> Any:
        """显式提交的图片源：单项有界 occurrence + 只有用户事实的代理正文（不做 OCR）。"""
        return parse_image(src, limits=self._limits(), sink=sink,
                           title=self._image_captions.get(job.source, ""), ordinal=1)

    def _parse_document_job(self, store: Any, job: Any, owner: str, src: Path, sink: Any,
                            record: Any) -> Any:
        """文档：E10 决定路由；本地质量复检失败时**最多一次**升级（不重定质量阈值）；
        本地 PDF 超页数预算时有界内存分卷（不升云、不落临时文件）。"""
        try:
            decision = decide_route(src, self.config, limits=self._limits())
        except LocalUnsupported as exc:
            result = self._run_local_volumes_or_raise(store, job, owner, src, exc)
            if result is None:
                raise
            return result
        if decision.route == "local":
            try:
                result = parse_local(src, limits=self._limits(),
                                     max_pages=getattr(self.config, "local_max_pages", 300),
                                     cancelled=self._stop.is_set)
            except LocalUnsupported as exc:
                upgraded = self._upgrade_after_local_failure(decision, exc)
                if upgraded is not None:
                    decision = upgraded
                    result = self._cloud_parse(job, src, sink, record)
                    result.capabilities["route_decision"] = decision.as_dict()
                    return result
                # 页数预算超限（decide_route 的探测会吞掉自己的预算异常，这里兜住
                # parse_local 的 "page limit exceeded"）→ 有界分卷，不升云。
                result = self._run_local_volumes_or_raise(store, job, owner, src, exc)
                if result is None:
                    raise
                return result
        else:
            result = self._cloud_parse(job, src, sink, record)
        result.capabilities["route_decision"] = decision.as_dict()
        return result

    #: 页数预算超限的稳定标记（加密 PDF 不是分卷理由）。
    _VOLUME_SPLIT_MARKERS = ("local page budget exceeded", "page limit exceeded")

    def _run_local_volumes_or_raise(self, store: Any, job: Any, owner: str, src: Path,
                                    exc: LocalUnsupported) -> Any | None:
        """本地 PDF 超页数预算 → 有界分卷（消费 E02 通用子记录）；否则 None 照常失败。"""
        if not any(marker in str(exc) for marker in self._VOLUME_SPLIT_MARKERS):
            return None
        if src.suffix.lower() != ".pdf" or self._stop.is_set():
            return None
        resume = self._volume_resume(store, job)
        result = parse_local_volumes(
            src, limits=self._limits(),
            max_pages=getattr(self.config, "local_max_pages", 300),
            cancelled=self._stop.is_set, resume=resume,
            source_sha256=str(getattr(job, "source_sha256", "") or ""),
            checkpoint=lambda ordinal, rng, payload: self._volume_checkpoint(
                store, job, owner, ordinal, rng, payload))
        result.capabilities["route_decision"] = {
            "route": "local", "initial_route": "local", "upgrade_count": 0,
            "confidence": 0.4, "partial": True, "failed_pages": [],
            "reasons": ("bounded in-memory volume split of local PDF",),
            "volumes": result.capabilities.get("volumes_total", 0),
        }
        return result

    def _volume_resume(self, store: Any, job: Any) -> dict[int, dict[str, Any]]:
        """读回已确认卷的 checkpoint（E02 `list_subjobs`；坏记录拒绝静默续跑）。"""
        reader = getattr(store, "list_subjobs", None)
        if reader is None:
            return {}
        rows = reader(job.job_id)
        resume: dict[int, dict[str, Any]] = {}
        for row in rows or []:
            ordinal = int(getattr(row, "ordinal", 0) or 0)
            if ordinal < 1 or str(getattr(row, "state", "") or "") != "done":
                continue
            raw = getattr(row, "checkpoint", "") or ""
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"volume checkpoint for ordinal {ordinal} is corrupt; refusing to resume "
                    "silently (that would redo confirmed volumes)") from exc
            if not isinstance(payload, dict) or str(payload.get("kind") or "") != "document_pages":
                continue
            resume[ordinal] = payload
        return resume

    @staticmethod
    def _volume_checkpoint(store: Any, job: Any, owner: str, ordinal: int, rng: tuple[int, int],
                           payload: dict[str, Any]) -> None:
        """逐卷写 E02 子记录（`document_pages` 0-based 半开页范围；失败不静默）。"""
        store.record_subjob(job.job_id, owner, ordinal=int(ordinal),
                            input_hash=str(payload.get("sha256") or ""),
                            range={"kind": "document_pages", "start": int(rng[0]),
                                   "end": int(rng[1])},
                            state="done", checkpoint=payload)

    def _cloud_parse(self, job: Any, src: Path, sink: Any, record: Any) -> Any:
        return self._client_or_make().parse_structured(
            src, poll_interval=float(getattr(self.config, "poll_interval", 3.0)),
            poll_timeout=float(getattr(self.config, "poll_timeout", 600.0)),
            sink=sink, intent_recorder=record, request_id=job.job_id)

    #: 本地解析**质量复检**失败 → E10 允许升级的唯一理由标记。取消/预算/加密/依赖缺失
    #: 不是质量理由：这些必须如实失败，不能被 cloud fallback 掩盖（也不重复上传）。
    _LOCAL_QUALITY_MARKERS = ("local quality revalidation failed",)

    def _upgrade_after_local_failure(self, decision: Any, exc: Exception) -> Any:
        """消费 E10 `upgrade_route_once` 的「最多一次」升级；不允许时返回 None。"""
        if not any(marker in str(exc) for marker in self._LOCAL_QUALITY_MARKERS):
            return None
        try:
            return upgrade_route_once(decision, self.config,
                                      quality_reasons=("empty_or_garbled",),
                                      failed_pages=tuple(getattr(decision, "failed_pages", ()) or ()))
        except LocalUnsupported:
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            store = self._store()
            owner = f"w-{uuid.uuid4().hex[:12]}"
            job = store.claim_job(owner, lease_seconds=self._lease_seconds)
            if job is None:
                return
            try:
                self._run_job(store, job, owner)
            except Exception as exc:  # 单 job 失败不拖垮队列
                try:
                    store.fail_job(job.job_id, owner,
                                   error_code=str(getattr(exc, "code_str", "") or type(exc).__name__),
                                   error_summary=str(exc)[:500],
                                   retryable=bool(getattr(exc, "retryable", False)))
                except Exception:
                    pass
            finally:
                # 逐 job 清理音频进度缓存（已通过 checkpoint 持久化的事实不丢）。
                self._audio_progress.pop(job.job_id, None)

    def _run_job(self, store: Any, job: Any, owner: str) -> None:
        src = _validate_safe_source(self.vault_path, job.source)
        stat_before = src.stat()
        if not self._check_file_size(stat_before.st_size):
            raise ValueError(
                f"file size {stat_before.st_size} bytes exceeds limit {self._size_limit_bytes()} bytes"
            )
        # 第一次源核验（解析前）：入队 hash 不符 → 废弃，不上传
        if _sha256(src) != job.source_sha256:
            store.fail_job(job.job_id, owner, error_code="SOURCE_CHANGED",
                           error_summary="source_changed before parse", retryable=False)
            return

        self._assert_network_policy(job.source)
        matcher = self._matcher()
        if matcher is not None and matcher.is_ignored(job.source)[0]:
            raise ValueError("source exempt before parse")
        store.renew_lease(job.job_id, owner, lease_seconds=self._lease_seconds)
        sink = StoreMediaSink(store, job_id=job.job_id, owner_token=owner)

        def _record(kind: str, payload: dict[str, Any]) -> None:
            store.report_phase(job.job_id, owner, phase=kind,
                               remote_task_id=str(payload.get("remote_task_id") or ""),
                               checkpoint=str(payload.get("payload_hash") or ""))

        with self.parse_budget.reserve(self._limits().memory_budget_bytes, self._stop.is_set):
            suffix = src.suffix.lower()
            if suffix in AUDIO_EXTS:
                result = self._parse_audio_job(store, job, owner, src, sink)
            elif suffix in IMAGE_EXTS:
                result = self._parse_image_job(job, src, sink)
            else:
                result = self._parse_document_job(store, job, owner, src, sink, _record)
        self._assert_network_policy(job.source)
        matcher = self._matcher()
        if matcher is not None and matcher.is_ignored(job.source)[0]:
            raise ValueError("source exempt before publication")
        # 第二次源核验（发布前）：stat 或 hash 变了 → 废弃候选并重新扫描
        stat_after = src.stat()
        if (stat_after.st_size, stat_after.st_mtime_ns) != (stat_before.st_size, stat_before.st_mtime_ns):
            store.fail_job(job.job_id, owner, error_code="SOURCE_CHANGED",
                           error_summary="source changed while parsing", retryable=False)
            return
        if _sha256(src) != job.source_sha256:
            store.fail_job(job.job_id, owner, error_code="SOURCE_CHANGED",
                           error_summary="source hash changed while parsing", retryable=False)
            return
        # 续租后再发布（跨越长时间解析后的 fencing）
        store.renew_lease(job.job_id, owner, lease_seconds=self._lease_seconds)

        capabilities = dict(result.capabilities)
        capabilities["page_spans"] = [
            {"page": span.page, "char_start": span.char_start, "char_end": span.char_end}
            for span in result.page_map
        ]
        capabilities["warnings"] = list(result.warnings)
        capabilities["parser_fingerprint"] = result.parser_fingerprint
        capabilities["duration_ms"] = round(result.duration_ms, 3)

        # E16：媒体 occurrence 原生能力核验 hunk
        media_items = [it for it in sink.items if getattr(it, "kind", "") in ("image", "audio")]
        if media_items:
            if self.media_provider is None:
                capabilities["warnings"].append(
                    "native media capability unavailable: media_provider not configured (falling back to proxy / text recall)"
                )
                capabilities["media_route"] = "proxy"
            else:
                try:
                    ability = self.media_provider.ability()
                    allowed_mimes = set(ability.get("allowed_mime_types", ()))
                    modalities = set(ability.get("modalities", ()))

                    unsupported: list[str] = []
                    for it in media_items:
                        kind = getattr(it, "kind", "")
                        mime = getattr(it, "mime_type", "")
                        if kind not in modalities:
                            unsupported.append(f"{kind} modality not supported by media_provider")
                        elif mime not in allowed_mimes:
                            unsupported.append(f"{mime} not in media_provider allowed_mime_types")
                    if unsupported:
                        for err in unsupported:
                            capabilities["warnings"].append(
                                f"native media capability unavailable: {err} (falling back to proxy / text recall)"
                            )
                        capabilities["media_route"] = "proxy"
                    else:
                        capabilities["media_route"] = "native"
                except Exception as exc:
                    capabilities["warnings"].append(
                        f"native media capability verification failed: {exc} (falling back to proxy / text recall)"
                    )
                    capabilities["media_route"] = "proxy"

        staged = store.stage_revision(
            source=job.source,
            source_sha256=job.source_sha256,
            render_sha256=_sha256_text(result.markdown),
            parser_fingerprint=result.parser_fingerprint,
            markdown=result.markdown,
            page_map=[span.page for span in result.page_map],
            quality=result.quality,
            capabilities=capabilities,
            source_size=stat_after.st_size,
            source_mtime_ns=stat_after.st_mtime_ns,
            job_id=job.job_id,
            owner_token=owner,
        )
        if sink.items:
            store.attach_occurrences(staged.revision_id, sink.items,
                                     job_id=job.job_id, owner_token=owner)
        store.commit_job_revision(
            job.job_id, owner, staged.revision_id, source_sha256=job.source_sha256,
            source_size=stat_after.st_size, source_mtime_ns=stat_after.st_mtime_ns,
        )
        store.record_auto_seen(
            job.source, source_sha256=job.source_sha256, last_job_id=job.job_id,
            state="done", source_size=stat_after.st_size, source_mtime_ns=stat_after.st_mtime_ns,
        )
        if self.on_job_finished is not None:
            try:
                self.on_job_finished(job.source, staged.revision_id)
            except Exception:
                pass


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_ingest_manager(
    vault_path: str | Path,
    config: Any,
    *,
    store_provider: Callable[[], Any] | None = None,
    on_job_finished: Callable[..., None] | None = None,
    ignore_provider: Callable[[], Any] | None = None,
    parse_budget: ParseBudget | None = None,
    audio_adapter: Any = None,
    chunker_fingerprint_provider: Callable[[], str] | None = None,
    audio_config: Any = None,
    audio_decoder: Any = None,
    media_provider: Any = None,
) -> Any:
    """按 `ingest.storage` 选择摄取实现（唯一的路径分派点）。

    `virtual` 需要 store 可用（门禁在 `DocumentStore.open(write=True)` 与
    `layout.writable`）；拿不到 store 时**回落 legacy** 并保持可诊断，
    绝不「半虚拟」地写盘。

    E09：`audio_config`/`audio_adapter`/`audio_decoder`/`chunker_fingerprint_provider`
    只在 virtual 路径消费（legacy 路径不落 store，音频/图片明确拒绝）。legacy 分支不接
    这几个参数也不静默丢弃——它本来就不具备音频/图片出口。
    """
    storage = str(getattr(config, "storage", "legacy") or "legacy")
    if storage == "virtual" and store_provider is not None:
        return VirtualIngestWorker(
            vault_path, config, store_provider,
            on_job_finished=on_job_finished, ignore_provider=ignore_provider,
            parse_budget=parse_budget, audio_adapter=audio_adapter,
            chunker_fingerprint_provider=chunker_fingerprint_provider,
            audio_config=audio_config, audio_decoder=audio_decoder,
            media_provider=media_provider,
        )
    return IngestManager(
        vault_path, config, on_job_finished=on_job_finished, ignore_provider=ignore_provider
    )
