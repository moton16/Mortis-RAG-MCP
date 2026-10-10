from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mortis_rag_mcp.config import AppConfig, EmbeddingConfig
from mortis_rag_mcp.doc_store import DocumentStore, resolve_storage_layout
from mortis_rag_mcp.indexer import MarkdownIndexer
from mortis_rag_mcp._indexer import scanning, sync_engine
from mortis_rag_mcp._indexer.exemptions import _prune_ignored_sources


@pytest.fixture
def indexer(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    cfg = AppConfig(embedding=EmbeddingConfig(mode='static', dimension=8))
    cfg.cache.dir = str(tmp_path / 'cache')
    cfg.cache.enabled = True
    idx = MarkdownIndexer(vault, cfg)
    idx._storage_layout = resolve_storage_layout(cfg, vault, registered_vaults=[])
    yield idx
    idx.close_document_store()


def publish(idx, source='a.pdf', body='# Virtual\n\nneedle rendered text\n'):
    path = idx.vault_path / source
    path.write_bytes(b'binary-source')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    store = idx.document_store(write=True)
    staged = store.stage_revision(source=source, source_sha256=sha,
        render_sha256=hashlib.sha256(body.encode()).hexdigest(),
        parser_fingerprint='test', markdown=body)
    store.commit_revision(staged.revision_id, source_sha256=sha)
    return staged


def test_virtual_committed_enumeration_reuse_and_restart(indexer):
    staged = publish(indexer)
    indexer.sync()
    chunks = indexer._chunks['a.pdf']
    assert chunks[0].metadata['revision_id'] == staged.revision_id
    assert indexer.search('needle', use_rerank=False)[0].source == 'a.pdf'
    signature = indexer._signatures['a.pdf']
    indexer.sync()
    assert indexer._chunks['a.pdf'] is chunks
    assert indexer._signatures['a.pdf'] == signature
    assert indexer.document_store().get_derived_generation(indexer._derived_profile_key()).status == 'ready'
    indexer.close_document_store()
    indexer.sync()
    assert indexer._chunks['a.pdf'] is chunks


def test_staged_and_cached_do_not_index(indexer):
    store = indexer.document_store(write=True)
    store.stage_revision(source='staged.pdf', source_sha256='sha', render_sha256='render',
                         parser_fingerprint='test', markdown='secret')
    cached = indexer.vault_path / '.mcp_cache'
    cached.mkdir()
    (cached / 'cache.md').write_text('cache secret', encoding='utf-8')
    indexer.config.cache.placement = 'vault'
    indexer.sync()
    assert not indexer._chunks
    assert store.list_documents()[0].visibility == 'unverified'


def test_root_offline_preserves_physical_state_identity(indexer, monkeypatch):
    (indexer.vault_path / 'note.md').write_text('needle', encoding='utf-8')
    indexer.sync()
    chunks, stats = indexer._chunks, indexer._stat_cache
    original = scanning.os.scandir
    def unavailable(path):
        if Path(path) == indexer.vault_path:
            raise PermissionError('offline')
        return original(path)
    monkeypatch.setattr(scanning.os, 'scandir', unavailable)
    result = scanning.scan_indexable_files(indexer.vault_path, indexer._ignore_matcher(), frozenset({'.md'}))
    assert not result.complete and not result.root_available
    indexer.sync()
    assert indexer._chunks is chunks and indexer._stat_cache is stats
    assert 'note.md' in chunks


def test_inaccessible_subtree_disables_all_removed(indexer, monkeypatch):
    folder = indexer.vault_path / 'private'
    folder.mkdir()
    (folder / 'note.md').write_text('needle', encoding='utf-8')
    other = indexer.vault_path / 'other.md'
    other.write_text('other', encoding='utf-8')
    indexer.sync()
    other.unlink()
    original = scanning.os.scandir
    def inaccessible(path):
        if Path(path) == folder:
            raise PermissionError('denied')
        return original(path)
    monkeypatch.setattr(scanning.os, 'scandir', inaccessible)
    result = scanning.scan_indexable_files(indexer.vault_path, indexer._ignore_matcher(), frozenset({'.md'}))
    assert result.inaccessible == frozenset({'private'}) and not result.complete
    indexer.sync()
    assert set(indexer._chunks) == {'private/note.md', 'other.md'}


@pytest.mark.parametrize('visibility', ['deleted', 'exempt', 'unverified'])
def test_hidden_source_final_filter_and_revoke(indexer, visibility):
    publish(indexer)
    (indexer.vault_path / 'note.md').write_text('needle physical', encoding='utf-8')
    indexer.sync()
    indexer.document_store(write=True).set_visibility('a.pdf', visibility)
    assert {c.source for c in indexer.search('needle', use_rerank=False)} == {'note.md'}
    assert {f['source'] for f in indexer.list_files()} == {'note.md'}
    indexer.sync()
    assert 'a.pdf' not in indexer._chunks
    assert 'note.md' in indexer._chunks


def test_revoke_clears_all_layers_and_exemption_store(indexer, monkeypatch):
    publish(indexer)
    indexer.sync()
    ids = {c.id for c in indexer._chunks['a.pdf']}
    indexer._pending_vectors.update({cid: [1] for cid in ids})
    indexer._disk_vectors.update(ids)
    indexer.fast_path_warnings['a.pdf'] = 'warn'
    indexer.failed_files['a.pdf'] = 'fail'
    deleted = []
    monkeypatch.setattr(indexer._vector_backend, 'delete_vectors', lambda values: deleted.extend(values))
    _prune_ignored_sources(indexer, scanning.IgnoreMatcher(['*.pdf']))
    for mapping in (indexer._chunks, indexer._signatures, indexer._stat_cache,
                    indexer._stat_seen_ns, indexer._stat_confirmations,
                    indexer.fast_path_warnings, indexer.failed_files):
        assert 'a.pdf' not in mapping
    assert ids == set(deleted)
    assert not indexer._disk_vectors.intersection(ids)
    assert not indexer._pending_vectors.keys() & ids
    assert indexer.document_store().list_documents('exempt')[0].source == 'a.pdf'


def test_source_change_revokes_without_parse_or_new_text(indexer):
    publish(indexer)
    indexer.sync()
    (indexer.vault_path / 'a.pdf').write_bytes(b'changed source')
    indexer.sync()
    assert 'a.pdf' not in indexer._chunks
    assert indexer.document_store().list_documents()[0].visibility == 'unverified'
    assert indexer.document_store().get_derived_generation(indexer._derived_profile_key()).status == 'stale'
    assert not indexer._vector_route_allowed()


def test_generation_no_record_compatible_stale_disables_query(indexer, monkeypatch):
    assert indexer._vector_route_allowed()
    publish(indexer)
    indexer.sync()
    store = indexer.document_store(write=True)
    store.mark_derived_generation(indexer._derived_profile_key(), change_seq=store.change_seq(),
        chunker_fingerprint='', space_fingerprint='', status='stale')
    indexer.config.embedding.mode = 'external'
    calls = []
    monkeypatch.setattr(indexer.embedding_provider, 'embed', lambda texts: calls.append(texts))
    assert indexer.search('needle', use_rerank=False)
    assert not calls


def test_hidden_facts_not_automatically_reactivated(indexer):
    publish(indexer)
    indexer.sync()
    _prune_ignored_sources(indexer, scanning.IgnoreMatcher(['*.pdf']))
    indexer.sync()
    assert 'a.pdf' not in indexer._chunks
    store = indexer.document_store(write=True)
    assert store.list_documents()[0].visibility == 'exempt'
    store.invalidate_source('a.pdf')
    indexer.sync()
    assert 'a.pdf' not in indexer._chunks
    assert store.list_documents()[0].visibility == 'unverified'


def test_same_text_new_revision_cannot_reuse_old_address(indexer):
    publish(indexer)
    indexer.sync()
    original = indexer._chunks['a.pdf'][0].id
    publish(indexer)
    assert not indexer.search('needle', use_rerank=False)
    indexer.sync()
    assert indexer._chunks['a.pdf'][0].id != original


def test_virtual_root_offline_retains_committed_fact(indexer, monkeypatch):
    publish(indexer)
    indexer.sync()
    original = Path.open
    def unavailable(path, *args, **kwargs):
        if path == indexer.vault_path / 'a.pdf':
            raise PermissionError('offline')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', unavailable)
    indexer.sync()
    store = indexer.document_store()
    document = store.list_documents()[0]
    assert document.visibility == 'unverified' and document.active_revision
    assert store.get_active('a.pdf', include_hidden=True) is not None
    assert not indexer.search('needle', use_rerank=False)


def test_true_virtual_delete_retains_revision_fact(indexer):
    publish(indexer)
    indexer.sync()
    (indexer.vault_path / 'a.pdf').unlink()
    indexer.sync()
    assert indexer.document_store().list_documents()[0].visibility == 'deleted'
    assert 'a.pdf' not in indexer._chunks


def test_corrupt_store_keeps_physical_sync(indexer, monkeypatch):
    from mortis_rag_mcp.doc_store import StoreCorrupt
    publish(indexer)
    indexer.sync()
    def corrupt(*args, **kwargs):
        raise StoreCorrupt('fault')
    monkeypatch.setattr(indexer.document_store(), 'list_documents', corrupt)
    (indexer.vault_path / 'new.md').write_text('needle physical', encoding='utf-8')
    indexer.sync()
    assert 'new.md' in indexer._chunks and 'a.pdf' not in indexer._chunks
    (indexer.vault_path / 'new.md').write_text('updated physical', encoding='utf-8')
    indexer.sync()
    assert indexer.search('updated', use_rerank=False)[0].source == 'new.md'
    assert indexer.list_files()[0]['source'] == 'new.md'


def test_publish_between_list_and_seq_never_ready(indexer, monkeypatch):
    publish(indexer)
    store = indexer.document_store()
    original = store.list_documents
    calls = []
    def publish_during_list(*args, **kwargs):
        documents = original(*args, **kwargs)
        if not calls:
            calls.append(True)
            publish(indexer, 'new.pdf')
        return documents
    monkeypatch.setattr(store, 'list_documents', publish_during_list)
    indexer.sync()
    generation = store.get_derived_generation(indexer._derived_profile_key())
    assert generation.status == 'stale'
    assert generation.change_seq < store.change_seq()
    indexer.sync()
    assert 'new.pdf' in indexer._chunks
    assert store.get_derived_generation(indexer._derived_profile_key()).status == 'ready'


def test_ignore_final_filter_with_broken_store(indexer, monkeypatch):
    from mortis_rag_mcp.doc_store import StoreCorrupt
    from mortis_rag_mcp._indexer import search
    (indexer.vault_path / 'note.md').write_text('needle physical', encoding='utf-8')
    indexer.sync()
    def corrupt(*args, **kwargs):
        raise StoreCorrupt('fault')
    monkeypatch.setattr(indexer.document_store(), 'list_documents', corrupt)
    def rerank(query, ranked, provider, **kwargs):
        (indexer.vault_path / '.vaultignore').write_text('note.md\n', encoding='utf-8')
        return ranked
    indexer.reranker_provider = object()
    monkeypatch.setattr(search, 'rerank_chunks', rerank)
    assert not indexer.search('needle', use_rerank=True)


def test_stale_generation_recovers_on_noop_sync(indexer):
    publish(indexer)
    indexer.sync()
    store = indexer.document_store(write=True)
    chunks = indexer._chunks['a.pdf']
    store.mark_derived_generation(indexer._derived_profile_key(), change_seq=store.change_seq(),
        chunker_fingerprint='', space_fingerprint='', status='stale')
    indexer.sync()
    assert indexer._chunks['a.pdf'] is chunks
    assert store.get_derived_generation(indexer._derived_profile_key()).status == 'ready'


def test_offline_request_refresh_identity_lock_order(indexer, monkeypatch):
    (indexer.vault_path / 'note.md').write_text('needle', encoding='utf-8')
    indexer.sync()
    chunks, stats = indexer._chunks, indexer._stat_cache
    original = Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda path: False if path == indexer.vault_path else original(path))
    class ForbiddenLock:
        def __enter__(self):
            pytest.fail('offline refresh must not acquire cache lock')
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(indexer, '_cache_lock', ForbiddenLock())
    assert not indexer.request_refresh()
    assert indexer._chunks is chunks and indexer._stat_cache is stats
    assert 'note.md' in chunks


