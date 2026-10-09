"""Evidence-gated native media adapter framework, with no built-in endpoint."""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .embedding_capabilities import ResolvedEmbeddingProfile, validate_profile, validate_vector
from .providers import ProviderError




@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    request_id: str
    modality: str
    data: bytes
    mime_type: str
    media_hash: str


@dataclass(frozen=True, slots=True)
class NativeMediaEvidence:
    model_reference: str
    endpoint_fixture_reference: str
    alignment_reference: str
    license_reference: str
    profile_fingerprint: str
    alignment_space_id: str
    max_input_bytes: int
    max_batch_size: int
    allowed_mime_types: tuple[str, ...]
    #: 可选：显式声明媒体 preprocess / endpoint 版本。提供时必须与 profile 一致，
    #: 缺省不校验（低层构造兼容）；工厂装配路径要求二者非空（见 providers.create_media_provider）。
    preprocess_version: str = ""
    endpoint_revision: str = ""


class MediaTransport(Protocol):
    def __call__(self, inputs: Sequence[EmbeddingInput]) -> Sequence[dict[str, Any]]: ...


class HttpMediaTransport:
    """真实 HTTP 媒体 embeddings transport（E15）。

    遵循 OpenAI-compatible / VL 多模态 Embeddings 协议（如 SiliconFlow / Jina CLIP）。
    - 仅支持图像模态（image/png, image/jpeg, image/webp）；音频模态无依据直接拒绝；
    - 请求体使用 base64 data URI 数组；
    - 响应提取 data[].index / data[].embedding；
    - 网络或 HTTP 错误抛出异常，由上层 NativeMediaProvider.embed_media 捕获并落 journal mark_unknown。
    """

    def __init__(
        self,
        endpoint: str,
        model: str = "",
        api_key: str = "",
        timeout: float = 30.0,
        *,
        dimension: int | None = None,
        send_dimensions: bool = False,
        allowed_mime_types: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp"),
        max_input_bytes: int = 10 * 1024 * 1024,
        max_batch_size: int = 16,
    ) -> None:
        if not endpoint:
            raise ValueError("media transport endpoint is required")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.dimension = dimension
        self.send_dimensions = send_dimensions
        self.allowed_mime_types = tuple(allowed_mime_types)
        self.max_input_bytes = max_input_bytes
        self.max_batch_size = max_batch_size

    def __call__(self, inputs: Sequence[EmbeddingInput]) -> Sequence[dict[str, Any]]:
        items = list(inputs)
        if not items:
            return []
        if len(items) > self.max_batch_size:
            raise ProviderError(f"media batch size {len(items)} exceeds limit {self.max_batch_size}")
        input_payloads: list[dict[str, Any]] = []
        for item in items:
            if item.modality != "image":
                raise ProviderError(f"media modality {item.modality!r} unsupported by VL embedding transport (only 'image' supported)")
            if item.mime_type not in self.allowed_mime_types:
                raise ProviderError(f"media mime type {item.mime_type!r} not in allowed types {self.allowed_mime_types}")
            if len(item.data) > self.max_input_bytes:
                raise ProviderError(f"media data size {len(item.data)} exceeds limit {self.max_input_bytes}")
            b64_data = base64.b64encode(item.data).decode("ascii")
            data_uri = f"data:{item.mime_type};base64,{b64_data}"
            input_payloads.append({"image": data_uri})

        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_payloads,
            "encoding_format": "float",
        }
        if self.dimension is not None and self.send_dimensions:
            payload["dimensions"] = self.dimension

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw_body = resp.read()
        except HTTPError as exc:
            err_text = ""
            try:
                err_text = exc.read(1024).decode("utf-8", errors="replace")
            except Exception:
                pass
            msg = f"HTTP Error {exc.code}: {exc.reason}"
            if err_text:
                try:
                    err_json = json.loads(err_text)
                    if isinstance(err_json, dict) and "error" in err_json:
                        e = err_json["error"]
                        detail = e.get("message") if isinstance(e, dict) else str(e)
                        msg += f" - {detail}"
                    else:
                        msg += f" - {err_text[:200]}"
                except Exception:
                    msg += f" - {err_text[:200]}"
            raise ProviderError(f"media embedding HTTP failure: {msg}") from exc
        except (URLError, OSError) as exc:
            raise ProviderError(f"media embedding network error: {exc}") from exc

        try:
            result = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"invalid JSON response from media embedding endpoint: {exc}") from exc

        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            err_msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ProviderError(f"media embedding API error: {err_msg}")

        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise ProviderError("media embedding response must contain a data list")
        return data


