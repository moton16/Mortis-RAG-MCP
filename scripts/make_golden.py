"""生成 v0.7.3 chunking 金测快照（v0.8.0 重构回归闸门）。

用法（仓库根目录）::

    python scripts/make_golden.py

产物 ``tests/golden/v073_chunks.json``：键 = 库内相对 posix 路径，值 =
chunk ``to_dict()`` 列表。``metadata.mtime`` 依赖文件系统时间戳、跨机器
不稳定，生成与比对两侧统一剥离。

金测库 ``tests/fixtures/golden_vault/`` 覆盖切块管线全部分支：中文
frontmatter/章节标题、HTML 表格原子块、代码围栏保护、.txt 同权收录、
图片引用（注入默认关闭）。裸 ``AppConfig`` 数据类默认 ``cache.enabled=
False``，全程零磁盘缓存副作用，不触碰用户真实缓存。

重构后重跑 ``tests/test_golden_v073.py``：任何 chunk 差异 = chunk.id
变化 = 存量用户全库重嵌 —— FAIL，禁止合并。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig  # noqa: E402
from mortis_rag_mcp.indexer import MarkdownIndexer  # noqa: E402

FIXTURE_VAULT = ROOT / "tests" / "fixtures" / "golden_vault"
OUT_FILE = ROOT / "tests" / "golden" / "v073_chunks.json"


def collect_chunks(vault: Path) -> dict[str, list[dict]]:
    indexer = MarkdownIndexer(
        vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    )
    snapshot: dict[str, list[dict]] = {}
    for chunk in indexer.sync():
        d = chunk.to_dict()
        d["metadata"].pop("mtime", None)
        snapshot.setdefault(chunk.source, []).append(d)
    return {k: snapshot[k] for k in sorted(snapshot)}


def main() -> None:
    data = collect_chunks(FIXTURE_VAULT)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    total = sum(len(v) for v in data.values())
    print(f"golden written: {OUT_FILE} ({len(data)} files, {total} chunks)")


if __name__ == "__main__":
    main()
