from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..fsnotify import WindowsDirectoryWatcher, watcher_available
from .chunking import _INDEXABLE_TEXT_EXTS

try:
    from ..ingest.worker import INGEST_EXTS
except ImportError:
    INGEST_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}

if TYPE_CHECKING:
    from ..indexer import MarkdownIndexer


# ---------------------------------------------------------------------------
# Architecture Note (Decoupling Text Sync from Ingest Scanning - Card C58c / C61):
#
# native event  .md/.txt       -> existing refresh flag -> vault-fs-debounce -> sync
#               PDF/Office    -> ingest dirty flag ----> vault-ingest-scan -> auto_submit
# poll/start/fallback cadence -+                                  |
#                                                          ingest-worker parse
#                                                                |
#                                           A4 callback -> immediate request_refresh
#
# Why text sync and ingest scanning are decoupled:
# 1. Latency & Resource Profile: Text sync is a millisecond-scale local file
#    reconciliation (hashing small markdown files and updating in-memory chunks/FTS).
#    Ingest scanning involves discovering and hashing large binary documents (PDF/Office)
#    and uploading them to cloud Mineru parsers, which takes seconds to minutes.
# 2. Worker Independence: Running document scanning or hashing inside the text
#    debounce scheduler (vault-fs-debounce) would stall all text sync operations and
#    starve real-time search updates.
# 3. Asymmetric Triggers: Pure PDF events must not trigger full-vault text syncs
#    until the ingest worker finishes parsing and writes the resulting .md file,
#    whereupon the A4 callback triggers an immediate request_refresh().
# ---------------------------------------------------------------------------

# native 监听回调的防抖延迟上限：编辑器保存风暴（事件持续不断）会让防抖定时器
# 一直顺延，超过这个窗口就必须同步一次，不能让事件流饿死同步。
_FS_MAX_DEBOUNCE_WAIT = 5.0
_DEFAULT_POLL_INGEST_INTERVAL = 30.0



def start_watching(
    owner: MarkdownIndexer,
    interval: float = 0.25,
    debounce_seconds: float | None = None,
) -> None:
    if getattr(owner, "_stopping", False):
        if owner._watch_thread is not None:
            # 上一次 stop 还没真正收干净，先把残留线程收掉再启新的。
            owner._watch_thread.join(timeout=2)
            if owner._watch_thread.is_alive():
                return
            owner._watch_thread = None
        if owner._ingest_worker_thread is not None:
            owner._ingest_worker_thread.join(timeout=2)
            if owner._ingest_worker_thread.is_alive():
                return
            owner._ingest_worker_thread = None
        owner._stopping = False
        owner._ingest_stopping = False
    else:
        owner._ingest_stopping = False
    if owner._watch_thread and owner._watch_thread.is_alive():
        return
    if owner._fs_watcher is not None and owner._fs_watcher.is_alive():
        return
    debounce = owner.config.debounce_seconds if debounce_seconds is None else debounce_seconds
    owner._fs_debounce_seconds = debounce
    # 注意：这里刻意不再内联 sync()。此前首轮全量索引在调用方线程里同步跑，
    # 而 start_watching 由首个工具调用（server._indexer_for）触发，于是
    # kb_init 返回的 "indexing: started in background" 是假的 —— 请求会
    # 阻塞到整个库索引 + embedding 完成（大库是分钟到小时级），客户端往往
    # 直接超时。首轮 sync 交给下面起的监听线程去做。
    owner._watch_stop.clear()
    method = owner.config.watch_method
    # auto：平台支持就用原生；native：优先原生（比如想在非 Windows 上显式
    # 表达意图）；两者启动失败都静默退回轮询，监听永不因此失效。
    if method in {"auto", "native"} and (method == "native" or watcher_available()):
        watcher = WindowsDirectoryWatcher(owner.vault_path, owner._on_fs_events)
        if watcher.start():
            owner._fs_watcher = watcher
            owner._watch_thread = threading.Thread(
                target=owner._native_watch_loop, args=(interval, debounce), daemon=True, name="vault-watch-native"
            )
            owner._watch_thread.start()
            owner._start_fs_scheduler()
            return
        owner._fs_watcher = None
    owner._watch_thread = threading.Thread(target=owner._watch_loop, args=(interval, debounce), daemon=True)
    owner._watch_thread.start()
    owner._start_fs_scheduler()


