"""增量索引同步引擎 / 向量补嵌 / 磁盘向量迁移（v0.8.0 自 indexer.py 逐字提取）。

Eng 协议边界（不可破坏）：
- 引擎主入口为自由函数 ``run_sync(owner)``，前置条件：调用方必须已持有 ``owner._sync_lock``；
- 22 个内部可变状态属性留守在主类实例上，引擎经 ``owner.*`` 就地变异；
- 严禁运行时反向导入 ``mortis_rag_mcp.indexer`` Facade（类型标注使用 TYPE_CHECKING）；
- Facade 留守项（绝不移入本模块）：锁获取点、全部 ``_cache_lock`` 获取点、
  Fast-Stat 判据族、``_fts_*`` 家族、切块薄委托。
"""
from __future__ import annotations

from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import time
from typing import Any, Iterable, TYPE_CHECKING

from ..providers import EmbeddingProvider, ProviderError
from ..embedding_capabilities import embed_with_profile
from .models import Chunk, _EMB_DTYPE
from .scanning import _MTIME_TICK_PROBE_MAX_SAMPLES, scan_indexable_files, source_compare_key
from .chunking import _INDEXABLE_TEXT_EXTS

if TYPE_CHECKING:
    from mortis_rag_mcp.indexer import MarkdownIndexer


def _to_emb(vectors: Iterable[float]) -> array:
    return array(_EMB_DTYPE, vectors)


def chunk_has_vector(owner: MarkdownIndexer, chunk: Chunk) -> bool:
    """这个 chunk 已经有可用向量了吗？

    磁盘后端看是否落盘；刚复用/刚算出来、还留在 RAM 里等 flush 的也算有
    （flush 会按它自己的 chunk.id 落盘，所以复用的向量最终会以副本形式
    各存一份——这是刻意的，磁盘省的是 RAM 而不是磁盘）。
    """
    if owner._vectors_on_disk:
        return chunk.id in owner._disk_vectors or chunk.embedding is not None
    return chunk.embedding is not None and len(chunk.embedding) > 0


def ensure_disk_vectors_migrated(owner: MarkdownIndexer) -> None:
    """Reconcile the disk vector store with in-memory chunks.

    Rebuilds the RAM bookkeeping set from the disk store, and performs a
    one-time migration from the legacy .vec.bin when the disk store is
    empty (so switching backends never triggers a full re-embed).
    """
    if not owner._vectors_on_disk or owner._disk_vectors:
        return
    try:
        stored = set(owner._vector_backend.list_ids())
        owner._disk_vectors = stored
        if not stored and owner._chunks:
            # Migrate the legacy .bin into the disk store once.
            if owner._vectors_cache_path is not None and owner._vectors_cache_path.exists():
                owner._load_vectors_cache()
            owner._flush_vectors_to_disk()
    except Exception:
        pass


def flush_vectors_to_disk(owner: MarkdownIndexer) -> None:
    """Upsert all in-RAM embeddings into the disk store, then drop them
    from Chunk to release resident memory. On failure keep them in RAM."""
    if not owner._vectors_on_disk:
        return
    vectors = {
        chunk.id: chunk.embedding
        for chunks in owner._chunks.values()
        for chunk in chunks
        if chunk.embedding is not None and len(chunk.embedding)
    }
    if not vectors:
        return
    try:
        persisted = owner._vector_backend.upsert_vectors(vectors)
    except Exception:
        # 落盘失败就保留在 RAM 里，让下一轮还能重试（不能假装已落盘）。
        return
    # 后端返回 None = 未实现成功计数，沿用旧的乐观语义；否则只认真正
    # 落盘的 id。此前无条件 update(vectors)：upsert 内部吞掉异常后照样
    # 记账，导致 chunk 被认为"已有向量"而永不重嵌。
    stored = set(vectors) if persisted is None else {str(item) for item in persisted}
    if not stored:
        return
    owner._disk_vectors.update(stored)
    for chunk in owner.all_chunks():
        if chunk.id in stored:
            chunk.embedding = None


def _paid_embedding_allowed(owner: MarkdownIndexer) -> bool:
    """索引批量路径的付费闸门查询（§20.7B / §20.7F）。

    比 `may_use_paid_profile` 多一条**未决意图暂停**：外部嵌入按 `batch_size` 切片，
    某批结果未知时同一文件的**前导批**已经发过并计费，而调用方「全有或全无」——
    只按载荷拦失败批，会让每轮 sync 都重发前导批却永远完不成（实测每次重计费 1 批）。
    所以这里按 profile 整体暂停，宁可 0 请求也不重复计费。
    该暂停**不**施加到查询期嵌入（单次、载荷唯一、不重放）。
    Facade 未提供该方法时按「不可用」处理（fail closed）。
    """
    may = getattr(owner, "may_use_paid_profile", None)
    if not callable(may):
        return False
    try:
        fingerprint = owner._embedding_profile.fingerprint
        if not may(fingerprint, kind="embed"):
            return False
        control = owner._paid_control_store()
        if control is None:
            return False
        checker = getattr(owner, "has_unresolved_paid_intents", None)
        if callable(checker) and checker(control, "embed", fingerprint):
            return False
        return True
    except Exception:
        return False


def _reuse_key(chunk: Chunk) -> str:
    """向量复用键：优先 `embedding_key`（含 space/profile/模板/媒体哈希），
    旧 chunk 没有该键时退回短 content_hash（保持既有库可复用）。"""
    key = chunk.metadata.get("embedding_key")
    if isinstance(key, str) and key:
        return key
    digest = chunk.metadata.get("content_hash")
    return digest if isinstance(digest, str) else ""


def _document_input(owner: MarkdownIndexer, content: str) -> str:
    """文档嵌入的**实际输入**：应用 profile 的 `document_template`（§17.4）。

    与 embed_with_profile 的渲染一致；非法模板在配置/profile解析时拒绝。
    """
    profile = getattr(owner, "_embedding_profile", None)
    return profile.document_template.format(text=content)


