from __future__ import annotations

import hashlib
import http.client
import json
import math
import socket
from typing import Any, NamedTuple, Protocol, Sequence
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .config import EmbeddingConfig, RerankerConfig
from .embedding_capabilities import (
    EmbeddingContractError, ResolvedEmbeddingProfile, accepts_keyword,
    resolve_embedding_profile, validate_vector,
)
from .request_lifecycle import (
    OUTCOME_NOT_SENT, OUTCOME_REJECTED, OUTCOME_RESPONSE_INVALID,
    OUTCOME_TRANSPORT_UNKNOWN, RequestContext, RequestOutcome,
    audit_is_blocking, classify_http_status, get_request_control_mode,
    legacy_error_code, manual_context, payload_fingerprint,
)

# HTTP 错误摘要上限（issue #17 纪律：受限摘要，不落完整正文）。
_HTTP_ERROR_READ_LIMIT = 1024
_HTTP_ERROR_DETAIL_LIMIT = 200


def _build_no_redirect_opener() -> urllib.request.OpenerDirector:
    """构造一个**不注册 `HTTPRedirectHandler`** 的 opener。

    付费请求非幂等，跟随 3xx 有三个真实后果（2026-10-12 本地探针实测确认，
    `E:/coding/.mortis-runs/v092/probe-gaierror/`）：

    1. `302` + 重定向目标 DNS 解析失败 → 第一跳**已收到完整付费 POST**，而
       `_transport_outcome` 会把它归成 `not_sent`（`remote_effect_unknown=False`）→
       经 `mark_not_sent` 终态化 → **放行同载荷重发**（重复计费）；
    2. 目标可解析时，调用会把**重定向目标的响应**当成本次结果（结果被归因到另一个主机）；
    3. CPython 的 `redirect_request` 只剥 `Content-Length` / `Content-Type`，
       **保留 `Authorization`** → 端点只要回一个 302 就能把 API key 引到任意主机。

    不跟随则 3xx 以 `HTTPError` 上抛，由 `classify_http_status` 归
    `http_error` + `remote_effect_unknown=True`（保守且正确：不宣称未收费）。
    """
    opener = urllib.request.OpenerDirector()
    for factory in (urllib.request.ProxyHandler, urllib.request.UnknownHandler,
                    urllib.request.HTTPHandler, urllib.request.HTTPDefaultErrorHandler,
                    urllib.request.HTTPErrorProcessor):
        try:
            opener.add_handler(factory())
        except Exception:  # noqa: BLE001 - 缺 ssl 等环境差异不该致命
            continue
    https = getattr(urllib.request, "HTTPSHandler", None)
    if https is not None:
        try:
            opener.add_handler(https())
        except Exception:  # noqa: BLE001
            pass
    return opener


_NO_REDIRECT_OPENER = _build_no_redirect_opener()


def urlopen(request: Any, timeout: float | None = None, **kwargs: Any) -> Any:
    """模块级物理请求出口：**刻意不跟随 3xx**（理由见 `_build_no_redirect_opener`）。

    沿用 `urlopen` 这个名字，是因为它是既有的测试注入点
    （`monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", ...)`）。
    """
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout, **kwargs)


def _post_response_contract_error(message: str, **kwargs: Any) -> ProviderError:
    """**已收到 2xx 响应**、但产物不可用：分类为 `response_invalid`。

    这类错误不是「远端影响未知」——请求确实发出去并收到了响应（很可能已经计费）。
    裸抛 `ProviderError(...)` 会让 `as_outcome()` 退化成 `transport_unknown` +
    `response_received=False`，于是运维看到的是「请求没到」，实际是「响应到了但不可用」；
    正确状态是 `held_response_invalid` / `RESPONSE_INVALID`（v0.9.2 复核修正）。
    """
    return ProviderError(message, outcome=OUTCOME_RESPONSE_INVALID, response_received=True,
                         remote_effect_unknown=True, **kwargs)


class _PostReceipt(NamedTuple):
    """一次物理 POST 的记账凭据（`defer_success=True` 时交回调用方补落终态）。

    2xx 只证明**传输**成功；产物合同由调用方校验。把终态记账延后到校验之后，
    是为了避免「形状非法的 2xx」被记成 `success` —— 那会让同一载荷的重发不再被
    意图闸门看见，与 `paid_requests` 的「success 只在产物通过后落账」承诺相反。
    """

    request_id: str | None
    mode: str
    status: int | None
    base: dict[str, Any]


