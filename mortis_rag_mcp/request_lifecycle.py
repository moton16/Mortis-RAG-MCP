"""请求生命周期值对象：不可变上下文、结果分类与后台 hold 判据。

本模块**不读用户配置、不发起任何请求、不触碰 SQLite**。它只提供：

* `RequestContext`：一次物理 POST 的调用上下文，必须显式传递，禁止挂在
  provider 实例的共享可变字段上（query 与 watcher 共用同一个 provider 实例）。
* `RequestOutcome`：把「是否收到 HTTP 响应」「产物是否可用」「是否允许后台
  自动重算」三个事实分开表达。任何一条都不能充当「供应商已收费/未收费」的确认。
* 稳定的 `component_key` 计算：后台占用按输入身份而非本次 operation_id 计算。

区分三件事（v0.9.2 §1）：
1. 是否收到 HTTP 响应（`response_received`）；
2. 产物是否可用于当前请求（由调用方在校验后推进到 `succeeded`）；
3. 是否允许后台自动重新计算（`hold_state`，None 表示无需 hold）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------- 目的枚举

#: 后台文档索引（watcher / run_sync 驱动的付费嵌入）。
PURPOSE_BACKGROUND_DOCUMENT = "background_document"
#: 后台原生媒体嵌入。
PURPOSE_BACKGROUND_MEDIA = "background_media"
#: 手动 query 期的嵌入。
PURPOSE_MANUAL_QUERY = "manual_query"
#: 手动 rerank。
PURPOSE_MANUAL_RERANK = "manual_rerank"
#: 手动 rebuild。
PURPOSE_MANUAL_REBUILD = "manual_rebuild"
#: doctor 显式探活。
PURPOSE_DOCTOR_PROBE = "doctor_probe"

ALL_PURPOSES = (
    PURPOSE_BACKGROUND_DOCUMENT,
    PURPOSE_BACKGROUND_MEDIA,
    PURPOSE_MANUAL_QUERY,
    PURPOSE_MANUAL_RERANK,
    PURPOSE_MANUAL_REBUILD,
    PURPOSE_DOCTOR_PROBE,
)

#: 后台占用族。`manual_rebuild` **不**与 `background_document` 分成两个族，
#: 否则 rebuild 会变成绕开 hold 的后门（v0.9.2 §4.1）。
PURPOSE_FAMILY_DOCUMENT = "document"
PURPOSE_FAMILY_MEDIA = "native_media"
PURPOSE_FAMILY_QUERY = "query"
PURPOSE_FAMILY_RERANK = "rerank"

_PURPOSE_FAMILY: dict[str, str] = {
    PURPOSE_BACKGROUND_DOCUMENT: PURPOSE_FAMILY_DOCUMENT,
    PURPOSE_MANUAL_REBUILD: PURPOSE_FAMILY_DOCUMENT,
    PURPOSE_BACKGROUND_MEDIA: PURPOSE_FAMILY_MEDIA,
    PURPOSE_MANUAL_QUERY: PURPOSE_FAMILY_QUERY,
    PURPOSE_MANUAL_RERANK: PURPOSE_FAMILY_RERANK,
    PURPOSE_DOCTOR_PROBE: PURPOSE_FAMILY_QUERY,
}

#: 参与后台组件占用（reserve/hold）的族；query/rerank 不加入（§4.1）。
BACKGROUND_PURPOSE_FAMILIES = (PURPOSE_FAMILY_DOCUMENT, PURPOSE_FAMILY_MEDIA)


def purpose_family(purpose: str) -> str:
    """目的 → 占用族。未知目的按 query 处理（最保守：不参与后台占用）。"""
    return _PURPOSE_FAMILY.get(str(purpose or ""), PURPOSE_FAMILY_QUERY)


def is_background_purpose(purpose: str) -> bool:
    return purpose_family(purpose) in BACKGROUND_PURPOSE_FAMILIES


# ------------------------------------------------------------------ 上下文对象


@dataclass(frozen=True, slots=True)
class RequestContext:
    """一次物理 POST 的调用上下文。

    `operation_id` 按**逻辑操作**生成（一次 sync / 一次 query / 一次 retry），
    `batch_id` 标识该操作内的一个批次。二者都不可变：provider 不得在飞行中改写
    共享实例字段来"切换"上下文。
    """

    operation_id: str
    purpose: str = PURPOSE_MANUAL_QUERY
    batch_id: str = ""

    def with_batch(self, batch_id: str) -> "RequestContext":
        return RequestContext(self.operation_id, self.purpose, str(batch_id))


def manual_context(purpose: str, *, operation_id: str = "") -> RequestContext:
    """手动/一次性调用的上下文；operation_id 缺省时按目的生成一个稳定前缀。"""
    return RequestContext(operation_id or f"manual:{purpose}", purpose, "")


# -------------------------------------------------------------------- 结果分类

OUTCOME_NOT_SENT = "not_sent"
OUTCOME_REJECTED = "rejected"
OUTCOME_HTTP_ERROR = "http_error"
OUTCOME_TRANSPORT_UNKNOWN = "transport_unknown"
OUTCOME_RESPONSE_INVALID = "response_invalid"
OUTCOME_SUCCEEDED = "succeeded"

ALL_OUTCOMES = (
    OUTCOME_NOT_SENT,
    OUTCOME_REJECTED,
    OUTCOME_HTTP_ERROR,
    OUTCOME_TRANSPORT_UNKNOWN,
    OUTCOME_RESPONSE_INVALID,
    OUTCOME_SUCCEEDED,
)

#: 明确「远端可能已受理」的结果：后台必须 hold，不能自动重发。
UNKNOWN_OUTCOMES = (OUTCOME_HTTP_ERROR, OUTCOME_TRANSPORT_UNKNOWN, OUTCOME_RESPONSE_INVALID)

#: 结果 → 后台组件状态（None = 不需要 hold，占用直接释放）。
OUTCOME_HOLD_STATE: dict[str, str | None] = {
    OUTCOME_NOT_SENT: None,
    OUTCOME_REJECTED: None,
    OUTCOME_SUCCEEDED: None,
    OUTCOME_HTTP_ERROR: "held_unknown",
    OUTCOME_TRANSPORT_UNKNOWN: "held_unknown",
    OUTCOME_RESPONSE_INVALID: "held_response_invalid",
}

#: 结果 → 诊断错误码。兼容映射见 `legacy_error_code`。
OUTCOME_ERROR_CODE: dict[str, str] = {
    OUTCOME_NOT_SENT: "REQUEST_NOT_SENT",
    OUTCOME_REJECTED: "REQUEST_REJECTED",
    OUTCOME_HTTP_ERROR: "REQUEST_HTTP_ERROR",
    OUTCOME_TRANSPORT_UNKNOWN: "REQUEST_TRANSPORT_UNKNOWN",
    OUTCOME_RESPONSE_INVALID: "RESPONSE_INVALID",
    OUTCOME_SUCCEEDED: "",
}

_LEGACY_ERROR_CODE: dict[str, str] = {
    OUTCOME_NOT_SENT: "LOCAL_REQUEST_FAILED",
    OUTCOME_REJECTED: "SUBMISSION_UNKNOWN",
    OUTCOME_HTTP_ERROR: "SUBMISSION_UNKNOWN",
    OUTCOME_TRANSPORT_UNKNOWN: "SUBMISSION_UNKNOWN",
    OUTCOME_RESPONSE_INVALID: "SUBMISSION_UNKNOWN",
}

#: 明确「请求在工作开始前就被拒绝」的 4xx（429 除外：它可能已计费）。
REJECTED_HTTP_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 413, 422})


def classify_http_status(status: int | None) -> tuple[str, bool]:
    """HTTP 状态码 → (outcome, remote_effect_unknown)。

    429 与其它 5xx 一律 `http_error` 且默认远端影响未知：只凭 Retry-After 不
    能推断供应商未处理，也不能据此自动重发。
    """
    if status is None:
        return OUTCOME_TRANSPORT_UNKNOWN, True
    code = int(status)
    if 200 <= code < 300:
        # 2xx 但产物不可用属于 response_invalid，由调用方在校验阶段推进。
        return OUTCOME_SUCCEEDED, True
    if code in REJECTED_HTTP_STATUSES:
        return OUTCOME_REJECTED, False
    return OUTCOME_HTTP_ERROR, True


def legacy_error_code(outcome: str) -> str:
    """旧错误码兼容映射（保留一个版本）：旧调用方按 `SUBMISSION_UNKNOWN` 收口。"""
    return _LEGACY_ERROR_CODE.get(outcome, "SUBMISSION_UNKNOWN")


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """一次物理 POST 的结果事实。不含任何"供应商是否收费"的判断。"""

    outcome: str
    response_received: bool = False
    remote_effect_unknown: bool = False
    http_status: int | None = None
    #: 脱敏后的失败原因（受限长度），用于落盘与展示。
    detail: str = ""
    #: 完整一行的诊断摘要（含 HTTP 状态），只用于日志/消息，不参与身份计算。
    summary: str = ""
    request_fingerprint: str = ""
    operation_id: str = ""
    batch_id: str = ""

    @property
    def hold_state(self) -> str | None:
        return OUTCOME_HOLD_STATE.get(self.outcome)

    @property
    def requires_hold(self) -> bool:
        return self.hold_state is not None

    @property
    def error_code(self) -> str:
        return OUTCOME_ERROR_CODE.get(self.outcome, "SUBMISSION_UNKNOWN")

    @property
    def legacy_error_code(self) -> str:
        return legacy_error_code(self.outcome)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "response_received": self.response_received,
            "remote_effect_unknown": self.remote_effect_unknown,
            "http_status": self.http_status,
            "detail": self.detail[:200],
            "request_fingerprint": self.request_fingerprint,
            "operation_id": self.operation_id,
            "batch_id": self.batch_id,
            "hold_state": self.hold_state,
        }


def outcome_for_status(status: int | None, *, detail: str = "", **kwargs: Any) -> RequestOutcome:
    outcome, unknown = classify_http_status(status)
    return RequestOutcome(
        outcome=outcome,
        response_received=status is not None,
        remote_effect_unknown=unknown,
        http_status=status,
        detail=detail,
        **kwargs,
    )


# -------------------------------------------------------------- 稳定计算身份


def canonical_json(value: Any) -> str:
    """稳定序列化：键排序、紧凑分隔、UTF-8 原文。用于身份计算，不用于审计展示。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(value: Any) -> str:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def component_key(*, purpose_family_name: str, profile_fingerprint: str,
                  embedding_key: str, exact_input_sha256: str) -> str:
    """后台占用的完整 64 位十六进制输入身份键（v0.9.2 §4.1）。

    `purpose_family` 不含本次 operation_id：同一次重算/重组批必须命中同一个键，
    否则换批大小或换顺序就能绕开 hold 重新付费。
    """
    return sha256_hex([
        str(purpose_family_name or ""),
        str(profile_fingerprint or ""),
        str(embedding_key or ""),
        str(exact_input_sha256 or ""),
    ])