@pytest.mark.parametrize('backend', ['memory', 'sqlite_vec'])
def test_revoke_fts_vectors_and_restart(tmp_path, backend):
    from mortis_rag_mcp.config import CacheConfig, VectorConfig
    if backend == 'sqlite_vec':
        pytest.importorskip('sqlite_vec')
    vault = tmp_path / 'vault'
    vault.mkdir()
    cfg = AppConfig(embedding=EmbeddingConfig(mode='external', dimension=8),
        cache=CacheConfig(enabled=True, dir=str(tmp_path / 'cache')),
        vector=VectorConfig(backend=backend))
    class Provider:
        dimension = 8
        def embed(self, texts):
            return [[1.0] + [0.0] * 7 for text in texts]
    idx = MarkdownIndexer(vault, cfg, embedding_provider=Provider())
    ids = set()
    try:
        publish(idx)
        idx.sync()
        ids = {chunk.id for chunk in idx.all_chunks()}
        assert idx._fts is not None and idx._fts.count() > 0
        assert idx._vectors_on_disk == (backend == 'sqlite_vec')
        assert idx._vector_backend.get_vectors(ids)
        idx.document_store(write=True).set_visibility('a.pdf', 'exempt')
        idx.sync()
        assert idx._fts.count() == 0
        assert not idx._vector_backend.get_vectors(ids)
        assert not idx.search('needle', use_rerank=False)
    finally:
        idx.close_document_store()
        close = getattr(idx._vector_backend, 'close', None)
        if close is not None:
            close()
        if idx._fts is not None:
            idx._fts.close()
    restarted = MarkdownIndexer(vault, cfg, embedding_provider=Provider())
    try:
        restarted.sync()
        assert not restarted.all_chunks()
        assert not restarted._vector_backend.get_vectors(ids)
        assert restarted._fts is not None and restarted._fts.count() == 0
    finally:
        restarted.close_document_store()
        close = getattr(restarted._vector_backend, 'close', None)
        if close is not None:
            close()
        if restarted._fts is not None:
            restarted._fts.close()


