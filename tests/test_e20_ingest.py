"""E20 ingest correctness: offline transports, deterministic claim, real SQLite."""
from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
import zlib

import pytest

from mortis_rag_mcp.config import AppConfig, IngestConfig
from mortis_rag_mcp.doc_store import DocumentStore, OwnershipLost, StoreConflict, resolve_storage_layout
from mortis_rag_mcp._indexer.media import occurrence_span
from mortis_rag_mcp.ingest import images, mineru, router, worker as worker_module
from mortis_rag_mcp.ingest.models import DictMediaSink, ParseResult, ResourceLimits, normalize_markdown
from mortis_rag_mcp.ingest.worker import VirtualIngestWorker


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.ingest.enabled = True
    cfg.ingest.storage = "virtual"
    cfg.ingest.auto_watch = True
    store = DocumentStore(resolve_storage_layout(cfg, vault, registered_vaults=[]), cfg)
    store.open(write=True)
    worker = VirtualIngestWorker(vault, cfg.ingest, lambda: store)
    starts = []
    monkeypatch.setattr(worker, "_ensure_worker", lambda s: starts.append(True))
    yield vault, cfg, store, worker, starts
    worker.stop()
    store.close()


class Page:
    rect = SimpleNamespace(width=600)

    def __init__(self, complex_page):
        self.complex_page = complex_page

    def get_text(self, kind):
        return "healthy printable body" if kind == "text" else [
            (72, 72, 300, 100, "healthy printable body", 0, 0)]

    def get_images(self):
        return [(1,)] if self.complex_page else []


class Pdf:
    is_encrypted = False

    def __init__(self, path):
        self.page = Page(Path(path).name.startswith("z-"))

    def __len__(self):
        return 1

    def __getitem__(self, index):
        return self.page

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def queue_inputs(env, monkeypatch):
    vault, cfg, store, worker, starts = env
    cfg.ingest.routing = "auto"
    cfg.ingest.network_policy = "local_only"
    for name in ("a-simple.pdf", "z-complex.pdf"):
        (vault / name).write_bytes(b"%PDF-1.4 E20 fixture\n")
    monkeypatch.setattr(router, "pdf_backend", lambda: ("pymupdf", SimpleNamespace(open=Pdf)))
    scan = worker.scan_pending
    monkeypatch.setattr(worker, "scan_pending", lambda: sorted(scan(), key=lambda x: x["source"]))
    return worker, store, starts


@pytest.mark.parametrize("reverse", [False, True], ids=["accepted-first", "rejected-first"])
def test_auto_partial_reports_actual_queue_and_wakes(env, monkeypatch, reverse):
    worker, store, starts = queue_inputs(env, monkeypatch)
    scan = worker.scan_pending
    if reverse:
        monkeypatch.setattr(worker, "scan_pending", lambda: list(reversed(scan())))
    result = worker.auto_submit()
    assert result["submitted"] == result["new_jobs"] == 1
    assert result["status"] == "partial"
    assert [j["source"] for j in result["jobs"]] == ["a-simple.pdf"]
    assert [r["source"] for r in result["rejected"]] == ["z-complex.pdf"]
    assert result["rejected"][0]["error_code"] == "NETWORK_POLICY_BLOCKED"
    assert store.queue_depth() == 1 and starts == [True]
    assert [j.source for j in store.list_jobs()] == ["a-simple.pdf"]
    assert {r["source"] for r in store.iter_auto_seen()} == {"a-simple.pdf"}


@pytest.mark.parametrize("entrypoint", ["submit", "auto_submit"])
def test_existing_queued_submission_wakes_without_new_job(env, monkeypatch, entrypoint):
    worker, store, starts = queue_inputs(env, monkeypatch)
    first = worker.submit(["a-simple.pdf"])
    starts.clear()
    worker.ignore_provider = lambda: SimpleNamespace(
        is_ignored=lambda rel, is_dir=False: (rel == "z-complex.pdf", "fixture exclusion"))
    result = (worker.submit(["a-simple.pdf"]) if entrypoint == "submit" else worker.auto_submit())
    assert result["submitted"] == result["new_jobs"] == 0
    assert starts == [True] and store.queue_depth() == 1
    assert store.list_jobs()[0].job_id == first["jobs"][0]["job_id"]


def test_all_blocked_reports_rejection_without_queue(env, monkeypatch):
    worker, store, starts = queue_inputs(env, monkeypatch)
    (worker.vault_path / "a-simple.pdf").unlink()
    result = worker.auto_submit()
    assert result["status"] == "network_policy_blocked"
    assert result["submitted"] == 0 and result["jobs"] == []
    assert [r["source"] for r in result["rejected"]] == ["z-complex.pdf"]
    assert store.queue_depth() == 0 and starts == []
    assert list(store.iter_auto_seen()) == []


def png(pixel=b"\xff\x00\x00"):
    def chunk(kind, body):
        return (len(body).to_bytes(4, "big") + kind + body
                + (zlib.crc32(kind + body) & 0xffffffff).to_bytes(4, "big"))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", (4).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00")
            + chunk(b"tEXt", b"note\x00" + b"x" * images._HEADER_WINDOW)
            + chunk(b"IDAT", zlib.compress((b"\x00" + pixel * 4) * 4))
            + chunk(b"IEND", b""))


