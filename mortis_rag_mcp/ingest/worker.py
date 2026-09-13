"""摄取任务管理器：按需异步解析 vault 里的 PDF/Office 文档。

硬性设计：
- 不做 watcher 自动摄取；只有 submit() 被显式调用才启动后台线程。
- 产物写 <vault>/<output_dirname>/（默认 .mortis-parsed/），镜像源相对路径。
- 状态文件 <output_dirname>/.ingest_state.json（原子写：tmp+replace，与 registry 同款）。
- 幂等：state 里记录源文件 sha256；未变 → skip；变了 → 重解析覆盖。
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
from typing import Callable

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


class IngestManager:
    def __init__(
        self,
        vault_path: str | Path,
        config: IngestConfig,
        on_job_finished: Callable[[str, Path], None] | None = None,
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

        # 启动时自动恢复僵尸 parsing 任务 (D10a)
        self._recover_zombie_jobs()

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
            if changed:
                self._save_state(state)

    # ------------------------------------------------------------ state

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "jobs": {}}

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
        """
        state = self._load_state()
        done = {j["source"]: j for j in state["jobs"].values() if j.get("state") == "done"}
        pending: list[dict] = []
        if not self.vault_path.exists():
            return pending

        out_dirname = self.out_root.name
        entries_to_visit = [self.vault_path]
        while entries_to_visit:
            curr_dir = entries_to_visit.pop()
            try:
                with os.scandir(curr_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name in _EXCLUDED_DIR_NAMES or entry.name == out_dirname:
                                    continue
                                entries_to_visit.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                path = Path(entry.path)
                                if path.suffix.lower() not in INGEST_EXTS:
                                    continue
                                rel = path.relative_to(self.vault_path).as_posix()
                                st = entry.stat()
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
        支持 force 强制重解析 (D7)；去重 (D11)；参数严格校验 (D1, D12)。
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
            targets = self.scan_pending()
        else:
            targets = []
            for rel in sources:
                path = _validate_safe_source(self.vault_path, rel)
                st = path.stat()
                targets.append({
                    "source": Path(rel).as_posix(),
                    "sha256": _sha256(path),
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "reason": "explicit",
                })

        with self._lock, _process_file_lock(self._lock_path):
            state = self._load_state()
            active_sources = {
                j["source"]: j for j in state["jobs"].values()
                if j.get("state") in ("queued", "parsing")
            }
            jobs = []
            new_job_count = 0
            for t in targets:
                src = t["source"]
                if not force and src in active_sources:
                    jobs.append(active_sources[src])
                    continue
                job_id = uuid.uuid4().hex[:12]
                new_job = {
                    "job_id": job_id, "source": src, "sha256": t["sha256"],
                    "mtime": t.get("mtime"), "size": t.get("size"),
                    "state": "queued", "channel": None, "error": "",
                    "submitted_at": time.time(), "finished_at": None, "output": None,
                    "parse_quality": None,
                }
                state["jobs"][job_id] = new_job
                active_sources[src] = new_job
                jobs.append(new_job)
                new_job_count += 1

            self._save_state(state)
            if any(j["state"] == "queued" for j in jobs) and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="ingest-worker")
                self._worker.start()

        return {"submitted": new_job_count, "new_jobs": new_job_count, "jobs": jobs}

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
        return {"summary": summary, "jobs": jobs[:20]}

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
                    self._save_state(state)

    def _run_job(self, job: dict) -> None:
        # D1: run_job 入口再次校验沙箱
        src = _validate_safe_source(self.vault_path, job["source"])
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
