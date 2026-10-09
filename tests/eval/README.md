# Offline evaluation inputs

`eval_search.py` is an exact-source-ID, static-only smoke test. It does not
resolve implicit host configuration, enable ingest, or rerank remotely.

`python scripts/eval_quality.py --input tests/eval/quality_input.example.json
--output .runtime/quality-report.json` measures supplied paired rankings.
The example is a **metric fixture**, not a representative semantic corpus.
`fixture_measured` must never be reported as C105 acceptance.

For final `scope=quality`, supply >=100 independently labeled queries across
`text`, `single_column_pdf`, `double_column_pdf`, `scan_formula_table`, `office`,
`chart`, `multilingual_audio`. Each query has a unique `id`, nonempty `query`,
graded `relevance` (document ID -> 0..3), candidate `ranking` and `baseline`.
Rankings have unique document IDs; both runs must use the same corpus/config.
Supply `corpus_evidence` entries (`path`, SHA256 `sha256`, `category`) and actual
`violations` counts (`cross_vault`, `citation`, `location`). Paths are resolved
from the command's working directory. Missing evidence returns `incomplete`;
Recall/NDCG regressions beyond 0.02 or violations return `fail`.
Paired NDCG bootstrap uses a recorded seed and 500 samples.

`python scripts/eval_resources.py --directory .runtime/new-resource-run
--output .runtime/resource-report.json` requires a **new**, synthetic directory.
Defaults: 1000 real SQLite queued jobs, 10000 persisted occurrences sharing one
blob, 10000 client chunks, 20 measured iterations. Reports p50/p95/p99,
SQLite bytes and Python allocation peak. Native RSS is explicitly unmeasured.
It does not claim service inference/decoder throughput or 100x success.

CI keeps the zero-runtime-dependency core matrix and required docs/media/vec
positive lanes separate. `MORTIS_REQUIRE_EXTRAS=1` makes missing format
dependencies an error; local environments without extras retain normal skips.
Real API/alignment, representative human labels and host display/playback remain
separate evidence, never replaced with synthetic provider results.
