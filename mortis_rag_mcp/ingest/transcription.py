"""转录 adapter 接缝与真实受控实现（E09 / E15）。

实现 OpenAI-compatible Audio Transcriptions 协议（如 OpenAI Whisper / Groq Whisper / SiliconFlow）：
- 唯一装配点 `create_transcription_adapter`：
  - 未配置 adapter → 返回 None（metadata_only）；
  - 配置了 adapter 但缺少必需 endpoint → 抛出 `TranscriptionContractUnverified`（不编造虚假端点）；
  - 合法配置 → 返回 `OpenAiTranscriptionAdapter`。
- POST 发送前走既有 paid_request journal/intent；响应未知（429/网络异常/超时）进入
  `SUBMISSION_UNKNOWN`，禁止静默自动重传。
- 支持 `timeout` 形参透传；
- 优先解析 `verbose_json` 的分段细粒度时间戳；不带时间戳时维持分段近似。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB response body limit


def _read_bounded(resp: Any, limit: int = _MAX_RESPONSE_BYTES) -> bytes:
    """受限读取响应体，防无界内存爆炸（对齐 mineru 预算纪律）。"""
    declared = getattr(resp, "headers", None)
    if declared is not None and hasattr(declared, "get"):
        cl = declared.get("Content-Length")
        if cl is not None:
            try:
                if int(str(cl).strip()) > limit:
                    raise TranscriptionError(f"response Content-Length exceeds limit {limit} bytes")
            except (ValueError, TypeError):
                pass
    try:
        raw = resp.read(limit + 1)
    except TypeError:
        raw = resp.read()
    if len(raw) > limit:
        raise TranscriptionError(f"response body exceeded limit {limit} bytes")
    return raw


#: 声明了转录 adapter 但没有任何可注入的已核验实现（服务合同缺）。
AUDIO_ADAPTER_UNAVAILABLE = "AUDIO_ADAPTER_UNAVAILABLE"
#: 合同字段缺失（请求/响应/幂等/额度），不得凭猜实现。
TRANSCRIPT_CONTRACT_UNVERIFIED = "TRANSCRIPT_CONTRACT_UNVERIFIED"

SUPPORTED_TRANSCRIPTION_ADAPTERS = frozenset({
    "openai_whisper",
    "whisper",
    "openai",
    "openai_compatible",
    "transcript",
})


class TranscriptionContractUnverified(RuntimeError):
    """没有已核验的服务合同：明确失败，不写猜测的 HTTP 实现。"""


class TranscriptionError(RuntimeError):
    """转录执行期错误（协议失败或网络未知）。"""


class OpenAiTranscriptionAdapter:
    """真实 OpenAI-compatible 音频转录 adapter（E15）。"""

    evidence_reference = "openai-audio-transcriptions-v1"
    local_only = False

    def __init__(
        self,
        endpoint: str,
        *,
        model: str = "whisper-1",
        api_key: str = "",
        timeout: float = 30.0,
        response_format: str = "verbose_json",
        language: str = "",
        transport: Any = None,
        journal: Any = None,
        paid_guard: Any = None,
        local_only: bool = False,
    ) -> None:
        if not endpoint:
            raise ValueError("transcription endpoint is required")
        self.endpoint = endpoint
        self.model = model or "whisper-1"
        self.api_key = api_key
        self.timeout = timeout
        self.response_format = response_format
        self.language = language
        self.transport = transport
        self.journal = journal
        self.paid_guard = paid_guard
        self.local_only = bool(local_only)
        self.fingerprint = f"openai-whisper-v1:{self.model}:{self.endpoint}"

    def configure_paid_requests(self, journal: Any, guard: Any = None, *args: Any, **kwargs: Any) -> None:
        self.journal = journal
        self.paid_guard = guard

    def transcribe(self, segment: Any, timeout: float | None = None) -> dict[str, Any]:
        effective_timeout = float(timeout) if timeout is not None else float(self.timeout)
        payload_hash = hashlib.sha256(segment.data).hexdigest()

        if self.paid_guard is not None and not self.paid_guard(self.fingerprint):
            raise TranscriptionError("TRANSCRIPTION_PENDING_APPROVAL: transcription request blocked")

        req_id: Any = None
        if self.journal is not None and hasattr(self.journal, "before_send"):
            req_id = self.journal.before_send(payload_hash, self.endpoint, self.fingerprint)

        try:
            if self.transport is not None:
                result = self.transport(segment, timeout=effective_timeout)
            else:
                result = self._post_multipart(segment, effective_timeout)
            if req_id is not None and self.journal is not None and hasattr(self.journal, "mark_success"):
                self.journal.mark_success(req_id)
        except Exception as exc:
            if req_id is not None and self.journal is not None and hasattr(self.journal, "mark_unknown"):
                self.journal.mark_unknown(req_id, "response_unconfirmed")
            raise TranscriptionError(
                f"SUBMISSION_UNKNOWN: transcription outcome unknown; automatic retry disabled: {exc}"
            ) from exc

        if not isinstance(result, dict):
            raise TranscriptionError("transcription response must be a mapping")

        text = str(result.get("text", "")).strip()
        segments = result.get("segments", [])
        return {
            "text": text,
            "segments": segments,
            "raw": result,
        }

    def _post_multipart(self, segment: Any, timeout: float) -> dict[str, Any]:
        boundary = f"----MortisAudioBoundary{uuid.uuid4().hex}"
        parts: list[bytes] = []

        # form text fields
        for field, value in (
            ("model", self.model),
            ("response_format", self.response_format),
        ):
            if value:
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n{value}\r\n".encode("utf-8")
                )

        if self.language:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{self.language}\r\n".encode("utf-8")
            )

        ordinal = getattr(segment, "ordinal", 1)
        filename = f"segment-{ordinal}.wav"
        file_header = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        parts.append(file_header + segment.data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))

        body = b"".join(parts)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "Content-Length": str(len(body)),
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw_body = _read_bounded(resp)
        except HTTPError as exc:
            err_text = ""
            try:
                err_text = exc.read(1024).decode("utf-8", errors="replace")
            except Exception:
                pass
            msg = f"HTTP Error {exc.code}: {exc.reason}"
            if err_text:
                try:
                    err_json = json.loads(err_text)
                    if isinstance(err_json, dict) and "error" in err_json:
                        e = err_json["error"]
                        detail = e.get("message") if isinstance(e, dict) else str(e)
                        code = e.get("code") or e.get("type") if isinstance(e, dict) else ""
                        msg += f" - {code}: {detail}" if code else f" - {detail}"
                    else:
                        msg += f" - {err_text[:200]}"
                except Exception:
                    msg += f" - {err_text[:200]}"
            raise TranscriptionError(f"transcription HTTP failure: {msg}") from exc
        except (URLError, OSError) as exc:
            raise TranscriptionError(f"transcription network failure: {exc}") from exc

        try:
            result = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise TranscriptionError(f"invalid JSON response from transcription endpoint: {exc}") from exc

        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            err_msg = err.get("message") if isinstance(err, dict) else str(err)
            raise TranscriptionError(f"transcription API error: {err_msg}")

        return result


def create_transcription_adapter(
    audio_config: Any,
    *,
    transport: Any = None,
    journal: Any = None,
    paid_guard: Any = None,
    guard: Any = None,
    local_only: bool | None = None,
) -> Any:
    """按配置装配转录 adapter（唯一入口）。

    - 未声明 adapter → 返回 None（metadata_only）；
    - 声明了不支持的 adapter 或缺少 endpoint → 抛出 `TranscriptionContractUnverified`；
    - 声明了支持的 adapter 且具备 endpoint → 返回 `OpenAiTranscriptionAdapter`。
    """
    declared = str(getattr(audio_config, "adapter", "") or "").strip()
    if not declared:
        return None

    if declared.lower() not in SUPPORTED_TRANSCRIPTION_ADAPTERS:
        raise TranscriptionContractUnverified(
            f"{TRANSCRIPT_CONTRACT_UNVERIFIED}: audio.adapter={declared!r} 缺少已核验的"
            "服务合同（未受支持的 adapter 标识）"
        )

    endpoint = str(getattr(audio_config, "transcription_endpoint", "") or "").strip()
    if not endpoint:
        raise TranscriptionContractUnverified(
            f"{TRANSCRIPT_CONTRACT_UNVERIFIED}: audio.adapter={declared!r} 缺少已核验的"
            "transcription_endpoint；不实现猜测的 HTTP 客户端"
        )

    key_env = str(getattr(audio_config, "transcription_api_key_env", "") or "").strip()
    api_key = os.environ.get(key_env, "") if key_env else ""
    if not api_key:
        api_key = str(getattr(audio_config, "transcription_api_key", "") or getattr(audio_config, "api_key", "") or "").strip()
    model = str(getattr(audio_config, "transcription_model", "") or "whisper-1").strip() or "whisper-1"
    timeout = float(getattr(audio_config, "transcription_timeout", 30.0))
    resolved_local_only = bool(getattr(audio_config, "local_only", False)) if local_only is None else bool(local_only)
    resp_format = str(getattr(audio_config, "transcription_response_format", "") or getattr(audio_config, "response_format", "") or "verbose_json").strip() or "verbose_json"
    lang = str(getattr(audio_config, "transcription_language", "") or getattr(audio_config, "language", "") or "").strip()
    effective_guard = paid_guard if paid_guard is not None else guard

    return OpenAiTranscriptionAdapter(
        endpoint=endpoint,
        model=model,
        api_key=api_key,
        timeout=timeout,
        response_format=resp_format,
        language=lang,
        transport=transport,
        journal=journal,
        paid_guard=effective_guard,
        local_only=resolved_local_only,
    )
