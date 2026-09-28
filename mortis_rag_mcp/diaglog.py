"""本地诊断日志模块（v0.8.0 F6）。

约束与设计原因：
1. 纯标准库实现：零第三方依赖，绝不引入外部日志框架以防破坏 stdio 纯净性。
2. 绝对静默保护：诊断日志属于旁路可观测性设施，任何写失败（磁盘满、权限不足、路径只读）
   必须静默吞掉（try-except pass），绝不重试、绝不写 stderr/stdout、绝不阻断主检索流程。
3. 严格字段白名单（10 键）：绝对禁止记录 query、正文/片段、文件名与绝对路径、标签、别名、
   URL、密码密钥或原始异常堆栈（防止日志成为隐私泄露源）。
4. stage 4阶段覆盖：kb_search 链路按 sync -> retrieve -> rerank -> serialize 分阶段记录，
   失败时记录 fail，同一个调用共享同一个 corr_id。
5. 上下文隔离（ContextVar）：使用 contextvars 隔离后台预索引线程与当前工具调用，
   防止多线程或后台任务污染请求级 corr_id；稳定单次挂载避免动态猴子补丁层叠与并发竞态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable
from contextvars import ContextVar

# 10 键白名单，超出此名单的任何字段绝对禁止落盘
WHITELIST_KEYS = frozenset({
    "ts",
    "corr_id",
    "tool",
    "stage",
    "ms",
    "result_count",
    "response_bytes",
    "truncated",
    "error_code",
    "version",
})

# 严格限定枚举值，非法 stage/error_code 拒绝写入或规整，确保下游分析数据干净
ALLOWED_STAGES = frozenset({"sync", "retrieve", "rerank", "serialize", "fail"})
ALLOWED_ERROR_CODES = frozenset({"value_error", "os_error", "provider_error", "unknown"})


@dataclass
class _CallTrace:
    corr_id: str
    tool_name: str
    config: Any
    sync_ms: float = 0.0
    retrieve_ms: float = 0.0
    rerank_ms: float = 0.0
    sync_called: bool = False
    retrieve_called: bool = False
    rerank_called: bool = False
    emitted_stages: set[str] = field(default_factory=set)

    def add_sync_ms(self, duration: float) -> None:
        self.sync_ms = round(self.sync_ms + duration, 2)
        self.sync_called = True

    def add_retrieve_ms(self, duration: float) -> None:
        self.retrieve_ms = round(self.retrieve_ms + duration, 2)
        self.retrieve_called = True

    def add_rerank_ms(self, duration: float) -> None:
        self.rerank_ms = round(self.rerank_ms + duration, 2)
        self.rerank_called = True

    def emit_search_stages(self) -> None:
        """在 kb_search 成功完成 handler 后，按契约顺序 (sync -> retrieve -> rerank) 各输出一行。"""
        if "sync" not in self.emitted_stages:
            record(corr_id=self.corr_id, tool=self.tool_name, stage="sync", ms=self.sync_ms, config=self.config)
            self.emitted_stages.add("sync")
        if "retrieve" not in self.emitted_stages:
            record(corr_id=self.corr_id, tool=self.tool_name, stage="retrieve", ms=self.retrieve_ms, config=self.config)
            self.emitted_stages.add("retrieve")
        if "rerank" not in self.emitted_stages:
            record(corr_id=self.corr_id, tool=self.tool_name, stage="rerank", ms=self.rerank_ms, config=self.config)
            self.emitted_stages.add("rerank")

    def emit_partial_stages(self) -> None:
        """当 kb_search 执行异常时，仅回放已确认完成的阶段，便于定位失败阶段。"""
        if self.tool_name != "kb_search":
            return
        if self.sync_called and "sync" not in self.emitted_stages:
            record(corr_id=self.corr_id, tool=self.tool_name, stage="sync", ms=self.sync_ms, config=self.config)
            self.emitted_stages.add("sync")
        if self.retrieve_called and "retrieve" not in self.emitted_stages:
            record(corr_id=self.corr_id, tool=self.tool_name, stage="retrieve", ms=self.retrieve_ms, config=self.config)
            self.emitted_stages.add("retrieve")
        if self.rerank_called and "rerank" not in self.emitted_stages:
            record(corr_id=self.corr_id, tool=self.tool_name, stage="rerank", ms=self.rerank_ms, config=self.config)
            self.emitted_stages.add("rerank")


_current_trace: ContextVar[_CallTrace | None] = ContextVar("_current_trace", default=None)


def generate_corr_id() -> str:
    """生成进程启动或请求级随机 hex，跨请求不作持久标识以遵守隐私约束。"""
    return secrets.token_hex(8)


def classify_error(exc: BaseException) -> str:
    """将异常安全映射至有限集合错误码，严禁输出原始异常堆栈或错误描述。"""
    if isinstance(exc, ValueError):
        return "value_error"
    if isinstance(exc, OSError):
        return "os_error"
    exc_type = type(exc)
    if "ProviderError" in exc_type.__name__ or any("ProviderError" in b.__name__ for b in exc_type.__mro__):
        return "provider_error"
    return "unknown"


def extract_result_count(raw_result: Any) -> int | None:
    """从结果 dict 的 chunks/files/groups 键安全提取结果条数，取不到则返回 None（不写该键）。"""
    if not isinstance(raw_result, dict):
        return None
    if "chunks" in raw_result and isinstance(raw_result["chunks"], list):
        return len(raw_result["chunks"])
    if "files" in raw_result and isinstance(raw_result["files"], list):
        return len(raw_result["files"])
    if "groups" in raw_result and isinstance(raw_result["groups"], list):
        count = 0
        for g in raw_result["groups"]:
            if isinstance(g, dict) and "chunks" in g and isinstance(g["chunks"], list):
                count += len(g["chunks"])
            else:
                count += 1
        return count
    return None


def _cleanup_retention(log_dir: Path, retention_days: int) -> None:
    """惰性清理超过保留天数的旧诊断日志文件。写前清理，避免磁盘堆积。"""
    try:
        cutoff = time.time() - (retention_days * 86400)
        for p in log_dir.glob("diag.log*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


def _rotate_log(log_file: Path, files: int) -> None:
    """日志轮转：超出 max_bytes 时将 diag.log 轮转为 .1，保留 files 代。
    使用 os.replace 在 Windows 与 POSIX 下均具备原子覆盖语义。
    """
    try:
        if files <= 1:
            log_file.unlink(missing_ok=True)
            return
        # 删除超出代际的最老文件
        oldest = log_file.with_name(f"{log_file.name}.{files - 1}")
        if oldest.exists():
            try:
                oldest.unlink(missing_ok=True)
            except OSError:
                pass
        # 逐代后移：如 diag.log.1 -> diag.log.2
        for i in range(files - 2, 0, -1):
            src = log_file.with_name(f"{log_file.name}.{i}")
            dst = log_file.with_name(f"{log_file.name}.{i + 1}")
            if src.exists():
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        # 当前活跃日志推为 .1
        first = log_file.with_name(f"{log_file.name}.1")
        os.replace(log_file, first)
    except Exception:
        pass


_write_lock = threading.Lock()


def record(
    corr_id: str,
    tool: str,
    stage: str,
    ms: float,
    *,
    result_count: int | None = None,
    response_bytes: int | None = None,
    truncated: bool | None = None,
    error_code: str | None = None,
    version: str = "0.8.0",
    config: Any = None,
    ts: str | None = None,
    **extra: Any,
) -> None:
    """写入单条诊断日志（jsonl 格式）。

    铁律：
    - 未开启时零文件系统操作、零耗时开销；
    - 任何写入异常一律静默吞掉，绝不抛出、绝不打印至 stderr/stdout；
    - 仅落盘白名单内的 10 个键，所有额外透传参数一律剔除。
    """
    if config is None:
        active = _current_trace.get()
        if active is not None:
            config = active.config

    if config is None or not getattr(config, "enabled", False):
        return

    stage_str = str(stage).strip().lower()
    if stage_str not in ALLOWED_STAGES:
        return

    # 构建并过滤字典，严格符合 10 键白名单
    entry: dict[str, Any] = {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "corr_id": str(corr_id),
        "tool": str(tool),
        "stage": stage_str,
        "ms": round(float(ms), 2),
        "version": str(version or "0.8.0"),
    }
    if result_count is not None:
        try:
            entry["result_count"] = int(result_count)
        except (ValueError, TypeError):
            pass
    if response_bytes is not None:
        try:
            entry["response_bytes"] = int(response_bytes)
        except (ValueError, TypeError):
            pass
    if truncated is not None:
        entry["truncated"] = bool(truncated)
    if error_code is not None:
        err_str = str(error_code).strip().lower()
        entry["error_code"] = err_str if err_str in ALLOWED_ERROR_CODES else "unknown"

    # 双重白名单过滤，确保 entry 绝无白名单外的任何键
    entry = {k: v for k, v in entry.items() if k in WHITELIST_KEYS and v is not None}

    try:
        log_dir = Path(getattr(config, "dir", "~/.mortis_rag_mcp")).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)

        retention_days = int(getattr(config, "retention_days", 7))
        if retention_days > 0:
            _cleanup_retention(log_dir, retention_days)

        log_file = log_dir / "diag.log"
        max_bytes = int(getattr(config, "max_bytes", 1048576))
        files = int(getattr(config, "files", 2))

        line_bytes = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")

        with _write_lock:
            if log_file.exists() and (log_file.stat().st_size + len(line_bytes) > max_bytes):
                _rotate_log(log_file, files)

            with open(log_file, "ab") as f:
                f.write(line_bytes)
    except Exception:
        # 铁律：写失败静默吞掉，绝不阻断主流程，绝不污染 stderr
        pass


class _IndexerProxy:
    """知识库索引器代理，用于在一次请求中统计 sync 与 retrieve 各阶段耗时。
    不直接修改全局共享的 MarkdownIndexer 实例属性，避免多线程并发互相覆盖状态。
    """

    def __init__(self, target: Any, trace: _CallTrace) -> None:
        self._target = target
        self._trace = trace

    def try_sync_with_guard(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        res = self._target.try_sync_with_guard(*args, **kwargs)
        duration = round((time.perf_counter() - t0) * 1000, 2)
        self._trace.add_sync_ms(duration)
        return res

    def sync(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        res = self._target.sync(*args, **kwargs)
        duration = round((time.perf_counter() - t0) * 1000, 2)
        self._trace.add_sync_ms(duration)
        return res

    def search(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        rerank_before = self._trace.rerank_ms
        res = self._target.search(*args, **kwargs)
        total_ms = round((time.perf_counter() - t0) * 1000, 2)
        # 精确扣减内部嵌套发生的 rerank 耗时，确保 retrieve 与 rerank 耗时互斥且不重复累加
        rerank_during = round(self._trace.rerank_ms - rerank_before, 2)
        retrieve_ms = max(0.0, round(total_ms - rerank_during, 2))
        self._trace.add_retrieve_ms(retrieve_ms)
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


_orig_search_rerank: Callable[..., Any] | None = None
_orig_fanout_rerank: Callable[..., Any] | None = None
_rerank_hooked: bool = False


def _ensure_rerank_hooked() -> None:
    """一次性全局挂载 rerank 拦截钩子。
    绝不在每次请求内反复覆写模块属性，彻底杜绝多线程并发竞态与闭包层叠内存泄漏。
    """
    global _orig_search_rerank, _orig_fanout_rerank, _rerank_hooked
    if _rerank_hooked:
        return

    try:
        from ._indexer import search as _s_mod
        _orig_search_rerank = getattr(_s_mod, "rerank_chunks", None)
        if _orig_search_rerank is not None:
            def _hooked_s_rerank(query: str, ranked: list[Any], reranker_provider: Any, cap: int = 60) -> list[Any]:
                trace = _current_trace.get()
                if trace is None or not getattr(trace.config, "enabled", False):
                    return _orig_search_rerank(query, ranked, reranker_provider, cap=cap)
                t0 = time.perf_counter()
                res = _orig_search_rerank(query, ranked, reranker_provider, cap=cap)
                duration = round((time.perf_counter() - t0) * 1000, 2)
                trace.add_rerank_ms(duration)
                return res

            _s_mod.rerank_chunks = _hooked_s_rerank
    except Exception:
        pass

    try:
        from ._server import fanout as _f_mod
        _orig_fanout_rerank = getattr(_f_mod, "rerank_chunks", None)
        if _orig_fanout_rerank is not None:
            def _hooked_f_rerank(query: str, ranked: list[Any], reranker_provider: Any, cap: int = 60) -> list[Any]:
                trace = _current_trace.get()
                if trace is None or not getattr(trace.config, "enabled", False):
                    return _orig_fanout_rerank(query, ranked, reranker_provider, cap=cap)
                t0 = time.perf_counter()
                res = _orig_fanout_rerank(query, ranked, reranker_provider, cap=cap)
                duration = round((time.perf_counter() - t0) * 1000, 2)
                trace.add_rerank_ms(duration)
                return res

            _f_mod.rerank_chunks = _hooked_f_rerank
    except Exception:
        pass

    _rerank_hooked = True


def _ensure_server_hooked(server: Any) -> None:
    """一次性将 server._indexer_for 包装为支持 Proxy 的安全函数，绝不反复多重嵌套。"""
    if getattr(server, "_diag_indexer_hooked", False):
        return
    orig_indexer_for = getattr(server, "_indexer_for", None)
    if orig_indexer_for is None:
        return

    def _instrumented_indexer_for(args: dict[str, Any]) -> Any:
        real_indexer = orig_indexer_for(args)
        trace = _current_trace.get()
        if trace is None or not getattr(trace.config, "enabled", False):
            return real_indexer
        return _IndexerProxy(real_indexer, trace)

    server._indexer_for = _instrumented_indexer_for
    server._diag_indexer_hooked = True


def instrument_call(
    server: Any,
    tool_name: str,
    corr_id: str,
    handler: Callable[[dict[str, Any]], Any],
    arguments: dict[str, Any],
    config: Any,
) -> Any:
    """在 call_tool 内部拦截工具执行过程，捕获并记录生命周期各阶段。"""
    _ensure_rerank_hooked()
    _ensure_server_hooked(server)

    trace = _CallTrace(
        corr_id=corr_id,
        tool_name=tool_name,
        config=config,
    )
    token = _current_trace.set(trace)

    try:
        result = handler(arguments)
        if tool_name == "kb_search":
            trace.emit_search_stages()
        return result
    except Exception:
        trace.emit_partial_stages()
        raise
    finally:
        _current_trace.reset(token)