class _TemplatingProvider:
    """在 provider 边界应用 `document_template`，其余行为（批量/合约校验）原样透传。"""

    def __init__(self, provider: Any, profile: Any) -> None:
        self._provider = provider
        self._profile = profile

    def embed(self, texts):
        return embed_with_profile(self._provider, texts, self._profile)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


def _document_provider(owner: MarkdownIndexer) -> Any:
    provider = owner.embedding_provider
    profile = getattr(owner, "_embedding_profile", None)
    return _TemplatingProvider(provider, profile)


def _stamp_embedding_keys(owner: MarkdownIndexer) -> None:
    """给每个可嵌入 chunk 盖上与捕获 space/profile 绑定的 embedding_key。

    key 只描述「同一输入 + 同一 profile」，不参与版本寻址（§20.7B），所以可以
    跨 revision 复用；oversize / embedding_disabled 的 chunk 不留 key。

    E08-c：媒体 proxy 的 key 必须把**媒体 blob 身份**算进去 —— 否则「同 caption
    异图」会得到同一个 key，复用阶段就会把两张不同图的向量互相冒充。
    """
    from .token_chunking import embedding_key as _embedding_key
    profile = getattr(owner, "_embedding_profile", None)
    space = getattr(profile, "fingerprint", "") or ""
    template = getattr(profile, "preprocess_version", "") or "text-v1"
    for chunks in owner._chunks.values():
        for chunk in chunks:
            if chunk.metadata.get("embedding_disabled") or chunk.metadata.get("kind") == "media_native":
                continue
            media_hashes = chunk.metadata.get("media_hashes")
            if not media_hashes:
                blob = chunk.metadata.get("blob_sha256")
                media_hashes = (blob,) if blob else ()
            chunk.metadata["embedding_key"] = _embedding_key(
                _document_input(owner, chunk.content), space, template, media_hashes)


def reuse_vectors_by_content_hash(owner: MarkdownIndexer) -> int:
    """把已有向量按输入身份（embedding_key，退回 content_hash）复用到缺向量的 chunk。

    典型场景：库里有一份整目录的备份（教材/ 与 教材_Raw_Backup/），两处
    正文逐字相同，但 chunk.id 因为 source 不同而不一样——与其把同一段文本
    送进 embedding API 两次，不如直接复用已经算出来的向量。返回复用条数。

    只读使用向量，所以多个 chunk 可以安全共享同一个 array 对象。
    """
    missing: list[Chunk] = []
    donors: dict[str, Chunk] = {}
    for chunks in owner._chunks.values():
        for chunk in chunks:
            if chunk.metadata.get("embedding_disabled"):
                # §17.3：超硬上限 / 禁用嵌入的 chunk 永不请求，也永不当捐赠者。
                continue
            digest = _reuse_key(chunk)
            if not digest:
                continue
            if owner._chunk_has_vector(chunk):
                donors.setdefault(digest, chunk)
            else:
                missing.append(chunk)
    if not missing or not donors:
        return 0

    needed = {_reuse_key(chunk) for chunk in missing}
    reusable: dict[str, Any] = {}
    if owner._vectors_on_disk:
        # 磁盘后端：chunk.embedding 在 flush 后是 None，得从 vec0 表读回来。
        by_id = {chunk.id: digest for digest, chunk in donors.items() if digest in needed}
        for chunk_id, vector in owner._vector_backend.get_vectors(by_id).items():
            digest = by_id.get(chunk_id)
            if digest is not None:
                reusable[digest] = vector
    else:
        for digest, chunk in donors.items():
            if digest in needed and chunk.embedding is not None and len(chunk.embedding):
                reusable[digest] = chunk.embedding
    if not reusable:
        return 0

    reused = 0
    for chunk in missing:
        vector = reusable.get(_reuse_key(chunk))
        if vector is None:
            continue
        chunk.embedding = vector
        reused += 1
    return reused


def embed_one_file(source: str, chunks: list[Chunk], provider: EmbeddingProvider) -> None:
    chunks = [chunk for chunk in chunks if not chunk.metadata.get("embedding_disabled")]
    if not chunks:
        return
    # 同一个文件里也可能出现逐字重复的段落（复制粘贴、模板套话），按内容
    # 哈希去重后只请求一次，回填时同 hash 的 chunk 共用同一个向量。
    # 老 chunk 没有 content_hash 时退回用正文算一个，行为与去重前一致。
    contents: dict[str, str] = {}
    order: list[str] = []
    keys: list[str] = []
    for chunk in chunks:
        digest = chunk.metadata.get("content_hash")
        if not digest:
            digest = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()[:16]
        keys.append(digest)
        if digest not in contents:
            contents[digest] = chunk.content
            order.append(digest)
    vectors = provider.embed([contents[digest] for digest in order])
    # provider 层已校验条数，这里再兜一次：zip() 截断会让部分 chunk 静默
    # 拿不到向量，既不报错也不进 failed_files，之后每轮 sync 重复付费。
    if len(vectors) != len(order):
        raise ProviderError(
            f"embedding returned {len(vectors)} vectors for {len(order)} unique chunks"
        )
    # 按哈希回填而不是按位置，避免 provider 少返回向量时整批错位。
    by_hash = dict(zip(order, vectors))
    for chunk, digest in zip(chunks, keys):
        vector = by_hash.get(digest)
        if vector is not None:
            chunk.embedding = _to_emb(vector)


