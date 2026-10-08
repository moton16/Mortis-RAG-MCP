import base64
import io
import json
import wave
from types import SimpleNamespace

import pytest

from mortis_rag_mcp._indexer.reading import VirtualReadError
from mortis_rag_mcp._server.media_dispatch import dispatch_media_read
from mortis_rag_mcp.doc_store import StoreConflict, StoreContractError, StoreQuotaExceeded

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff\xe0"


def pcm_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 40)
    return output.getvalue()


def code(result):
    return json.loads(result["content"][0]["text"])["code"]


class Store:
    """按真实 `DocumentStore.read_media` 的关键字-only 契约与数据预算的替身。"""

    def __init__(self, variants, occurrence=None, *, active="r", active_after=None):
        self.variants = variants
        self.occurrence = occurrence or {
            "occurrence_id": "o", "kind": "image", "page": 2, "caption": "chart",
            "ocr": "text", "t_start_ms": None, "t_end_ms": None}
        self.active = active
        self.active_after = active_after
        self.reads = []
        self.listed = 0

    def read_media(self, source, *, revision_id, occurrence_id, variant_id="original",
                   max_bytes=20 * 1024 * 1024, include_data=False):
        self.reads.append((variant_id, include_data, max_bytes))
        if revision_id != self.active:
            raise StoreConflict("stale")
        entry = self.variants.get(variant_id)
        if entry is None:
            raise StoreContractError("MEDIA_NOT_FOUND")
        data = entry["data"]
        result = {"blob_id": entry.get("blob_id", "b"), "mime_type": entry["mime"],
                  "byte_size": len(data), "width": entry.get("width"), "height": entry.get("height"),
                  "duration_ms": entry.get("duration_ms"), "source": source,
                  "revision_id": revision_id, "occurrence_id": occurrence_id, "variant_id": variant_id}
        if include_data:
            if len(data) > max_bytes:
                raise StoreQuotaExceeded("MEDIA_TOO_LARGE")
            result["data"] = data
        return result

    def list_media(self, source, *, revision_id="", offset=0, limit=100):
        self.listed += 1
        return [dict(self.occurrence)] if offset == 0 else []

    def get_active(self, source, *, include_hidden=False):
        rev = self.active_after or self.active
        return SimpleNamespace(revision=SimpleNamespace(revision_id=rev))


def make_server(store, resolver=None, media=None):
    indexer = SimpleNamespace(
        resolve_virtual_source=resolver or (lambda source, revision_id="": {"revision_id": revision_id}),
        document_store=lambda: store)
    return SimpleNamespace(_resolve_vault_path=lambda value: value,
                           _indexer_for=lambda value: indexer,
                           config=SimpleNamespace(media=media or SimpleNamespace()))


ARGS = {"vault_path": "vault", "source": "book.pdf", "revision_id": "r", "occurrence_id": "o"}


def test_metadata_default_reads_no_blob_data():
    store = Store({"original": {"mime": "image/png", "data": PNG + b"payload", "width": 3, "height": 3}})
    result = dispatch_media_read(make_server(store), ARGS)
    assert "isError" not in result
    meta = json.loads(result["content"][0]["text"])
    assert meta["caption"] == "chart" and meta["page"] == 2 and meta["host_media_verified"] is False
    assert all(include is False for _, include, _ in store.reads)
    assert store.listed == 1


def test_resolver_stale_or_mismatched_revision_fails_closed():
    store = Store({"original": {"mime": "image/png", "data": PNG}})
    mismatch = lambda source, revision_id="": {"revision_id": "other"}
    assert code(dispatch_media_read(make_server(store, resolver=mismatch), ARGS)) == "MEDIA_UNAVAILABLE"

    def raiser(source, revision_id=""):
        raise VirtualReadError("STALE", source)

    assert code(dispatch_media_read(make_server(store, resolver=raiser), ARGS)) == "MEDIA_UNAVAILABLE"

    def owner_raiser(source, revision_id=""):
        raise VirtualReadError("UNAVAILABLE", source)  # 越权/库归属/ignore 均 fail-closed

    assert code(dispatch_media_read(make_server(store, resolver=owner_raiser), ARGS)) == "MEDIA_UNAVAILABLE"


def test_missing_occurrence_maps_to_unavailable():
    store = Store({})  # no original variant -> MEDIA_NOT_FOUND
    assert code(dispatch_media_read(make_server(store), ARGS)) == "MEDIA_UNAVAILABLE"


def test_inline_original_requires_magic_and_current_revision():
    args = {**ARGS, "representation": "inline", "variant": "original"}
    store = Store({"original": {"mime": "image/png", "data": PNG + b"payload"}})
    result = dispatch_media_read(make_server(store), args)
    assert result["content"][1]["type"] == "image"
    assert base64.b64decode(result["content"][1]["data"]) == PNG + b"payload"

    liar = Store({"original": {"mime": "image/png", "data": b"not a png at all"}})
    assert code(dispatch_media_read(make_server(liar), args)) == "MEDIA_UNAVAILABLE"

    late = Store({"original": {"mime": "image/png", "data": PNG + b"x"}}, active_after="r2")
    assert code(dispatch_media_read(make_server(late), args)) == "MEDIA_UNAVAILABLE"


