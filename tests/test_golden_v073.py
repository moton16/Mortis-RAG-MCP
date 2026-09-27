"""v0.7.3 chunking 金测（v0.8.0 重构回归闸门）。

契约：重构前后，同一金测库（``tests/fixtures/golden_vault/``）经
``MarkdownIndexer`` 完整管线（扫描 → 切块 → sync 返回）产出的 chunk
集合必须与 ``tests/golden/v073_chunks.json``（v0.7.3 基线快照）完全一致。

任何差异 = chunk content/id 变化 = 存量用户全库重嵌 —— FAIL，禁止合并。
``metadata.mtime`` 依赖文件系统时间戳，两侧统一剥离后比对。
"""
from __future__ import annotations

import json
from pathlib import Path

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = ROOT / "tests" / "fixtures" / "golden_vault"
GOLDEN_FILE = ROOT / "tests" / "golden" / "v073_chunks.json"


def _collect(vault: Path) -> dict[str, list[dict]]:
    indexer = MarkdownIndexer(
        vault, AppConfig(embedding=EmbeddingConfig(mode="static", dimension=8))
    )
    snapshot: dict[str, list[dict]] = {}
    for chunk in indexer.sync():
        d = chunk.to_dict()
        d["metadata"].pop("mtime", None)
        snapshot.setdefault(chunk.source, []).append(d)
    return {k: snapshot[k] for k in sorted(snapshot)}


def test_chunks_match_v073_golden():
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    current = _collect(FIXTURE_VAULT)
    drifted = [k for k in current if current[k] != golden.get(k)]
    assert current == golden, (
        "chunking 金测失配：重构改变了切块输出，将导致存量 chunk.id 变化与"
        f"全库重嵌。文件集差异: {sorted(set(current) ^ set(golden))}；"
        f"内容漂移文件: {drifted}"
    )


def test_golden_covers_all_fixture_files():
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    expected = {"中文笔记.md", "教材/表格.md", "代码示例.md", "普通.txt", "图片.md"}
    assert set(golden) == expected, f"金测覆盖面漂移: {set(golden) ^ expected}"