def embedding_changed_state(
    owner: MarkdownIndexer,
    pending_chunks: list[Chunk],
    failed_before: dict[str, str],
    reused: int = 0,
) -> bool:
    """本轮 embedding 是否真的改变了需要落盘的状态。

    关键：全部失败时必须返回 False。此前三条路径一律 return True，
    于是 _sync_locked 每轮都重写 chunks.bin / vectors / fts / failed.json；
    在 [cache] placement = "vault" + 原生递归监听下，写缓存立刻再次触发
    文件事件 → 防抖后再 sync → 再失败 → 再写缓存，形成自激死循环，
    每轮都把全部 pending chunk 重发一遍（真的会烧钱）。
    """
    if reused > 0 or owner.failed_files != failed_before:
        return True
    return any(owner._chunk_has_vector(chunk) for chunk in pending_chunks)


def embed_missing(owner: MarkdownIndexer) -> bool:
    """Embed every chunk that has no vector yet.

    Returns True when there was embedding work to do (or failures to record),
    so the caller knows whether the cache files need rewriting. When the
    vectors cache was invalidated (model/dimension change) this re-embeds the
    whole corpus while reusing the text chunks; when only a few files changed
    it embeds just those chunks.

    注意：失败文件的重试是天然的——这里的判据是"缺向量"（memory 后端看
    chunk.embedding is None，磁盘后端看 chunk.id not in _disk_vectors），
    而不是看 failed_files 字典，所以这里不需要任何额外重试逻辑；把
    failed_files 持久化到磁盘只是为了跨进程重启的可观测性。
    """
    # 输入身份先盖章，再复用：embedding_key 含捕获的 space/profile/模板/媒体哈希，
    # 比旧的 16 位 content_hash 更严格，也能跨 revision 安全复用（§20.7B）。
    owner._stamp_embedding_keys()
    # 先做一轮输入身份复用：已经算过的内容不再花钱重算一次。
    reused = owner._reuse_vectors_by_content_hash()

    failed_before = dict(owner.failed_files)
    pending: dict[str, list[Chunk]] = {}
    for source, chunks in owner._chunks.items():
        missing = [chunk for chunk in chunks
                   if not chunk.metadata.get("embedding_disabled")
                   and chunk.metadata.get("kind") != "media_native"
                   and not owner._chunk_has_vector(chunk)]
        if missing:
            pending[source] = missing
    if not pending:
        # 复用到已有向量的 chunk 也需要落盘，否则下轮重新花钱重算。
        return reused > 0 or owner.failed_files != failed_before
    pending_chunks = [chunk for chunks in pending.values() for chunk in chunks]
    owner._sync_progress = {
        "phase": "embedding",
        "files_done": 0,
        "files_total": len(pending),
        "chunks_done": 0,
        "chunks_total": len(pending_chunks),
    }

    if owner.config.embedding.mode == "external" and not _paid_embedding_allowed(owner):
        # R2：仅明确撤销、控制不可用、未决批量意图暂停索引，不创建漂移审批。
        owner._embedding_paused = True
        owner._sync_progress["phase"] = "embedding_paused"
        return False
    owner._embedding_paused = False

    if owner.config.embedding.mode != "external":
        provider = _document_provider(owner)
        for source, chunks in pending.items():
            try:
                vectors = provider.embed([chunk.content for chunk in chunks])
                for chunk, vector in zip(chunks, vectors):
                    chunk.embedding = _to_emb(vector)
            except Exception as exc:
                owner.failed_files[source] = str(exc)
            else:
                # 补向量成功即撤销旧失败记录，否则持久化文件会永久撒谎。
                owner.failed_files.pop(source, None)
            finally:
                owner._sync_progress["files_done"] += 1
                owner._sync_progress["chunks_done"] += len(chunks)
        return owner._embedding_changed_state(pending_chunks, failed_before, reused)

    max_workers = owner.config.cache.embedding_max_workers
    tasks = list(pending.items())
    document_provider = _document_provider(owner)
    if max_workers <= 1 or len(tasks) <= 1:
        for source, chunks in tasks:
            try:
                owner._embed_one_file(source, chunks, document_provider)
            except Exception as exc:
                owner.failed_files[source] = str(exc)
            else:
                owner.failed_files.pop(source, None)
            finally:
                owner._sync_progress["files_done"] += 1
                owner._sync_progress["chunks_done"] += len(chunks)
        return owner._embedding_changed_state(pending_chunks, failed_before, reused)

    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="vault-emb") as pool:
        future_map = {
            pool.submit(owner._embed_one_file, source, chunks, document_provider): source
            for source, chunks in tasks
        }
        for future in as_completed(future_map):
            source = future_map[future]
            chunks = pending.get(source, [])
            try:
                future.result()
            except Exception as exc:
                failures[source] = str(exc)
            finally:
                owner._sync_progress["files_done"] += 1
                owner._sync_progress["chunks_done"] += len(chunks)
    for source in pending:
        if source in failures:
            owner.failed_files[source] = failures[source]
        else:
            owner.failed_files.pop(source, None)
    return owner._embedding_changed_state(pending_chunks, failed_before, reused)


def revoke_source(owner: MarkdownIndexer, source: str, *, reason: str) -> bool:
    chunks = owner._chunks.pop(source, [])
    ids = {chunk.id for chunk in chunks}
    for state in (owner._signatures, owner._stat_cache, owner._stat_seen_ns,
                  owner._stat_confirmations, owner.fast_path_warnings, owner.failed_files):
        state.pop(source, None)
    for chunk_id in ids:
        owner._pending_vectors.pop(chunk_id, None)
    owner._fts_delete(source)
    if ids:
        try:
            owner._vector_backend.delete_vectors(ids)
        except Exception as exc:
            owner.failed_files[source] = f"{reason}: vector revocation failed: {exc}"
        owner._disk_vectors.difference_update(ids)
    # E08-c：撤销派生媒体召回。更新/删除/豁免/导入切代都不复活旧 links；只删
    # 派生映射，**不**触碰解析事实（revision/markdown）与媒体 blob。
    if any(chunk.metadata.get("revision_id") or chunk.metadata.get("kind") == "media_proxy"
           for chunk in chunks):
        try:
            store = owner._existing_document_store()
        except Exception:
            store = None
        if store is not None:
            try:
                store.delete_media_chunk_links(source=source)
            except Exception as exc:
                owner.failed_files[source] = f"{reason}: media link revocation failed: {exc}"
    return bool(chunks)


