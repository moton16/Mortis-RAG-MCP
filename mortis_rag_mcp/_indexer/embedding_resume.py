"""后台付费嵌入的续算层：原子占用、持久 hold、检查点确认与恢复（v0.9.2 §5）。

分工：`doc_store.ControlStore` 只提供**表与 CAS**；本模块负责调度语义：

* 一次后台批次 = 一批「稳定输入身份」（`component_key`）。占用、发送许可、结果
  记录、检查点确认全部走同一控制库，跨进程可见。
* 未知结果只把**受影响的组件**转成持久 hold，绝不冻结整个 profile，也不按 TTL
  自动解禁。
* 恢复扫描先取得短范围 dispatch 锁、**在锁内取快照**；逐批处置发生在锁外，由
  状态机 CAS（`expected_state` / `from_states` / `owner_token`）兜住——快照之后
  被推进过的批次一律拒绝，绝不被降级误判。锁期间不做网络等待与磁盘核验，
  不能只凭进程时间或租约到期就释放占用。

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

#: 恢复扫描默认考虑的中断态；`complete` 只在显式审计时纳入（避免每轮全表扫）。
RECOVERY_ACTIVE_STATES = ("reserved", "sending", "result_pending")


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
                           confirm_repeat_cost: bool = False, note: str = "",
                           expected_state: str = "") -> dict:
    """显式处置：`retry`（需确认可能重复计费）/ `abandon` / `release`。

    `expected_state`：调用方按快照做的判据（如「`reserved` 免确认」）必须一并传入，
    由 store 在同一事务内按刚读到的状态复核。
    """
    return control.resolve_embedding_work(batch_id, action=action,
                                          confirm_repeat_cost=confirm_repeat_cost,
                                          expected_state=expected_state, note=note)


def active_component_states(control: ControlStore, component_keys: Sequence[str]) -> dict[str, str]:
    """规划阶段过滤：held / 已在飞行的组件不得再次进入发送集合。"""
    return _states_for(control, component_keys)


# ------------------------------------------------------------------ 崩溃恢复


def recover_interrupted_batches(control: ControlStore, *, layout: StorageLayout,
                               owner_token: str, live_owners: Iterable[str] = (),
                               verify_recoverable: Any = None,
                               audit_complete: bool = False,
                               owner_predicate: Any = None,
                               timeout: float = 30.0,
                               states: Sequence[str] = RECOVERY_ACTIVE_STATES) -> dict:
    """重启恢复扫描：**快照在 dispatch 锁内取**，处置在锁外靠状态机 CAS 兜底。

    锁只保证「这批快照与另一个调度者的处置不交错」；它**不能**证明旧调度者在
    处置那一刻已退出（锁随后就释放了）。真正的保证是每个写操作都带状态判据
    （`expected_state` / `from_states` / `owner_token`）：快照后被推进过的批次
    一律拒绝并计入 `errors`，绝不谎报已处置。因此这里刻意**不**把改库循环放进
    锁内——那会让锁的持有时间随批次数与磁盘核验线性增长。

    | 中断点 | 处置 |
    |---|---|
    | `reserved` | 旧 owner 已不在 → 释放（状态保证此前没有发送），零自动重发 |
    | `sending` | 有可恢复成果 → 先推进 `result_pending` 再确认；否则转 `held_unknown` |
    | `result_pending` | 核验持久向量：有则 `complete`，无则 `held_result_missing` |
    | `complete` | 仅在 `audit_complete` 且成果不可恢复时转 `held_result_missing` |

    `owner_predicate`：可选过滤（接收 owner_token，返回是否可判定为「已退出」）。
    跨进程场景下**没有**可靠的存活证明时不得判定他人已死，调用方应只传入自己
    进程的 token；为 None 时沿用旧语义（只看 live_owners）。

    `verify_recoverable`：接收该批次的 `components` 列表（含 `embedding_key` /
    `chunk_id`），返回**被严格确认已持久化**的 `embedding_key` 集合。缺省 None =
    一律不可恢复（fail closed，走 held 路线）。

    `states`：只考虑这些批次状态。默认只扫中断态；`complete` 的成果审计必须走
    显式调用（`states=("complete",), audit_complete=True`），否则每轮同步都要全表
    回扫历史批次。

    返回处置明细；**不发送任何请求**。计数器只在控制库真的接受变更后自增，
    否则计入 `errors`（绝不谎报已处置）。
    """
    live = {str(item) for item in live_owners or ()} | {str(owner_token)}
    summary = {"released_reserved": 0, "held_sending": 0, "completed_pending": 0,
               "missing_result": 0, "held_complete": 0, "skipped_live": 0,
               "skipped_unproven": 0, "errors": []}
    try:
        with dispatch_lock(layout, timeout=timeout):
            work = control.list_embedding_work(states=tuple(states))
    except Exception as exc:  # noqa: BLE001 - 恢复不可用必须显式报告，不静默
        summary["errors"].append(f"BACKGROUND_RESUME_UNAVAILABLE: {exc}")
        return summary
    for batch in work:
        state = str(batch.get("state") or "")
        owner = str(batch.get("owner_token") or "")
        if owner in live:
            summary["skipped_live"] += 1
            continue
        if callable(owner_predicate) and not owner_predicate(owner):
            summary["skipped_unproven"] += 1
            continue
        batch_id = str(batch.get("batch_id") or "")
        components = list(batch.get("components") or ())
        keys = {str(c.get("embedding_key") or "") for c in components if c.get("embedding_key")}
        recoverable: set[str] = set()
        verify_ok = True
        if callable(verify_recoverable) and keys:
            try:
                recoverable = {str(item) for item in verify_recoverable(components) or ()}
            except Exception as exc:  # noqa: BLE001
                # 核验器不可用 ≠ 成果不存在。绝不能据此下发 held_*（那是不可逆的
                # 状态推进，且 `held_*` 会冻结整个 profile）。只记账并跳过本批次，
                # 让它保持当前状态（仍然占用输入身份、仍然阻塞重发）等下一轮。
                summary["errors"].append(f"verify_failed:{batch_id}:{exc}")
                verify_ok = False
        if not verify_ok:
            continue
        try:
            if state == "reserved":
                # `reserved` 从未进入 sending：状态本身证明此前没有发送。
                # 快照在锁内、处置在锁外，所以必须把快照状态交给 store 复核
                # （`expected_state`）：被推进过就拒绝，且**不得**计入已处置。
                result = control.resolve_embedding_work(
                    batch_id, action="release", note="recover:reserved-before-send",
                    expected_state="reserved")
                if result.get("applied"):
                    summary["released_reserved"] += 1
                else:
                    summary["errors"].append(
                        f"{batch_id}:{result.get('error') or 'RESUME_RELEASE_REFUSED'}")
                continue
            if state == "sending":
                if keys and keys <= recoverable:
                    # 有成果：先把「已收到响应」这件事落库（sending → result_pending），
                    # 再走检查点确认 —— `confirm_embedding_checkpoint` 的 CAS 只接受
                    # `result_pending`，直接对 `sending` 调用会整事务回滚、什么都没做成。
                    if not control.record_embedding_outcome(
                            batch_id, owner, OUTCOME_SUCCEEDED,
                            reason="recover:sending-artifact-persisted",
                            from_states=("sending",)):
                        summary["errors"].append(f"{batch_id}:RESUME_ADVANCE_REFUSED")
                        continue
                    result = control.confirm_embedding_checkpoint(batch_id, owner, recoverable)
                    if result.get("applied"):
                        summary["completed_pending"] += 1
                    else:
                        summary["errors"].append(f"{batch_id}:RESUME_CHECKPOINT_REFUSED")
                else:
                    # `from_states` 只认 `sending`：快照之后状态若已被推进（例如旧
                    # owner 其实拿到了响应并落了 `result_pending`），绝不能把它降级
                    # 成 `held_unknown` —— 那是"已成功"被误判成"结果未知"，且
                    # `held_*` 会冻结整条路由。拒绝并按未处置记账。
                    if control.record_embedding_outcome(
                            batch_id, owner, "transport_unknown",
                            reason="recover:sending-owner-lost",
                            component_state="held_unknown", from_states=("sending",)):
                        summary["held_sending"] += 1
                    else:
                        summary["errors"].append(f"{batch_id}:RESUME_HELD_REFUSED")
                continue
            if state == "result_pending":
                result = control.confirm_embedding_checkpoint(batch_id, owner, recoverable)
                if not result.get("applied"):
                    summary["errors"].append(f"{batch_id}:RESUME_CHECKPOINT_REFUSED")
                elif int(result.get("confirmed") or 0) == int(result.get("total") or 0):
                    summary["completed_pending"] += 1
                else:
                    summary["missing_result"] += 1
                continue
            if state == "complete" and audit_complete and keys:
                missing = sorted(keys - recoverable)
                if missing:
                    applied = control.record_embedding_outcome(
                        batch_id, owner, "response_invalid",
                        reason="recover:complete-artifact-missing",
                        component_state="held_result_missing", from_states=("complete",))
                    if applied:
                        summary["held_complete"] += 1
                    else:
                        summary["errors"].append(f"{batch_id}:RESUME_AUDIT_REFUSED")
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{batch_id}:{exc}")
    return summary


def held_summary(control: ControlStore) -> dict[str, Any]:
    """`kb_stats` 用的只读摘要：hold 与**在飞行中**的批次/组件数、最后更新时间。

    `active_*` 覆盖 `reserved` / `sending` / `result_pending`：它们同样占用输入身份
    且会阻塞后续轮次，但**不在** `HELD_COMPONENT_STATES` 里。中断残留的批次只出现在
    这里——只统计 hold 会让"卡住的活跃批次"完全不可见（v0.9.2 复核修正）。
    """
    if not control.resume_schema_present():
        # 续算台账还没建（例如从未跑过付费后台）：报 available=False，与"有表但为空"
        # 区分开，避免把"没有台账"读成"一切正常零 hold"。
        return {"available": False, "held_batches": 0, "held_components": 0,
                "active_batches": 0, "active_components": 0, "updated_at": 0.0}
    try:
        held = control.list_embedding_work(states=HELD_COMPONENT_STATES)
    except Exception:  # noqa: BLE001 - 不可读必须显式报告，不返回假空列表
        return {"available": False, "held_batches": 0, "held_components": 0,
                "active_batches": 0, "active_components": 0, "updated_at": 0.0}
    try:
        active = control.list_embedding_work(states=BATCH_ACTIVE_STATES)
    except Exception:  # noqa: BLE001 - 同上：不可读不能退化成"没有活跃批次"
        return {"available": False, "held_batches": 0, "held_components": 0,
                "active_batches": 0, "active_components": 0, "updated_at": 0.0}
    components = 0
    updated = 0.0
    for batch in held:
        rows = [c for c in batch.get("components") or () if c.get("state") in HELD_COMPONENT_STATES]
        components += len(rows)
        try:
            updated = max(updated, float(batch.get("updated_at") or 0.0))
        except (TypeError, ValueError):
            pass
    active_components = 0
    for batch in active:
        rows = [c for c in batch.get("components") or () if c.get("state") in BATCH_ACTIVE_STATES]
        active_components += len(rows)
        try:
            updated = max(updated, float(batch.get("updated_at") or 0.0))
        except (TypeError, ValueError):
            pass
    return {"available": True, "held_batches": len(held), "held_components": components,
            "active_batches": len(active), "active_components": active_components,
            "updated_at": updated}
