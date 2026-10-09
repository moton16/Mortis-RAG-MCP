"""Bounded, synthetic client resource probe; never reads a registered user vault."""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
from pathlib import Path
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mortis_rag_mcp.config import AppConfig, CacheConfig
from mortis_rag_mcp.doc_store import DocumentStore, MediaOccurrenceSpec, resolve_storage_layout
from mortis_rag_mcp.indexer import Chunk
from mortis_rag_mcp._indexer.search import semantic_rank


def _percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))]


def measure(directory, *, jobs=1000, occurrences=10000, chunks=10000, samples=20):
    directory = Path(directory)
    if directory.exists():
        raise ValueError("resource probe requires a new directory")
    if not (0 < jobs <= 1000 and 0 < occurrences <= 10000 and 0 < chunks <= 10000 and 0 < samples <= 100):
        raise ValueError("probe exceeds bounded synthetic limits")
    directory.mkdir(parents=True)
    vault = directory / "vault"
    vault.mkdir()
    payload = b"%PDF-1.4 synthetic-resource-input"
    (vault / "probe.pdf").write_bytes(payload)
    config = AppConfig(cache=CacheConfig(dir=str(directory / "cache"), enabled=True))
    store = DocumentStore(resolve_storage_layout(config, vault, registered_vaults=[]), config)
    store.open(write=True)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        sha = hashlib.sha256(payload).hexdigest()
        revision = store.stage_revision(source="probe.pdf", source_sha256=sha,
            render_sha256=sha, parser_fingerprint="synthetic-resource-v1", markdown="probe text")
        blob = store.put_media_blob(data=b"shared synthetic bytes", mime_type="image/png")
        store.attach_occurrences(revision.revision_id, [
            MediaOccurrenceSpec(f"occ-{i}", blob, "image", i, mime_type="image/png")
            for i in range(occurrences)])
        store.commit_revision(revision.revision_id)
        for i in range(jobs):
            store.enqueue_job(source=f"job-{i}.pdf", source_sha256=sha,
                              parser_fingerprint="synthetic-resource-v1")
        population_ms = (time.perf_counter() - started) * 1000
        candidates = [Chunk(f"chunk-{i}", f"synthetic content {i}", "probe.pdf", "",
            {}, embedding=array("f", [1., (i % 7) / 7., 0., 0.])) for i in range(chunks)]
        timings = {"list_media": [], "semantic": []}
        for _ in range(samples):
            t = time.perf_counter()
            assert len(store.list_media("probe.pdf", revision_id=revision.revision_id, limit=20)) == min(20, occurrences)
            timings["list_media"].append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            assert len(semantic_rank([1., 0., 0., 0.], candidates)) == chunks
            timings["semantic"].append((time.perf_counter() - t) * 1000)
        _, peak = tracemalloc.get_traced_memory()
        report = {"scope": "synthetic_client_components", "jobs": jobs,
            "occurrences": occurrences, "chunks": chunks, "shared_blobs": 1,
            "population_ms": population_ms, "python_peak_bytes": peak,
            "rss_bytes": None, "real_api_requests": 0, "samples": samples,
            "note": "Python allocations are not process RSS/native RAM; no service/decoder throughput claim."}
        for name, values in timings.items():
            for label, fraction in (("p50", .5), ("p95", .95), ("p99", .99)):
                report[f"{name}_{label}_ms"] = _percentile(values, fraction)
    finally:
        tracemalloc.stop()
        store.close()
    report["sqlite_bytes"] = sum(p.stat().st_size for p in directory.rglob("*.sqlite"))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = measure(args.directory)
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
