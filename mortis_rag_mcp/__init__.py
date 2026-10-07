"""Core package for the Obsidian vault indexer."""

# 版本号单一真源：server.SERVER_INFO["version"] 与 diaglog 的诊断日志版本都取自这里，
# 发版时只改这一处（tests/test_version_sync.py 会锁住它与 pyproject.toml / 两者的一致性）。
# 必须定义在下方 import 之前：任何子模块都可能在本包 __init__ 执行过程中读它。
__version__ = "0.8.1"

from .config import AppConfig, EmbeddingConfig, RerankerConfig, VectorConfig, load_config
from .indexer import Chunk, MarkdownIndexer

__all__ = ["AppConfig", "EmbeddingConfig", "RerankerConfig", "VectorConfig", "Chunk", "MarkdownIndexer", "load_config"]
