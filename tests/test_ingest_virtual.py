"""C94 虚拟摄取路径的端到端冒烟（不碰网络；MineruClient 整体打桩）。

覆盖：入队 → 领取 → 解析（媒体逐项落 blob）→ 两次源核验 → stage → attach →
同事务 commit → done 可见；合并去重；local_only 在入队前拒绝；源变更废弃候选；
ignore matcher fail-closed。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout
from mortis_rag_mcp.ingest.worker import VirtualIngestWorker
from mortis_rag_mcp.ingest import models as ingest_models


def make_config(tmp_path, *, storage: str = "virtual", network_policy: str = "configured") -> AppConfig:
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.enabled = True
    cfg.cache.placement = "home"
    cfg.cache.id = ""
    cfg.ingest.enabled = True
    cfg.ingest.storage = storage
    cfg.ingest.network_policy = network_policy
    return cfg


def open_store(tmp_path, vault, cfg) -> DocumentStore:
    layout = resolve_storage_layout(cfg, vault, registered_vaults=[])
    store = DocumentStore(layout, cfg)
    store.open(write=True)
    return store


class _FakeClient:
    """parse_structured 打桩：返回一份可验证的 ParseResult，并（可选）向 sink 写一项媒体。"""

    calls: list[str] = []
    media_payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24

    def __init__(self, *args, **kwargs):
        pass

    def parse_structured(self, path, *, sink=None, intent_recorder=None, request_id="",
                         **kwargs):
        type(self).calls.append(request_id)
        if sink is not None:
            sink.add(
                name="images/fig.png",
                data=self.media_payload,
                kind="image",
                ordinal=1,
                mime_type="image/png",
                width=4,
                height=4,
            )
        return ingest_models.ParseResult(
            markdown="# Virtual Doc\n\nbody text\n",
            parser_fingerprint=ingest_models.parser_fingerprint(
                adapter="fake", channel="v4", model="vlm", language="ch",
                is_ocr=False, enable_table=True, enable_formula=True,
            ),
            channel="v4",
            model="vlm",
            quality="full",
            capabilities={"page_map": False, "images": True},
        )


@pytest.fixture()
def env(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = make_config(tmp_path)
    store = open_store(tmp_path, vault, cfg)
    worker = VirtualIngestWorker(vault, cfg.ingest, store_provider=lambda: store)
    yield vault, cfg, store, worker
    worker.stop()


def _pdf(vault: Path, name: str = "doc.pdf", payload: bytes = b"%PDF-1.4 virtual") -> Path:
    doc = vault / name
    doc.write_bytes(payload)
    return doc


def test_virtual_submit_parses_into_store_without_mirror(env, monkeypatch):
    vault, cfg, store, worker = env
    monkeypatch.setattr("mortis_rag_mcp.ingest.worker.MineruClient", _FakeClient)
    doc = _pdf(vault)

    result = worker.submit(["doc.pdf"])
    assert result["submitted"] == 1
    assert worker._worker is not None
    worker._worker.join(timeout=5.0)

    jobs = worker.status()["jobs"]
    assert jobs and jobs[0]["state"] == "done"
    assert jobs[0]["revision_id"]

    # 发布事实：文档可见、正文/媒体都进了 store
    active = store.get_active("doc.pdf")
    assert active is not None
    assert "# Virtual Doc" in active.revision.parsed_markdown
    conn = store._open_conn(store._ensure_read_generation())
    occurrences = conn.execute(
        "SELECT COUNT(*) FROM media_occurrences WHERE revision_id = ?",
        (active.active_revision,),
    ).fetchone()[0]
    assert occurrences == 1
    # 去重账本 + 队列归零
    seen = {item["source"]: item for item in store.iter_auto_seen()}
    assert seen["doc.pdf"]["state"] == "done"
    assert store.queue_depth() == 0
    # 虚拟路径**不写**物理镜像
    assert not (vault / ".mortis-parsed").exists()
    assert doc.exists()


def test_virtual_submit_merges_same_source_sha(env, monkeypatch):
    vault, cfg, store, worker = env
    monkeypatch.setattr("mortis_rag_mcp.ingest.worker.MineruClient", _FakeClient)
    _pdf(vault)
    first = worker.submit(["doc.pdf"])
    second = worker.submit(["doc.pdf"])
    assert first["submitted"] == 1
    assert second["submitted"] == 0
    assert second["jobs"][0]["job_id"] == first["jobs"][0]["job_id"]


def test_local_only_rejects_before_enqueue(env):
    vault, cfg, store, worker = env
    cfg.ingest.network_policy = "local_only"
    _pdf(vault)
    with pytest.raises(ValueError) as exc_info:
        worker.submit(["doc.pdf"])
    assert "local_only" in str(exc_info.value)
    assert store.queue_depth() == 0          # 入队前拒绝，绝不先上传


def test_source_changed_before_claim_never_parses(env, monkeypatch):
    vault, cfg, store, worker = env
    monkeypatch.setattr("mortis_rag_mcp.ingest.worker.MineruClient", _FakeClient)
    monkeypatch.setattr(worker, "_ensure_worker", lambda store: None)
    monkeypatch.setattr(_FakeClient, "calls", [])
    doc = _pdf(vault)
    result = worker.submit(["doc.pdf"])
    doc.write_bytes(b"%PDF-1.4 CHANGED")
    job = store.claim_job("before-parse")
    assert job.job_id == result["jobs"][0]["job_id"]
    worker._run_job(store, job, "before-parse")
    status = store.job_status(job.job_id)
    assert status.state == "failed" and status.error_code == "SOURCE_CHANGED"
    assert _FakeClient.calls == []
    assert store.get_active("doc.pdf") is None
    assert store.queue_depth() == 0


def test_source_changed_during_parse_never_publishes(env, monkeypatch):
    vault, cfg, store, worker = env
    monkeypatch.setattr(worker, "_ensure_worker", lambda store: None)
    monkeypatch.setattr(_FakeClient, "calls", [])
    doc = _pdf(vault)
    before = doc.stat()

    class ChangesSource(_FakeClient):
        def parse_structured(self, path, **kwargs):
            result = super().parse_structured(path, **kwargs)
            # 同大小、同 mtime：第二次全文 SHA 核验不能被 stat 快路代替。
            path.write_bytes(b"%PDF-1.4 CHANGED")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result

    worker._client = ChangesSource()
    worker.submit(["doc.pdf"])
    job = store.claim_job("during-parse")
    worker._run_job(store, job, "during-parse")
    status = store.job_status(job.job_id)
    assert status.state == "failed" and status.error_code == "SOURCE_CHANGED"
    assert ChangesSource.calls == [job.job_id]
    assert store.get_active("doc.pdf") is None
    assert store.queue_depth() == 0


def test_ignore_matcher_unavailable_fails_closed(env):
    vault, cfg, store, worker = env
    worker.ignore_provider = lambda: None
    _pdf(vault)
    with pytest.raises(ValueError) as exc_info:
        worker.submit(["doc.pdf"])
    assert "ignore matcher" in str(exc_info.value)
    assert store.queue_depth() == 0


@pytest.mark.parametrize('policy_change', ['ignore', 'local_only'])
@pytest.mark.parametrize('when', ['before_parse', 'before_publication'])
def test_worker_rechecks_current_policy(env, monkeypatch, policy_change, when):
    from mortis_rag_mcp._indexer.scanning import IgnoreMatcher
    vault, cfg, store, worker = env
    _pdf(vault)
    patterns = []
    worker.ignore_provider = lambda: IgnoreMatcher(patterns)
    monkeypatch.setattr(worker, '_ensure_worker', lambda store: None)
    worker.submit(['doc.pdf'])
    job = store.claim_job('A')
    assert job is not None
    calls = []
    def change_policy():
        if policy_change == 'ignore':
            patterns.append('*.pdf')
        else:
            cfg.ingest.network_policy = 'local_only'
    class Client:
        def parse_structured(self, *args, **kwargs):
            calls.append(True)
            change_policy()
            return ingest_models.ParseResult(markdown='body', parser_fingerprint='fake',
                channel='v4', model='fake', quality='full')
    worker._client = Client()
    if when == 'before_parse':
        change_policy()
    with pytest.raises(ValueError):
        worker._run_job(store, job, 'A')
    assert len(calls) == (0 if when == 'before_parse' else 1)
    assert store.get_active('doc.pdf') is None