def media_proxy_for_revision(owner: MarkdownIndexer, store: Any, source: str, revision_id: str,
                             markdown: str, text_chunks: list[Chunk]) -> tuple[list[Chunk], list[Any]]:
    """为一条已提交 revision 生成媒体 proxy chunk，并**整代**落 profile 作用域 links。

    - 正文与 proxy 走同一 FTS/embed 候选：返回的 proxy 由调用方并入 `owner._chunks`；
    - links 至少按 profile/derived generation/revision/occurrence/chunk 绑定；
    - 无 occurrence 时也整代替换（清掉本代旧链），源失去媒体后不残留旧召回。
    返回 `(proxy_chunks, occurrences)`，供同轮原生媒体向量路线复用同一 occurrence 事实。
    """
    from .media import build_media_proxy_chunks, media_chunk_links

    profile_key = owner._embedding_profile.fingerprint
    fingerprint = owner._chunker_fingerprint()
    generation = owner._derived_profile_key()
    writable = owner.document_store(write=True)
    # 切代撤销：先按 source+profile 清掉该源**所有历史 revision** 的派生召回，再写当前代。
    # 这样「revision 更新后旧代 links 不复活」，而其它 profile 的作用域完全不受影响。
    writable.delete_media_chunk_links(source=source, profile_key=profile_key)
    occurrences = list(writable.iter_media(source, revision_id=revision_id))
    proxy = build_media_proxy_chunks(
        markdown, occurrences, source=source, revision_id=revision_id,
        profile_key=profile_key, chunker_fingerprint=fingerprint,
        derived_generation_id=generation, chunking=owner._chunking_config)
    links = media_chunk_links(
        [*text_chunks, *proxy], occurrences, revision_id=revision_id,
        profile_key=profile_key, chunker_fingerprint=fingerprint,
        derived_generation_id=generation, markdown=markdown)
    writable.replace_media_chunk_links(
        revision_id=revision_id, profile_key=profile_key,
        derived_generation_id=generation, chunker_fingerprint=fingerprint, links=links)
    return proxy, occurrences


def media_native_route(owner: MarkdownIndexer) -> Any:
    """返回可用的原生媒体 provider；缺能力/缺 alignment 声明时返回 None（route 不可用）。

    **绝不用文本成功、同维向量或模型名冒充**：只有媒体 profile 显式声明的
    `alignment_space_id` 与正文 profile 的声明**完全一致**时，媒体向量才允许进入
    同一检索空间。
    """
    provider = getattr(owner, "media_provider", None)
    profile = getattr(provider, "profile", None)
    text_profile = getattr(owner, "_embedding_profile", None)
    if provider is None or profile is None or text_profile is None:
        return None
    text_alignment = getattr(text_profile, "alignment_space_id", "") or ""
    if not text_alignment or getattr(profile, "alignment_space_id", "") != text_alignment:
        return None
    if getattr(profile, "effective_dim", None) != getattr(text_profile, "effective_dim", None):
        return None
    if not any(modality in getattr(profile, "modalities", ()) for modality in ("image", "audio")):
        return None
    return provider