def test_inline_original_too_large_maps_media_too_large():
    store = Store({"original": {"mime": "image/png", "data": PNG + b"x" * 5000}})
    media = SimpleNamespace(inline_max_bytes=64)
    args = {**ARGS, "representation": "inline", "variant": "original"}
    result = dispatch_media_read(make_server(store, media=media), args)
    assert code(result) == "MEDIA_TOO_LARGE"
    assert "data" not in result["content"][0]


def test_inline_budget_rejects_without_truncating_base64():
    blob = PNG + b"y" * 4000
    store = Store({"original": {"mime": "image/png", "data": blob}})
    args = {**ARGS, "representation": "inline", "variant": "original", "budget_bytes": 512}
    result = dispatch_media_read(make_server(store), args)
    assert code(result) == "MEDIA_TOO_LARGE"
    assert isinstance(json.loads(result["content"][0]["text"]).get("required_bytes"), int)


def test_stored_preview_variant_is_preferred_and_not_regenerated():
    store = Store({"preview": {"mime": "image/jpeg", "data": JPEG + b"small"},
                   "original": {"mime": "image/png", "data": PNG + b"huge"}})
    args = {**ARGS, "representation": "inline"}
    result = dispatch_media_read(make_server(store), args)
    meta = json.loads(result["content"][0]["text"])
    assert meta["variant_source"] == "stored" and meta["mime_type"] == "image/jpeg"
    assert base64.b64decode(result["content"][1]["data"]) == JPEG + b"small"
    assert ("original", True, 20 * 1024 * 1024) not in store.reads


def test_preview_generation_and_unavailable_fallback():
    pytest.importorskip("PIL")
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 20, 30)).save(buffer, format="PNG")
    store = Store({"original": {"mime": "image/png", "data": buffer.getvalue()}})
    args = {**ARGS, "representation": "inline"}
    result = dispatch_media_read(make_server(store), args)
    meta = json.loads(result["content"][0]["text"])
    assert meta["variant_source"] == "generated" and result["content"][1]["type"] == "image"

    broken = Store({"original": {"mime": "image/png", "data": PNG + b"not raster"}})
    assert code(dispatch_media_read(make_server(broken), args)) == "MEDIA_UNAVAILABLE"


def test_oversized_image_uses_preview_and_never_truncates_base64():
    pytest.importorskip("PIL")
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), (200, 30, 30)).save(buffer, format="PNG")
    original = buffer.getvalue()
    store = Store({"original": {"mime": "image/png", "data": original}})
    server = make_server(store)

    # 预算连 preview 都放不下 → 稳定 MEDIA_TOO_LARGE，绝不截断 base64。
    tiny = dispatch_media_read(server, {**ARGS, "representation": "inline", "budget_bytes": 64})
    assert code(tiny) == "MEDIA_TOO_LARGE"
    assert "data" not in tiny["content"][0]

    # 正常预算 → 返回内存生成的 preview，而不是过大原图。
    ok = dispatch_media_read(server, {**ARGS, "representation": "inline"})
    assert "isError" not in ok
    meta = json.loads(ok["content"][0]["text"])
    assert meta["variant_source"] == "generated" and meta["mime_type"] == "image/jpeg"
    assert base64.b64decode(ok["content"][1]["data"]) != original


def test_audio_inline_requires_original_segment():
    occurrence = {"occurrence_id": "o", "kind": "audio", "page": None, "caption": "clip",
                  "ocr": "", "t_start_ms": 0, "t_end_ms": 10}
    store = Store({"original": {"mime": "audio/wav", "data": pcm_wav()}}, occurrence)
    args = {**ARGS, "representation": "inline", "variant": "original"}
    result = dispatch_media_read(make_server(store), args)
    assert result["content"][1]["type"] == "audio"
    # preview 变体对非光栅音频没有可用生成 → 明确失败，提示改用 original。
    assert code(dispatch_media_read(make_server(store), {**ARGS, "representation": "inline"})) == "MEDIA_UNAVAILABLE"


def test_invalid_representation_and_budget_are_rejected_before_lookup():
    store = Store({"original": {"mime": "image/png", "data": PNG}})
    server = make_server(store)
    assert code(dispatch_media_read(server, {**ARGS, "representation": "raw"})) == "INVALID_ARGUMENT"
    assert code(dispatch_media_read(server, {**ARGS, "variant": "thumb"})) == "INVALID_ARGUMENT"
    assert code(dispatch_media_read(server, {**ARGS, "budget_bytes": 0})) == "INVALID_ARGUMENT"
    assert code(dispatch_media_read(server, {**ARGS, "budget_bytes": 8 * 1024 * 1024 + 1})) == "INVALID_ARGUMENT"
    assert store.reads == [] and store.listed == 0