def request_refresh(owner: MarkdownIndexer, *, immediate: bool = False,
                    start_scheduler: bool = True, for_read: bool = False) -> bool:
    """登记一次刷新请求；返回 True 即**确实**保留了 pending（E04-a）。

    `start_scheduler=False` 只登记标志、不拉起调度线程：导入等已持有 mutation
    锁的调用方在**释放锁之后**用它登记下一轮，避免为"登记"而启动后台 sync。
    """
    if owner._watch_stop.is_set() or getattr(owner, "_stopping", False):
        return False
    if not Path(owner.vault_path).is_dir():
        return False
    if start_scheduler:
        _start_fs_scheduler(owner)
    with owner._fs_debounce_lock:
        now = time.monotonic()
        # 查询只是机会性刷新：刚完成的索引无需因每次轮询再排一轮。
        # 文件事件/import/显式请求不走此分支，接受仍等于真实保留 pending。
        if (for_read and not immediate and not owner._fs_requested
                and not getattr(owner, "_indexing", False)
                and getattr(owner, "_sync_state", "idle") == "idle"
                and not getattr(owner, "_refresh_error", None)
                and owner.last_sync is not None
                and time.time() - owner.last_sync < owner._READ_REFRESH_MIN_INTERVAL_SECONDS):
            return False
        owner._refresh_requested_at = now
        if immediate:
            owner._fs_refresh_immediate = True
        # E04-a：返回 True 就是"已安排下一轮"的承诺。此前在「最近完成 < min_interval」
        # 的合并窗口里直接 return True 却**不置 `_fs_requested`**，调用方以为已登记、
        # 调度器却永远等不到事件——刷新请求被静默丢弃。合并窗口只影响防抖起点，
        # 不能吞掉请求；真正的合并由调度器的 debounce 完成。
        if owner._fs_pending_since is None:
            owner._fs_pending_since = now
        owner._fs_requested = True
        owner._fs_debounce_cv.notify_all()
        return True


def refresh_status(owner: MarkdownIndexer) -> dict[str, Any]:
    with owner._fs_debounce_lock:
        pending = bool(owner._fs_requested)
        ref_err = getattr(owner, "_refresh_error", None)
    indexing = pending or getattr(owner, "_indexing", False) or (getattr(owner, "_sync_state", "idle") != "idle")
    progress_copy = dict(getattr(owner, "_sync_progress", {}))
    return {
        "last_sync": owner.last_sync,
        "refresh_pending": pending,
        "indexing_in_progress": indexing,
        "indexing_progress": progress_copy,
        "refresh_error": ref_err,
    }


INDEX_STATES = ("empty", "rebuilding", "unverified", "ready")


def index_state(owner: MarkdownIndexer) -> dict[str, Any]:
    """Additive 索引状态（E04-b / Q05）：single、fanout、import 共用同一口径。

    * `rebuilding`：还有可重建工作未完成（后台 sync 在跑或刷新已登记）；
    * `unverified`：无待重建但有隔离事实（exempt/unverified/deleted 文档）；
      `isolated_facts` 给出数量，**ready 不自动激活这些事实**；
    * `empty`：既无可见内容也无隔离事实；
    * `ready`：其余。
    """
    status = refresh_status(owner)
    pending = bool(status.get("indexing_in_progress")) or bool(getattr(owner, "_fs_requested", False))
    try:
        visible = len(getattr(owner, "_chunks", {}) or {})
    except Exception:
        visible = 0
    isolated = 0
    try:
        store = owner._existing_document_store()
        if store is not None:
            isolated = sum(1 for doc in store.list_documents() if str(doc.visibility) != "active")
    except Exception:
        isolated = 0
    if pending:
        state, action = "rebuilding", "wait for the background sync to finish, then retry"
    elif isolated:
        state, action = "unverified", (
            "isolated parsed facts are kept but not activated; re-parse or verify them, "
            "or import with trust_parsed_documents after checking the source SHA"
        )
    elif visible == 0:
        state, action = "empty", "run kb_init and a sync to build the index from local sources"
    else:
        state, action = "ready", ""
    return {"index_state": state, "isolated_facts": isolated,
            "visible_sources": visible, "next_action": action}


