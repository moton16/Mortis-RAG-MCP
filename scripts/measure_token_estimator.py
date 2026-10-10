"""Offline estimator measurement; never downloads a tokenizer or changes profiles."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

# Works when launched by absolute script path from the verified workspace.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mortis_rag_mcp._indexer.token_chunking import ESTIMATOR_VERSION, estimate_tokens


def measure(samples, count_tokens, *, max_mape=.2):
    rows = []
    groups = {}
    for sample in samples:
        text = sample["text"]
        actual = count_tokens(text)
        estimated = estimate_tokens(text)
        relative = (estimated - actual) / max(1, actual)
        row = {"id": sample["id"], "category": sample["category"], "characters": len(text),
               "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "estimated": estimated, "actual": actual, "relative_error": relative}
        rows.append(row)
        groups.setdefault(sample["category"], []).append(abs(relative))
    categories = {name: {"samples": len(errors), "mape": sum(errors) / len(errors),
                         "max_absolute_relative_error": max(errors)}
                  for name, errors in groups.items()}
    return {"estimator": ESTIMATOR_VERSION, "samples": rows, "categories": categories,
            "threshold_max_category_mape": max_mape,
            "threshold_pass": bool(categories) and all(v["mape"] <= max_mape for v in categories.values()),
            "profile_calibrated": False,
            "scope": "measurement of supplied synthetic samples only; no model-family extrapolation"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer-json", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True, help="JSONL: id/category/text")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-mape", type=float, default=.2)
    args = parser.parse_args(argv)
    if not args.tokenizer_json.is_file() or not args.samples.is_file():
        parser.error("explicit existing offline tokenizer and samples are required")
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(args.tokenizer_json))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    samples = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not samples:
        parser.error("samples must contain at least one record")
    result = measure(samples, lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids),
                     max_mape=args.max_mape)
    result.update(tokenizer_path=str(args.tokenizer_json.resolve()),
                  tokenizer_sha256=hashlib.sha256(args.tokenizer_json.read_bytes()).hexdigest(),
                  samples_path=str(args.samples.resolve()), python=sys.executable,
                  python_version=sys.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"samples": len(samples), "categories": result["categories"],
                      "threshold_pass": result["threshold_pass"], "output": str(args.output.resolve())},
                     ensure_ascii=False))
    return 0  # Successful measurement, not a claim of successful calibration.


if __name__ == "__main__":
    raise SystemExit(main())
