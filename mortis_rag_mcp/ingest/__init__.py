"""PDF/Office 摄取层：MinerU 云端解析 → Markdown 落盘 .mortis-parsed/。

设计约束：默认关闭（IngestConfig.enabled=False）；默认手动，显式授权后可自动（auto_watch=True）；
纯标准库实现；云端失败可降级 pymupdf（可选依赖）。
"""
from .worker import IngestManager, INGEST_EXTS, AGENT_EXTS

__all__ = ["IngestManager", "INGEST_EXTS", "AGENT_EXTS"]