def request_ingest_scan(owner: MarkdownIndexer) -> bool:
    """Request an ingest scan for PDF/Office documents (coalesced).

    Returns True if request was accepted/coalesced; False if auto-ingest hook
    is not configured or indexer is stopping.
    """
    if owner._ingest_hook is None:
        return False
    if owner._watch_stop.is_set() or getattr(owner, "_stopping", False) or owner._ingest_stopping:
        return False
    _start_ingest_scan_worker(owner)
    with owner._ingest_lock:
        owner._ingest_dirty = True
        owner._ingest_cv.notify_all()
        return True


def _start_ingest_scan_worker(owner: MarkdownIndexer) -> None:
    if owner._ingest_hook is None:
        return
    if owner._watch_stop.is_set() or getattr(owner, "_stopping", False) or owner._ingest_stopping:
        return
    if owner._ingest_worker_thread is not None and owner._ingest_worker_thread.is_alive():
        return
    start_lock = getattr(owner, "_ingest_scan_start_lock", None)
    if start_lock is None:
        owner._ingest_scan_start_lock = threading.Lock()
        start_lock = owner._ingest_scan_start_lock
    with start_lock:
        if owner._watch_stop.is_set() or getattr(owner, "_stopping", False) or owner._ingest_stopping:
            return
        if owner._ingest_worker_thread is not None and owner._ingest_worker_thread.is_alive():
            return
        t = threading.Thread(
            target=owner._ingest_scan_loop, daemon=True, name="vault-ingest-scan"
        )
        owner._ingest_worker_thread = t
        t.start()


_MAX_INGEST_RESCAN_STREAK = 30
"""连续补扫上限（review R3 补丁）。

`rescan_after_seconds` 在「仍有判稳中的文件」或「本轮扫描不完整」时恒为正；若该条件
持久（长期 OSError 的目录、复制中途被删而残留的判稳登记），无上限补扫会把扫描循环
钉成 1Hz 永久重扫：每轮对全库重算 sha256 并重写状态文件。与 hook 异常分支的
「连续失败 ≤5 次」对称，这里给补扫也封顶；封顶后退回事件驱动。
"""


def _ingest_scan_loop(owner: MarkdownIndexer) -> None:
    """按需 ingest 扫描循环：合并 PDF/Office 变动事件，锁外调用 _ingest_hook。
    失败执行指数退避 (0.5s -> 5.0s)，避免紧密自旋。
    review R3 追加两条主动重扫（此前完全依赖下一个文件事件驱动）：
    1) hook 异常：退避后置回 dirty 重试，连续失败超过 5 次停止自动重试、退回事件驱动；
    2) hook 返回 rescan_after_seconds>0（仍有判稳中的文件或扫描不完整）：
       延时后置回 dirty 重扫——事件被防抖吞并/丢失时，复制中的文件不会永远滞留，
       连续补扫超过 _MAX_INGEST_RESCAN_STREAK 次后同样退回事件驱动。
    """
    while not (owner._watch_stop.is_set() or getattr(owner, "_stopping", False) or owner._ingest_stopping):
        with owner._ingest_lock:
            while not owner._ingest_dirty:
                if owner._watch_stop.is_set() or getattr(owner, "_stopping", False) or owner._ingest_stopping:
                    return
                owner._ingest_cv.wait(timeout=0.5)
            owner._ingest_dirty = False

        hook = owner._ingest_hook
        if hook is None:
            return
        rescan_after = 0.0
        try:
            result = hook()
            owner._last_ingest_scan_at = time.time()
            owner._ingest_scan_failures = 0
            owner._last_ingest_error = None
            if isinstance(result, dict):
                try:
                    rescan_after = max(0.0, float(result.get("rescan_after_seconds") or 0.0))
                except (TypeError, ValueError):
                    rescan_after = 0.0
        except Exception as exc:
            owner._ingest_scan_failures = getattr(owner, "_ingest_scan_failures", 0) + 1
            err_msg = str(exc)
            if len(err_msg) > 200:
                err_msg = err_msg[:197] + "..."
            owner._last_ingest_error = err_msg or exc.__class__.__name__
            backoff = min(0.5 * (2 ** min(owner._ingest_scan_failures - 1, 4)), 5.0)
            owner._watch_stop.wait(backoff)
            if owner._watch_stop.is_set() or getattr(owner, "_stopping", False) or owner._ingest_stopping:
                return
            if owner._ingest_scan_failures <= 5:
                # review R3/F5：临时扫描异常主动退避重扫，不再干等下一个事件。
                with owner._ingest_lock:
                    owner._ingest_dirty = True
                    owner._ingest_cv.notify_all()
            continue

        if rescan_after > 0:
            streak = getattr(owner, "_ingest_rescan_streak", 0) + 1
            owner._ingest_rescan_streak = streak
            if streak <= _MAX_INGEST_RESCAN_STREAK:
                owner._watch_stop.wait(rescan_after)
                if owner._watch_stop.is_set() or getattr(owner, "_stopping", False) or owner._ingest_stopping:
                    return
                with owner._ingest_lock:
                    owner._ingest_dirty = True
                    owner._ingest_cv.notify_all()
        else:
            # 本轮无需补扫（无判稳残留且枚举完整）→ 连续计数归零。
            owner._ingest_rescan_streak = 0


