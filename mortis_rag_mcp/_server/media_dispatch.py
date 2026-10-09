"""Explicit, bounded MCP media reads; no host-capability assumptions."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from typing import Any

DEFAULT_BUDGET = 2 * 1024 * 1024
MAX_BUDGET = 8 * 1024 * 1024
INLINE_IMAGES = {"image/png", "image/jpeg", "image/webp"}
INLINE_AUDIO = {"audio/wav", "audio/x-wav"}
INLINE_MIME = INLINE_IMAGES | INLINE_AUDIO
# 生成 preview 需要读回原图字节，这与「inline JSON 包络预算」是两回事：用 store 默认
# 上限（20MiB）约束单张光栅的内存占用，避免被过小 budget 误挡掉 preview 生成。
ORIGINAL_READ_CAP = 20 * 1024 * 1024


def _json_size(value: Any) -> int:
    # E08-f：与实际 stdio 写出**完全一致**的序列化参数（ensure_ascii=False + 紧凑分隔符），
    # 否则预算与线上包络对不上：用默认分隔符会多算空格，把本该放得下的媒体误判为超限。
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(
        {"code": code, "message": message, **details}, ensure_ascii=False)}], "isError": True}


def _detail(exc: BaseException) -> str:
    return str(getattr(exc, "detail", "") or (exc.args[0] if exc.args else "") or "")


def _media_error(exc: BaseException) -> dict[str, Any]:
    """稳定 fail-closed 映射；绝不把未知异常吞成伪造成功。"""
    code = str(getattr(exc, "code", "") or "")
    detail = _detail(exc)
    if code == "STORE_QUOTA_EXCEEDED" or "MEDIA_TOO_LARGE" in detail:
        return _error("MEDIA_TOO_LARGE", "Media blob exceeds the read budget; use metadata or a smaller variant")
    if "MEDIA_NOT_FOUND" in detail:
        return _error("MEDIA_UNAVAILABLE", "Media occurrence is unavailable for this revision")
    if code in {"STALE", "SOURCE_CHANGED", "STORE_CONFLICT"}:
        return _error("MEDIA_UNAVAILABLE", "Media address is stale; re-read the source for a current revision")
    return _error("MEDIA_UNAVAILABLE", "Media address is unavailable or invalid")


def _magic_matches(mime: str, data: bytes) -> bool:
    if mime == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if mime == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if mime == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime in INLINE_AUDIO:
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    return False


def _preview(data: bytes, config: Any) -> tuple[bytes, str] | None:
    if not getattr(config, "preview_enabled", True):
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000:
                return None
            image.thumbnail((getattr(config, "preview_max_edge", 1600),) * 2)
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=80)
            value = output.getvalue()
            if len(value) > getattr(config, "preview_max_bytes", 524288):
                return None
            return value, "image/jpeg"
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def media_content_result(metadata: dict[str, Any], data: bytes | None = None, *,
                         budget_bytes: int = DEFAULT_BUDGET,
                         request_id: Any = None) -> dict[str, Any]:
    """Return standard text+image/audio blocks, measured as a whole JSON-RPC result."""
    text = {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)}
    result: dict[str, Any] = {"content": [text]}
    if data is not None:
        mime = metadata.get("mime_type", "")
        if mime in INLINE_IMAGES:
            kind = "image"
        elif (mime in INLINE_AUDIO
              and metadata.get("kind") == "audio"
              and metadata.get("t_start_ms") is not None
              and metadata.get("t_end_ms") is not None):
            import wave
            try:
                with wave.open(io.BytesIO(data), "rb") as audio:
                    if audio.getcomptype() != "NONE" or audio.getnframes() <= 0:
                        return _error("MEDIA_UNAVAILABLE", "Audio is not a verified PCM segment")
            except (wave.Error, EOFError):
                return _error("MEDIA_UNAVAILABLE", "Audio is not a verified PCM segment")
            kind = "audio"
        else:
            return _error("MEDIA_UNAVAILABLE", "This MIME is not safe for inline media")
        result["content"].append({"type": kind, "data": base64.b64encode(data).decode("ascii"), "mimeType": mime})
    required = _json_size({"jsonrpc": "2.0", "id": request_id, "result": result})
    if required > budget_bytes:
        # 只做**整包拒绝**：绝不截断 base64，也绝不只裁掉半条 content item。
        return _error("MEDIA_TOO_LARGE", "Complete media envelope exceeds budget; use metadata or preview",
                      required_bytes=required, budget_bytes=budget_bytes,
                      next_action="请求 representation=metadata，或改用 variant=preview，或显式提高 budget_bytes",
                      available_variants=metadata.get("available_variants", []))
    return result


def _occurrence_detail(store: Any, source: str, revision_id: str, occurrence_id: str) -> dict[str, Any]:
    """metadata-only 列举该 revision 的 occurrence，定位并返回其 page/caption/OCR 等事实。"""
    offset, page_size = 0, 200
    while True:
        page = store.list_media(source, revision_id=revision_id, offset=offset, limit=page_size)
        for item in page:
            if item.get("occurrence_id") == occurrence_id:
                return dict(item)
        if len(page) < page_size:
            return {}
        offset += len(page)


def _link_context(store: Any, revision_id: str, occurrence_id: str) -> dict[str, Any] | None:
    """从 media_chunk_links 双向取 context（E08-e）：同一 occurrence 关联的 chunk 地址。

    store 不支持（旧库/替身）时返回 None，绝不让读取因附加上下文失败。
    """
    lister = getattr(store, "list_media_chunk_links", None)
    if not callable(lister):
        return None
    try:
        rows = lister(revision_id=revision_id, occurrence_id=occurrence_id)
    except Exception:
        return None
    if not rows:
        return None
    return {
        "chunk_ids": sorted({str(row["chunk_id"]) for row in rows if row.get("chunk_id")}),
        "relations": sorted({str(row.get("relation") or "") for row in rows if row.get("relation")}),
    }


def _stored_or_generated_preview(store: Any, source: str, revision_id: str, occurrence_id: str,
                                 maximum: int, config: Any, mime: str) -> tuple[bytes, str, str] | None:
    """先取已持久化 preview 变体；缺失时用原图在内存生成；两者都不可用返回 None。"""
    try:
        payload = store.read_media(source, revision_id=revision_id, occurrence_id=occurrence_id,
                                   variant_id="preview", max_bytes=maximum, include_data=True)
    except Exception as exc:
        if "MEDIA_NOT_FOUND" not in _detail(exc):
            raise
        payload = None
    if payload is not None:
        return payload.get("data") or b"", str(payload.get("mime_type") or mime), "stored"
    original = store.read_media(source, revision_id=revision_id, occurrence_id=occurrence_id,
                                variant_id="original", max_bytes=max(maximum, ORIGINAL_READ_CAP),
                                include_data=True)
    generated = _preview(original.get("data") or b"", config)
    if generated is None:
        return None
    # 持久化派生变体（§20.7D）：下次读取直接命中 stored 分支，不必重新解码原图。
    # 只读路径不得因持久化失败而失败（quota/旧库无 variants 表/只读打开）——降级返回本次生成结果。
    try:
        store.put_media_variant(
            revision_id=revision_id, occurrence_id=occurrence_id, data=generated[0],
            mime_type=generated[1], kind="preview", variant_id="preview",
            parent_blob_id=str(original.get("blob_id") or "") or None,
            preprocess_version="preview-jpeg-v1",
        )
    except Exception:
        pass
    return generated[0], generated[1], "generated"


def _capture_read_pin(store: Any) -> Any:
    """捕获 E04 固定版本 handle；store 不支持时返回 None（不阻断旧/替身 store）。"""
    capture = getattr(store, "capture_generation_pin", None)
    if not callable(capture):
        return None
    return capture(lease_seconds=120)


def _pin_still_live(store: Any, pin: Any) -> bool:
    if pin is None:
        return True
    validate = getattr(store, "validate_generation_pin", None)
    if not callable(validate):
        return True
    try:
        return bool(validate(pin))
    except Exception:
        return False


def _release_read_pin(store: Any, pin: Any) -> None:
    if pin is None:
        return
    release = getattr(store, "release_generation_pin", None)
    if callable(release):
        try:
            release(pin)
        except Exception:
            pass


def dispatch_media_read(server: Any, arguments: dict[str, Any], *, request_id: Any = None) -> dict[str, Any]:
    """核验库归属/当前 revision 后，按显式 representation 返回 metadata 或受限 inline 媒体。

    E08-e：读取期间用 E04 固定版本 handle 钉住 generation；**最终媒体包络前**再验租，
    到期/换代一律拒绝，绝不把旧 chunk 与新 media 拼在同一个响应里。
    """
    for key in ("vault_path", "source", "revision_id", "occurrence_id"):
        if not isinstance(arguments.get(key), str) or not arguments[key].strip():
            return _error("INVALID_ARGUMENT", f"{key} is required")
    representation = arguments.get("representation", "metadata")
    variant = arguments.get("variant", "preview")
    if representation not in {"metadata", "inline"} or variant not in {"preview", "original"}:
        return _error("INVALID_ARGUMENT", "Invalid representation or variant")
    budget = arguments.get("budget_bytes", DEFAULT_BUDGET)
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= MAX_BUDGET:
        return _error("INVALID_ARGUMENT", "budget_bytes must be an integer from 1 to 8388608")

    source = arguments["source"]
    revision_id = arguments["revision_id"]
    occurrence_id = arguments["occurrence_id"]
    pin = None
    store = None
    try:
        vault = server._resolve_vault_path(arguments["vault_path"])
        indexer = server._indexer_for({"vault_path": vault})
        resolver = getattr(indexer, "resolve_virtual_source", None)
        if resolver is None:
            return _error("MEDIA_UNAVAILABLE", "Secure virtual-source resolver is unavailable")
        resolved = resolver(source, revision_id=revision_id)
        if not isinstance(resolved, dict) or resolved.get("revision_id") != revision_id:
            return _error("MEDIA_UNAVAILABLE", "Media revision is stale or unavailable")
        store = indexer.document_store()
        # 固定版本：捕获失败（跨代冲突）即拒绝，不读取半新半旧的媒体。
        pin = _capture_read_pin(store)
        blob = store.read_media(source, revision_id=revision_id, occurrence_id=occurrence_id,
                                variant_id="original", include_data=False)
        detail = _occurrence_detail(store, source, revision_id, occurrence_id)
    except Exception as exc:
        _release_read_pin(store, pin)
        return _media_error(exc)

    mime = str(blob.get("mime_type", ""))
    metadata = {
        "vault_path": vault, "source": source, "revision_id": revision_id,
        "occurrence_id": occurrence_id, "blob_id": blob.get("blob_id"),
        "mime_type": mime, "byte_size": blob.get("byte_size"),
        "width": blob.get("width"), "height": blob.get("height"),
        "duration_ms": blob.get("duration_ms"),
        "kind": detail.get("kind") or ("audio" if mime in INLINE_AUDIO else "image"),
        "page": detail.get("page"), "caption": detail.get("caption", ""),
        "ocr": detail.get("ocr", ""), "t_start_ms": detail.get("t_start_ms"),
        "t_end_ms": detail.get("t_end_ms"), "host_media_verified": False,
    }
    # 从 links 双向取 context：保留 vault/source/revision/occurrence 归属，只回 chunk 地址事实。
    context = _link_context(store, revision_id, occurrence_id)
    if context:
        metadata["context_chunk_ids"] = context["chunk_ids"]
        metadata["context_relations"] = context["relations"]
    if representation == "metadata":
        if not _pin_still_live(store, pin):
            _release_read_pin(store, pin)
            return _error("MEDIA_UNAVAILABLE", "Media revision pin expired before the final envelope")
        result = media_content_result(metadata, budget_bytes=budget, request_id=request_id)
        _release_read_pin(store, pin)
        return result

    config = server.config.media
    maximum = min(getattr(config, "inline_max_bytes", MAX_BUDGET), MAX_BUDGET)
    try:
        if variant == "preview":
            preview = _stored_or_generated_preview(store, source, revision_id, occurrence_id,
                                                   maximum, config, mime)
            if preview is None:
                return _error("MEDIA_UNAVAILABLE", "Preview unavailable; request metadata or explicit original")
            data, mime, variant_source = preview
        else:
            payload = store.read_media(source, revision_id=revision_id, occurrence_id=occurrence_id,
                                       variant_id="original", max_bytes=maximum, include_data=True)
            data = payload.get("data")
            mime = str(payload.get("mime_type") or mime)
            variant_source = "original"
        # 最终复核：字节必须与声明 MIME 的魔术头一致，且只允许光栅图/已验证音频 inline。
        if mime not in INLINE_MIME:
            return _error("MEDIA_UNAVAILABLE", "This MIME is not safe for inline media")
        if not data or not _magic_matches(mime, data):
            return _error("MEDIA_UNAVAILABLE", "Media bytes do not match the declared MIME type")
        # 最终复核：该源当前 active revision 必须仍是本次 revision。
        active = store.get_active(source)
        if active is None or active.revision.revision_id != revision_id:
            return _error("MEDIA_UNAVAILABLE", "Media revision is stale")
    except Exception as exc:
        _release_read_pin(store, pin)
        return _media_error(exc)

    metadata.update({"variant": variant, "variant_source": variant_source, "mime_type": mime,
                     "byte_size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    # 包络最终组装前最后一次验租（E08-e）：到期/换代拒绝，不拼旧 chunk 与新 media。
    if not _pin_still_live(store, pin):
        _release_read_pin(store, pin)
        return _error("MEDIA_UNAVAILABLE", "Media revision pin expired before the final envelope")
    result = media_content_result(metadata, data, budget_bytes=budget, request_id=request_id)
    _release_read_pin(store, pin)
    return result
