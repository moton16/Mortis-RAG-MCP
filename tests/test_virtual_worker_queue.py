"""VirtualIngestWorker 队列指纹与共享预算合同测试。

E17：原 `test_ingest_route_audio.py` 的 legacy 音频拒绝用例随 ffmpeg 解码 /
Whisper 转录链路物理清除一并移除，本文件只保留与音频无关的队列/指纹合同。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mortis_rag_mcp.ingest.worker import (
    DEFAULT_PARSE_BUDGET,
    VirtualIngestWorker,
)


def _virtual_worker(**cfg_kwargs) -> VirtualIngestWorker:
    base = {"routing": "auto", "network_policy": "configured", "storage": "virtual"}
    base.update(cfg_kwargs)
    return VirtualIngestWorker(Path("."), SimpleNamespace(**base), lambda: object())


def test_virtual_worker_defaults_to_process_shared_budget():
    """worker 默认注入模块级共享 ParseBudget，不得每库新建冒充全局。"""
    worker = _virtual_worker()
    assert worker.parse_budget is DEFAULT_PARSE_BUDGET


def test_job_fingerprint_distinguishes_routing_profile():
    """同内容不同 routing/network profile 不得误合并。"""
    auto = _virtual_worker(routing="auto")
    mineru = _virtual_worker(routing="mineru")
    auto2 = _virtual_worker(routing="auto")
    assert auto._parser_fingerprint() != mineru._parser_fingerprint()
    assert auto._parser_fingerprint() == auto2._parser_fingerprint()


def test_job_fingerprint_includes_chunker_fingerprint():
    base = _virtual_worker()
    provider_a = VirtualIngestWorker(Path("."), base.config, lambda: object(),
                                     chunker_fingerprint_provider=lambda: "chunker-A")
    provider_b = VirtualIngestWorker(Path("."), base.config, lambda: object(),
                                     chunker_fingerprint_provider=lambda: "chunker-B")
    assert provider_a._parser_fingerprint() != provider_b._parser_fingerprint()
    assert provider_a._parser_fingerprint() != base._parser_fingerprint()
