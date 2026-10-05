from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..fsnotify import WindowsDirectoryWatcher, watcher_available
from .chunking import _INDEXABLE_TEXT_EXTS

if TYPE_CHECKING:
    from ..indexer import MarkdownIndexer


# native 监听回调的防抖延迟上限：编辑器保存风暴（事件持续不断）会让防抖定时器
# 一直顺延，超过这个窗口就必须同步一次，不能让事件流饿死同步。
_FS_MAX_DEBOUNCE_WAIT = 5.0


def start_watching(
    owner: MarkdownIndexer,
    interval: float = 0.25,
    debounce_seconds: float | None = None,
) -> None:
    if getattr(owner, "_stopping", False) and owner._watch_thread is not None:
        # 上一次 stop 还没真正收干净，先把残留线程收掉再启新的。
        owner._watch_thread.join(timeout=2)
        if owner._watch_thread.is_alive():
            return
        owner._watch_thread = None
        owner._stopping = False
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


def request_refresh(owner: MarkdownIndexer, *, immediate: bool = False) -> bool:
    if owner._watch_stop.is_set() or getattr(owner, "_stopping", False):
        return False
    if not Path(owner.vault_path).is_dir():
        with owner._cache_lock:
            owner._chunks = {}
            owner._signatures = {}
        return False
    _start_fs_scheduler(owner)
    with owner._fs_debounce_lock:
        now = time.monotonic()
        owner._refresh_requested_at = now
        if immediate:
            owner._fs_refresh_immediate = True
            owner._fs_requested = True
            owner._fs_pending_since = now
            owner._fs_debounce_cv.notify_all()
            return True
        min_interval = getattr(owner, "_READ_REFRESH_MIN_INTERVAL_SECONDS", 1.0)
        last_completed = getattr(owner, "_last_refresh_completed_at", 0.0)
        if (now - last_completed) < min_interval:
            return True
        owner._fs_requested = True
        if owner._fs_pending_since is None:
            owner._fs_pending_since = now
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
    """原生监听的回调：把「库里有动静」翻译成一次防抖后的全量 sync。

    正确性由 sync() 的全量 sha256 对账兜底。events=None（内核缓冲区溢出，
    具体改动不可知）也走同一条路。事件路径在这里做第一层过滤：Obsidian
    的 .obsidian/、缓存目录、以及被 ignore 规则排除的路径变动不需要唤醒
    全量 sync——递归监视看不到排除目录，所以必须过滤而不是指望不触发。
    """
    if events is not None:
        keep = False
        for _, rel in events:
            if _fs_event_matters(owner, rel):
                keep = True
                break
        if not keep:
            return
    with owner._fs_debounce_lock:
        now = time.monotonic()
        if owner._fs_pending_since is None:
            owner._fs_pending_since = now
        owner._fs_requested = True
        owner._fs_debounce_cv.notify_all()


def _fs_event_matters(owner: MarkdownIndexer, rel: str) -> bool:
    """事件路径是否需要触发全量 sync。"""
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
    # 基线必须取在 sync 之前：先 sync 后取基线的话，「sync 完成到取基线之间」
    # 落盘的改动会被当成已同步而从此丢失——线程刚启动时这个窗口最大（主线程
    # 往往在 watcher 线程第一次扫描前就写完了文件）。先取基线再 sync，两者
    # 之间出现的改动由随后的 sync 补上，之后的改动才由轮询发现。
    previous = owner._quick_signatures()
    owner._run_sync_quietly()
    while not owner._watch_stop.wait(interval):
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
            if owner._fs_scheduler_thread is None:
                owner._stopping = False
