"""R2 journal: real SQLite connections, atomic prepare and terminal-state CAS."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from mortis_rag_mcp.config import AppConfig
from mortis_rag_mcp.doc_store import ControlStore, resolve_storage_layout
from mortis_rag_mcp.paid_requests import PaidRequestJournal, paid_request_guard
from mortis_rag_mcp.providers import ProviderError


def layout_for(tmp_path):
    cfg = AppConfig()
    cfg.cache.dir = str(tmp_path / "cache")
    cfg.cache.placement = "home"
    return resolve_storage_layout(cfg, tmp_path / "vault")


def test_concurrent_prepare_two_connections(tmp_path):
    layout = layout_for(tmp_path)
    initial = ControlStore(layout)
    initial.open(write=True)
    initial.close()
    barrier = threading.Barrier(2)

    def prepare():
        control = ControlStore(layout)
        control.open(write=True)
        try:
            journal = PaidRequestJournal(control, "embed")
            barrier.wait(timeout=5)
            try:
                return journal.before_send("same", "https://example.invalid", "fp")
            except ProviderError as exc:
                assert "PAID_REQUEST_UNRESOLVED" in str(exc)
                return None
        finally:
            control.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(), range(2)))
    assert sum(item is not None for item in results) == 1
    control = ControlStore(layout)
    try:
        assert len(control.list_request_intents()) == 1
    finally:
        control.close()


def test_unknown_survives_reopen_and_abandon_is_cas(tmp_path):
    layout = layout_for(tmp_path)
    first = ControlStore(layout)
    journal = PaidRequestJournal(first, "embed")
    request_id = journal.before_send("hash", "https://example.invalid", "fp")
    journal.mark_unknown(request_id)
    first.close()
    second = ControlStore(layout)
    try:
        journal = PaidRequestJournal(second, "embed")
        with pytest.raises(ProviderError, match="PAID_REQUEST_UNRESOLVED"):
            journal.before_send("hash", "https://example.invalid", "fp")
        assert second.mark_intent(request_id, "abandoned",
                                  expected_states=("prepared", "submission_unknown")) is True
        assert second.mark_intent(request_id, "success", expected_states=("prepared",)) is False
        assert second.mark_intent(request_id, "submission_unknown") is False
        assert second.intent_state(request_id) == "abandoned"
        next_id = journal.before_send("hash", "https://example.invalid", "fp")
        journal.mark_success(next_id)
        assert second.mark_intent(next_id, "abandoned") is False
        assert second.intent_state(next_id) == "success"
        assert [x["attempt"] for x in second.list_request_intents()] == [1, 2]
    finally:
        second.close()


def test_abandon_and_late_response_two_connections(tmp_path):
    layout = layout_for(tmp_path)
    first, second = ControlStore(layout), ControlStore(layout)
    journal = PaidRequestJournal(first, "embed")
    request_id = journal.before_send("hash", "https://example.invalid", "fp")
    try:
        assert second.mark_intent(request_id, "abandoned") is True
        with pytest.raises(ProviderError, match="REQUEST_INTENT_FINALIZED"):
            journal.mark_success(request_id)
        journal.mark_unknown(request_id)
        assert first.intent_state(request_id) == "abandoned"
    finally:
        first.close()
        second.close()


def test_abandon_success_compete_two_connections(tmp_path):
    layout = layout_for(tmp_path)
    control = ControlStore(layout)
    request_id = PaidRequestJournal(control, "embed").before_send("same", "https://example.invalid", "fp")
    barrier = threading.Barrier(2)
    def finish(state):
        other = ControlStore(layout)
        other.open(write=True)
        try:
            barrier.wait(timeout=5)
            return state, other.mark_intent(request_id, state, expected_states=("prepared",))
        finally:
            other.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, ("success", "abandoned")))
        winners = [state for state, changed in results if changed]
        assert len(winners) == 1
        assert control.intent_state(request_id) == winners[0]
        assert control.mark_intent(request_id, "submission_unknown") is False
        assert control.intent_state(request_id) == winners[0]
    finally:
        control.close()


def test_revoke_absent_profile_records_explicit_revocation(tmp_path):
    control = ControlStore(layout_for(tmp_path))
    try:
        control.revoke_paid_authorization("new-fp")
        assert control.paid_authorization_state("new-fp") == "revoked"
        assert paid_request_guard(control, "new-fp") is False
        assert control.list_payment_authorizations()[0]["revoked_at"] is not None
        control.authorize_paid_profile("new-fp", scope="compat")
        assert paid_request_guard(control, "new-fp", pending_approval=True) is True
    finally:
        control.close()


def test_failed_intent_write_sends_zero_posts(tmp_path, monkeypatch):
    from mortis_rag_mcp.doc_store import StoreCorrupt
    from mortis_rag_mcp.providers import ExternalEmbeddingProvider
    control = ControlStore(layout_for(tmp_path))
    provider = ExternalEmbeddingProvider("https://example.invalid", dimension=2)
    provider.configure_paid_requests(PaidRequestJournal(control, "embed"), lambda _: True, "fp")
    posts = []
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", lambda *a, **k: posts.append(a))
    def fail(*args, **kwargs):
        raise StoreCorrupt("fixture write failed")
    monkeypatch.setattr(control, "record_send_intent", fail)
    with pytest.raises(ProviderError, match="JOURNAL_UNAVAILABLE"):
        provider.embed(["synthetic"])
    assert posts == []
    control.close()


def test_compatible_restart_zero_document_posts(tmp_path, monkeypatch):
    import json
    from mortis_rag_mcp.indexer import MarkdownIndexer
    from test_paid_request_journal import make_config, ResponseStub
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\nfixture body\n", encoding="utf-8")
    calls = []
    def post(request, **kwargs):
        payload = json.loads(request.data)
        calls.append(payload)
        return ResponseStub(json.dumps({"data": [
            {"index": i, "embedding": [1.0] + [0.0] * 1023}
            for i in range(len(payload["input"]))
        ]}).encode())
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", post)
    first = MarkdownIndexer(vault, make_config(tmp_path))
    first.sync()
    assert calls
    first.close_document_store()
    calls.clear()
    second = MarkdownIndexer(vault, make_config(tmp_path))
    try:
        second.sync()
        assert calls == []
        assert all(c.embedding for c in second.all_chunks())
    finally:
        second.close_document_store()


def test_invalid_response_never_persists_vectors(tmp_path, monkeypatch):
    import json
    from mortis_rag_mcp.indexer import MarkdownIndexer
    from test_paid_request_journal import make_config, ResponseStub
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("synthetic invalid vector", encoding="utf-8")
    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", lambda *a, **k: ResponseStub(
        json.dumps({"data": [{"index": 0, "embedding": [float("nan")] * 1024}]}).encode()
    ))
    owner = MarkdownIndexer(vault, make_config(tmp_path))
    try:
        owner.sync()
        assert owner.failed_files
        assert all(c.embedding is None for c in owner.all_chunks())
        assert not owner._vectors_cache_path.exists()
    finally:
        owner.close_document_store()
