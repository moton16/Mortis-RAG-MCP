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
import threading
import time
import uuid
from pathlib import Path

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

INGEST_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
_STATE_NAME = ".ingest_state.json"


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


class IngestManager:
    def __init__(self, vault_path: str | Path, config: IngestConfig):
        self.vault_path = Path(vault_path).expanduser().resolve()
        self.config = config
        self.out_root = self.vault_path / config.output_dirname
        self.state_path = self.out_root / _STATE_NAME
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._client: MineruClient | None = None

    # ------------------------------------------------------------ state

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "jobs": {}}

    def _save_state(self, state: dict) -> None:
        _atomic_write_json(self.state_path, state)

    # ------------------------------------------------------------ public

    def scan_pending(self) -> list[dict]:
        """列出待解析文档：未解析过的，或源文件 sha256 已变化的。"""
        state = self._load_state()
        done = {j["source"]: j for j in state["jobs"].values() if j.get("state") == "done"}
        pending: list[dict] = []
        if not self.vault_path.exists():
            return pending
        out_dirname = self.config.output_dirname.strip("/\\")
        for path in sorted(self.vault_path.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in INGEST_EXTS:
                continue
            rel = path.relative_to(self.vault_path).as_posix()
            if rel == out_dirname or rel.startswith(out_dirname + "/"):  # 产物目录不摄取
                continue
            digest = _sha256(path)
            old = done.get(rel)
            if old is None or old.get("sha256") != digest:
                pending.append({"source": rel, "sha256": digest,
                                "reason": "new" if old is None else "changed"})
        return pending

    def submit(self, sources: list[str] | None = None) -> dict:
        """提交摄取任务（异步）。sources 为库内相对路径列表；None → scan_pending() 全量。"""
        if not self.config.enabled:
            raise ValueError(
                "ingest disabled: PDF 摄取层默认关闭。请在 config/app.toml 设置 "
                "[ingest] enabled = true 并重启 MCP 服务后重试。"
            )
        if sources is None:
            targets = self.scan_pending()
        else:
            targets = []
            for rel in sources:
                path = (self.vault_path / rel).resolve()
                if not path.is_file() or path.suffix.lower() not in INGEST_EXTS:
                    raise ValueError(f"not an ingestible document: {rel}")
                targets.append({"source": Path(rel).as_posix(), "sha256": _sha256(path),
                                "reason": "explicit"})
        with self._lock:
            state = self._load_state()
            jobs = []
            for t in targets:
                job_id = uuid.uuid4().hex[:12]
                state["jobs"][job_id] = {
                    "job_id": job_id, "source": t["source"], "sha256": t["sha256"],
                    "state": "queued", "channel": None, "error": "",
                    "submitted_at": time.time(), "finished_at": None, "output": None,
                    "parse_quality": None,
                }
                jobs.append(state["jobs"][job_id])
            self._save_state(state)
            if jobs and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                                name="ingest-worker")
                self._worker.start()
        return {"submitted": len(jobs), "jobs": jobs}

    def status(self, job_id: str | None = None) -> dict:
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
            summary[st] = summary.get(st, 0) + 1
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
            with self._lock:
                state = self._load_state()
                job = next((j for j in state["jobs"].values() if j["state"] == "queued"), None)
                if job is None:
                    return
                job["state"] = "parsing"
                self._save_state(state)
            try:
                self._run_job(job)
                job["state"] = "done"
                job["finished_at"] = time.time()
            except Exception as exc:  # 单 job 失败不拖垮队列
                job["state"] = "failed"
                job["error"] = str(exc)[:500]
                job["finished_at"] = time.time()
            finally:
                with self._lock:
                    state = self._load_state()
                    state["jobs"][job["job_id"]] = job
                    self._save_state(state)

    def _run_job(self, job: dict) -> None:
        src = self.vault_path / job["source"]
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
                markdown = markdown.replace("images/", f"{rel.stem}.assets/")
        except MineruError as exc:
            if not self.config.pymupdf_fallback:
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
        tmp.write_text(header + markdown, encoding="utf-8")
        tmp.replace(out_md)
        job["output"] = out_md.relative_to(self.vault_path).as_posix()

    @staticmethod
    def _pymupdf_fallback(path: Path) -> str:
        """云端全挂时的本地纯文本兜底（可选依赖；版面/表格/公式无保障）。"""
        try:
            import pymupdf  # type: ignore
        except ImportError as exc:
            try:
                import fitz as pymupdf  # type: ignore
            except ImportError:
                raise MineruError("cloud channels failed and pymupdf not installed") from exc
        doc = pymupdf.open(str(path))
        return "\n\n".join(page.get_text() for page in doc)
