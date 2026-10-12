"""持久付费请求闸门：发送意图 journal + profile 授权（§20.7B / §20.7F）。

R2：正常启用配置不新增费用审批；明确撤销、持久意图和未知结果语义保留。

* **先落意图再发送**：任何非幂等计费 POST 在实际发出前必须 `before_send`，
  把 `request_id / payload_hash / endpoint / profile / attempt` 写进本机 control。
* **结果分类**：success 只在产物合同通过后落账；已确认媒体拒绝/坏响应复用
  abandoned + 固定 reason，不升级 schema。响应丢失（网络错误、5xx、429）一律
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

import time
import uuid
from typing import Any

from .doc_store import ControlStore, DocStoreError, StoreContractError
from .providers import PaidRequestRefused, ProviderError, _is_loopback_endpoint
from .request_lifecycle import OUTCOME_NOT_SENT

#: 发送意图状态：prepared = 已落意图但结果未知（重启后只能人工确认）。
INTENT_STATES = ("prepared", "success", "submission_unknown", "abandoned")

#: 未决意图：结果未知（可能已计费），禁止自动重发，只能人工确认/放弃。
UNRESOLVED_INTENT_STATES = ("prepared", "submission_unknown")

#: `prepared` 意图的租约上限（秒）。
#: 超过它仍未推进的 `prepared` 只可能来自两种情形：进程死在「已落意图、未落结果」
#: 之间，或结果落账失败——两者都不会再有代码路径去复核它，于是整条路由被永久冻住
#: （issue #13）。取传输层最长在飞时间（timeout 30s × 重试 3 + 退避上限 30s × 3
#: ≈ 180s）之上留余量，避免把「还在飞」的请求误判为泄漏。
#: `submission_unknown` **不适用**租约：它的结果可能已计费，永远需要人工对账。
INTENT_LEASE_SECONDS = 300.0

#: 落账失败后的补位间隔（秒）。控制库的锁竞争是瞬时的，一次补位能救回
#: 「请求已成功、只是没记上」的那类 prepared。
_MARK_RETRY_DELAY = 0.25


def intent_age(item: dict[str, Any], now: float | None = None) -> float:
    """意图自最后一次状态变更以来的存活秒数（不可解析按 0 处理，不误判为泄漏）。"""
    stamp = item.get("updated_at")
    if stamp is None:
        stamp = item.get("created_at")
    try:
        return max(0.0, (time.time() if now is None else now) - float(stamp))
    except (TypeError, ValueError):
        return 0.0


def is_stale_prepared(item: dict[str, Any], now: float | None = None,
                      lease: float = INTENT_LEASE_SECONDS) -> bool:
    """超租约的 `prepared`，且指向**零成本 loopback 端点** → 不再阻塞路由。

    只放宽零成本端点：付费端点的 `prepared` 即便超期也代表可能已计费的请求，
    自动放行等于丢掉对账凭据，必须留给人工确认（`--abandon-request`）。
    """
    if item.get("state") != "prepared":
        return False
    if intent_age(item, now) < lease:
        return False
    return _is_loopback_endpoint(str(item.get("endpoint") or ""))


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

    def before_send(self, payload_hash: str, endpoint: str, profile_fingerprint: str = "", *,
                    reject_unresolved: bool = True) -> str:
        """落一条持久意图并返回 request_id。

        两种情况都抛 `ProviderError`（fail closed）：
        控制面不可用；或同一载荷已有未决意图（结果未知，禁止自动重发）。

        `reject_unresolved=False`：供**组件账本管辖**的发送使用（该路径的重发只可能
        来自操作者显式确认，见 `RequestContext.ledger_managed`）。
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
                reject_unresolved=reject_unresolved,
            )
        except (DocStoreError, StoreContractError) as exc:
            if "PAID_REQUEST_UNRESOLVED" in str(exc):
                # 闸门**主动拒绝**：不能用 `ProviderError` 裸抛，否则会被
                # `providers._audit_before_send` 的非阻塞分支当成「审计写失败」吞掉，
                # 同一载荷会被第二次物理 POST（实机复现见 `PaidRequestRefused`）。
                raise PaidRequestRefused(
                    str(exc), outcome=OUTCOME_NOT_SENT, response_received=False,
                    remote_effect_unknown=False) from exc
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

    def _mark(self, request_id: str, state: str, reason: str = "",
              expected_states: tuple[str, ...] = ("prepared",)) -> bool:
        """落一次账；控制库上**可重试**的冲突（锁竞争 / busy）给一次补位机会。

        一次都不补的代价是：请求已经成功、向量已经取到，却因为一次锁竞争把意图
        永久留在 `prepared`，进而冻死整条路由（issue #13）。不可重试的错误
        （损坏 / 契约 / 绑定降级）保持原样立即抛出，不放宽 fail closed。
        """
        try:
            return self._resolve().mark_intent(request_id, state, reason,
                                               expected_states=expected_states)
        except DocStoreError as exc:
            if not getattr(exc, "retryable", False):
                raise
        time.sleep(_MARK_RETRY_DELAY)
        return self._resolve().mark_intent(request_id, state, reason,
                                           expected_states=expected_states)

    def mark_success(self, request_id: str) -> None:
        if not self._mark(request_id, "success", expected_states=("prepared",)):
            raise ProviderError("REQUEST_INTENT_FINALIZED: late response discarded")

    def mark_unknown(self, request_id: str, reason: str = "response_unconfirmed") -> None:
        self._mark(request_id, "submission_unknown", str(reason)[:200],
                   expected_states=("prepared",))

    def mark_not_sent(self, request_id: str, reason: str = "request_never_sent") -> None:
        """请求**确定没有发出**（本地校验/能力缺失，远端一个字都没收到）：终态化。

        复用 `abandoned` 终态（无 schema 变更），`reason` 区分「从未发出」与
        「响应不可用」/「人工放弃」。留成 `submission_unknown` 会把一条从未发出的
        请求当成未决付费意图，于是 `has_unresolved_paid_intents` 让整条路由停摆。
        只有 `prepared` 能转移，晚到的响应无法把它翻回来。
        """
        if self._mark(request_id, "abandoned", str(reason)[:200], expected_states=("prepared",)):
            return
        raise ProviderError("REQUEST_INTENT_FINALIZED: late response discarded")

    def mark_rejected(self, request_id: str, reason: str = "request_rejected") -> None:
        """远端**明确拒绝**这次请求（HTTP 4xx，远端未处理）：终态化。

        与 `mark_not_sent` 同型：复用 `abandoned` 终态、用 `reason` 区分来源。留成
        `submission_unknown` 会让一条确定被拒的请求永久留在未决集合（它不适用
        `INTENT_LEASE_SECONDS`），于是旧闸门下 `has_unresolved_paid_intents` 永久冻结
        整条付费路由——而远端一个字都没处理（`classify_http_status` 对 4xx 判
        `remote_effect_unknown=False`）。只有 `prepared` 能转移，晚到的响应翻不回来。
        """
        if self._mark(request_id, "abandoned", str(reason)[:200], expected_states=("prepared",)):
            return
        raise ProviderError("REQUEST_INTENT_FINALIZED: late response discarded")

    def mark_response_failed(self, request_id: str, reason: str = "media_response_contract_failed") -> None:
        """A confirmed unusable response is terminal, not success or unknown.

        Reuse the existing abandoned state (no schema change); reason distinguishes
        response rejection/contract failure from an operator abandoning an unknown.
        Only a prepared intent may transition, so late responses cannot undo abandon.
        """
        if reason not in {"media_response_contract_failed", "media_response_rejected"}:
            raise ValueError("unsupported confirmed media failure reason")
        if not self._mark(request_id, "abandoned", reason, expected_states=("prepared",)):
            raise ProviderError("REQUEST_INTENT_FINALIZED: late response discarded")

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
