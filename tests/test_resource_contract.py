import importlib

import pytest


def test_resource_probe_uses_real_store_and_shared_blob(tmp_path):
    module = importlib.import_module("scripts.eval_resources")
    report = module.measure(tmp_path / "probe", jobs=8, occurrences=24, chunks=32, samples=3)
    assert report["jobs"] == 8
    assert report["occurrences"] == 24
    assert report["chunks"] == 32
    assert report["shared_blobs"] == 1
    assert report["sqlite_bytes"] > 0
    assert report["real_api_requests"] == 0
    assert report["list_media_p95_ms"] >= 0
    assert report["semantic_p99_ms"] >= 0


def test_resource_probe_wont_reuse_existing_directory(tmp_path):
    with pytest.raises(ValueError, match="new"):
        importlib.import_module("scripts.eval_resources").measure(
            tmp_path, jobs=1, occurrences=1, chunks=1, samples=1)