class GeminiMediaTransport:
    """真实 Google Gemini 多模态与音频媒体 embeddings transport。

    遵循 Google Generative Language API / Gemini Embeddings 协议（如 gemini-embedding-2）。
    - 原生支持图像模态（image/png, image/jpeg, image/webp）与音频模态（audio/mp3, audio/mpeg, audio/wav）；
    - 请求体使用 content.parts[].inline_data { mime_type, data (base64) } 结构；
    - 优先支持 batchEmbedContents 端点（多条批量），单条兼容 embedContent 端点；
    - 响应提取 embeddings[].values 并映射为符合 map_media_response 契约的 index 结构；
    - 认证支持 x-goog-api-key 请求头或 Bearer Authorization；
    - 网络或 HTTP 错误抛出 ProviderError，由上层 NativeMediaProvider 捕获并落 journal mark_unknown。
    """

    def __init__(
        self,
        endpoint: str,
        model: str = "gemini-embedding-2",
        api_key: str = "",
        timeout: float = 30.0,
        *,
        dimension: int | None = None,
        send_dimensions: bool = False,
        allowed_mime_types: tuple[str, ...] = (
            "image/png",
            "image/jpeg",
            "image/webp",
            "audio/mp3",
            "audio/mpeg",
            "audio/wav",
        ),
        max_input_bytes: int = 10 * 1024 * 1024,
        max_batch_size: int = 16,
    ) -> None:
        if not endpoint:
            raise ValueError("gemini media transport endpoint is required")
        self.endpoint = endpoint
        self.model = model or "gemini-embedding-2"
        self.api_key = api_key
        self.timeout = timeout
        self.dimension = dimension
        self.send_dimensions = send_dimensions
        self.allowed_mime_types = tuple(allowed_mime_types)
        self.max_input_bytes = max_input_bytes
        self.max_batch_size = max_batch_size

    def __call__(self, inputs: Sequence[EmbeddingInput]) -> Sequence[dict[str, Any]]:
        items = list(inputs)
        if not items:
            return []
        if len(items) > self.max_batch_size:
            raise ProviderError(f"media batch size {len(items)} exceeds limit {self.max_batch_size}")

        if self.endpoint.endswith(":embedContent") and len(items) > 1:
            raise ProviderError(
                f"Gemini single :embedContent endpoint does not accept batch requests (got {len(items)} items); "
                "use :batchEmbedContents endpoint for batched requests"
            )

        is_single_embed = self.endpoint.endswith(":embedContent") and len(items) == 1

        requests_payload: list[dict[str, Any]] = []
        for item in items:
            if item.modality not in ("image", "audio"):
                raise ProviderError(
                    f"media modality {item.modality!r} unsupported by Gemini media embedding transport "
                    "(only 'image' and 'audio' supported)"
                )
            if item.mime_type not in self.allowed_mime_types:
                raise ProviderError(f"media mime type {item.mime_type!r} not in allowed types {self.allowed_mime_types}")
            if len(item.data) > self.max_input_bytes:
                raise ProviderError(f"media data size {len(item.data)} exceeds limit {self.max_input_bytes}")
            b64_data = base64.b64encode(item.data).decode("ascii")
            part_content = {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": item.mime_type,
                            "data": b64_data,
                        }
                    }
                ]
            }
            req_item: dict[str, Any] = {
                "content": part_content,
            }
            model_res = self.model if self.model.startswith("models/") else f"models/{self.model}"
            req_item["model"] = model_res
            if self.dimension is not None and self.send_dimensions:
                req_item["output_dimensionality"] = self.dimension
            requests_payload.append(req_item)

        if is_single_embed:
            payload: dict[str, Any] = {
                "model": requests_payload[0]["model"],
                "content": requests_payload[0]["content"],
            }
            if self.dimension is not None and self.send_dimensions:
                payload["output_dimensionality"] = self.dimension
        else:
            payload = {
                "requests": requests_payload,
            }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            if self.api_key.startswith("Bearer "):
                headers["Authorization"] = self.api_key
            else:
                headers["x-goog-api-key"] = self.api_key

        req = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw_body = resp.read()
        except HTTPError as exc:
            err_text = ""
            try:
                err_text = exc.read(1024).decode("utf-8", errors="replace")
            except Exception:
                pass
            msg = f"HTTP Error {exc.code}: {exc.reason}"
            if err_text:
                try:
                    err_json = json.loads(err_text)
                    if isinstance(err_json, dict) and "error" in err_json:
                        e = err_json["error"]
                        detail = e.get("message") if isinstance(e, dict) else str(e)
                        msg += f" - {detail}"
                    else:
                        msg += f" - {err_text[:200]}"
                except Exception:
                    msg += f" - {err_text[:200]}"
            raise ProviderError(f"Gemini media embedding HTTP failure: {msg}") from exc
        except (URLError, OSError) as exc:
            raise ProviderError(f"Gemini media embedding network error: {exc}") from exc

        try:
            result = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"invalid JSON response from Gemini media embedding endpoint: {exc}") from exc

        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            err_msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ProviderError(f"Gemini media embedding API error: {err_msg}")

        mapped_records: list[dict[str, Any]] = []
        if isinstance(result, dict) and "embeddings" in result:
            embs = result["embeddings"]
            if not isinstance(embs, list):
                raise ProviderError("Gemini media embedding response 'embeddings' must be a list")
            for idx, item_resp in enumerate(embs):
                if not isinstance(item_resp, dict):
                    raise ProviderError("Gemini media embedding item must be an object")
                values = item_resp.get("values")
                if not isinstance(values, list):
                    raise ProviderError("Gemini media embedding item missing 'values' list")
                mapped_records.append({"index": idx, "embedding": values})
        elif isinstance(result, dict) and "embedding" in result:
            if len(items) != 1:
                raise ProviderError(
                    f"Gemini single embedding response received but request had {len(items)} items"
                )
            emb = result["embedding"]
            values = emb.get("values") if isinstance(emb, dict) else None
            if not isinstance(values, list):
                raise ProviderError("Gemini media embedding single response missing 'values' list")
            mapped_records.append({"index": 0, "embedding": values})
        else:
            raise ProviderError("Gemini media embedding response must contain 'embedding' or 'embeddings'")

        return mapped_records