def _start_fs_scheduler(owner: MarkdownIndexer) -> None:
    if owner._watch_stop.is_set() or getattr(owner, "_stopping", False):
        return
    if owner._fs_scheduler_thread is not None and owner._fs_scheduler_thread.is_alive():
        return
    start_lock = getattr(owner, "_fs_scheduler_start_lock", None)
    if start_lock is None:
        owner._fs_scheduler_start_lock = threading.Lock()
        start_lock = owner._fs_scheduler_start_lock
    with start_lock:
        if owner._watch_stop.is_set() or getattr(owner, "_stopping", False):
            return
        if owner._fs_scheduler_thread is not None and owner._fs_scheduler_thread.is_alive():
            return
        t = threading.Thread(
            target=owner._fs_scheduler_loop, daemon=True, name="vault-fs-debounce"
        )
        owner._fs_scheduler_thread = t
        t.start()


def _fs_scheduler_loop(owner: MarkdownIndexer) -> None:
    """防抖调度：事件到达后，安静 debounce 秒才 sync；事件持续到达就顺延，
    但受 _FS_MAX_DEBOUNCE_WAIT 封顶（到期立即执行）。常驻单线程，
    替代此前「每事件新建/取消一个 threading.Timer」的线程洪泛。
    """
    while not owner._watch_stop.is_set():
        with owner._fs_debounce_lock:
            if not owner._fs_requested:
                owner._fs_debounce_cv.wait(timeout=0.5)
                continue
            now = time.monotonic()
            pending_since = owner._fs_pending_since or now
            elapsed = max(0.0, now - pending_since)
            if not getattr(owner, "_fs_refresh_immediate", False):
                if elapsed < owner._fs_debounce_seconds and elapsed < _FS_MAX_DEBOUNCE_WAIT:
                    wait_time = min(owner._fs_debounce_seconds - elapsed, _FS_MAX_DEBOUNCE_WAIT - elapsed)
                    owner._fs_debounce_cv.wait(timeout=max(0.01, wait_time))
                    continue  # 重新评估：期间又有事件则继续顺延
            # Clear dirty flags BEFORE sync
            owner._fs_requested = False
            owner._fs_refresh_immediate = False
            owner._fs_pending_since = None
            due = True
        if not due:
            continue
        if owner._watch_stop.is_set():
            return
        owner._run_sync_quietly()


def _on_fs_events(owner: MarkdownIndexer, events: list[tuple[int, str]] | None) -> None:
    """原生监听的回调：将文件事件分类为文本事件与摄取事件，分别派发。

    文本事件 -> vault-fs-debounce 调度器 -> 全量 sync
    摄取事件 -> vault-ingest-scan 调度器 -> auto_submit
    两者互不阻塞，纯 PDF 事件不触发全量文本 sync。
    """
    if events is None:
        has_text_event = True
        has_ingest_event = owner._ingest_hook is not None
    else:
        has_text_event = False
        has_ingest_event = False
        for _action, rel in events:
            if not _fs_event_path_allowed(owner, rel):
                continue
            if not rel:
                has_text_event = True
                if owner._ingest_hook is not None:
                    has_ingest_event = True
                continue
            lower = rel.replace("\\", "/").lower()
            is_text = any(lower.endswith(ext) for ext in _INDEXABLE_TEXT_EXTS)
            is_ingest = any(lower.endswith(ext) for ext in INGEST_EXTS)
            if is_text:
                has_text_event = True
            if is_ingest and owner._ingest_hook is not None:
                has_ingest_event = True
            if not is_text and not is_ingest:
                name = Path(rel).name
                if "." not in name:
                    has_text_event = True
                    if owner._ingest_hook is not None:
                        has_ingest_event = True

    if has_ingest_event:
        owner.request_ingest_scan()

    if has_text_event:
        with owner._fs_debounce_lock:
            now = time.monotonic()
            if owner._fs_pending_since is None:
                owner._fs_pending_since = now
            owner._fs_requested = True
            owner._fs_debounce_cv.notify_all()


