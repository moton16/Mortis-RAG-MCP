"""Windows 原生目录监听：用 ctypes 直接调 Win32 ``ReadDirectoryChangesW``。

存在理由
--------
``indexer.py`` 的监听循环目前是 0.25 秒一次的**轮询**：每轮 ``_quick_signatures()``
都要 ``rglob`` 整个库再对每个文件 stat 两次，文件一多 CPU/IO 成本就上去了，而且
两次轮询之间的改动最长要等 250ms 才被看到。Windows 提供了
``ReadDirectoryChangesW``——系统事件驱动，线程阻塞在内核里等通知，空闲时零 CPU，
事件到达后毫秒级唤醒。

为什么是 ctypes
--------------
本项目坚守"零依赖"（纯标准库 + Python 3.10+），所以不能用 watchdog、不能用
pywin32。``ctypes`` / ``ctypes.wintypes`` 都在标准库里，直接声明函数原型即可调用
Win32 API，代价只是要自己解析 ``FILE_NOTIFY_INFORMATION`` 结构体链表。

为什么用 overlapped I/O
----------------------
常见的简化写法是"同步阻塞调 ReadDirectoryChangesW，stop 时 CloseHandle 把阻塞
调用打断"。这在 Windows 上是**未定义行为**——关闭一个仍有 I/O 挂起的文件句柄，
内核要拆一个还没完成的 IRP。实测在 Windows 11 / Python 3.13 上，关闭句柄的瞬间
整个进程被硬杀（没有 Python 异常、没有 faulthandler 输出、连父 shell 一起没
掉），完全不可接受。

所以这里用 overlapped（异步）I/O：

* 目录句柄带 ``FILE_FLAG_OVERLAPPED`` 打开，读请求立即返回，实际完成由事件通知；
* 每次发完读请求后 ``WaitForMultipleObjects`` 同时等两个事件——"有变更"和
  "要停止"；
* ``stop()`` 只做 ``SetEvent``，线程从等待里醒来后自己 ``CancelIo`` + 等 I/O 真正
  落地，再关句柄。关闭句柄时上面已经没有任何挂起 I/O，是干净路径。

平台限制
--------
仅 Windows（``sys.platform == "win32"``）。非 Windows 平台可以安全 import 本模块：
``watcher_available()`` 返回 False，``parse_notify_buffer()`` 是纯函数可正常使用，
不会尝试加载 kernel32，也不会 import ``ctypes.wintypes``。调用方应先用
``watcher_available()`` 探测，不可用时降级回轮询。

当前状态
--------
本模块只提供能力，尚未接入 ``indexer.py``——集成由后续改动完成。
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from typing import Any, Callable

# --------------------------------------------------------------------- 动作常量
# FILE_NOTIFY_INFORMATION.Action 的取值（Win32 官方定义）。
FILE_ACTION_ADDED = 1
FILE_ACTION_REMOVED = 2
FILE_ACTION_MODIFIED = 3
FILE_ACTION_RENAMED_OLD_NAME = 4
FILE_ACTION_RENAMED_NEW_NAME = 5

# --------------------------------------------------------------------- Win32 常量
# 纯 Python 数值，放模块级是安全的（非 Windows 平台 import 不会炸）。
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
# 打开**目录**句柄必须带 BACKUP_SEMANTICS，否则 CreateFileW 报 ERROR_ACCESS_DENIED。
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
# 打开成异步句柄，才能用事件通知 + 可取消（见模块 docstring）。
FILE_FLAG_OVERLAPPED = 0x40000000

FILE_NOTIFY_CHANGE_FILE_NAME = 0x001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x002
FILE_NOTIFY_CHANGE_ATTRIBUTES = 0x004
FILE_NOTIFY_CHANGE_SIZE = 0x008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x010

NOTIFY_FILTER = (
    FILE_NOTIFY_CHANGE_FILE_NAME
    | FILE_NOTIFY_CHANGE_DIR_NAME
    | FILE_NOTIFY_CHANGE_LAST_WRITE
    | FILE_NOTIFY_CHANGE_SIZE
    | FILE_NOTIFY_CHANGE_ATTRIBUTES
)

# 变更太多、内核缓冲区装不下时，ReadDirectoryChangesW 以这个错误失败——具体哪些
# 文件变了已经不可知，必须做一次全量同步。
ERROR_NOTIFY_ENUM_DIR = 1022
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995

INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
WAIT_ABANDONED_0 = 128
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF

# 单次读取的缓冲区大小。超出部分会触发 ERROR_NOTIFY_ENUM_DIR。
# 刻意保持 64KB 不放大：MSDN 明确写了网络目录（UNC / 映射盘）上超过 64KB 会
# 直接报 ERROR_INVALID_PARAMETER，而本模块对 "issued == False" 的处理是退出
# 监听线程并静默降级为轮询 —— 放大会让网络盘上的库永久失去原生监听。
# 溢出频率靠 _OVERFLOW_BACKOFF_* 的退避来兜，而不是靠加大缓冲区。
BUFFER_SIZE = 64 * 1024

# 缓冲区溢出后的退避：不 sleep 直接重试会形成紧密自旋（100% CPU），
# 而且每次循环都派发一次全量同步。连续溢出时指数退避，成功读取一次即清零。
_OVERFLOW_BACKOFF_BASE = 0.05
_OVERFLOW_BACKOFF_MAX = 1.0

# FILE_NOTIFY_INFORMATION 三个 DWORD 头（NextEntryOffset / Action /
# FileNameLength）的总字节数，FileName 从第 12 字节开始。
_HEADER_SIZE = 12

# stop() 时 join 线程的兜底超时，绝不等死。
_JOIN_TIMEOUT = 2.0


class _OVERLAPPED(ctypes.Structure):
    """Win32 OVERLAPPED。只用 ctypes（不 import wintypes），所以模块级定义是跨平台的。

    字段布局必须与 Win32 一致；异步 I/O 期间这块内存必须保持有效，所以实例常驻在
    监听线程里。
    """

    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


# --------------------------------------------------------------------- 惰性加载
# ctypes.WinDLL("kernel32") 不能在模块 import 时执行，否则非 Windows 平台 import
# 本模块会直接抛异常。因此放到第一次真正需要时才加载，并缓存结果。
_kernel32: Any = None
_kernel32_lock = threading.Lock()
_kernel32_failed = object()


def _declare_prototypes(dll: Any) -> None:
    """给用到的 Win32 函数设置 argtypes/restype。

    不设 argtypes 时 ctypes 按 C 默认规则推断，指针/句柄很容易传错；显式声明后
    ctypes 会做类型检查并正确传参，``use_last_error`` 还会把 GetLastError 存下来
    供 ``ctypes.get_last_error()`` 取用。
    """
    from ctypes import wintypes  # 只在 win32 分支内 import

    dll.CreateFileW.argtypes = [
        wintypes.LPCWSTR,  # lpFileName
        wintypes.DWORD,  # dwDesiredAccess
        wintypes.DWORD,  # dwShareMode
        ctypes.c_void_p,  # lpSecurityAttributes
        wintypes.DWORD,  # dwCreationDisposition
        wintypes.DWORD,  # dwFlagsAndAttributes
        wintypes.HANDLE,  # hTemplateFile
    ]
    dll.CreateFileW.restype = wintypes.HANDLE

    dll.ReadDirectoryChangesW.argtypes = [
        wintypes.HANDLE,  # hDirectory
        ctypes.c_void_p,  # lpBuffer
        wintypes.DWORD,  # nBufferLength
        wintypes.BOOL,  # bWatchSubtree
        wintypes.DWORD,  # dwNotifyFilter
        ctypes.POINTER(wintypes.DWORD),  # lpBytesReturned（异步调用传 NULL）
        ctypes.c_void_p,  # lpOverlapped
        ctypes.c_void_p,  # lpCompletionRoutine
    ]
    dll.ReadDirectoryChangesW.restype = wintypes.BOOL

    dll.CreateEventW.argtypes = [
        ctypes.c_void_p,  # lpEventAttributes
        wintypes.BOOL,  # bManualReset
        wintypes.BOOL,  # bInitialState
        wintypes.LPCWSTR,  # lpName
    ]
    dll.CreateEventW.restype = wintypes.HANDLE

    dll.SetEvent.argtypes = [wintypes.HANDLE]
    dll.SetEvent.restype = wintypes.BOOL

    dll.ResetEvent.argtypes = [wintypes.HANDLE]
    dll.ResetEvent.restype = wintypes.BOOL

    dll.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,  # nCount
        ctypes.POINTER(wintypes.HANDLE),  # lpHandles
        wintypes.BOOL,  # bWaitAll
        wintypes.DWORD,  # dwMilliseconds
    ]
    dll.WaitForMultipleObjects.restype = wintypes.DWORD

    dll.GetOverlappedResult.argtypes = [
        wintypes.HANDLE,  # hFile
        ctypes.c_void_p,  # lpOverlapped
        ctypes.POINTER(wintypes.DWORD),  # lpNumberOfBytesTransferred
        wintypes.BOOL,  # bWait
    ]
    dll.GetOverlappedResult.restype = wintypes.BOOL

    # 取消**本线程**在该句柄上挂起的 I/O——读请求是监听线程发的，所以由它自己取消。
    dll.CancelIo.argtypes = [wintypes.HANDLE]
    dll.CancelIo.restype = wintypes.BOOL

    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL


def _load_kernel32() -> Any:
    """加载 kernel32 并声明原型；不可用（非 Windows / 加载异常）返回哨兵对象。"""
    if sys.platform != "win32":
        return _kernel32_failed
    try:
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        _declare_prototypes(dll)
        return dll
    except Exception:
        return _kernel32_failed


def kernel32() -> Any:
    """返回已加载的 kernel32，不可用返回 None。惰性加载 + 缓存。"""
    global _kernel32
    if _kernel32 is None:
        with _kernel32_lock:
            if _kernel32 is None:
                _kernel32 = _load_kernel32()
    return None if _kernel32 is _kernel32_failed else _kernel32


def watcher_available() -> bool:
    """本平台能否使用原生监听。调用方据此决定降级回轮询。"""
    return kernel32() is not None


# --------------------------------------------------------------------- 解析
def parse_notify_buffer(buffer: bytes) -> list[tuple[int, str]]:
    """解析 FILE_NOTIFY_INFORMATION 链表 -> [(Action, 相对路径), ...]。

    纯函数：不碰 ctypes，任何平台都能 import 和单测。

    结构体布局（小端）：:

        DWORD NextEntryOffset;  // 到下一条的字节偏移，0 表示最后一条
        DWORD Action;
        DWORD FileNameLength;   // 单位：**字节**（UTF-16LE，所以是字符数 * 2）
        WCHAR FileName[];       // 变长，非 NUL 结尾

    链表可能因为缓冲区截断而不完整，所以全程做边界检查：剩余字节不足头部、或声明
    的文件名长度越界时立即停止，返回**已成功解析的**条目，不抛异常。
    """
    events: list[tuple[int, str]] = []
    offset = 0
    total = len(buffer)
    while offset + _HEADER_SIZE <= total:
        next_offset, action, name_length = struct.unpack_from("<III", buffer, offset)
        start = offset + _HEADER_SIZE
        end = start + name_length
        if end > total:
            # 声明的长度超出缓冲区实际内容 -> 截断，丢弃这条及之后的。
            break
        try:
            name = buffer[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            break
        events.append((action, name))
        if next_offset == 0:
            break
        offset += next_offset
    return events


def _to_device_path(path: str) -> str:
    """长路径兜底：普通绝对路径超过 260 字符时 CreateFileW 会失败，加 ``\\\\?\\``
    前缀可绕过 Win32 路径规范化。仅在必要时使用，避免 verbatim 路径的副作用（不做
    规范化、".." 变字面量）。"""
    absolute = os.path.abspath(path)
    if len(absolute) > 240 and not absolute.startswith("\\\\?\\"):
        return "\\\\?\\" + absolute
    return absolute


# --------------------------------------------------------------------- Watcher
OnEvents = Callable[[list[tuple[int, str]] | None], None]


class WindowsDirectoryWatcher:
    """一个目录一个实例，内部一个 daemon 线程守着 ReadDirectoryChangesW。

    回调 ``on_events`` 的契约：

    * 收到事件时传 ``[(action, relative_path), ...]``——路径相对被监听目录，
      ``bWatchSubdirectory=True`` 时子目录下的文件带子目录前缀（用 ``\\`` 分隔）。
    * 传 ``None`` 表示"不确定具体变了什么，请做一次全量同步"（缓冲区溢出或状态
      不确定时）。

    线程内所有异常都被兜住，不会因为回调抛异常或 Win32 报错而崩线程；``stop()``
    后线程退出，句柄在确认没有挂起 I/O 之后才关闭。
    """

    def __init__(self, path: Any, on_events: OnEvents) -> None:
        self.path = path
        self.on_events = on_events
        self._lock = threading.Lock()
        self._dir_handle: Any = None
        self._io_event: Any = None
        self._stop_event: Any = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        """打开目录句柄并启动监听线程。

        句柄打开失败（路径不存在、权限不足、网络盘不支持、非 Windows）返回 False
        且**不启动线程**——调用方据此降级回轮询。已经在工作时返回 True（幂等）。
        """
        dll = kernel32()
        if dll is None:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True

        handle = self._open_handle(dll)
        if handle is None:
            return False
        # 停止事件用 manual-reset：一旦置位就一直有效，线程在发请求和等事件之间的
        # 空档里也能立刻看到，不会漏掉停止信号。
        stop_event = dll.CreateEventW(None, True, False, None)
        io_event = dll.CreateEventW(None, False, False, None)
        if not stop_event or not io_event:
            self._close(dll, handle, stop_event, io_event)
            return False

        with self._lock:
            self._dir_handle = handle
            self._stop_event = stop_event
            self._io_event = io_event
        self._closed.clear()
        self._thread = threading.Thread(
            target=self._run, args=(dll,), name="fsnotify-watcher", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = _JOIN_TIMEOUT) -> None:
        """通知线程退出并 join（带超时兜底）。幂等，可重复调用。

        注意这里不直接 CloseHandle：句柄上还挂着异步读请求，关它属于未定义行为
        （实测会把进程打死）。只置位停止事件，由线程自己 CancelIo、等 I/O 落地、
        再关句柄。
        """
        self._closed.set()
        dll = kernel32()
        if dll is not None:
            with self._lock:
                stop_event = self._stop_event
            if stop_event:
                try:
                    dll.SetEvent(stop_event)
                except Exception:
                    pass

        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread is None or not thread.is_alive():
            # 线程已退出（或根本没启动过）：拿走并关闭句柄，避免泄漏。
            with self._lock:
                handles = (self._dir_handle, self._stop_event, self._io_event)
                self._dir_handle = self._stop_event = self._io_event = None
            if dll is not None:
                self._close(dll, *handles)

    def is_alive(self) -> bool:
        """监听线程是否还在跑。调用方可据此发现 watcher 已死并降级回轮询。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def __enter__(self) -> WindowsDirectoryWatcher:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ 内部

    def _open_handle(self, dll: Any) -> Any:
        """CreateFileW 打开目录句柄（异步 + 备份语义），失败返回 None。"""
        invalid = ctypes.c_void_p(-1).value
        for candidate in (self.path, _to_device_path(str(self.path))):
            try:
                handle = dll.CreateFileW(
                    str(candidate),
                    FILE_LIST_DIRECTORY,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
                    None,
                )
            except Exception:
                return None
            if handle is not None and handle != invalid:
                return handle
        # 取一次错误信息（顺便把 last_error 消费掉）。失败原因不外抛，只以返回
        # False 的形式告诉调用方"用不了，请降级"。
        try:
            ctypes.WinError(ctypes.get_last_error())
        except Exception:
            pass
        return None

    @staticmethod
    def _close(dll: Any, *handles: Any) -> None:
        """关闭句柄，忽略 None 和任何异常。"""
        for handle in handles:
            if not handle:
                continue
            try:
                dll.CloseHandle(handle)
            except Exception:
                pass

    def _emit(self, payload: list[tuple[int, str]] | None) -> None:
        """回调。回调自己抛异常不能把监听线程带崩。"""
        try:
            self.on_events(payload)
        except Exception:
            pass

    def _run(self, dll: Any) -> None:
        """监听线程主循环：发异步读请求 -> 等"有变更"或"要停止"-> 处理 -> 重复。"""
        dll = dll or kernel32()
        if dll is None:
            return
        from ctypes import wintypes as wt  # 本函数只在 win32 上跑

        buffer = ctypes.create_string_buffer(BUFFER_SIZE)
        overlapped = _OVERLAPPED()
        transferred = wt.DWORD(0)
        wait_handles = (wt.HANDLE * 2)()
        # 连续缓冲区溢出计数，用于指数退避（见下方 ERROR_NOTIFY_ENUM_DIR 分支）。
        overflow_count = 0

        with self._lock:
            dir_handle = self._dir_handle
            io_event = self._io_event
            stop_event = self._stop_event
        if not dir_handle or not io_event or not stop_event:
            return
        overlapped.hEvent = io_event
        wait_handles[0] = io_event
        wait_handles[1] = stop_event

        try:
            while not self._closed.is_set():
                try:
                    dll.ResetEvent(io_event)
                    issued = dll.ReadDirectoryChangesW(
                        dir_handle,
                        buffer,
                        BUFFER_SIZE,
                        True,  # bWatchSubdirectory
                        NOTIFY_FILTER,
                        None,  # 异步调用：字节数从 GetOverlappedResult 取
                        ctypes.byref(overlapped),
                        None,
                    )
                except Exception:
                    return
                if not issued and ctypes.get_last_error() not in (0, ERROR_IO_PENDING):
                    # 连请求都没发出去（句柄失效等），状态已不确定。
                    self._emit(None)
                    return

                try:
                    wait_result = dll.WaitForMultipleObjects(
                        2, wait_handles, False, INFINITE
                    )
                except Exception:
                    return

                if wait_result == WAIT_OBJECT_0 + 1 or self._closed.is_set():
                    # 停止：先取消挂起的 I/O，等它真正结束，之后关句柄才是安全的。
                    try:
                        dll.CancelIo(dir_handle)
                        dll.GetOverlappedResult(
                            dir_handle, ctypes.byref(overlapped),
                            ctypes.byref(transferred), True,
                        )
                    except Exception:
                        pass
                    return
                if wait_result != WAIT_OBJECT_0:
                    # WAIT_TIMEOUT / WAIT_FAILED / WAIT_ABANDONED：不再继续。
                    return

                try:
                    transferred.value = 0
                    completed = dll.GetOverlappedResult(
                        dir_handle, ctypes.byref(overlapped),
                        ctypes.byref(transferred), False,
                    )
                except Exception:
                    return

                if not completed:
                    if ctypes.get_last_error() == ERROR_OPERATION_ABORTED:
                        return
                    # 典型是 ERROR_NOTIFY_ENUM_DIR：变更太多装不进缓冲区，只有一次
                    # 全量同步才能恢复一致。但立刻重试会紧密自旋（持续高频写入的
                    # 目录每次都会再次溢出），所以按连续溢出次数退避。
                    self._emit(None)
                    overflow_count += 1
                    time.sleep(
                        min(
                            _OVERFLOW_BACKOFF_BASE * (2 ** (overflow_count - 1)),
                            _OVERFLOW_BACKOFF_MAX,
                        )
                    )
                    continue

                overflow_count = 0
                if transferred.value <= 0:
                    continue
                events = parse_notify_buffer(buffer[: transferred.value])
                if events:
                    self._emit(events)
        finally:
            # 到这里已经没有挂起 I/O（正常停止时上面 CancelIo 等过了），可以安全关闭。
            with self._lock:
                # 只清掉自己这一代句柄：万一 start() 已经开了新的，别把新句柄关了。
                if self._dir_handle == dir_handle:
                    self._dir_handle = self._stop_event = self._io_event = None
            self._close(dll, dir_handle, io_event, stop_event)
