"""后台付费嵌入的续算层：原子占用、持久 hold、检查点确认与恢复（v0.9.2 §5）。

分工：`doc_store.ControlStore` 只提供**表与 CAS**；本模块负责调度语义：

* 一次后台批次 = 一批「稳定输入身份」（`component_key`）。占用、发送许可、结果
  记录、检查点确认全部走同一控制库，跨进程可见。
* 未知结果只把**受影响的组件**转成持久 hold，绝不冻结整个 profile，也不按 TTL
  自动解禁。
* 恢复扫描必须先取得短范围 dispatch 锁，证明旧调度者已不在执行该批网络边界，
  再处理中断状态；不能只凭进程时间或租约到期就释放占用。

本模块不读取用户配置、不拼接请求体、不调用 transport。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..doc_store import ControlStore, ResumeConflict, StorageLayout
from ..request_lifecycle import OUTCOME_SUCCEEDED, RequestOutcome

#: 组件占用状态：这些状态会阻塞同一输入身份的再次 reserve（唯一索引约束）。
ACTIVE_COMPONENT_STATES = (
    "reserved", "sending", "result_pending",
    "held_unknown", "held_response_invalid", "held_result_missing", "manual_only",
)

#: 需要人工/显式处置才能推进的 hold 状态。
HELD_COMPONENT_STATES = (
    "held_unknown", "held_response_invalid", "held_result_missing", "manual_only",
)

BATCH_ACTIVE_STATES = ("reserved", "sending", "result_pending")


def new_operation_id(purpose: str) -> str:
    """逻辑操作 id：一次 sync / 一次 query / 一次显式 retry 一个。"""
    return f"{purpose}:{uuid.uuid4().hex}"


def new_batch_id() -> str:
    return f"batch:{uuid.uuid4().hex}"


def owner_token_for(scope: str = "") -> str:
    """调度者实例 token：按**进程/调度者**生成并保持稳定，不能与 operation_id 混用。"""
    return f"pid{__import__('os').getpid()}:{scope or 'dispatch'}:{uuid.uuid4().hex}"


@contextmanager
def dispatch_lock(layout: StorageLayout, timeout: float = 30.0) -> Iterator[None]:
    """短范围跨进程调度锁（由现有 mutation 锁机制派生）。

    只在「恢复扫描 / 处置中断批次」这类需要证明旧调度者已退出的边界持有；
    网络等待期间**不**持有控制面锁。
    """
    from ..doc_store import _file_mutex_for
    base = Path(getattr(layout, "mutation_lock_path", "") or "")
    if not base.name:
        raise ValueError("dispatch lock requires a storage layout")
    mutex = _file_mutex_for(base.with_name("request_dispatch.lock"))
    if not mutex.acquire(timeout):
        raise RuntimeError("BACKGROUND_RESUME_UNAVAILABLE: dispatch lock busy")
    try:
        yield
    finally:
        mutex.release()


@dataclass(frozen=True, slots=True)
class Reservation:
    batch_id: str
    owner_token: str
    ok: bool
    components: int = 0
    error: str = ""
    #: 冲突时回读到的当前 active 状态（供调用方重新计划）。
    states: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendPermit:
    batch_id: str
    allowed: bool
    error: str = ""


def _states_for(control: ControlStore, keys: Sequence[str]) -> dict[str, str]:
    return control.embedding_component_states(list(keys))


def reserve_embedding_batch(control: ControlStore, plan: Mapping[str, Any],
                            owner_token: str) -> Reservation:
    """按计划原子占用一批组件。

    `plan` 至少包含 `batch_id / operation_id / purpose_family / profile_fingerprint /
    endpoint / components`。冲突时**整批回滚**：调用方必须重新读取状态后重新计划，
    不允许吞掉冲突继续发送。
    """
    components = list(plan.get("components") or ())
    keys = [str(item.get("component_key") or "") for item in components]
    try:
        control.reserve_embedding_batch(
            str(plan["batch_id"]),
            operation_id=str(plan["operation_id"]),
            purpose_family=str(plan["purpose_family"]),
            profile_fingerprint=str(plan["profile_fingerprint"]),
            endpoint=str(plan["endpoint"]),
            owner_token=owner_token,
            components=components,
            vault_epoch=int(plan.get("vault_epoch") or 0),
            generation_id=str(plan.get("generation_id") or ""),
            payload_hash=str(plan.get("payload_hash") or ""),
        )
    except ResumeConflict as exc:
        return Reservation(str(plan.get("batch_id") or ""), owner_token, False,
                           len(components), str(exc), _states_for(control, keys))
    return Reservation(str(plan["batch_id"]), owner_token, True, len(components))


def begin_embedding_send(control: ControlStore, batch_id: str, owner_token: str,
                         payload_hash: str = "") -> SendPermit:
    """取得发送许可。`reserved → sending` 必须**在调用 transport 前**持久提交。"""
    try:
        ok = control.begin_embedding_send(batch_id, owner_token, payload_hash)
    except Exception as exc:  # noqa: BLE001 - 提交失败 = 零 POST
        return SendPermit(batch_id, False, f"RESUME_BEGIN_SEND_FAILED: {exc}")
    return SendPermit(batch_id, bool(ok), "" if ok else "RESUME_SEND_NOT_PERMITTED")


def record_embedding_outcome(control: ControlStore, batch_id: str, owner_token: str,
                             outcome: RequestOutcome | str, **details: Any) -> bool:
    """记录结果。未知结果保留 active 占用（held），成功只推进到 `result_pending`。"""
    if isinstance(outcome, RequestOutcome):
        name = outcome.outcome
        status = outcome.http_status
        reason = outcome.detail
    else:
        name = str(outcome)
        status = details.get("http_status")
        reason = str(details.get("reason") or "")
    return control.record_embedding_outcome(batch_id, owner_token, name,
                                            http_status=status, reason=reason)


def confirm_embedding_checkpoint(control: ControlStore, batch_id: str, owner_token: str,
                                 persisted_keys: Iterable[str]) -> dict:
    """只把**被严格确认持久化**的输入键推进到 `complete`。"""
    return control.confirm_embedding_checkpoint(batch_id, owner_token, list(persisted_keys or ()))


def list_embedding_work(control: ControlStore, *, source: str = "", batch_id: str = "",
                        states: Sequence[str] = ()) -> list[dict]:
    return control.list_embedding_work(source=source, batch_id=batch_id, states=states)


def resolve_embedding_work(control: ControlStore, batch_id: str, *, action: str,
                           confirm_repeat_cost: bool = False, note: str = "") -> dict:
    """显式处置：`retry`（需确认可能重复计费）/ `abandon` / `release`。"""
    return control.resolve_embedding_work(batch_id, action=action,
                                          confirm_repeat_cost=confirm_repeat_cost, note=note)


def active_component_states(control: ControlStore, component_keys: Sequence[str]) -> dict[str, str]:
    """规划阶段过滤：held / 已在飞行的组件不得再次进入发送集合。"""
    return _states_for(control, component_keys)


# ------------------------------------------------------------------ 崩溃恢复


def recover_interrupted_batches(control: ControlStore, *, layout: StorageLayout,
                                owner_token: str, live_owners: Iterable[str] = (),
                                verify_recoverable: Any = None,
                                audit_complete: bool = False,
                                timeout: float = 30.0) -> dict:
    """重启恢复扫描：**必须先取得 dispatch 锁**才能判定旧调度者已退出。

    | 中断点 | 处置 |
    |---|---|
    | `reserved` | 旧 owner 已不在 → 释放（状态保证此前没有发送），零自动重发 |
    | `sending` | 有可能已发出 → 无可恢复成果时转 `held_unknown` |
    | `result_pending` | 核验持久向量：有则 `complete`，无则 `held_result_missing` |
    | `complete` | 仅在 `audit_complete` 且成果不可恢复时转 `held_result_missing` |

    返回处置明细；**不发送任何请求**。
    """
    live = {str(item) for item in live_owners or ()} | {str(owner_token)}
    summary = {"released_reserved": 0, "held_sending": 0, "completed_pending": 0,
               "missing_result": 0, "held_complete": 0, "skipped_live": 0, "errors": []}
    try:
        with dispatch_lock(layout, timeout=timeout):
            work = control.list_embedding_work()
    except Exception as exc:  # noqa: BLE001 - 恢复不可用必须显式报告，不静默
        summary["errors"].append(f"BACKGROUND_RESUME_UNAVAILABLE: {exc}")
        return summary
    for batch in work:
        state = str(batch.get("state") or "")
        owner = str(batch.get("owner_token") or "")
        if owner in live:
            summary["skipped_live"] += 1
            continue
        batch_id = str(batch.get("batch_id") or "")
        components = list(batch.get("components") or ())
        keys = [str(c.get("embedding_key") or "") for c in components]
        recoverable: set[str] = set()
        if callable(verify_recoverable) and keys:
            try:
                recoverable = {str(item) for item in verify_recoverable(keys) or ()}
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"verify_failed:{batch_id}:{exc}")
        try:
            if state == "reserved":
                # `reserved` 从未进入 sending：状态本身证明此前没有发送。
                control.resolve_embedding_work(batch_id, action="release",
                                               note="recover:reserved-before-send")
                summary["released_reserved"] += 1
                continue
            if state == "sending":
                if recoverable and len(recoverable) == len(keys) and keys:
                    control.confirm_embedding_checkpoint(batch_id, owner, recoverable)
                    summary["completed_pending"] += 1
                else:
                    control.record_embedding_outcome(
                        batch_id, owner, "transport_unknown",
                        reason="recover:sending-owner-lost", component_state="held_unknown")
                    summary["held_sending"] += 1
                continue
            if state == "result_pending":
                result = control.confirm_embedding_checkpoint(batch_id, owner, recoverable)
                if result.get("applied") and int(result.get("confirmed") or 0) == int(result.get("total") or 0):
                    summary["completed_pending"] += 1
                else:
                    summary["missing_result"] += 1
                continue
            if state == "complete" and audit_complete and keys:
                missing = [k for k in keys if k not in recoverable]
                if missing:
                    control.record_embedding_outcome(
                        batch_id, owner, "response_invalid",
                        reason="recover:complete-artifact-missing",
                        component_state="held_result_missing")
                    summary["held_complete"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{batch_id}:{exc}")
    return summary


def held_summary(control: ControlStore) -> dict[str, Any]:
    """`kb_stats` 用的只读摘要：当前 hold 的批次/组件数与最后更新时间。"""
    try:
        work = control.list_embedding_work(states=HELD_COMPONENT_STATES)
    except Exception:  # noqa: BLE001 - 不可读必须显式报告，不返回假空列表
        return {"available": False, "held_batches": 0, "held_components": 0, "updated_at": 0.0}
    components = 0
    updated = 0.0
    for batch in work:
        rows = [c for c in batch.get("components") or () if c.get("state") in HELD_COMPONENT_STATES]
        components += len(rows)
        try:
            updated = max(updated, float(batch.get("updated_at") or 0.0))
        except (TypeError, ValueError):
            pass
    return {"available": True, "held_batches": len(work), "held_components": components,
            "updated_at": updated}