def fake_v4(monkeypatch, markdown):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("full.md", markdown.encode("utf-8"))
        z.writestr("images/fig.png", png())
        z.writestr("images/second.png", png(b"\x00\x00\xff"))
        z.writestr("content_list.json", json.dumps([
            {"type": "text", "page_idx": 0, "text": "Heading"},
            {"type": "image", "page_idx": 0, "img_path": "images/fig.png"},
            {"type": "text", "page_idx": 1, "text": "tail"},
        ]))
    calls = []

    def http(req, timeout):
        calls.append(req.get_method())
        if req.get_method() == "POST":
            return {"code": 0, "data": {"batch_id": "fixture", "file_urls": [
                "https://upload.example.test/file"]}}
        return {"code": 0, "data": {"extract_result": [{
            "state": "done", "full_zip_url": "https://result.example.test/result.zip"}]}}

    monkeypatch.setattr(mineru, "_http_json", http)
    monkeypatch.setattr(mineru, "_put_upload", lambda *a: calls.append("PUT"))
    monkeypatch.setattr(mineru, "_http_bytes", lambda *a: archive.getvalue())
    return calls


@pytest.mark.parametrize("prefix,newline", [
    ("", "\n"), ("", "\r\n"), ("", "\r"),
    ("\ufeff", "\n"), ("\ufeff", "\r\n"), ("\ufeff", "\r"),
], ids=["LF", "CRLF", "CR", "BOM-LF", "BOM-CRLF", "BOM-CR"])
def test_normalized_coordinates_survive_publication(env, monkeypatch, prefix, newline):
    vault, cfg, store, worker, starts = env
    raw = prefix + newline.join([
        "# Heading", "", "![first](images/fig.png)", "",
        "tail", "", "![second](images/second.png)", "",
    ])
    calls = fake_v4(monkeypatch, raw)
    cfg.ingest.routing = "mineru"
    source = vault / "doc.pdf"
    source.write_bytes(b"%PDF-1.4 E20 fixture\n")
    sink = DictMediaSink()
    client = mineru.MineruClient(api_key="fixture-token")
    result = client.parse_structured(source, sink=sink)
    assert result.markdown == normalize_markdown(raw)
    for item, saved in zip(result.media, sink.occurrences):
        reference = ("![first](images/fig.png)" if item.name.endswith("/fig.png")
                     else "![second](images/second.png)")
        span = (result.markdown.index(reference), result.markdown.index(reference) + len(reference))
        assert (item.anchor_start, item.anchor_end) == span
        assert (saved.anchor_start, saved.anchor_end) == span
        assert result.markdown[slice(*span)] == reference
    assert len(result.media) == len(sink.occurrences) == 2
    assert result.page_map[1].char_start == result.markdown.index("tail")
    assert result.page_map[-1].char_end == len(result.markdown)
    assert client.parse(source).markdown == raw, "legacy parse must preserve raw bytes"
    worker._client = client
    worker.submit(["doc.pdf"])
    job = store.claim_job("coordinates")
    worker._run_job(store, job, "coordinates")
    active = store.get_active("doc.pdf")
    assert store.job_status(job.job_id).state == "done"
    assert active.revision.render_sha256 == hashlib.sha256(result.markdown.encode()).hexdigest()
    assert active.revision.capabilities["page_spans"][1]["char_start"] == result.markdown.index("tail")
    stored_items = store.list_media("doc.pdf", revision_id=active.revision.revision_id)
    assert len(stored_items) == 2
    for item, expected in zip(stored_items, result.media):
        span = (expected.anchor_start, expected.anchor_end)
        assert occurrence_span(active.revision.parsed_markdown, item) == span
        assert (item["metadata"]["anchor_start"], item["metadata"]["anchor_end"]) == span
    assert calls.count("POST") == 3


def test_image_sha_covers_full_payload_and_published_metadata(env):
    vault, cfg, store, worker, starts = env
    payloads = [png(), png(b"\x00\x00\xff")]
    assert payloads[0][:images._HEADER_WINDOW] == payloads[1][:images._HEADER_WINDOW]
    expected = [hashlib.sha256(data).hexdigest() for data in payloads]
    assert expected[0] != expected[1]
    cfg.ingest.routing = "local"
    cfg.ingest.network_policy = "local_only"
    for i, payload in enumerate(payloads):
        source = f"photo{i}.png"
        path = vault / source
        path.write_bytes(payload)
        admission = images.validate_image_source(path, ResourceLimits())
        assert admission["sha256"] == expected[i]
        assert "header_sha256" not in admission
        sink = DictMediaSink()
        result = images.parse_image(path, limits=ResourceLimits(), sink=sink)
        assert result.capabilities["image_sha256"] == expected[i]
        assert sink.occurrences[0].metadata["source_sha256"] == expected[i]
        worker.submit([source])
        job = store.claim_job(f"image-{i}")
        worker._run_job(store, job, f"image-{i}")
        active = store.get_active(source)
        occurrence = store.list_media(source, revision_id=active.revision.revision_id)[0]
        assert active.revision.source_sha256 == expected[i]
        assert active.revision.capabilities["image_sha256"] == expected[i]
        assert occurrence["metadata"]["source_sha256"] == expected[i]
        saved = store.read_media(source, revision_id=active.revision.revision_id,
                                 occurrence_id=occurrence["occurrence_id"], include_data=True)
        assert saved["data"] == payload and hashlib.sha256(saved["data"]).hexdigest() == expected[i]
        assert path.read_bytes() == payload