def media_native_for_revision(owner: MarkdownIndexer, store: Any, source: str, revision_id: str,
                              occurrences: list[Any]) -> list[Chunk]:
    """从**已验证 blob** 读受限 bytes 并消费 E07 `embed_media`，产出原生媒体向量 chunk。

    route 不可用（无 provider / 未声明 alignment / 无受限读取能力）时返回空列表——
    明确该 route 不可用，纯词法 proxy 仍可用，但**不**把 proxy 冒充 native。
    """
    from ..media_providers import EmbeddingInput
    from .media import MediaStageError

    provider = media_native_route(owner)
    if (occurrences and getattr(owner, "media_provider", None) is None
            and getattr(owner.config.embedding, "media_modalities", ())):
        error = getattr(owner, "_media_capability_error", None) or "declared media provider did not assemble"
        raise ProviderError(f"MEDIA_CAPABILITY_UNAVAILABLE: {error}")
    if provider is None or not occurrences:
        return []
    ability = provider.ability()
    allowed = set(ability.get("allowed_mime_types", ()))
    max_bytes = int(ability.get("max_input_bytes", 0) or 0)
    batch = max(1, int(ability.get("max_batch_size", 0) or 1))
    if not allowed or max_bytes <= 0:
        return []
    inputs: list[Any] = []
    identity: dict[str, tuple[str, str, str]] = {}
    for occurrence in occurrences:
        occ_id = str(occurrence.get("occurrence_id") or "")
        kind = "audio" if str(occurrence.get("kind")) == "audio" else "image"
        if not occ_id or kind not in ability.get("modalities", ()):
            continue
        # MIME 以**已验证 blob**（read_media 返回值）为准，不采信列举摘要里可能缺失/自述的值。
        try:
            payload = store.read_media(source, revision_id=revision_id, occurrence_id=occ_id,
                                       variant_id="original", max_bytes=max_bytes, include_data=True)
        except Exception as exc:
            raise ProviderError(f"native media blob read failed: {exc}") from exc
        mime = str(payload.get("mime_type") or "")
        data = payload.get("data") or b""
        if mime not in allowed or not data or len(data) > max_bytes:
            continue
        request_id = f"{source}\0{revision_id}\0{occ_id}"
        blob_hash = hashlib.sha256(bytes(data)).hexdigest()
        inputs.append(EmbeddingInput(request_id, kind, bytes(data), mime, blob_hash))
        identity[request_id] = (occ_id, kind, blob_hash)
    from .token_chunking import embedding_key
    candidates: dict[str, Chunk] = {}
    for item in inputs:
        occ_id, kind, blob_hash = identity[item.request_id]
        chunk_id = "media-native-" + hashlib.sha256(
            json.dumps([source, revision_id, occ_id, blob_hash, provider.profile.fingerprint],
                       ensure_ascii=False).encode()).hexdigest()
        metadata = {
            "source_kind": "virtual", "revision_id": revision_id, "kind": "media_native",
            "occurrence_id": occ_id, "media_occurrence_ids": [occ_id],
            "media_route": "native", "blob_sha256": blob_hash,
            "profile_key": provider.profile.fingerprint,
            "alignment_space_id": provider.profile.alignment_space_id,
            "line_basis": "media_alignment", "synthetic_segments": [],
            "source_spans": [], "anchor_available": False, "anchor_confidence": "unavailable",
            "embedding_key": embedding_key(
                blob_hash, provider.profile.fingerprint, provider.profile.preprocess_version, (blob_hash,)),
        }
        candidates[item.request_id] = Chunk(
            chunk_id, f"[Media native: {source} / {revision_id} / {occ_id}]", source, occ_id, metadata)
    # A failed later batch must not discard/recharge its successful prefix.
    old = {c.id: c for c in owner._chunks.get(source, ()) if c.metadata.get("kind") == "media_native"}
    chunks: list[Chunk] = []
    pending: list[Any] = []
    for item in inputs:
        chunk = candidates[item.request_id]
        prior = old.get(chunk.id)
        vector = prior.embedding if prior is not None else None
        if prior is not None and vector is None and owner._vectors_on_disk:
            vector = owner._vector_backend.get_vectors([chunk.id]).get(chunk.id)
        if vector is not None and len(vector):
            chunk.embedding = _to_emb(vector)
            chunks.append(chunk)
        else:
            pending.append(item)
    if not pending:
        return chunks
    control = owner._paid_control_store()
    if control is not None and owner.has_unresolved_paid_intents(control, "media", provider.profile.fingerprint):
        raise MediaStageError("PAID_REQUEST_UNRESOLVED: native media outcome unknown; automatic retry disabled", chunks)
    for start in range(0, len(pending), batch):
        batch_inputs = pending[start:start + batch]
        try:
            vectors = provider.embed_media(batch_inputs)
            if len(vectors) != len(batch_inputs):
                raise ProviderError("native media response count mismatch")
        except Exception as exc:
            raise MediaStageError(str(exc), chunks) from exc
        for item, vector in zip(batch_inputs, vectors):
            chunk = candidates[item.request_id]
            chunk.embedding = _to_emb(vector)
            chunks.append(chunk)
    return chunks


