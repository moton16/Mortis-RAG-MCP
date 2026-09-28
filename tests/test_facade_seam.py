"""Facade monkeypatch 接缝回归（v0.8.0 D2 决策闸门）。

契约：测试对 indexer 模块属性的补丁必须能影响迁移后的真实调用路径。
``_probe_mtime_tick_ns`` 实现已提取至 ``_indexer/scanning.py``，Facade 保留
显式包装函数作为**唯一调用路径**——补丁模块属性后，``_finalize_mtime_tick_probe``
观察到的必须是补丁本身。若接缝断裂（scanning 内部直呼自己的符号），
``calls`` 计数为 0 → FAIL，粗刻度 fail-closed 回归从此有人看守。
"""
from __future__ import annotations

import mortis_rag_mcp.indexer as indexer_module
from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def test_probe_monkeypatch_on_facade_is_observed(tmp_path, monkeypatch):
    """补丁 Facade 模块属性 → 迁移后的探测调用路径必须观察到补丁。"""
    calls: list[int] = []

    def fake_probe(samples):
        calls.append(1)
        return 2_000_000_000  # 2s 粗刻度 → 应触发 fail-closed 禁用快速路径

    monkeypatch.setattr(indexer_module, "_probe_mtime_tick_ns", fake_probe)

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\n内容甲\n", encoding="utf-8")
    (vault / "b.md").write_text("# B\n\n内容乙\n", encoding="utf-8")

    indexer = MarkdownIndexer(
        vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    )
    indexer.sync()

    assert calls, "Facade 补丁未被调用：monkeypatch 接缝断裂（scanning 内部直呼符号？）"
    assert indexer._mtime_tick_ns == 2_000_000_000
    assert indexer._fast_path_disabled_reason, "粗刻度未触发 fail-closed 禁用"


def test_probe_wrapper_delegates_to_scanning():
    """未打补丁时，Facade 包装必须委托到 scanning 的真实实现（gcd 语义）。"""
    result = indexer_module._probe_mtime_tick_ns([1_000_000, 2_000_000, 4_000_000])
    assert result == 1_000_000