class ProviderError(RuntimeError):
    """Provider 失败。结构化字段表达「发生了什么」，不表达「供应商是否收费」。"""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        response_detail: str = "",
        batch_count: int | None = None,
        batch_fingerprint: str = "",
        outcome: str = "",
        response_received: bool | None = None,
        remote_effect_unknown: bool | None = None,
        request_fingerprint: str = "",
        operation_id: str = "",
        batch_id: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.response_detail = response_detail[:_HTTP_ERROR_DETAIL_LIMIT]
        self.batch_count = batch_count
        self.batch_fingerprint = batch_fingerprint
        self.outcome = outcome
        self.response_received = response_received
        self.remote_effect_unknown = remote_effect_unknown
        self.request_fingerprint = request_fingerprint
        self.operation_id = operation_id
        self.batch_id = batch_id

    def as_outcome(self) -> RequestOutcome:
        """投影为结构化结果（缺省按「远端影响未知」的保守解释）。"""
        known = self.outcome or OUTCOME_TRANSPORT_UNKNOWN
        return RequestOutcome(
            outcome=known,
            response_received=bool(self.response_received) if self.response_received is not None
            else self.http_status is not None,
            remote_effect_unknown=True if self.remote_effect_unknown is None
            else bool(self.remote_effect_unknown),
            http_status=self.http_status,
            detail=self.response_detail,
            request_fingerprint=self.request_fingerprint,
            operation_id=self.operation_id,
            batch_id=self.batch_id,
        )


class PaidRequestRefused(ProviderError):
    """付费闸门**主动拒绝**（不是「审计写失败」）。

    典型场景：同一 `(kind, payload_hash, endpoint, profile_fingerprint)` 已有未决意图
    （`PAID_REQUEST_UNRESOLVED`）。这类拒绝表达「不许再发同一载荷」，因此
    `_audit_before_send` 必须**无条件上抛**，不能按「审计尽力而为」吞掉——
    吞掉会让字节完全一致的第二次 POST 照发（2026-10-12 实机复现：
    `E:/coding/.mortis-runs/v092/probe-resend/`，同 sha256 两次 POST、无用户确认）。
    """


def _transport_outcome(exc: BaseException) -> RequestOutcome:
    """把传输层异常分类为结构化结果。

    顺序要紧：`HTTPError` 是 `URLError` 的子类，必须先判；只有能证明「连接在
    提交前就失败」的情形（DNS/地址解析）才可归为 `not_sent`——把全部
    `URLError` 都认成 `not_sent` 等于宣称供应商没收费，是假的安全。
    """
    if isinstance(exc, HTTPError):
        code = int(exc.code) if exc.code is not None else None
        summary, _, detail = _request_failure_summary(exc)
        outcome, unknown = classify_http_status(code)
        return RequestOutcome(outcome=outcome, response_received=True,
                              remote_effect_unknown=unknown, http_status=code,
                              detail=str(detail) or summary, summary=summary)
    if isinstance(exc, http.client.IncompleteRead):
        # 响应已经开始返回但读取被截断：收到过响应，但产物不可用。
        return RequestOutcome(outcome=OUTCOME_RESPONSE_INVALID, response_received=True,
                              remote_effect_unknown=True, http_status=None,
                              detail=f"incomplete read: {exc}"[:_HTTP_ERROR_DETAIL_LIMIT])
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return RequestOutcome(outcome=OUTCOME_TRANSPORT_UNKNOWN, response_received=False,
                              remote_effect_unknown=True, http_status=None,
                              detail=str(exc)[:_HTTP_ERROR_DETAIL_LIMIT])
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.gaierror):
            return RequestOutcome(outcome=OUTCOME_NOT_SENT, response_received=False,
                                  remote_effect_unknown=False, http_status=None,
                                  detail=str(exc)[:_HTTP_ERROR_DETAIL_LIMIT])
        return RequestOutcome(outcome=OUTCOME_TRANSPORT_UNKNOWN, response_received=False,
                              remote_effect_unknown=True, http_status=None,
                              detail=str(exc)[:_HTTP_ERROR_DETAIL_LIMIT])
    if isinstance(exc, (http.client.HTTPException, OSError)):
        return RequestOutcome(outcome=OUTCOME_TRANSPORT_UNKNOWN, response_received=False,
                              remote_effect_unknown=True, http_status=None,
                              detail=str(exc)[:_HTTP_ERROR_DETAIL_LIMIT])
    return RequestOutcome(outcome=OUTCOME_TRANSPORT_UNKNOWN, response_received=False,
                          remote_effect_unknown=True, http_status=None,
                          detail=str(exc)[:_HTTP_ERROR_DETAIL_LIMIT])