def run_sync(owner: MarkdownIndexer) -> list[Chunk]:
    """增量同步主执行引擎。

    前置条件：调用方必须已持有 owner._sync_lock。
    运行时通过 owner.* 就地变异 22 个内部状态属性。
    """
    failed_before = dict(owner.failed_files)
    # Media and text are independent stages. Text success must not erase media
    # failure; the existing persisted failure map is also the restart retry signal.
    media_failures = {source: error for source, error in failed_before.items()
                      if error.startswith(("media_proxy:", "media_native:"))}
    matcher = owner._ignore_matcher()
    scan = scan_indexable_files(owner.vault_path, matcher, _INDEXABLE_TEXT_EXTS)
    revoked = False
    try:
        store = owner._existing_document_store()
        captured_seq = store.change_seq() if store is not None else 0
        documents = store.list_documents() if store is not None else []
        generation = store.get_derived_generation(owner._derived_profile_key()) if store is not None else None
        if generation is not None and generation.status == "failed" and generation.last_error:
            try:
                recorded = json.loads(generation.last_error)
            except (ValueError, TypeError):
                recorded = {}
            files = recorded.get("files", {}) if isinstance(recorded, dict) else {}
            if isinstance(files, dict):
                for source, error in files.items():
                    if isinstance(source, str) and isinstance(error, str) and error.startswith(("media_proxy:", "media_native:")):
                        media_failures.setdefault(source, error)
    except Exception:
        store = None
        captured_seq = 0
        documents = []
        for source, chunks in list(owner._chunks.items()):
            if any(chunk.metadata.get("revision_id") for chunk in chunks):
                revoked = revoke_source(owner, source, reason="store_unavailable") or revoked
    # E20/F02: first ingest may have no active revision yet. Reconcile its
    # pending source on a complete scan too, so deletion fences a worker paused
    # between source verification and stage/commit. Never infer deletion from
    # an incomplete scan, a policy-pruned subtree, or permission errors.
    if store is not None and scan.complete:
        active = {doc.source for doc in documents if doc.visibility == "active" and doc.active_revision}
        # An unverified fact without a live worker is not an observed deletion:
        # imports/manual staging legitimately retain it with no physical source.
        pending = set(store.pending_ingest_sources())
        for source in sorted(pending - active):
            if matcher.is_ignored(source)[0] or any(
                    source == path or source.startswith(path + "/") for path in scan.policy_pruned):
                continue
            try:
                from ..doc_store import normalize_source_path
                normalize_source_path(source, owner.vault_path)
                (owner.vault_path / source).stat()
            except FileNotFoundError:
                owner.document_store(write=True).mark_deleted(source)
            except OSError:
                pass
        documents = store.list_documents()
    hidden = {doc.source for doc in documents if doc.visibility != "active"}
    hidden_keys = {source_compare_key(source) for source in hidden}
    virtual_sources = {source_compare_key(doc.source): doc.source for doc in documents
                       if doc.visibility == "active" and doc.active_revision}
    # §20.1「来源精确排除」：解析事实已进文档库的 source，其**同名旧镜像**不再作为
    # 物理文本重复进索引（否则一次检索同时命中虚拟文档与镜像，双份结果）。逐条按
    # source 推导镜像路径，不做目录级忽略；未入库的镜像与用户内容照旧可见。
    #
    # E03-a：路径吻合（或仅有库内同名 source）**不是**归属证明。同路径上的普通用户
    # 文件此前会被一并排除（F04 误排除）。现在只排除经 `prove_mirror_ownership`
    # 逐项证明的镜像：frontmatter 声明 source/SHA + 物理源 SHA 复核 + 唯一 ledger
    # done 归属 + 媒体资产校验。证明失败→保留为普通文件，绝不永久隐藏。
    mirror_prefix = owner._ingest_mirror_prefix()
    excluded_mirrors: set[str] = set()
    mirror_candidates: dict[str, str] = {}
    for document in documents:
        # 仅当存在可见、有效的 virtual 副本（active 且有 active_revision）时才考虑
        # 排除其镜像；副本失效后下一轮重新评估。
        if document.visibility != "active" or not document.active_revision:
            continue
        source = document.source
        name = source if source.lower().endswith((".md", ".markdown", ".txt")) \
            else source.rsplit(".", 1)[0] + ".md"
        mirror_candidates[source] = mirror_prefix + name
    if mirror_candidates and store is not None:
        from ..ingest.migration import resolve_excluded_mirrors

        try:
            quota = int(store._quota_limit_bytes())
        except Exception:
            quota = 0
        excluded_mirrors, _mirror_rejections = resolve_excluded_mirrors(
            owner.vault_path, mirror_candidates, quota_bytes=quota,
            mirrors_root=owner.vault_path / mirror_prefix.rstrip("/"),
        )
    for source in set(owner._chunks) | set(owner.failed_files):
        canonical = virtual_sources.get(source_compare_key(source))
        if (source_compare_key(source) in hidden_keys or source in excluded_mirrors or matcher.is_ignored(source)[0]
                or (canonical is not None and source != canonical)
                or any(source == path or source.startswith(path + "/") for path in scan.policy_pruned)):
            revoked = revoke_source(owner, source, reason="visibility/policy") or revoked
    found: set[str] = set()
    changed: list[tuple[str, str, list[Chunk], tuple[int, int, int], int]] = []
    # 时间戳刻度探测的样本：直接复用本循环本来就要做的 stat（零额外 I/O）。
    mtime_samples: list[int] = []
    files = [path for path in scan.found
             if source_compare_key(owner._source(path)) not in hidden_keys
             and owner._source(path) not in excluded_mirrors
             and source_compare_key(owner._source(path)) not in virtual_sources]
    owner._sync_progress["files_total"] = len(files)
    for i, path in enumerate(files):
        source = owner._source(path)
        found.add(source)
        try:
            stat = path.stat()
            # 签名是三元组：mtime + size 之外再加入 ctime。POSIX 下 ctime 由内核
            # 维护、os.utime 改不回去，是「mtime 被显式回拨」这条路径的唯一
            # 鉴别通道（Windows 下 st_ctime 是创建时间、无鉴别力，残余风险见
            # _fast_path_is_trustworthy）。
            fast_sig = (int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ctime_ns))
            if len(mtime_samples) < _MTIME_TICK_PROBE_MAX_SAMPLES:
                mtime_samples.append(int(stat.st_mtime_ns))

            if (
                source in owner._signatures
                and owner._stat_cache.get(source) == fast_sig
                and owner._fast_path_is_trustworthy(source, int(stat.st_mtime_ns))
            ):
                continue

            raw = path.read_bytes()
            signature = hashlib.sha256(raw).hexdigest()
            # 可信时刻：在 read + sha256 完成之后取。它与刚哈希的那份内容严格
            # 对应——若登记之后文件再被写入，新 mtime 必然晚于该时刻，签名
            # 随之变化，快速路径自然失效。注意登记进 _stat_cache 的签名用
            # 读盘前的 fast_sig 而非重新 stat：若读盘期间文件恰好被写入，
            # fast_sig 与磁盘新状态不一致，下一轮签名比对会失配并自动重读
            # （自愈）；重新 stat 反而可能把「新 mtime + 旧哈希」这对错误
            # 组合固化进缓存。
            verified_at_ns = time.time_ns()
            if owner._signatures.get(source) == signature:
                # 内容实测未变（可能是被 racily clean 判据逼下来复核的，也可能
                # 只是 mtime 被 touch 过）。按本轮验证时刻重新登记；只要
                # verified_at 与 mtime 拉开了足够余量，该条目即恢复可信、
                # 重新走零读盘快速路径。
                prev_seen_ns = owner._stat_seen_ns.get(source)
                owner._stat_cache[source] = fast_sig
                owner._stat_seen_ns[source] = verified_at_ns
                owner._record_confirmation(source, prev_seen_ns, verified_at_ns)
                continue
            text = raw.decode("utf-8-sig")
            # 顺手复用上面 read_bytes 已经打开的目录项做一次 stat，记录文件
            # 修改时间供 mtime 过滤用：签名未变的文件不会走到这里，所以这个
            # mtime 语义上是"内容最后一次变化的时间"，而不是每次 touch 都更新。
            mtime = float(stat.st_mtime)
            chunks = owner._chunk_file(source, text, mtime)
            changed.append(
                (source, signature, chunks, fast_sig, verified_at_ns)
            )
        except Exception as exc:
            revoked = revoke_source(owner, source, reason="read_failed") or revoked
            owner.failed_files[source] = str(exc)
        finally:
            owner._sync_progress["files_done"] = i + 1

    # 扫描结束即收敛刻度探测结果。安全：快速路径只可能在 _stat_cache 非空时
    # 命中，而它是进程内存量、每进程首次 sync 必然为空，故首次扫描不会用未
    # 探测的余量做跳过决定。
    owner._finalize_mtime_tick_probe(mtime_samples)

    for document in documents:
        source = document.source
        if document.visibility != "active" or not document.active_revision:
            continue
        if any(source == path or source.startswith(path + "/") for path in scan.policy_pruned):
            revoked = revoke_source(owner, source, reason="policy_pruned") or revoked
            continue
        if matcher.is_ignored(source)[0]:
            owner.document_store(write=True).set_visibility(source, "exempt")
            revoked = revoke_source(owner, source, reason="exempt") or revoked
            continue
        found.add(source)
        try:
            from ..doc_store import normalize_source_path
            normalize_source_path(source, owner.vault_path)
            path = owner.vault_path / source
            stat = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            raw_sha = digest.hexdigest()
            after = path.stat()
            if (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns) != (
                after.st_mtime_ns, after.st_size, after.st_ctime_ns
            ):
                raise ValueError("source changed during verification")
            active = store.get_active(source, include_hidden=True)
            if active is None:
                revoked = revoke_source(owner, source, reason="visibility") or revoked
                continue
            revision = active.revision
            if raw_sha != revision.source_sha256:
                raise ValueError("source SHA differs from committed revision")
            media_route = media_native_route(owner)
            media_fingerprint = media_route.profile.fingerprint if media_route is not None else ""
            signature = f"virtual:{revision.revision_id}:{raw_sha}:{revision.render_sha256}:media:{media_fingerprint}"
            missing_native = any(c.metadata.get("kind") == "media_native" and not chunk_has_vector(owner, c)
                                 for c in owner._chunks.get(source, ()))
            if owner._signatures.get(source) == signature and not missing_native and source not in media_failures:
                continue
            if (owner._signatures.get(source) == signature and not missing_native
                    and source in media_failures and media_route is not None
                    and owner.has_unresolved_paid_intents(
                        owner._paid_control_store(), "media", media_fingerprint)):
                # A settled source with an unknown send stays failed without
                # regenerating identical caches or replaying successful batches.
                continue
            media_failures.pop(source, None)
            store_uuid = store.store_meta().store_uuid
            # §20.7B：虚拟 chunk ID 由 store_uuid/doc_id/revision_id/chunker 指纹/
            # index/content 共同决定（`virtual_chunk_id`），同文新 revision 与
            # A→B→A 都不会复活旧地址；这里不再用 config.chunk_size 手搓哈希。
            chunks = owner._chunk_file(
                source, revision.parsed_markdown, float(stat.st_mtime),
                virtual_identity={"store_uuid": store_uuid, "doc_id": active.doc_id,
                                  "revision_id": revision.revision_id},
            )
            for chunk in chunks:
                chunk.metadata.update({"revision_id": revision.revision_id,
                                       "source_sha256": raw_sha,
                                       "render_sha256": revision.render_sha256,
                                       "line_basis": "rendered_markdown"})
            # E08-b：捕获已提交 revision 的 occurrences，生成媒体 proxy 并落 links。
            # proxy 并入同一 FTS/embed 候选；失败只记该 source，不丢正文层。
            try:
                proxy_chunks, occurrences = media_proxy_for_revision(
                    owner, store, source, revision.revision_id, revision.parsed_markdown, chunks)
                chunks.extend(proxy_chunks)
            except Exception as exc:
                occurrences = []
                media_failures[source] = f"media_proxy: {exc}"
            # E08-d：原生媒体向量路线（消费 E07 embed_media）。route 不可用即空，不冒充。
            try:
                native_chunks = media_native_for_revision(
                    owner, owner.document_store(write=True), source, revision.revision_id, occurrences)
                chunks.extend(native_chunks)
            except Exception as exc:
                chunks.extend(getattr(exc, "chunks", ()))
                media_failures[source] = f"media_native: {exc}"
            # Successful prefixes are native too; never embed their proxies as text.
            native_occurrences = {c.metadata["occurrence_id"] for c in chunks
                                  if c.metadata.get("kind") == "media_native"}
            for chunk in chunks:
                if (chunk.metadata.get("kind") == "media_proxy"
                        and chunk.metadata.get("occurrence_id") in native_occurrences):
                    chunk.metadata["embedding_disabled"] = True
            for chunk in chunks:
                chunk.metadata.update(source_sha256=raw_sha, render_sha256=revision.render_sha256)
            changed.append((source, signature, chunks,
                            (int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ctime_ns)),
                            time.time_ns()))
        except FileNotFoundError as exc:
            writable = owner.document_store(write=True)
            if scan.complete:
                writable.mark_deleted(source)
            else:
                writable.invalidate_source(source)
            revoked = revoke_source(owner, source, reason="source_missing") or revoked
            if not scan.complete:
                owner.failed_files[source] = str(exc)
        except Exception as exc:
            owner.document_store(write=True).invalidate_source(source)
            revoked = revoke_source(owner, source, reason="source_unverified") or revoked
            owner.failed_files[source] = str(exc)

    # Text layer: changed files update the index even if embedding fails
    # afterwards, so lexical search still works without vectors.
    if changed:
        owner._sync_state = "fts"
        owner._sync_progress["phase"] = "fts"
    for source, signature, chunks, fast_sig, verified_at_ns in changed:
        old_chunks = owner._chunks.get(source)
        # A native-stage retry doesn't change text inputs. Preserve their exact
        # profile-scoped vectors instead of recharging the successful text stage.
        prior_by_id = {c.id: c for c in old_chunks or ()}
        from .token_chunking import embedding_key as text_embedding_key
        profile = owner._embedding_profile
        for chunk in chunks:
            if chunk.metadata.get("kind") == "media_native" or chunk.metadata.get("embedding_disabled"):
                continue
            prior = prior_by_id.get(chunk.id)
            hashes = chunk.metadata.get("media_hashes") or (
                (chunk.metadata["blob_sha256"],) if chunk.metadata.get("blob_sha256") else ())
            key = text_embedding_key(_document_input(owner, chunk.content), profile.fingerprint,
                                     profile.preprocess_version, hashes)
            if prior is not None and _reuse_key(prior) == key:
                vector = prior.embedding
                if vector is None and owner._vectors_on_disk:
                    vector = owner._vector_backend.get_vectors([prior.id]).get(prior.id)
                if vector is not None and len(vector):
                    chunk.embedding = _to_emb(vector)
        owner._chunks[source] = chunks
        owner._signatures[source] = signature
        owner._stat_cache[source] = fast_sig
        owner._stat_seen_ns[source] = verified_at_ns
        # 内容刚被重建：这是该签名的第一次内容级确认。
        owner._stat_confirmations[source] = 1
        owner.fast_path_warnings.pop(source, None)
        owner.failed_files.pop(source, None)
        owner._fts_upsert(source, chunks)
        # Disk-backed mode: re-chunking a file orphans its old vector ids.
        if owner._vectors_on_disk and old_chunks:
            old_ids = [chunk.id for chunk in old_chunks]
            try:
                owner._vector_backend.delete_vectors(old_ids)
                owner._disk_vectors.difference_update(old_ids)
            except Exception:
                pass

    removed: set[str] = (set(owner._chunks) - found) if scan.complete else set()
    for source in removed:
        revoked = revoke_source(owner, source, reason="removed") or revoked
    if documents and (changed or removed or revoked):
        try:
            owner.document_store(write=True).mark_derived_generation(
                owner._derived_profile_key(), change_seq=captured_seq,
                chunker_fingerprint=owner._chunker_fingerprint(),
                space_fingerprint=owner._derived_profile_key(), status="stale",
            )
        except Exception:
            store = None
            for source, chunks in list(owner._chunks.items()):
                if any(chunk.metadata.get("revision_id") for chunk in chunks):
                    revoked = revoke_source(owner, source, reason="derived_unavailable") or revoked

    # Disk-backed mode: on first sync (or after a crash) make sure the
    # vector store matches the in-memory chunk set before embedding.
    owner._ensure_disk_vectors_migrated()
    # 文本层刚被重建过（chunker 版本提升 / 缓存损坏）时，把初始化阶段
    # 暂存的老向量挂回去，避免整个库重新 embedding。
    owner._attach_pending_vectors()

    # Vector layer: embed every chunk that lacks a vector. When the vectors
    # cache was invalidated (model/dimension change) this re-embeds the whole
    # corpus while reusing the text chunks; when only a few files changed it
    # embeds just those chunks.
    owner._sync_state = "embedding"
    owner._sync_progress["phase"] = "embedding"
    embed_did_work = owner._embed_missing()
    for source, error in media_failures.items():
        if source in owner._chunks:
            text_error = owner.failed_files.get(source, "")
            owner.failed_files[source] = (error if not text_error or text_error.startswith(error)
                                          else f"{error}; text: {text_error}")
    # Disk-backed mode: persist newly embedded vectors and release RAM.
    owner._flush_vectors_to_disk()
    if scan.complete and owner._vectors_on_disk and not owner._embedding_paused:
        current_ids = {c.id for chunks in owner._chunks.values() for c in chunks}
        stale_ids = set(owner._vector_backend.list_ids()) - current_ids
        if stale_ids:
            owner._vector_backend.delete_vectors(stale_ids)
            owner._disk_vectors.difference_update(stale_ids)

    try:
        latest_documents = store.list_documents() if store is not None else []
    except Exception:
        latest_documents = []
        store = None
        for source, chunks in list(owner._chunks.items()):
            if any(chunk.metadata.get("revision_id") for chunk in chunks):
                revoked = revoke_source(owner, source, reason="store_unavailable") or revoked
    for document in latest_documents:
        if document.visibility != "active" and document.source in owner._chunks:
            revoked = revoke_source(owner, document.source, reason="visibility") or revoked
    owner.last_sync = time.time()
    # 本轮扫描的结束时刻（纳秒），仅供观测/诊断使用，不参与快速路径判据
    # （判据见 _fast_path_is_trustworthy，基于 _stat_seen_ns）。
    owner._scan_completed_ns = time.time_ns()
    # 什么都没变时跳过缓存重写：原生监听（[cache] placement = "vault"）下，
    # 每次写缓存都会再次触发文件事件，无变化也重写等于自激的同步死循环。
    if changed or removed or revoked or embed_did_work or owner.failed_files != failed_before:
        owner._save_cache()
    if store is not None and (documents or latest_documents):
        try:
            generation = store.get_derived_generation(owner._derived_profile_key())
            media_failed = any(error.startswith(("media_proxy:", "media_native:"))
                               for error in owner.failed_files.values())
            status = ("failed" if media_failed else
                      "ready" if scan.complete and not owner.failed_files
                      and store.change_seq() == captured_seq else "stale")
            last_error = (json.dumps({"files": owner.failed_files}, sort_keys=True, ensure_ascii=False)
                          if owner.failed_files else "")
            if (changed or removed or revoked or embed_did_work or generation is None
                    or generation.status != status or generation.change_seq != captured_seq
                    or generation.last_error != last_error):
                owner.document_store(write=True).mark_derived_generation(
                    owner._derived_profile_key(), change_seq=captured_seq,
                    chunker_fingerprint=owner._chunker_fingerprint(),
                    space_fingerprint=owner._derived_profile_key(), status=status, last_error=last_error,
                )
        except Exception:
            for source, chunks in list(owner._chunks.items()):
                if any(chunk.metadata.get("revision_id") for chunk in chunks):
                    revoke_source(owner, source, reason="derived_unavailable")
            owner._save_cache()
    return owner.all_chunks()
