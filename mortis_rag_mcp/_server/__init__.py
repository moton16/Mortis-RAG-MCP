"""MCP 服务端内部实现包（v0.8.0 路由与跨库编排解耦）。

模块说明：
- ``search_dispatch.py``：单库 / Scoped 多库 / 全局检索入参归一化与分发入口；
- ``fanout.py``：跨库聚合、单次向量计算、权重计算、去重、rerank 与分组分页。

活实例契约（不可破坏）：
本包所有函数接收 live 的 ``VaultMcpServer`` 实例，直读 ``indexer._chunks``
与 ``_sync_progress`` 状态，严禁做任何静态快照化。
"""
from __future__ import annotations

from .fanout import fanout_search
from .search_dispatch import dispatch_search

__all__ = ["dispatch_search", "fanout_search"]
