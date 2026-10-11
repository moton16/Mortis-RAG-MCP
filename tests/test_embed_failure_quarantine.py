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
