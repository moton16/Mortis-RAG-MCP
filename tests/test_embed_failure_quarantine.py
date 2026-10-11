from __future__ import annotations

from io import BytesIO
import hashlib
import json
from urllib.error import HTTPError

from mortis_rag_mcp.config import AppConfig, CacheConfig, EmbeddingConfig
from mortis_rag_mcp.indexer import MarkdownIndexer


def _external_config(tmp_path, *, batch_size: int = 2) -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(
            mode="external",
            endpoint="http://127.0.0.1:8000/v1/embeddings",
            dimension=1,
            send_dimensions=False,
            batch_size=batch_size,
        ),
        cache=CacheConfig(
            dir=str(tmp_path / "cache"),
            enabled=True,
            embedding_max_workers=1,
        ),
    )


def test_failed_file_records_bounded_http_batch_summary(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    captured = {}

    def fail(request, timeout):
        captured.update(json.loads(request.data.decode("utf-8")))
        raise HTTPError(
            request.full_url,
            422,
            "unprocessable",
            {},
            BytesIO(b'{"error":{"message":"bad text modality"}}'),
        )

    monkeypatch.setattr("mortis_rag_mcp.providers.urlopen", fail)
    indexer = MarkdownIndexer(tmp_path, _external_config(tmp_path))
    try:
        indexer.sync()
        failure = indexer.failed_files["a.md"]
    finally:
        indexer.close_document_store()

    batch = captured["input"]
    fingerprint = hashlib.sha256(
        json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    assert failure == (
        f"HTTP 422 · bad text modality · batch={len(batch)} sha256:{fingerprint}"
    )
    assert len(failure) <= 1024


class _FlakyProvider:
    is_local_free = True

    def __init__(self) -> None:
        self.calls = 0
        self.fail = True

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("endpoint unavailable")
        return [[1.0] for _ in texts]


def _flaky_indexer(tmp_path):
    (tmp_path / "a.md").write_text("# A\nhello world", encoding="utf-8")
    provider = _FlakyProvider()
    indexer = MarkdownIndexer(
        tmp_path,
        _external_config(tmp_path),
        embedding_provider=provider,
    )
    return indexer, provider


def test_quarantine_skips_round_after_failure_threshold(tmp_path):
    indexer, provider = _flaky_indexer(tmp_path)
    try:
        for _ in range(3):
            indexer.sync()
        assert provider.calls == 3

        indexer.sync()

        assert provider.calls == 3
        assert indexer._sync_progress["quarantined"] == 1
        assert all(chunk.embedding is None for chunk in indexer.all_chunks())
    finally:
        indexer.close_document_store()


def test_quarantine_forces_retries_on_exponential_rounds(tmp_path):
    indexer, provider = _flaky_indexer(tmp_path)
    calls_after_round = []
    try:
        for _ in range(8):
            indexer.sync()
            calls_after_round.append(provider.calls)
    finally:
        indexer.close_document_store()

    assert calls_after_round == [1, 2, 3, 3, 4, 4, 4, 5]


def test_successful_forced_retry_clears_quarantine(tmp_path):
    indexer, provider = _flaky_indexer(tmp_path)
    try:
        for _ in range(4):
            indexer.sync()
        assert provider.calls == 3

        provider.fail = False
        indexer.sync()

        assert provider.calls == 4
        assert indexer.failed_files == {}
        assert indexer._embed_failure_quarantine == {}
        assert all(chunk.embedding is not None for chunk in indexer.all_chunks())
    finally:
        indexer.close_document_store()
