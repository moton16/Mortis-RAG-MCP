"""PDF/Office 摄取层：MinerU 云端解析 → Markdown 落盘 .mortis-parsed/。

设计约束：默认关闭（IngestConfig.enabled=False）；默认手动，显式授权后可自动（auto_watch=True）；
纯标准库实现；云端失败可降级 pymupdf（可选依赖）。

v0.9.0（C93/C94）：`ingest.storage=virtual` 时走 `VirtualIngestWorker`（docstore 队列/租约/
发布 CAS），由 `make_ingest_manager()` 统一分派；默认仍是 legacy（物理镜像）——虚拟文档
的读取适配器属 C96（Lane C），提前切默认会让已摄取文档不可读。
"""
from .worker import (
    AGENT_EXTS,
    INGEST_EXTS,
    IngestManager,
    StoreMediaSink,
    VirtualIngestWorker,
    make_ingest_manager,
)

__all__ = [
    "IngestManager",
    "StoreMediaSink",
    "VirtualIngestWorker",
    "make_ingest_manager",
    "INGEST_EXTS",
    "AGENT_EXTS",
]