def _request_failure_summary(exc: BaseException) -> tuple[str, int | None, str]:
    if not isinstance(exc, HTTPError):
        return str(exc)[:_HTTP_ERROR_DETAIL_LIMIT], None, ""
    err_text = ""
    try:
        err_text = exc.read(_HTTP_ERROR_READ_LIMIT).decode("utf-8", errors="replace")
    except Exception:
        pass
    detail = ""
    if err_text:
        try:
            err_json = json.loads(err_text)
            error = err_json.get("error") if isinstance(err_json, dict) else None
            detail = error.get("message", "") if isinstance(error, dict) else str(error or "")
        except Exception:
            detail = err_text
    detail = str(detail)[:_HTTP_ERROR_DETAIL_LIMIT]
    status = int(exc.code) if exc.code is not None else None
    reason = str(exc.reason)[:_HTTP_ERROR_DETAIL_LIMIT]
    summary = f"HTTP {status}: {reason}"
    if detail:
        summary += f" - {detail}"
    return summary, status, detail


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class RerankerProvider(Protocol):
    def rerank(self, query: str, documents: Sequence[str]) -> list[dict[str, Any]]: ...


class StaticEmbeddingProvider:
    """Deterministic local hash embeddings; it never performs network/LLM calls."""

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    #: 后台单批能力标记：静态/本地 provider 天然支持「一次调用一个批次」。
    supports_batch_once = True

    def embed(self, texts: Sequence[str], *, request_context: RequestContext | None = None) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def embed_batch_once(self, texts: Sequence[str], *,
                         request_context: RequestContext | None = None) -> list[list[float]]:
        """单批入口：本地 provider 不产生任何网络请求，行为与 `embed` 一致。"""
        return self.embed(texts, request_context=request_context)

    def _embed_one(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        for index in range(self.dimension):
            byte = digest[index % len(digest)]
            values[index] = (byte / 127.5) - 1.0
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]


def _retry_after_seconds(headers: Any) -> float | None:
    """解析 Retry-After 响应头（只认数字秒数；HTTP-date 形式返回 None）。

    只作**诊断**：v0.9.2 起 provider 内零自动重发，读取 Retry-After 不授权重发，
    也不推断供应商是否计费。
    """
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, 60.0)


def _is_loopback_endpoint(endpoint: str) -> bool:
    if not endpoint:
        return False
    try:
        from urllib.parse import urlsplit
        import ipaddress
        host = (urlsplit(endpoint).hostname or "").strip().lower()
        if not host:
            return False
        if host in {"127.0.0.1", "localhost", "::1"}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    except Exception:
        return False