def _fs_event_path_allowed(owner: MarkdownIndexer, rel: str) -> bool:
    """事件路径是否在允许范围内（非缓存、非排除目录）。"""
    if not rel:
        return True
    rel = rel.replace("\\", "/").lstrip("/")
    # 缓存落在 vault 内时，自己写缓存不能再次触发自己。
    if owner.config.cache.placement == "vault" and owner.config.cache.subdir:
        if rel.startswith(owner.config.cache.subdir.rstrip("/") + "/") or rel == owner.config.cache.subdir:
            return False
    lower = rel.lower()
    for pattern in owner.config.exclude_patterns:
        stripped = pattern.strip().strip("/").lower()
        if stripped and (lower == stripped or lower.startswith(stripped + "/")):
            return False
    return True


def _fs_event_matters(owner: MarkdownIndexer, rel: str) -> bool:
    """事件路径是否需要触发全量 sync（保持旧接口兼容）。"""
    if not _fs_event_path_allowed(owner, rel):
        return False
    if not rel:
        return True
    lower = rel.replace("\\", "/").lower()
    return any(lower.endswith(ext) for ext in _INDEXABLE_TEXT_EXTS)



def _run_sync_quietly(owner: MarkdownIndexer) -> None:
    """sync() 的守护包装：监听线程里的任何异常都不能把线程打死。

    轮询/兜底循环此前直接 try/except 包住 sync()，但异常吞掉后没有任何
    痕迹；这里统一加退避记账，避免端点持续故障时变成紧密自旋。
    """
    owner._indexing = True
    try:
        owner.sync()
        owner._sync_failures = 0
        owner._refresh_error = None
        owner._last_refresh_completed_at = time.monotonic()
    except Exception as exc:
        owner._sync_failures = getattr(owner, "_sync_failures", 0) + 1
        err_msg = str(exc)
        if len(err_msg) > 200:
            err_msg = err_msg[:197] + "..."
        owner._refresh_error = err_msg or exc.__class__.__name__
        backoff = min(0.5 * (2 ** min(owner._sync_failures - 1, 4)), 5.0)
        owner._watch_stop.wait(backoff)
    finally:
        owner._indexing = False


def _native_watch_loop(owner: MarkdownIndexer, interval: float, debounce: float) -> None:
    """原生监听生效期间的兜底循环，职责有二：

    1. 低频（watch_fallback_interval，默认 30s）全量对账，覆盖原生事件可能
       丢失的极端情况——sync 是全量 sha256 对账，多跑只是白花一点 IO；
    2. 盯住 watcher 线程存活性：一旦它退出（句柄失效等），退回全速轮询，
       监听永不静默失效。
    """
    fallback_interval = owner.config.watch_fallback_interval
    # 启动请求一次 ingest 扫描（处理启动时既有的待摄取文档）
    owner.request_ingest_scan()
    # 首轮全量 sync：watch_fallback_interval=0 时兜底循环一次都不会跑，
    # 没有这一句就永远等不到第一次索引。
    if not owner._watch_stop.is_set():
        owner._run_sync_quietly()
    while not owner._watch_stop.is_set():
        if owner._fs_watcher is None or not owner._fs_watcher.is_alive():
            break
        if owner._watch_stop.wait(fallback_interval if fallback_interval > 0 else interval):
            return
        if owner._fs_watcher is None or not owner._fs_watcher.is_alive():
            break
        if fallback_interval > 0:
            owner.request_ingest_scan()
            owner._run_sync_quietly()
    if owner._watch_stop.is_set():
        return
    # 降级：原生线程已退出，退回 0.25s 全速轮询（0.4.1 行为）。
    watcher = owner._fs_watcher
    owner._fs_watcher = None
    if watcher is not None:
        watcher.stop()
    owner._watch_loop(interval, debounce)


