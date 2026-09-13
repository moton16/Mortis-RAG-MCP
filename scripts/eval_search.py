"""检索评测：对金标准查询集跑 MarkdownIndexer.search，报告 Hit@K。

用法：
    python scripts/eval_search.py --golden tests/eval/golden_queries.json --k 5 [--config config/app.toml] [--vault path]
退出码：全部命中 0；有 miss 1（可挂 CI）。
注意：external embedding 模式下 sync/search 会真实调用 API，建议先用 static 模式
（config 里 [embedding] mode = "static"）跑回归，外部模式只用于最终抽查。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mortis_rag_mcp.config import load_config  # noqa: E402
from mortis_rag_mcp.indexer import MarkdownIndexer, SearchFilter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="检索评测：对金标准查询集跑 MarkdownIndexer.search，报告 Hit@K 与 MRR。")
    ap.add_argument("--golden", required=True, help="金标准查询集 JSON 路径")
    ap.add_argument("--config", default=None, help="配置文件路径（默认使用内置默认配置/static 模式）")
    ap.add_argument("--vault", default=None, help="可选：覆盖金标准集中的 vault 路径")
    ap.add_argument("--k", type=int, default=5, help="Hit@K 中的 K（默认 5）")
    ap.add_argument("--rerank", action="store_true", default=False, help="是否使用 rerank（默认 False，避免依赖外部服务）")
    args = ap.parse_args()

    config = load_config(args.config)
    golden_path = Path(args.golden).resolve()
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    queries = golden["queries"]

    indexers: dict[str, MarkdownIndexer] = {}
    hits = 0
    rr_total = 0.0
    misses: list[dict] = []
    for i, case in enumerate(queries, 1):
        raw_vault = args.vault or case.get("vault") or ""
        if raw_vault:
            vpath = Path(raw_vault)
            if not vpath.is_absolute():
                vpath = (golden_path.parent / vpath).resolve()
            vault = str(vpath)
        else:
            vault = ""
        vault_path = Path(vault)
        if not vault or not vault_path.exists():
            mark = "MISS"
            print(f"[{i:02d}/{len(queries)}] {mark} (0.00s) {case['query']!r} -> expect {case['expect']!r} (vault not found: {vault})")
            misses.append({"query": case["query"], "expect": case["expect"], "got": []})
            continue

        try:
            if vault not in indexers:
                idx = MarkdownIndexer(vault_path=vault, config=config)
                idx.sync()
                indexers[vault] = idx
            idx = indexers[vault]
            filters = None
            if case.get("path_prefix"):
                filters = SearchFilter(path_prefix=case["path_prefix"])
            t0 = time.time()
            results = idx.search(case["query"], top_k=args.k, use_rerank=args.rerank, filters=filters)
            elapsed = time.time() - t0
            sources = [c.source for c in results]
            
            hit_rank = None
            for rank, s in enumerate(sources, 1):
                if case["expect"] in s:
                    hit_rank = rank
                    break

            if hit_rank is not None:
                hits += 1
                rr = 1.0 / hit_rank
                rr_total += rr
                mark = f"HIT(#{hit_rank})"
            else:
                rr = 0.0
                mark = "MISS"
                misses.append({"query": case["query"], "expect": case["expect"], "got": sources[:3]})

            print(f"[{i:02d}/{len(queries)}] {mark} ({elapsed:.2f}s) {case['query']!r} -> expect {case['expect']!r} (RR: {rr:.3f})")
        except Exception as exc:
            mark = "MISS"
            print(f"[{i:02d}/{len(queries)}] {mark} (0.00s) {case['query']!r} -> expect {case['expect']!r} (error: {exc})")
            misses.append({"query": case["query"], "expect": case["expect"], "got": []})

    total = max(1, len(queries))
    rate = hits / total
    mrr = rr_total / total
    print(f"\nHit@{args.k}: {hits}/{len(queries)} = {rate:.1%}")
    print(f"MRR@{args.k}: {mrr:.3f}")
    if misses:
        print("Misses:")
        for m in misses:
            print(f"  {m['query']!r} expect {m['expect']!r}, top3={m['got']}")
    return 0 if not misses else 1


if __name__ == "__main__":
    raise SystemExit(main())