class _JsonHttpProvider:
    def __init__(self, endpoint: str, api_key: str = "", timeout: float = 30.0, max_retries: int = 3, retry_backoff: float = 1.0) -> None:
        if not endpoint:
            raise ValueError("external provider endpoint is required")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        # 弃用字段：v0.9.2 起 provider 内零自动重发，仅保留解析兼容。
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.request_journal: Any = None
        self.paid_guard: Any = None
        self.request_profile = ""
        #: 非空时覆盖全局模式（配置装配写入），空串表示跟随全局默认。
        self.request_control_mode = ""
        #: 审计写失败独立记录，绝不阻断已验证响应的返回（T06）。
        self.audit_errors: list[dict[str, str]] = []
        self.last_request_fingerprint = ""

    @property
    def is_local_free(self) -> bool:
        return not bool(self.api_key and self.api_key.strip()) and _is_loopback_endpoint(self.endpoint)

    @property
    def request_mode(self) -> str:
        return self.request_control_mode or get_request_control_mode()

    @property
    def audit_write_errors(self) -> int:
        return len(self.audit_errors)

    def configure_paid_requests(self, journal: Any, guard: Any, profile_fingerprint: str,
                                *, mode: str = "") -> None:
        self.request_journal = journal
        self.paid_guard = guard
        self.request_profile = profile_fingerprint
        if mode:
            self.request_control_mode = mode

    def _record_audit_error(self, phase: str, exc: BaseException) -> None:
        self.audit_errors.append({"phase": phase, "error": type(exc).__name__,
                                  "detail": str(exc)[:_HTTP_ERROR_DETAIL_LIMIT]})

    def _audit_before_send(self, fingerprint: str, mode: str, *,
                           reject_unresolved: bool = True) -> str | None:
        """非阻塞审计：**写失败**只记账，不丢掉已验证响应。

        但「闸门主动拒绝」（`PaidRequestRefused`，例如同一载荷已有未决意图）**不是**
        写失败：它表达「不许再发」，必须无条件上抛。此前二者被同一个 `except` 吞掉，
        于是默认 `bounded_resume` 模式下同一载荷会被第二次物理 POST（实机复现见
        `PaidRequestRefused` 的 docstring）。

        `reject_unresolved=False` 供**组件账本管辖**的发送使用（见
        `RequestContext.ledger_managed`）：那条路径的重发已由占用 + CAS 挡住，只可能
        来自操作者显式确认，再按意图拒一次会把操作者刚批准的重发拦回去。
        """
        journal = self.request_journal
        if journal is None or self.is_local_free:
            return None
        # 不认识的 journal 实现（第三方/测试替身）不传新关键字，避免把它们打成
        # `TypeError` —— 与 `embed_with_profile` 用 `accepts_keyword` 是同一口径。
        extra = ({"reject_unresolved": reject_unresolved}
                 if accepts_keyword(journal.before_send, "reject_unresolved") else {})
        try:
            return journal.before_send(fingerprint, self.endpoint, self.request_profile, **extra)
        except PaidRequestRefused as exc:
            self._record_audit_error("before_send", exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 审计写失败必须尽力而为
            self._record_audit_error("before_send", exc)
            if audit_is_blocking(mode):
                raise
            return None

    def _audit_finish(self, request_id: str | None, outcome: RequestOutcome, mode: str) -> None:
        if request_id is None:
            return
        journal = self.request_journal
        if journal is None:
            return
        try:
            if outcome.outcome == "succeeded":
                journal.mark_success(request_id)
            elif outcome.outcome == OUTCOME_NOT_SENT:
                # 确定没发出去（本地校验/能力缺失）：终态化，别留成未决意图 ——
                # 否则一条从未发出的请求会让整条路由被 `has_unresolved_paid_intents` 冻住。
                journal.mark_not_sent(request_id, f"{outcome.outcome}: {outcome.detail}")
            elif outcome.outcome == OUTCOME_REJECTED:
                # 远端**明确拒绝**（4xx，`classify_http_status` 判 remote_effect_unknown=False）：
                # 同属「远端未处理」，必须终态化。否则 `submission_unknown` 永久占用未决集合
                # （它不适用租约）→ 旧闸门下整条付费路由被永久冻住，而远端一个字都没处理。
                # 媒体路径（`media_providers._finish`）早已这样处理，此处对齐口径。
                journal.mark_rejected(request_id, f"{outcome.outcome}: {outcome.detail}")
            else:
                journal.mark_unknown(request_id, f"{outcome.outcome}: {outcome.detail}")
        except Exception as exc:  # noqa: BLE001
            self._record_audit_error("after_send", exc)
            if audit_is_blocking(mode):
                raise

    def _raise_classified(self, outcome: RequestOutcome, mode: str, *, local: bool) -> None:
        if local:
            code = "LOCAL_REQUEST_FAILED"
        elif audit_is_blocking(mode):
            code = legacy_error_code(outcome.outcome)
        else:
            code = outcome.error_code
        summary = outcome.summary or outcome.detail or "request failed"
        raise ProviderError(
            f"{code}: {summary}",
            http_status=outcome.http_status,
            response_detail=outcome.detail,
            outcome=outcome.outcome,
            response_received=outcome.response_received,
            remote_effect_unknown=outcome.remote_effect_unknown,
            request_fingerprint=outcome.request_fingerprint,
            operation_id=outcome.operation_id,
            batch_id=outcome.batch_id,
        )

    def _send_once(self, payload: dict[str, Any], *,
                   request_context: RequestContext | None = None,
                   defer_success: bool = False) -> Any:
        """恰好一个物理 POST；零自动重发（429/5xx/网络/JSON 都不重发）。

        `defer_success=True`：2xx 只代表**传输**成功，产物合同由调用方校验，终态记账
        一并交回调用方 —— 返回 `(result, _PostReceipt)`，调用方在校验通过后调
        `_finalize_receipt`。默认 False 保持原语义（解码成功即落 `success`）。
        调用方必须先确认本方法接受该关键字：测试桩/自定义 provider 会用只接受
        `(payload, request_context=None)` 的 lambda 替换 `_send_once`。
        """
        context = request_context if isinstance(request_context, RequestContext) else manual_context("manual_query")
        mode = self.request_mode
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        body = request.data or b""
        fingerprint = payload_fingerprint(body)
        self.last_request_fingerprint = fingerprint
        base = {"request_fingerprint": fingerprint, "operation_id": context.operation_id,
                "batch_id": context.batch_id}

        guard = self.paid_guard
        if guard is None or self.paid_guard is None:
            if not self.is_local_free and audit_is_blocking(mode):
                raise ProviderError(
                    "PAID_REQUEST_CONTROL_UNAVAILABLE: durable journal required; profile may be revoked",
                    outcome=OUTCOME_NOT_SENT, response_received=False,
                    remote_effect_unknown=False, **base)
        elif not guard(self.request_profile):
            raise ProviderError(
                "REQUEST_ROUTE_PAUSED: request route is paused (explicit revoke or policy)",
                outcome=OUTCOME_NOT_SENT, response_received=False,
                remote_effect_unknown=False, **base)

        request_id = self._audit_before_send(
            fingerprint, mode,
            reject_unresolved=not bool(getattr(context, "ledger_managed", False)))
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 0) or 0) or None
                raw = response.read()
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                outcome = RequestOutcome(outcome=OUTCOME_RESPONSE_INVALID,
                                         response_received=True, remote_effect_unknown=True,
                                         http_status=status,
                                         detail=f"invalid JSON response: {exc}"[:_HTTP_ERROR_DETAIL_LIMIT],
                                         **base)
                self._audit_finish(request_id, outcome, mode)
                self._raise_classified(outcome, mode, local=self.is_local_free)
        except (HTTPError, TimeoutError, socket.timeout, http.client.HTTPException, URLError, OSError) as exc:
            classified = _transport_outcome(exc)
            outcome = RequestOutcome(
                outcome=classified.outcome, response_received=classified.response_received,
                remote_effect_unknown=classified.remote_effect_unknown,
                http_status=classified.http_status, detail=classified.detail,
                summary=classified.summary, **base)
            self._audit_finish(request_id, outcome, mode)
            self._raise_classified(outcome, mode, local=self.is_local_free)
        if defer_success:
            return result, _PostReceipt(request_id=request_id, mode=mode, status=status, base=base)
        self._audit_finish(request_id, RequestOutcome(
            outcome="succeeded", response_received=True, remote_effect_unknown=True,
            http_status=status, **base), mode)
        return result

    def _finalize_receipt(self, receipt: "_PostReceipt | None", outcome: str,
                          detail: str = "") -> None:
        """`defer_success=True` 的另一半：调用方校验完产物后补落终态。

        `receipt is None`（`_send_once` 被测试桩替换，没有可延后的账）时不动任何账。
        """
        if receipt is None:
            return
        self._audit_finish(
            receipt.request_id,
            RequestOutcome(outcome=outcome, response_received=True,
                           remote_effect_unknown=(outcome != OUTCOME_NOT_SENT),
                           http_status=receipt.status,
                           detail=str(detail)[:_HTTP_ERROR_DETAIL_LIMIT],
                           **receipt.base),
            receipt.mode)

    def _post(self, payload: dict[str, Any], *, request_context: RequestContext | None = None) -> Any:
        """兼容入口：等价于 `_send_once`（一次物理 POST，零自动重发）。"""
        return self._send_once(payload, request_context=request_context)


