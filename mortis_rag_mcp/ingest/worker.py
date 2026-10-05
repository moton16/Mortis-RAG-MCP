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

INGEST_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
_STATE_NAME = ".ingest_state.json"
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
    if resolved.suffix.lower() not in INGEST_EXTS:
        raise ValueError(f"not an ingestible document ({resolved.suffix}): {rel_path_str}")
    return resolved


def _is_excluded_dir_name(name: str, out_dirname: str) -> bool:
    if name in _EXCLUDED_DIR_NAMES or name == out_dirname:
        return True
    if name in {".assets", "assets", ".temp", "temp", ".tmp", "tmp"} or name.endswith(".assets"):
        return True
    return False


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

        # 启动时自动恢复僵尸 parsing 任务 (D10a)
        self._recover_zombie_jobs()

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
                                if path.suffix.lower() not in INGEST_EXTS:
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

    def submit(self, sources: list[str] | None = None, force: bool = False) -> dict:
        """提交摄取任务（异步）。sources 为库内相对路径列表；None → scan_pending() 全量。
        支持 force 强制重解析 (D7)；去重 (D11)；参数严格校验 (D1, D12)；尺寸上限统一闸门。
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
            scan_stats["clean_scan"] = True
            return [], scan_stats

        state = self._load_state()
        auto_seen = state.get("auto_seen", {})

        matcher = self.ignore_provider() if self.ignore_provider is not None else None
        out_dirname = self.out_root.name
        limit = self._size_limit_bytes()

        targets: list[dict] = []
        scanned_sources: set[str] = set()
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
                                    continue
                                entries_to_visit.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                path = Path(entry.path)
                                if path.suffix.lower() not in INGEST_EXTS:
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

            if scan_stats.get("clean_scan") and scan_stats.get("scanned_sources") is not None:
                scanned_set = scan_stats["scanned_sources"]
                for s in list(auto_seen.keys()):
                    if s not in scanned_set and s not in active_sources:
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
        st = src.stat()
        if not self._check_file_size(st.st_size):
            limit = self._size_limit_bytes()
            raise ValueError(f"file size {st.st_size} bytes exceeds limit {limit} bytes: {job['source']}")
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
