"""MinerU 云端解析客户端（纯标准库 urllib，零第三方依赖）。

双通道（官方文档 https://mineru.net/apiManage/docs 已核实）：
- v4 精准 API（需 token）：POST /api/v4/file-urls/batch 申请上传链接 →
  PUT 上传文件（不设 Content-Type）→ GET /api/v4/extract-results/batch/{batch_id}
  轮询 → state=done 后下载 full_zip_url（zip，内含 full.md）。
  限制：≤200MB、≤200页；每天 1000 页高优先级额度。
- Agent 轻量 API（免 token）：POST /api/v1/agent/parse/file 得 (task_id, file_url) →
  PUT 上传 → GET /api/v1/agent/parse/{task_id} 轮询 → done 后下载 markdown_url。
  限制：≤10MB、≤20页、IP 限频（超限 HTTP 429）；仅 PDF/图片/DOCX/PPTX/XLSX。
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

V4_BASE = "https://mineru.net/api/v4"
AGENT_BASE = "https://mineru.net/api/v1/agent"
V4_MAX_BYTES = 200 * 1024 * 1024
AGENT_MAX_BYTES = 10 * 1024 * 1024
# v4 全格式；Agent 接口不含老 Office 三件套（doc/ppt/xls）
V4_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
AGENT_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}
# 终态错误码：额度/超限类不值得退避重试，直接失败让上层降级
_V4_FATAL_CODES = {-60005, -60006, -60017, -60018, -60019}  # 大小/页数超限、重试上限、每日额度
_AGENT_FATAL_CODES = {-30001, -30002, -30003, -30004}


class MineruError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


@dataclass(slots=True)
class ParsedDocument:
    markdown: str
    images: dict[str, bytes]   # 图片相对路径（如 images/xxx.jpg）→ 字节；Agent 通道恒为空
    channel: str               # "v4" | "agent"
    model: str                 # 实际使用的模型版本


def _http_json(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code == 429:
            try:
                retry_after = float(exc.headers.get("Retry-After", "60"))
            except (ValueError, TypeError):
                retry_after = 60.0
            raise MineruError(f"rate limited (429), retry after {retry_after}s",
                              http_status=429, retryable=True) from exc
        raise MineruError(f"HTTP {exc.code}: {body}", http_status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"network error: {exc}", retryable=True) from exc


def _http_bytes(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"download failed: {exc}", retryable=True) from exc


def _put_upload(url: str, payload: bytes, timeout: float) -> None:
    # 官方文档明确：上传无须设置 Content-Type
    req = urllib.request.Request(url, data=payload, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201):
                raise MineruError(f"upload failed: HTTP {resp.status}", http_status=resp.status)
    except urllib.error.HTTPError as exc:
        raise MineruError(f"upload failed: HTTP {exc.code}", http_status=exc.code,
                          retryable=exc.code >= 500) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MineruError(f"upload network error: {exc}", retryable=True) from exc


class MineruClient:
    def __init__(self, api_key: str = "", *, model_version: str = "vlm", language: str = "ch",
                 is_ocr: bool = False, enable_formula: bool = True, enable_table: bool = True,
                 timeout: float = 30.0):
        self.api_key = api_key.strip()
        self.model_version = model_version
        self.language = language
        self.is_ocr = is_ocr
        self.enable_formula = enable_formula
        self.enable_table = enable_table
        self.timeout = timeout

    # ---------------------------------------------------------------- public

    def channel_for(self, path: Path) -> str:
        """通道选择：有 token 且格式/大小合规 → v4；无 token 且 ≤10MB 且 Agent 格式 → agent；
        都不满足抛 MineruError（上层决定是否 pymupdf 兜底）。"""
        ext = path.suffix.lower()
        size = path.stat().st_size
        if self.api_key:
            if ext not in V4_EXTS:
                raise MineruError(f"v4 unsupported ext: {ext}", code=-60002)
            if size > V4_MAX_BYTES:
                raise MineruError(f"file exceeds v4 200MB limit: {size}", code=-60005)
            return "v4"
        if ext not in AGENT_EXTS:
            raise MineruError(f"agent channel unsupported ext (need v4 token): {ext}", code=-30002)
        if size > AGENT_MAX_BYTES:
            raise MineruError(f"file exceeds agent 10MB limit (need v4 token): {size}", code=-30001)
        return "agent"

    def parse(self, path: Path, *, poll_interval: float = 3.0, poll_timeout: float = 600.0) -> ParsedDocument:
        channel = self.channel_for(path)
        if channel == "v4":
            return self._parse_v4(path, poll_interval=poll_interval, poll_timeout=poll_timeout)
        return self._parse_agent(path, poll_interval=poll_interval, poll_timeout=poll_timeout)

    # ---------------------------------------------------------------- v4

    def _v4_headers(self) -> dict:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def _parse_v4(self, path: Path, *, poll_interval: float, poll_timeout: float) -> ParsedDocument:
        # 1) 申请上传链接（本地文件走 file-urls/batch；单次 ≤50 个，本客户端一次一文件一批）
        body = {
            "files": [{
                "name": path.name,
                "data_id": path.stem[:120],
                "is_ocr": self.is_ocr,
            }],
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        req = urllib.request.Request(f"{V4_BASE}/file-urls/batch",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=self._v4_headers(), method="POST")
        result = _http_json(req, self.timeout)
        if result.get("code") != 0:
            raise MineruError(f"v4 apply upload url failed: {result.get('msg')}",
                              code=result.get("code"),
                              retryable=result.get("code") not in _V4_FATAL_CODES)
        batch_id = result["data"]["batch_id"]
        upload_url = result["data"]["file_urls"][0]
        # 2) PUT 上传（系统自动提交解析任务）
        _put_upload(upload_url, path.read_bytes(), max(self.timeout, 300.0))
        # 3) 轮询批量结果
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            poll_req = urllib.request.Request(f"{V4_BASE}/extract-results/batch/{batch_id}",
                                              headers=self._v4_headers())
            data = _http_json(poll_req, self.timeout)
            if data.get("code") != 0:
                raise MineruError(f"v4 poll failed: {data.get('msg')}", code=data.get("code"))
            item = (data["data"].get("extract_result") or [{}])[0]
            state = item.get("state", "")
            if state == "done":
                zip_bytes = _http_bytes(item["full_zip_url"], max(self.timeout, 300.0))
                markdown, images = self._extract_zip(zip_bytes)
                return ParsedDocument(markdown=markdown, images=images,
                                      channel="v4", model=self.model_version)
            if state == "failed":
                raise MineruError(f"v4 parse failed: {item.get('err_msg')}", retryable=False)
            time.sleep(poll_interval)  # waiting-file/pending/running/converting 继续等
        raise MineruError(f"v4 poll timeout after {poll_timeout}s (batch_id={batch_id})", retryable=True)

    @staticmethod
    def _extract_zip(zip_bytes: bytes) -> tuple[str, dict[str, bytes]]:
        """官方 zip 结构：full.md + images/ + *.json。只取 full.md 与图片。"""
        markdown = ""
        images: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.endswith("full.md"):
                    markdown = zf.read(name).decode("utf-8")
                elif "/" in name and name.lower().rsplit(".", 1)[-1] in {
                    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
                }:
                    images[name] = zf.read(name)
        if not markdown:
            raise MineruError("full.md not found in result zip", retryable=False)
        return markdown, images

    # ---------------------------------------------------------------- agent

    def _parse_agent(self, path: Path, *, poll_interval: float, poll_timeout: float) -> ParsedDocument:
        body = {
            "file_name": path.name,
            "language": self.language,
            "enable_table": self.enable_table,
            "is_ocr": self.is_ocr,
            "enable_formula": self.enable_formula,
        }
        req = urllib.request.Request(f"{AGENT_BASE}/parse/file",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        result = _http_json(req, self.timeout)
        if result.get("code") != 0:
            raise MineruError(f"agent submit failed: {result.get('msg')}", code=result.get("code"),
                              retryable=result.get("code") not in _AGENT_FATAL_CODES)
        task_id = result["data"]["task_id"]
        _put_upload(result["data"]["file_url"], path.read_bytes(), max(self.timeout, 300.0))
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            poll_req = urllib.request.Request(f"{AGENT_BASE}/parse/{task_id}")
            data = _http_json(poll_req, self.timeout)
            if data.get("code") != 0:
                raise MineruError(f"agent poll failed: {data.get('msg')}", code=data.get("code"))
            item = data["data"]
            state = item.get("state", "")
            if state == "done":
                md_bytes = _http_bytes(item["markdown_url"], max(self.timeout, 120.0))
                return ParsedDocument(markdown=md_bytes.decode("utf-8"), images={},
                                      channel="agent", model="pipeline-light")
            if state == "failed":
                raise MineruError(f"agent parse failed: {item.get('err_msg')}",
                                  code=item.get("err_code"),
                                  retryable=item.get("err_code") not in _AGENT_FATAL_CODES)
            time.sleep(poll_interval)  # waiting-file/uploading/pending/running 继续等
        raise MineruError(f"agent poll timeout after {poll_timeout}s (task_id={task_id})", retryable=True)