def payload_fingerprint(payload_bytes: bytes) -> str:
    """完整请求体指纹：只用于审计对照与可选远端幂等，不代替 component_key。"""
    return hashlib.sha256(payload_bytes).hexdigest()


def short_fingerprint(digest: str, length: int = 16) -> str:
    """显示用短指纹。**禁止**用于占用或向量复用权威判断（issue #17 纪律）。"""
    return str(digest or "")[:length]


# ------------------------------------------------------------ 请求控制模式开关

MODE_BOUNDED_RESUME = "bounded_resume"
MODE_LEGACY_PROFILE = "legacy_profile"
ALL_MODES = (MODE_BOUNDED_RESUME, MODE_LEGACY_PROFILE)

#: 模块级默认模式：**未装配** provider 的 fail-closed 兜底，故意保持 `legacy_profile`。
#:
#: 产品默认（`RequestControlConfig.mode`）是 `bounded_resume`，但那是**按实例**装配的
#: （`indexer._configure_paid_providers` / `server._media_provider_and_error` 都显式传
#: `mode=`）。这里不是"还没切"的遗留值：`providers._send_once` 在阻塞模式下会对
#: 「没有 journal/guard 的付费 provider」直接拒发（`PAID_REQUEST_CONTROL_UNAVAILABLE`），
#: 把它改成 bounded_resume 等于让任何漏装配的付费 provider 自由 POST。所以**不要**
#: 为了"和配置默认对齐"而改这个常量；漏装配应当修装配点。
_DEFAULT_MODE = MODE_LEGACY_PROFILE


def get_request_control_mode() -> str:
    return _DEFAULT_MODE


def set_request_control_mode(mode: str) -> str:
    """切换全局默认模式；返回切换后的值。仅供配置装配与测试使用。"""
    global _DEFAULT_MODE
    if mode not in ALL_MODES:
        raise ValueError(f"unsupported request control mode: {mode!r}")
    _DEFAULT_MODE = mode
    return _DEFAULT_MODE


def bounded_resume_enabled(mode: str | None = None) -> bool:
    return (mode or _DEFAULT_MODE) == MODE_BOUNDED_RESUME


def audit_is_blocking(mode: str | None = None) -> bool:
    """旧模式下 journal 不可写即拒绝发送（fail closed）；新模式下审计是非阻塞的。"""
    return not bounded_resume_enabled(mode)
