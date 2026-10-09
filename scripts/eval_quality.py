"""Offline paired ranking evaluation. Input rankings are evidence, not an API probe."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path
from statistics import mean

CATEGORIES = {"text", "single_column_pdf", "double_column_pdf",
              "scan_formula_table", "office", "chart", "multilingual_audio"}


def _metrics(ranking, relevance, k):
    relevant = {key for key, value in relevance.items() if value > 0}
    recall = len(set(ranking[:k]) & relevant) / len(relevant)
    gains = [(2 ** relevance.get(key, 0) - 1) / math.log2(i + 2)
             for i, key in enumerate(ranking[:k])]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    denominator = sum((2 ** value - 1) / math.log2(i + 2)
                      for i, value in enumerate(ideal))
    return recall, sum(gains) / denominator


def evaluate(data, *, k=10, seed=17):
    if k <= 0 or data.get("scope") not in {"fixture", "quality"}:
        raise ValueError("positive k and explicit fixture/quality scope required")
    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("nonempty queries required")
    ids, categories, candidate, baseline = set(), set(), [], []
    for case in queries:
        ident, query = case.get("id"), case.get("query")
        if not isinstance(ident, str) or not ident or ident in ids or not isinstance(query, str) or not query.strip():
            raise ValueError("unique query ids and nonempty query required")
        ids.add(ident)
        category = case.get("category")
        if category not in CATEGORIES:
            raise ValueError("unknown category")
        categories.add(category)
        relevance = case.get("relevance", {})
        if not isinstance(relevance, dict) or not relevance or any(
            not isinstance(key, str) or type(value) not in (int, float)
            or not math.isfinite(value) or not 0 <= value <= 3
            for key, value in relevance.items()
        ) or not any(relevance.values()):
            raise ValueError("graded relevance 0..3 with a positive judgment required")
        for field, output in (("ranking", candidate), ("baseline", baseline)):
            ranking = case.get(field)
            if not isinstance(ranking, list) or any(not isinstance(x, str) for x in ranking) or len(set(ranking)) != len(ranking):
                raise ValueError("candidate and baseline require unique ranked document ids")
            output.append(_metrics(ranking, relevance, k))
    deltas = [a[1] - b[1] for a, b in zip(candidate, baseline)]
    rng = random.Random(seed)
    bootstrap = sorted(mean(rng.choices(deltas, k=len(deltas))) for _ in range(500))
    missing = []
    if len(queries) < 100:
        missing.append("100_queries")
    if categories != CATEGORIES:
        missing.append("seven_categories")
    evidence = data.get("corpus_evidence", [])
    if not evidence:
        missing.append("corpus_evidence")
    else:
        evidence_categories = set()
        for item in evidence:
            path = Path(item["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"].lower():
                raise ValueError("corpus evidence SHA mismatch")
            evidence_categories.add(item["category"])
        if evidence_categories != CATEGORIES:
            missing.append("seven_corpus_categories")
    violations = data.get("violations", {})
    if not isinstance(violations, dict) or any(type(v) is not int or v < 0 for v in violations.values()):
        raise ValueError("violations must contain nonnegative measured counts")
    if not {"cross_vault", "citation", "location"} <= violations.keys():
        missing.append("citation_location_permission_checks")
    recall, ndcg = mean(x[0] for x in candidate), mean(x[1] for x in candidate)
    delta_recall = recall - mean(x[0] for x in baseline)
    delta_ndcg = ndcg - mean(x[1] for x in baseline)
    status = ("fixture_measured" if data["scope"] == "fixture" else
              "incomplete" if missing else
              "pass" if delta_recall >= -.02 - 1e-12 and delta_ndcg >= -.02 - 1e-12
              and not any(violations.values()) else "fail")
    return {"scope": data["scope"], "status": status, "queries": len(queries),
            "k": k, "seed": seed, "recall_at_10" if k == 10 else f"recall_at_{k}": recall,
            "ndcg_at_10" if k == 10 else f"ndcg_at_{k}": ndcg,
            "delta_recall": delta_recall, "delta_ndcg": delta_ndcg,
            "ndcg_delta_95_ci": [bootstrap[12], bootstrap[487]],
            "missing": missing, "violations": violations, "real_api_requests": 0,
            "note": "Measures supplied rankings; does not establish service alignment or host playback."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(Path(args.input).read_text(encoding="utf-8")), seed=args.seed)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"invalid evaluation input: {exc}\n")
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["status"] in {"pass", "fixture_measured"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
