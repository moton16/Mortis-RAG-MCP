from __future__ import annotations

import multiprocessing
import time

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout


def open_store(vault, cache):
    cfg = AppConfig()
    cfg.cache.enabled = True
    cfg.cache.dir = str(cache)
    store = DocumentStore(resolve_storage_layout(cfg, vault, registered_vaults=[]), cfg)
    store.open(write=True)
    return store


def claim_process(vault, cache, owner, start, results):
    store = open_store(vault, cache)
    start.wait(10)
    job = store.claim_job(owner)
    results.put(job.owner_token if job else None)
    store.close()


def test_two_real_processes_claim_once(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    cache = tmp_path / 'cache'
    store = open_store(vault, cache)
    store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.close()
    ctx = multiprocessing.get_context('spawn')
    start, results = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=claim_process, args=(vault, cache, owner, start, results))
               for owner in ('A', 'B')]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(20)
        assert not worker.is_alive()
        assert worker.exitcode == 0
    claimed = [results.get(timeout=5), results.get(timeout=5)]
    assert sum(value is not None for value in claimed) == 1


def test_done_failure_rolls_back_revision_and_change_seq(tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    owned = store.claim_job('A')
    assert owned is not None
    staged = store.stage_revision(source='a.pdf', source_sha256='sha', render_sha256='render',
        parser_fingerprint='parser', markdown='body', job_id=job.job_id, owner_token='A')
    before = store.change_seq()
    original = store._write
    def fail_done(conn, sql, params=()):
        if sql.startswith('UPDATE ingest_jobs SET state = ?'):
            raise RuntimeError('fault between revision and done')
        return original(conn, sql, params)
    monkeypatch.setattr(store, '_write', fail_done)
    with pytest.raises(RuntimeError, match='fault'):
        store.commit_job_revision(job.job_id, owned.owner_token, staged.revision_id)
    assert store.get_active('a.pdf') is None
    assert store.change_seq() == before
    status = store.job_status(job.job_id)
    assert status is not None and status.state == 'parsing'
    store.close()


def test_send_intent_expired_lease_must_not_be_reclaimed(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.claim_job('A', now=time.time() - 2, lease_seconds=1)
    with store.mutation() as conn:
        conn.execute("UPDATE ingest_jobs SET phase = 'send_intent' WHERE job_id = ?", (job.job_id,))
        conn.commit()
    store.close()
    restarted = open_store(vault, tmp_path / 'cache')
    assert restarted.claim_job('B') is None
    status = restarted.job_status(job.job_id)
    assert status is not None and status.error_code == 'SUBMISSION_UNKNOWN'
    from mortis_rag_mcp.doc_store import StoreConflict
    for force in (False, True):
        with pytest.raises(StoreConflict, match='SUBMISSION_UNKNOWN'):
            restarted.enqueue_job(source='a.pdf', source_sha256='sha',
                                  parser_fingerprint='parser', force=force)
    with pytest.raises(StoreConflict, match='SUBMISSION_UNKNOWN'):
        restarted.retry_job(job.job_id)
    restarted.close()


def test_intent_persistence_failure_prevents_post(monkeypatch):
    from mortis_rag_mcp.ingest.mineru import MineruClient
    calls = []
    monkeypatch.setattr('mortis_rag_mcp.ingest.mineru._http_json',
                        lambda *args: calls.append(args))
    def refuse(kind, payload):
        if kind == 'send_intent':
            raise RuntimeError('cannot persist intent')
    client = MineruClient(api_key='fake')
    with pytest.raises(RuntimeError, match='persist intent'):
        client._paid_post('https://mineru.net/api/v4/test', {},
                          intent_recorder=refuse, request_id='test', label='test')
    assert not calls


@pytest.mark.parametrize('operation', ['stage', 'renew', 'phase', 'blob', 'fail'])
def test_epoch_change_fences_every_owner_write(tmp_path, operation):
    from mortis_rag_mcp.doc_store import OwnershipLost
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.claim_job('A')
    from mortis_rag_mcp.doc_store import ControlStore
    control = ControlStore(store.layout)
    control.open(write=True)
    control.bump_epoch()
    control.close()
    writes = {
        'stage': lambda: store.stage_revision(source='a.pdf', source_sha256='sha',
            render_sha256='render', parser_fingerprint='parser', markdown='body',
            job_id=job.job_id, owner_token='A'),
        'renew': lambda: store.renew_lease(job.job_id, 'A'),
        'phase': lambda: store.report_phase(job.job_id, 'A', phase='send_intent'),
        'blob': lambda: store.put_media_blob(data=b'payload', mime_type='image/png',
            job_id=job.job_id, owner_token='A'),
        'fail': lambda: store.fail_job(job.job_id, 'A', error_code='FAIL'),
    }
    with pytest.raises(OwnershipLost):
        writes[operation]()
    assert store.get_active('a.pdf') is None
    store.close()


def test_job_cannot_publish_another_source(tmp_path):
    from mortis_rag_mcp.doc_store import StoreConflict
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.claim_job('A')
    with pytest.raises(StoreConflict):
        store.stage_revision(source='b.pdf', source_sha256='sha', render_sha256='render',
            parser_fingerprint='parser', markdown='body', job_id=job.job_id, owner_token='A')
    other = store.stage_revision(source='b.pdf', source_sha256='sha', render_sha256='render',
        parser_fingerprint='parser', markdown='body')
    with pytest.raises(StoreConflict):
        store.commit_job_revision(job.job_id, 'A', other.revision_id)
    assert store.get_active('b.pdf') is None
    store.close()


def test_expired_sink_writes_zero_blobs(tmp_path):
    from mortis_rag_mcp.doc_store import OwnershipLost
    from mortis_rag_mcp.ingest.worker import StoreMediaSink
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.claim_job('A', now=time.time() - 2, lease_seconds=1)
    sink = StoreMediaSink(store, job_id=job.job_id, owner_token='A')
    with pytest.raises(OwnershipLost):
        sink.add(name='image.png', data=b'payload', kind='image', ordinal=1, mime_type='image/png')
    conn = store._open_conn(store._ensure_read_generation())
    assert conn.execute('SELECT COUNT(*) FROM media_blobs').fetchone()[0] == 0
    assert not sink.items
    store.close()


@pytest.mark.parametrize('phase', ['send_intent', 'submitted', 'polling', 'downloaded'])
def test_force_and_cancel_cannot_restart_pending_remote(tmp_path, phase):
    from mortis_rag_mcp.doc_store import StoreConflict
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.claim_job('A')
    store.report_phase(job.job_id, 'A', phase=phase)
    merged, created = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    assert not created and merged.job_id == job.job_id
    for sha in ('sha', 'changed'):
        with pytest.raises(StoreConflict, match='SUBMISSION_UNKNOWN'):
            store.enqueue_job(source='a.pdf', source_sha256=sha, parser_fingerprint='parser', force=True)
    assert store.cancel_job(job.job_id)
    with pytest.raises(StoreConflict, match='SUBMISSION_UNKNOWN'):
        store.retry_job(job.job_id)
    with pytest.raises(StoreConflict, match='SUBMISSION_UNKNOWN'):
        store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.close()


@pytest.mark.parametrize('http_status', [429, 500, 503])
def test_paid_http_without_nonacceptance_evidence_is_unknown(monkeypatch, http_status):
    from mortis_rag_mcp.ingest.mineru import MineruClient, MineruError
    calls, intents = [], []
    def refuse(*args):
        calls.append(True)
        raise MineruError('response uncertain', http_status=http_status, retryable=True)
    monkeypatch.setattr('mortis_rag_mcp.ingest.mineru._http_json', refuse)
    with pytest.raises(MineruError) as error:
        MineruClient(api_key='fake')._paid_post('https://mineru.net/api/v4/test', {},
            intent_recorder=lambda kind, payload: intents.append(kind), request_id='test', label='test')
    assert error.value.code_str == 'SUBMISSION_UNKNOWN' and not error.value.retryable
    assert len(calls) == 1 and intents == ['prepared', 'send_intent', 'submission_unknown']


def test_upload_actual_bytes_bounded_even_when_stat_lies(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from mortis_rag_mcp.ingest.mineru import MineruClient, MineruError
    from mortis_rag_mcp.ingest.models import ResourceLimits
    path = tmp_path / 'input.pdf'
    path.write_bytes(b'X' * 11)
    original = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda value, *args, **kwargs:
        SimpleNamespace(st_size=1) if value == path else original(value, *args, **kwargs))
    client = MineruClient(api_key='fake', limits=ResourceLimits(memory_budget_bytes=10))
    with pytest.raises(MineruError) as error:
        client._upload_payload(path)
    assert error.value.code_str == 'RESOURCE_LIMIT'


def test_downloaded_expired_job_never_reposts(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    stamp = time.time()
    store.claim_job('A', now=stamp, lease_seconds=1)
    store.report_phase(job.job_id, 'A', phase='downloaded', remote_task_id='paid-task')
    assert store.claim_job('B', now=stamp + 2) is None
    status = store.job_status(job.job_id)
    assert status is not None and status.error_code == 'SUBMISSION_UNKNOWN'
    conn = store._open_conn(store._ensure_read_generation())
    assert conn.execute('SELECT remote_task_id FROM ingest_subjobs WHERE job_id = ?',
                        (job.job_id,)).fetchone()[0] == 'paid-task'
    store.close()


def test_nested_io_preserves_and_tightens_deadline():
    from mortis_rag_mcp.ingest.mineru import _io_profile, _current_io, _remaining_seconds, MineruError
    stamp = time.monotonic()
    with _io_profile(deadline=stamp + 1):
        with _io_profile(deadline=stamp + 300):
            current = _current_io()
            assert current is not None and current.deadline == stamp + 1
            assert 0 < _remaining_seconds(300) <= 1
        with _io_profile(deadline=stamp - 1):
            with pytest.raises(MineruError):
                _remaining_seconds(300)
    assert _current_io() is None


def test_live_parse_blocks_gc_until_media_attached(tmp_path):
    from mortis_rag_mcp.doc_store import StoreConflict
    from mortis_rag_mcp.ingest.worker import StoreMediaSink
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = open_store(vault, tmp_path / 'cache')
    job, _ = store.enqueue_job(source='a.pdf', source_sha256='sha', parser_fingerprint='parser')
    store.claim_job('A')
    sink = StoreMediaSink(store, job_id=job.job_id, owner_token='A')
    sink.add(name='image.png', data=b'payload', kind='image', ordinal=1, mime_type='image/png')
    other = open_store(vault, tmp_path / 'cache')
    with pytest.raises(StoreConflict, match='active parse lease'):
        other.gc_unreferenced()
    staged = store.stage_revision(source='a.pdf', source_sha256='sha', render_sha256='render',
        parser_fingerprint='parser', markdown='body', job_id=job.job_id, owner_token='A')
    assert store.attach_occurrences(staged.revision_id, sink.items, job_id=job.job_id, owner_token='A') == 1
    store.commit_job_revision(job.job_id, 'A', staged.revision_id)
    assert other.gc_unreferenced() == 0
    other.close()
    store.close()