def test_worker_config_is_direct_single_source_alias():
    assert worker_module.IngestConfig is IngestConfig
    tree = ast.parse(Path(worker_module.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.ClassDef) and n.name == "IngestConfig" for n in ast.walk(tree))
    assert any(isinstance(n, ast.ImportFrom) and n.module == "config"
               and any(a.name == "IngestConfig" for a in n.names) for n in tree.body)


def test_image_metadata_hashes_payload_not_admission_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "changing.png"
    before, after = png(), png(b"\x00\x00\xff")
    path.write_bytes(before)
    validate = images.validate_image_source
    admission = []

    def changed_after_admission(source, limits):
        info = validate(source, limits)
        admission.append(info["sha256"])
        source.write_bytes(after)
        return info

    monkeypatch.setattr(images, "validate_image_source", changed_after_admission)
    sink = DictMediaSink()
    result = images.parse_image(path, sink=sink)
    expected = hashlib.sha256(after).hexdigest()
    assert admission == [hashlib.sha256(before).hexdigest()]
    assert result.capabilities["image_sha256"] == expected
    assert sink.occurrences[0].metadata["source_sha256"] == expected
    assert sink.images["source.png"] == after


@pytest.mark.parametrize("interleave", ["publish", "delete", "unrelated"])
def test_worker_consumes_source_local_cas_without_global_false_conflict(env, interleave):
    vault, cfg, store, worker, starts = env
    cfg.ingest.routing = "mineru"
    payload = b"%PDF-1.4 source-local CAS\n"
    (vault / "doc.pdf").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    worker.submit(["doc.pdf"])
    job = store.claim_job("cas-owner")
    writer = DocumentStore(store.layout, cfg)
    writer.open(write=True)
    latest = []

    class InterleavedClient:
        def parse_structured(self, *args, **kwargs):
            if interleave == "delete":
                writer.mark_deleted("doc.pdf")
            else:
                staged = writer.stage_revision(
                    source="other.pdf" if interleave == "unrelated" else "doc.pdf",
                    source_sha256=sha, render_sha256=hashlib.sha256(b"newer fact").hexdigest(),
                    parser_fingerprint="interleaved", markdown="newer fact")
                writer.commit_revision(staged.revision_id)
                latest.append(staged.revision_id)
            return ParseResult("late candidate\n", "fixture", "v4", "fixture")

    worker._client = InterleavedClient()
    try:
        if interleave == "unrelated":
            worker._run_job(store, job, "cas-owner")
            assert store.job_status(job.job_id).state == "done"
            assert store.get_active("doc.pdf").revision.parsed_markdown == "late candidate\n"
            assert store.get_active("other.pdf").active_revision == latest[0]
        else:
            expected = OwnershipLost if interleave == "delete" else StoreConflict
            with pytest.raises(expected):
                worker._run_job(store, job, "cas-owner")
            if interleave == "delete":
                assert store.get_active("doc.pdf", include_hidden=True) is None
                assert store.job_status(job.job_id).state == "superseded"
            else:
                assert store.get_active("doc.pdf").active_revision == latest[0]
                assert store.get_active("doc.pdf").revision.parsed_markdown == "newer fact"
                assert store.job_status(job.job_id).state != "done"
    finally:
        writer.close()


def test_worker_captures_source_token_once_before_source_checks(env, monkeypatch):
    vault, cfg, store, worker, starts = env
    cfg.ingest.routing = "mineru"
    path = vault / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 capture order\n")
    worker.submit(["doc.pdf"])
    job = store.claim_job("capture")
    expected = store.source_seq(job.source)
    events = []
    capture = store.source_seq
    hash_source = worker_module._sha256
    commit = store.commit_job_revision

    def source_seq(source):
        events.append("capture")
        return capture(source)

    def sha256(source):
        events.append("hash")
        return hash_source(source)

    def publish(*args, **kwargs):
        assert kwargs["expected_source_seq"] == expected
        events.append("commit")
        return commit(*args, **kwargs)

    class Client:
        def parse_structured(self, *args, **kwargs):
            events.append("parse")
            return ParseResult("body\n", "fixture", "v4", "fixture")

    worker._client = Client()
    monkeypatch.setattr(store, "source_seq", source_seq)
    monkeypatch.setattr(worker_module, "_sha256", sha256)
    monkeypatch.setattr(store, "commit_job_revision", publish)
    worker._run_job(store, job, "capture")
    assert events == ["capture", "hash", "parse", "hash", "commit"]