def map_media_response(response: Sequence[Any], ids: Sequence[str]) -> dict[str, Any]:
    """把离线/服务响应归一为 `{内部 request_id: embedding}`。

    只接受两种**精确**定位：显式 `request_id`（必须命中输入集合）或 `index`
    （0..N-1，按输入顺序定位）。不要求服务回显 request_id；但数量必须完整、无
    重复、无缺项、无越界——任何缺口都抛错，绝不静默补位。归一接口设计不等于
    批准实际 HTTP 请求体。
    """
    ordered = list(ids)
    mapped: dict[str, Any] = {}
    for item in response:
        if not isinstance(item, Mapping):
            raise ProviderError("native media response items must be mappings")
        key: str | None = None
        if "request_id" in item:
            candidate = item.get("request_id")
            if not isinstance(candidate, str) or candidate not in ordered:
                raise ProviderError("native media response request_id does not match an input")
            key = candidate
        elif "index" in item:
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(ordered):
                raise ProviderError("native media response index is out of range")
            key = ordered[index]
        else:
            raise ProviderError("native media response must map by request_id or index")
        if key in mapped:
            raise ProviderError("duplicate native media response entry")
        mapped[key] = item.get("embedding")
    missing = [item for item in ordered if item not in mapped]
    if missing:
        raise ProviderError("native media response is missing entries")
    return mapped