def _watch_loop(owner: MarkdownIndexer, interval: float, debounce: float) -> None:
    pending_since: float | None = None
    # 启动请求一次 ingest 扫描（处理启动时既有的待摄取文档）
    owner.request_ingest_scan()
    # 基线必须取在 sync 之前：先 sync 后取基线的话，「sync 完成到取基线之间」
    # 落盘的改动会被当成已同步而从此丢失——线程刚启动时这个窗口最大（主线程
    # 往往在 watcher 线程第一次扫描前就写完了文件）。先取基线再 sync，两者
    # 之间出现的改动由随后的 sync 补上，之后的改动才由轮询发现。
    previous = owner._quick_signatures()
    owner._run_sync_quietly()

    poll_ingest_interval = (
        owner.config.watch_fallback_interval
        if owner.config.watch_fallback_interval > 0
        else _DEFAULT_POLL_INGEST_INTERVAL
    )
    last_ingest_poll = time.monotonic()

    while not owner._watch_stop.wait(interval):
        now = time.monotonic()
        if (now - last_ingest_poll) >= poll_ingest_interval:
            owner.request_ingest_scan()
            last_ingest_poll = now
        try:
            current = owner._quick_signatures()
        except OSError:
            # rglob 与 stat 之间文件被删（Obsidian 的编辑器 churn 下很常见）
            # 此前会让整个线程死于 FileNotFoundError，监控从此静默关闭、
            # 服务用旧数据继续答搜索。跳过本轮，下一轮再试。
            time.sleep(0.05)
            continue
        if current != previous:
            pending_since = pending_since or time.monotonic()
            if time.monotonic() - pending_since >= debounce:
                owner._run_sync_quietly()
                previous = owner._quick_signatures()
                pending_since = None
        else:
            pending_since = None


def _quick_signatures(owner: MarkdownIndexer) -> dict[str, tuple[int, int]]:
    # 同一路径只 stat 一次：此前对每个文件 stat 两次（mtime_ns 一次、
    # size 一次），0.25s 轮询模式下把开销翻倍。
    out: dict[str, tuple[int, int]] = {}
    for path in owner._markdown_files():
        try:
            stat = path.stat()
        except OSError:
            continue  # 文件刚被删：跳过，下一轮自然消失
        out[owner._source(path)] = (stat.st_mtime_ns, stat.st_size)
    return out


def stop_watching(owner: MarkdownIndexer) -> None:
    owner._watch_stop.set()
    # 停止 ingest 扫描 worker
    owner._ingest_stopping = True
    with owner._ingest_lock:
        owner._ingest_dirty = False
        owner._ingest_cv.notify_all()
    if owner._ingest_worker_thread is not None:
        owner._ingest_worker_thread.join(timeout=2)
        if owner._ingest_worker_thread.is_alive():
            owner._stopping = True
        else:
            owner._ingest_worker_thread = None

    with owner._fs_debounce_lock:
        owner._fs_requested = False
        owner._fs_refresh_immediate = False
        owner._fs_pending_since = None
        owner._fs_debounce_cv.notify_all()
    if owner._fs_scheduler_thread is not None:
        owner._fs_scheduler_thread.join(timeout=2)
        if owner._fs_scheduler_thread.is_alive():
            owner._stopping = True
        else:
            owner._fs_scheduler_thread = None
    watcher, owner._fs_watcher = owner._fs_watcher, None
    if watcher is not None:
        watcher.stop()
    if owner._watch_thread is not None:
        owner._watch_thread.join(timeout=2)
        if owner._watch_thread.is_alive():
            # 线程没停就别把引用丢掉：持引用才能让下一次 stop_watching
            # 继续 join，也让 is_alive() 对外如实反映"还在跑"。
            # 丢掉引用会导致 kb_remove→kb_init 同一目录时新旧两个
            # watcher 并存，各自写同一批缓存文件（cache key 相同）。
            owner._stopping = True
        else:
            owner._watch_thread = None
            if owner._fs_scheduler_thread is None and owner._ingest_worker_thread is None:
                owner._stopping = False

