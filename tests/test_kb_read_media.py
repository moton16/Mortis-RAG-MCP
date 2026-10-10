import base64
import io
import json
import wave
from types import SimpleNamespace

from mortis_rag_mcp._server.media_dispatch import dispatch_media_read, media_content_result


def metadata(**extra):
    return {"kind": "image", "mime_type": "image/png", "caption": "chart", **extra}


def error(result):
    return json.loads(result["content"][0]["text"])["code"]


def test_standard_image_and_full_envelope_budget():
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    result = media_content_result(metadata(), data, budget_bytes=2048)
    assert result["content"][1]["type"] == "image"
    assert base64.b64decode(result["content"][1]["data"]) == data
    small = media_content_result(metadata(), data, budget_bytes=100)
    assert error(small) == "MEDIA_TOO_LARGE"
    assert "data" not in small["content"][0]
    assert json.loads(small["content"][0]["text"])["required_bytes"] > 100


def test_active_mime_not_inline_and_metadata_supported():
    for mime in ("image/svg+xml", "text/html", "application/octet-stream"):
        assert error(media_content_result(metadata(mime_type=mime), b"active")) == "MEDIA_UNAVAILABLE"
        assert "isError" not in media_content_result(metadata(mime_type=mime))


def test_pcm_segment_audio_standard_block():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 80)
    meta = metadata(kind="audio", mime_type="audio/wav", t_start_ms=0, t_end_ms=10)
    assert media_content_result(meta, output.getvalue())["content"][1]["type"] == "audio"
    assert error(media_content_result(meta, b"not wave")) == "MEDIA_UNAVAILABLE"
    assert error(media_content_result(metadata(kind="audio", mime_type="audio/wav"), output.getvalue())) == "MEDIA_UNAVAILABLE"


def test_missing_explicit_address_no_indexer_lookup():
    server = SimpleNamespace()
    for missing in ("vault_path", "source", "revision_id", "occurrence_id"):
        args = {"vault_path": "vault", "source": "book.pdf", "revision_id": "r", "occurrence_id": "o"}
        args.pop(missing)
        assert error(dispatch_media_read(server, args)) == "INVALID_ARGUMENT"


def test_metadata_never_reads_blob_and_explicit_solo_is_allowed():
    reads = []

    class Store:
        def read_media(self, source, *, revision_id, occurrence_id, variant_id="original",
                       max_bytes=20 * 1024 * 1024, include_data=False):
            reads.append((variant_id, include_data))
            return {"blob_id": "b", "mime_type": "image/png", "byte_size": 4,
                    "width": 2, "height": 2, "duration_ms": None}

        def list_media(self, source, *, revision_id="", offset=0, limit=100):
            return [{"occurrence_id": "o", "kind": "image", "page": 1, "caption": "cap",
                     "ocr": "", "t_start_ms": None, "t_end_ms": None}]

    indexer = SimpleNamespace(
        resolve_virtual_source=lambda source, revision_id="": {"revision_id": revision_id},
        document_store=lambda: Store())
    server = SimpleNamespace(_resolve_vault_path=lambda value: value,
                             _indexer_for=lambda value: indexer,
                             config=SimpleNamespace(media=SimpleNamespace()))
    args = {"vault_path": "solo", "source": "book.pdf", "revision_id": "r", "occurrence_id": "o"}
    result = dispatch_media_read(server, args)
    assert "isError" not in result
    assert reads == [("original", False)]
    meta = json.loads(result["content"][0]["text"])
    assert meta["host_media_verified"] is False and meta["caption"] == "cap"


def test_no_secure_resolver_fails_closed():
    server = SimpleNamespace(_resolve_vault_path=lambda value: value,
                             _indexer_for=lambda value: SimpleNamespace())
    args = {"vault_path": "vault", "source": "book.pdf", "revision_id": "r", "occurrence_id": "o"}
    assert error(dispatch_media_read(server, args)) == "MEDIA_UNAVAILABLE"
