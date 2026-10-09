"""转录 adapter 接缝（E09）。

Q09 的转录请求/响应/幂等/额度合同未提供：本模块**不编造 HTTP 实现**，只提供
- 稳定错误码（job 错误面与用户提示引用同一常量）；
- 唯一装配点 `create_transcription_adapter`：空配置 → None（metadata_only）；
  声明了 adapter 但没有已核验合同 → 明确抛错，绝不静默降级成 metadata_only，
  也绝不用 embeddings 管线或同维向量冒充转录。

合同到达后（请求/响应样本、幂等键、额度/超时）在这里补**唯一**实现，并沿
`server → make_ingest_manager → VirtualIngestWorker(audio_adapter=...)` 注入；
发请求前必须走既有 journal/paid_request 守卫（不新增第二套审批）。
"""
from __future__ import annotations

from typing import Any

#: 声明了转录 adapter 但没有任何可注入的已核验实现（服务合同缺）。
AUDIO_ADAPTER_UNAVAILABLE = "AUDIO_ADAPTER_UNAVAILABLE"
#: 合同字段缺失（请求/响应/幂等/额度），不得凭猜实现。
TRANSCRIPT_CONTRACT_UNVERIFIED = "TRANSCRIPT_CONTRACT_UNVERIFIED"


class TranscriptionContractUnverified(RuntimeError):
    """没有已核验的服务合同：明确失败，不写猜测的 HTTP 实现。"""


def create_transcription_adapter(audio_config: Any, *, transport: Any = None,
                                 journal: Any = None) -> Any:
    """按配置装配转录 adapter（唯一入口）。

    当前返回 None（未声明 adapter）或抛出 `TranscriptionContractUnverified`（声明了
    adapter 但没有合同）。调用方据此在入队/执行时给出可见错误，而不是继续用默认音频
    参数假装成功。
    """
    declared = str(getattr(audio_config, "adapter", "") or "").strip()
    if not declared:
        return None
    raise TranscriptionContractUnverified(
        f"{TRANSCRIPT_CONTRACT_UNVERIFIED}: audio.adapter={declared!r} 缺少已核验的"
        "请求/响应/幂等/额度合同；不实现猜测的 HTTP 客户端"
    )