class NativeMediaProvider:
    """Transport must use the same durable paid-request gate as text POSTs."""

    def __init__(self, profile: ResolvedEmbeddingProfile, evidence: NativeMediaEvidence, transport: MediaTransport) -> None:
        validate_profile(profile)
        refs = (evidence.model_reference, evidence.endpoint_fixture_reference,
                evidence.alignment_reference, evidence.license_reference)
        if not all(refs) or evidence.profile_fingerprint != profile.fingerprint:
            raise ProviderError("NATIVE_MEDIA_EVIDENCE_REQUIRED: exact endpoint evidence missing")
        if not profile.alignment_space_id or evidence.alignment_space_id != profile.alignment_space_id:
            raise ProviderError("NATIVE_MEDIA_ALIGNMENT_UNVERIFIED")
        if not any(modality in profile.modalities for modality in ("image", "audio")):
            raise ProviderError("profile has no verified native media modality")
        limits = (evidence.max_input_bytes, evidence.max_batch_size)
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in limits):
            raise ProviderError("native media limits must be verified positive integers")
        if not evidence.allowed_mime_types:
            raise ProviderError("native media MIME contract required")
        if evidence.preprocess_version and evidence.preprocess_version != profile.preprocess_version:
            raise ProviderError("native media preprocess version mismatch")
        if evidence.endpoint_revision and evidence.endpoint_revision != profile.endpoint_revision:
            raise ProviderError("native media endpoint revision mismatch")
        self.profile = profile
        self.evidence = evidence
        self._transport = transport
        self.request_journal: Any = None
        self.paid_guard: Any = None

    def configure_paid_requests(self, journal: Any, guard: Any, profile_fingerprint: str) -> None:
        if profile_fingerprint != self.profile.fingerprint:
            raise ProviderError("native media authorization profile mismatch")
        self.request_journal = journal
        self.paid_guard = guard

    def ability(self) -> dict[str, Any]:
        """只报告**声明**事实（不是已测能力，也不代表真实端点可用），供 E08 消费。"""
        return {
            "profile_fingerprint": self.profile.fingerprint,
            "modalities": tuple(self.profile.modalities),
            "alignment_space_id": self.profile.alignment_space_id,
            "preprocess_version": self.profile.preprocess_version,
            "endpoint_revision": self.profile.endpoint_revision,
            "allowed_mime_types": tuple(self.evidence.allowed_mime_types),
            "max_input_bytes": self.evidence.max_input_bytes,
            "max_batch_size": self.evidence.max_batch_size,
        }

    def embed_media(self, inputs: Sequence[EmbeddingInput]) -> list[list[float]]:
        items = list(inputs)
        if not items:
            return []
        if len(items) > self.evidence.max_batch_size:
            raise ProviderError("native media batch exceeds verified limit")
        ids = [item.request_id for item in items]
        if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
            raise ProviderError("native media request ids must be unique")
        for item in items:
            if item.modality not in self.profile.modalities or item.modality == "text":
                raise ProviderError("unsupported native media modality")
            if item.mime_type not in self.evidence.allowed_mime_types:
                raise ProviderError("unsupported native media MIME type")
            if not isinstance(item.data, bytes) or not 0 < len(item.data) <= self.evidence.max_input_bytes:
                raise ProviderError("native media input exceeds verified byte limit")
            if hashlib.sha256(item.data).hexdigest() != item.media_hash:
                raise ProviderError("native media input hash mismatch")
        if self.request_journal is None or self.paid_guard is None or not self.paid_guard(self.profile.fingerprint):
            raise ProviderError("EMBEDDING_PENDING_APPROVAL: native media request blocked")
        material = repr([(item.request_id, item.modality, item.mime_type, item.media_hash) for item in items])
        payload_hash = hashlib.sha256(material.encode()).hexdigest()
        request_id = self.request_journal.before_send(payload_hash, self.profile.endpoint, self.profile.fingerprint)
        try:
            response = list(self._transport(items))
            self.request_journal.mark_success(request_id)
        except Exception as exc:
            self.request_journal.mark_unknown(request_id, "response_unconfirmed")
            raise ProviderError("SUBMISSION_UNKNOWN: native media outcome unknown") from exc
        # 请求已响应（mark_success 已记账，不重发）；此处只做**产物合同**校验：
        # 数量/定位/向量不合规即抛错，绝不把契约失败的响应当可索引产物返回。
        if len(response) != len(items):
            raise ProviderError("native media response count mismatch")
        mapped = map_media_response(response, ids)
        return [validate_vector(mapped[item_id], self.profile) for item_id in ids]
