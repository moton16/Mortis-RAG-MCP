"""PDF/Office 摄取层：MinerU 云端解析 → Markdown 落盘 .mortis-parsed/。

设计约束：默认关闭（IngestConfig.enabled=False）；仅 kb_ingest 显式触发；
纯标准库实现；云端失败可降级 pymupdf（可选依赖）。
"""
from .worker import IngestManager, INGEST_EXTS, AGENT_EXTS

__all__ = ["IngestManager", "INGEST_EXTS", "AGENT_EXTS"]