def test_ready_mark_rechecks_change_seq_under_mutation(indexer, monkeypatch):
    publish(indexer)
    store = indexer.document_store(write=True)
    original = store.mark_derived_generation
    calls = []
    def publish_before_ready(profile_key, **kwargs):
        if kwargs['status'] == 'ready' and not calls:
            calls.append(True)
            publish(indexer, 'late.pdf')
        return original(profile_key, **kwargs)
    monkeypatch.setattr(store, 'mark_derived_generation', publish_before_ready)
    indexer.sync()
    assert store.get_derived_generation(indexer._derived_profile_key()).status == 'stale'
    indexer.sync()
    assert 'late.pdf' in indexer._chunks
    assert store.get_derived_generation(indexer._derived_profile_key()).status == 'ready'


@pytest.mark.parametrize('operation', ['get_derived_generation', 'mark_derived_generation'])
def test_derived_failure_keeps_physical_sync(indexer, monkeypatch, operation):
    from mortis_rag_mcp.doc_store import StoreCorrupt
    publish(indexer)
    indexer.sync()
    store = indexer.document_store(write=True)
    def broken(*args, **kwargs):
        raise StoreCorrupt('derived table unavailable')
    monkeypatch.setattr(store, operation, broken)
    (indexer.vault_path / 'new.md').write_text('new physical needle', encoding='utf-8')
    indexer.sync()
    assert 'new.md' in indexer._chunks
    assert 'a.pdf' not in indexer._chunks
    assert any(chunk.source == 'new.md' for chunk in indexer.search('needle', use_rerank=False))


def test_empty_query_filters_recheck_visibility(indexer, monkeypatch):
    from mortis_rag_mcp._indexer.models import SearchFilter
    publish(indexer)
    indexer.sync()
    store = indexer.document_store(write=True)
    def hide(self, chunk):
        store.set_visibility(chunk.source, 'exempt')
        return True
    monkeypatch.setattr(SearchFilter, 'matches', hide)
    assert indexer.search('', filters=SearchFilter(), dedupe=False) == []