class ExternalEmbeddingProvider(_JsonHttpProvider):
    def __init__(self, endpoint: str, model: str = "", api_key: str = "", timeout: float = 30.0, dimension: int | None = None, send_dimensions: bool = True, max_retries: int = 3, retry_backoff: float = 1.0, batch_size: int = 32, *, input_modality_declaration: str = "none", profile: ResolvedEmbeddingProfile | None = None) -> None:
        super().__init__(endpoint, api_key, timeout, max_retries, retry_backoff)
        self.model = model
        self.dimension = dimension
        self.send_dimensions = send_dimensions
        self.batch_size = batch_size
        self.input_modality_declaration = input_modality_declaration
        self.profile = profile
        if profile is not None:
            self.dimension = profile.effective_dim
            self.send_dimensions = profile.request_dim is not None

    #: 外部 provider 具备后台单批能力（一次调用 = 恰好一个物理 POST）。
    supports_batch_once = True

    def embed(self, texts: Sequence[str], *, request_context: RequestContext | None = None) -> list[list[float]]:
        """旧兼容签名。内部多批时**不**自动重发失败批次——异常直接上抛。"""
        if not texts:
            return []
        items = list(texts)
        if self.batch_size <= 0 or len(items) <= self.batch_size:
            return self._embed_batch(items, request_context=request_context)
        # 长文件按 batch_size 切片：单请求过大既容易被限流，也容易触发服务端长度上限。
        vectors: list[list[float]] = []
        for start in range(0, len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            # 条数校验在 _embed_batch 内统一做，单批与多批路径都不会漏。
            vectors.extend(self._embed_batch(batch, request_context=request_context))
        return vectors

    def embed_batch_once(self, texts: Sequence[str], *,
                         request_context: RequestContext | None = None) -> list[list[float]]:
        """后台单批入口：**恰好一个物理 POST**。

        超过 `batch_size` 直接拒绝，绝不隐藏再次分批——那会让「一次调用 = 一次
        POST」的预算与 hold 判据失真。调用方负责按 batch_size 组批。
        """
        items = list(texts or [])
        if not items:
            return []
        if self.batch_size > 0 and len(items) > self.batch_size:
            raise ProviderError(
                f"embed_batch_once received {len(items)} inputs but batch_size={self.batch_size}; "
                "caller must group batches before dispatch",
                outcome=OUTCOME_NOT_SENT, response_received=False,
                remote_effect_unknown=False, batch_count=len(items))
        return self._embed_batch(items, request_context=request_context)

    def _embed_batch(self, batch: list[str], *,
                     request_context: RequestContext | None = None) -> list[list[float]]:
        request_input: list[str] | list[dict[str, str]] = batch
        if self.input_modality_declaration == "typed":
            request_input = [{"text": text} for text in batch]
        payload: dict[str, Any] = {"model": self.model, "input": request_input}
        if self.input_modality_declaration == "hint":
            payload["input_modality"] = "text"
        if self.dimension and self.send_dimensions:
            payload["dimensions"] = self.dimension
        batch_fingerprint = hashlib.sha256(
            json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        # TODO(issue-17): 单条定位只能由 doctor 人工确认入口显式重发；
        # 索引路径不得自动逐条探测或二分，以免未知结果的 POST 重复计费。
        try:
            if accepts_keyword(self._send_once, "defer_success"):
                response, receipt = self._send_once(
                    payload, request_context=request_context, defer_success=True)
            else:
                # 测试桩/自定义 provider 的替换件：它自己记账，没有可延后的凭据。
                response, receipt = self._send_once(payload, request_context=request_context), None
        except ProviderError as exc:
            exc.batch_count = len(batch)
            exc.batch_fingerprint = batch_fingerprint
            raise
        try:
            parsed = self._parse_embedding_response(response, batch)
        except ProviderError as exc:
            # 2xx 但产物不可用：**不能**在账上留 success。按同一结果分类补落
            # `response_invalid`（与 `_send_once` 内 JSON 解码失败同口径），
            # 于是意图仍是「可能已计费的未知结果」，而不是「已成功」。
            self._finalize_receipt(receipt, OUTCOME_RESPONSE_INVALID, str(exc))
            exc.batch_count = len(batch)
            exc.batch_fingerprint = batch_fingerprint
            raise
        self._finalize_receipt(receipt, "succeeded")
        return parsed

    def _parse_embedding_response(self, response: Any, batch: list[str]) -> list[list[float]]:
        """校验一个 2xx 响应体并转成向量；不合约一律抛 `response_invalid`。"""
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            raise _post_response_contract_error("embedding response must contain a data list")
        # 少返回向量会让后续 chunk 的向量整体错位（静默污染整个向量库），
        # 这里拦住。默认 batch_size=32，多数文件走的就是这条单批路径。
        if len(data) != len(batch):
            raise _post_response_contract_error(
                f"embedding response returned {len(data)} vectors for {len(batch)} inputs"
            )
        # OpenAI 兼容接口不保证返回顺序，data[i].index 才是权威位置。
        indexes = []
        indexed = [isinstance(item, dict) and "index" in item for item in data]
        if any(indexed) and not all(indexed):
            raise _post_response_contract_error("embedding response mixes indexed and unindexed items")
        for position, item in enumerate(data):
            index = item.get("index", position) if isinstance(item, dict) else None
            if isinstance(index, bool) or not isinstance(index, int):
                raise _post_response_contract_error("embedding response contains invalid indexes")
            indexes.append(index)
        if sorted(indexes) != list(range(len(batch))):
            raise _post_response_contract_error(
                f"embedding response indexes {sorted(indexes)} do not match {len(batch)} inputs"
            )
        ordered = [item for _, item in sorted(zip(indexes, data), key=lambda pair: pair[0])]
        try:
            vectors = [list(item["embedding"]) for item in ordered]
            profile = self.profile
            if profile is None:
                dim = self.dimension or (len(vectors[0]) if vectors else 0)
                profile = ResolvedEmbeddingProfile("legacy-text", "openai", self.model, self.endpoint, "", "", dim, dim, None, "unsupported")
            return [validate_vector(vector, profile) for vector in vectors]
        except (KeyError, TypeError, EmbeddingContractError) as exc:
            raise _post_response_contract_error("embedding response contains invalid vectors") from exc


class ExternalRerankerProvider(_JsonHttpProvider):
    def __init__(self, endpoint: str, model: str = "", api_key: str = "", timeout: float = 30.0, max_retries: int = 3, retry_backoff: float = 1.0) -> None:
        super().__init__(endpoint, api_key, timeout, max_retries, retry_backoff)
        self.model = model

    def rerank(self, query: str, documents: Sequence[str], *,
               request_context: RequestContext | None = None) -> list[dict[str, Any]]:
        if not documents:
            return []
        payload = {"model": self.model, "query": query, "documents": list(documents)}
        if accepts_keyword(self._send_once, "defer_success"):
            response, receipt = self._send_once(
                payload, request_context=request_context, defer_success=True)
        else:
            response, receipt = self._send_once(payload, request_context=request_context), None
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            # 同 embedding：形状非法的 2xx 不得记成 success。
            exc = _post_response_contract_error("reranker response must contain a results list")
            self._finalize_receipt(receipt, OUTCOME_RESPONSE_INVALID, str(exc))
            raise exc
        self._finalize_receipt(receipt, "succeeded")
        return results

    def rerank_once(self, query: str, documents: Sequence[str], *,
                    request_context: RequestContext | None = None) -> list[dict[str, Any]]:
        """manual rerank 的单次入口：一次调用最多一个物理 POST。"""
        return self.rerank(query, documents, request_context=request_context)

    def rerank_or_none(self, query: str, documents: Sequence[str]) -> list[dict[str, Any]] | None:
        try:
            return self.rerank(query, documents)
        except Exception:
            return None


def create_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    profile = resolve_embedding_profile(config)
    if config.mode == "static":
        provider = StaticEmbeddingProvider(config.dimension)
        provider.profile = profile
        return provider
    if config.mode == "external":
        return ExternalEmbeddingProvider(
            config.endpoint,
            config.model,
            config.api_key,
            config.timeout,
            config.dimension,
            config.send_dimensions,
            config.max_retries,
            config.retry_backoff,
            config.batch_size,
            input_modality_declaration=config.input_modality_declaration,
            profile=profile,
        )
    raise ValueError(f"unsupported embedding mode: {config.mode}")


def create_media_provider(config: EmbeddingConfig, *, transport: Any = None) -> Any:
    """受控原生媒体 provider 工厂（E07）。

    - 配置未声明任何媒体模态 → 返回 `None`（该能力不存在，不是错误；文本路径不受影响）。
    - 声明不完整（modality / alignment_space_id / media preprocess / endpoint revision /
      MIME / 限额 / 证据引用）→ 抛 `ProviderError`：明确「该 route 不可用」，绝不用
      文本成功、同维向量或模型名冒充，也绝不自动试端点。
    - 已声明 adapter 使用合同 transport；注入的 transport 也使用同一 resolved 身份。
    """
    declared = tuple(getattr(config, "media_modalities", ()) or ())
    if not declared:
        return None
    from .embedding_capabilities import EmbeddingContractError, resolve_media_connection, resolve_media_profile
    from .media_providers import NativeMediaEvidence, NativeMediaProvider
    try:
        connection = resolve_media_connection(config)
        profile = resolve_media_profile(config, connection=connection)
    except EmbeddingContractError as exc:
        raise ProviderError(f"native media capability unavailable: {exc}") from exc
    mime = tuple(getattr(config, "media_allowed_mime_types", ()) or ())
    max_bytes = int(getattr(config, "media_max_input_bytes", 0) or 0)
    max_batch = int(getattr(config, "media_max_batch_size", 0) or 0)
    references = tuple(str(getattr(config, name, "") or "") for name in (
        "media_model_reference", "media_endpoint_fixture_reference",
        "media_alignment_reference", "media_license_reference"))
    if not mime:
        raise ProviderError("native media capability unavailable: allowed MIME types are not declared")
    if max_bytes <= 0 or max_batch <= 0:
        raise ProviderError("native media capability unavailable: byte/batch limits are not declared")
    if not all(references):
        raise ProviderError("native media capability unavailable: endpoint evidence references are incomplete")
    if transport is None:
        adapter_name = connection.adapter
        if adapter_name == "openai_vl":
            from .media_providers import HttpMediaTransport
            transport_type = HttpMediaTransport
        elif adapter_name in {"gemini", "embeddinggemma2"}:
            ep = connection.endpoint.lower()
            local = any(h in ep for h in ("127.0.0.1", "localhost", "::1"))
            gemma_unverified = (adapter_name == "embeddinggemma2"
                               and not any(dom in ep for dom in ("googleapis.com", "google", "aiplatform", "gemini")))
            if local or gemma_unverified:
                raise ProviderError(
                    "native media capability unavailable: local inference server (e.g. llama.cpp / Ollama) "
                    "lacks a verified HTTP REST audio embedding schema on /v1/embeddings. "
                    "For local EmbeddingGemma 2, run in-process via sentence-transformers/LiteRT; "
                    "for HTTP REST audio embeddings, route to Google Gemini cloud service 'gemini-embedding-2' "
                    "with adapter='gemini'."
                )
            from .media_providers import GeminiMediaTransport
            transport_type = GeminiMediaTransport
        else:
            raise ProviderError("native media capability unavailable: no declared transport (Q09 protocol missing)")
        transport = transport_type(
            endpoint=profile.endpoint, model=profile.model, api_key=connection.api_key,
            timeout=getattr(config, "timeout", 30.0), dimension=profile.effective_dim,
            send_dimensions=profile.request_dim is not None, allowed_mime_types=mime,
            max_input_bytes=max_bytes, max_batch_size=max_batch)

    evidence = NativeMediaEvidence(
        references[0], references[1], references[2], references[3],
        profile.fingerprint, profile.alignment_space_id, max_bytes, max_batch, mime,
        preprocess_version=profile.preprocess_version, endpoint_revision=profile.endpoint_revision,
    )
    return NativeMediaProvider(profile, evidence, transport)


def create_reranker_provider(config: RerankerConfig) -> ExternalRerankerProvider | None:
    if not config.enabled:
        return None
    return ExternalRerankerProvider(
        config.endpoint, config.model, config.api_key, config.timeout,
        max_retries=config.max_retries, retry_backoff=config.retry_backoff,
    )
