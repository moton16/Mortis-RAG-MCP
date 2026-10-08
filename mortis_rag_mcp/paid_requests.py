"""持久付费请求闸门：发送意图 journal + profile 授权（§20.7B / §20.7F）。

R2：正常启用配置不新增费用审批；明确撤销、持久意图和未知结果语义保留。

* **先落意图再发送**：任何非幂等计费 POST 在实际发出前必须 `before_send`，
  把 `request_id / payload_hash / endpoint / profile / attempt` 写进本机 control。
* **结果只有 success / submission_unknown**：响应丢失（网络错误、5xx、429）一律
  记 unknown；未知受理**不得自动重传**，只能由持久任务/人工确认后再决定。
  这一条是**被强制的**，不只是文案：同一 `kind + payload_hash + endpoint + profile`
  只要存在未决意图（`prepared` / `submission_unknown`），`before_send` 一律拒绝
  （`PAID_REQUEST_UNRESOLVED`），必须人工确认/放弃（`mark_intent(..., "abandoned")`）
  之后才能再次发送。以前只有异常文案宣称「automatic retry disabled」，实际下一次
  `sync()` 会把同一载荷重新发出去（网络抖动 = 重复计费）。
* **不存正文**：只存哈希与定位，`reason` 只写脱敏阶段（如 `response_unconfirmed`）。
* **闸门 fail closed**：控制面不可用（cache 关闭 / 控制库不可读）时拒绝外部付费请求，
  而不是静默放行或去掉闸门。
"""
from __future__ import annotations

import uuid
from typing import Any

from .doc_store import ControlStore, DocStoreError, StoreContractError
from .providers import ProviderError

#: 发送意图状态：prepared = 已落意图但结果未知（重启后只能人工确认）。
INTENT_STATES = ("prepared", "success", "submission_unknown", "abandoned")

#: 未决意图：结果未知（可能已计费），禁止自动重发，只能人工确认/放弃。
UNRESOLVED_INTENT_STATES = ("prepared", "submission_unknown")


class PaidRequestJournal:
    """绑定单一 kind（如 `embed` / `rerank` / `media` / `transcription`）的持久 journal。

    `control` 可以是 `ControlStore`，也可以是返回 `ControlStore | None` 的可调用对象：
    provider 在构造期就拿到 journal，但控制库连接延迟到真正要发送时才建立，避免
    只读探测（`load_vectors=False`）或纯词法使用路径凭空创建写库。
    """

    def __init__(self, control: Any, kind: str) -> None:
        if not isinstance(kind, str) or not kind:
            raise ValueError("journal kind 必填")
        self._control = control
        self._kind = kind

    @property
    def kind(self) -> str:
        return self._kind

    def _resolve(self) -> ControlStore:
        control = self._control() if callable(self._control) else self._control
        if control is None:
            raise ProviderError(
                "PAID_REQUEST_JOURNAL_UNAVAILABLE: 本机控制面不可用（[cache] 关闭或控制库不可读），"
                "已拒绝外部付费请求"
            )
        return control

    def before_send(self, payload_hash: str, endpoint: str, profile_fingerprint: str = "") -> str:
        """落一条持久意图并返回 request_id。

        两种情况都抛 `ProviderError`（fail closed）：
        控制面不可用；或同一载荷已有未决意图（结果未知，禁止自动重发）。
        """
        if not isinstance(payload_hash, str) or not payload_hash:
            raise ProviderError("paid request journal requires a payload hash")
        if not isinstance(endpoint, str) or not endpoint:
            raise ProviderError("paid request journal requires an endpoint")
        control = self._resolve()
        try:
            request_id = uuid.uuid4().hex
            control.record_send_intent(
                request_id, kind=self._kind, payload_hash=payload_hash, endpoint=endpoint,
                profile_fingerprint=str(profile_fingerprint or ""), attempt=None,
                reject_unresolved=True,
            )
        except (DocStoreError, StoreContractError) as exc:
            if "PAID_REQUEST_UNRESOLVED" in str(exc):
                raise ProviderError(str(exc)) from exc
            raise ProviderError(
                "PAID_REQUEST_JOURNAL_UNAVAILABLE: 无法持久化发送意图，已拒绝外部付费请求"
            ) from exc
        return request_id

    def _matching_intents(self, payload_hash: str, endpoint: str, profile_fingerprint: str) -> list[dict[str, Any]]:
        """同一（kind, payload_hash, endpoint, profile）的历史意图，按落库顺序。

        `attempt` 只做计数与可观测：它的增长绝不能被解释为「可以再发一次」。
        """
        return [item for item in self._resolve().list_request_intents()
                if item["kind"] == self._kind and item["payload_hash"] == payload_hash
                and item["endpoint"] == endpoint
                and item["profile_fingerprint"] == str(profile_fingerprint or "")]

    def mark_success(self, request_id: str) -> None:
        if not self._resolve().mark_intent(request_id, "success", expected_states=("prepared",)):
            raise ProviderError("REQUEST_INTENT_FINALIZED: late response discarded")

    def mark_unknown(self, request_id: str, reason: str = "response_unconfirmed") -> None:
        self._resolve().mark_intent(request_id, "submission_unknown", str(reason)[:200],
                                    expected_states=("prepared",))

    def pending(self) -> list[dict[str, Any]]:
        """仍无明确结果的意图（`prepared` / `submission_unknown`）。

        这两类都禁止自动重发，只能人工确认或轮询；`submission_unknown` 以前不在
        这里列出（`pending()` 只看 `prepared`），于是「未决意图」在可观测性上消失，
        与实际会重发的行为正好相反。现在两者同列。
        """
        return [item for item in self._resolve().list_request_intents()
                if item["kind"] == self._kind and item["state"] in UNRESOLVED_INTENT_STATES]


def paid_request_guard(control: ControlStore, profile_fingerprint: str, *,
                       pending_approval: bool = False) -> bool:
    """R2：正常配置允许；明确撤销拒绝，旧 pending_approval 参数兼容但不再阻塞。

    注意这里**不**包含「有未决意图（结果未知）就暂停」这一条：那条只适用于
    **索引批量**路径（`sync_engine._paid_embedding_allowed`）。查询期嵌入是单次、
    载荷唯一、不重放的请求，把它一起拦掉只会让语义检索静默退回词法。
    """
    if not isinstance(profile_fingerprint, str) or not profile_fingerprint:
        return False
    try:
        state = control.paid_authorization_state(profile_fingerprint)
    except (DocStoreError, StoreContractError):
        return False
    if state == "authorized":
        return True
    if state == "revoked":
        return False
    return True


def open_paid_control(layout: Any) -> ControlStore:
    """按存储布局打开（必要时创建）本机控制库，供 journal/授权使用。"""
    control = ControlStore(layout)
    try:
        control.open(create=True, write=True)
    except Exception:
        control.close()
        raise
    return control
