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
import time
from typing import Any, Iterable, TYPE_CHECKING

from ..providers import EmbeddingProvider, ProviderError
from .models import Chunk, _EMB_DTYPE
from .scanning import _MTIME_TICK_PROBE_MAX_SAMPLES, scan_indexable_files

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

    默认模板 `{text}` 是恒等变换；只有显式配置模板时才改变发送内容。模板里出现
    未知占位符时按原文发送（不静默丢正文），并由 embedding_key 记录同一份输入。
    """
    profile = getattr(owner, "_embedding_profile", None)
    template = getattr(profile, "document_template", "{text}") or "{text}"
    if template == "{text}":
        return content
    try:
        return template.format(text=content)
    except (KeyError, IndexError, ValueError):
        return content


class _TemplatingProvider:
    """在 provider 边界应用 `document_template`，其余行为（批量/合约校验）原样透传。"""

    def __init__(self, provider: Any, apply: Any) -> None:
        self._provider = provider
        self._apply = apply

    def embed(self, texts):
        return self._provider.embed([self._apply(text) for text in texts])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


def _document_provider(owner: MarkdownIndexer) -> Any:
    provider = owner.embedding_provider
    profile = getattr(owner, "_embedding_profile", None)
    template = getattr(profile, "document_template", "{text}") or "{text}"
    if template == "{text}":
        return provider
    return _TemplatingProvider(provider, lambda text: _document_input(owner, text))


def _stamp_embedding_keys(owner: MarkdownIndexer) -> None:
    """给每个可嵌入 chunk 盖上与捕获 space/profile 绑定的 embedding_key。

    key 只描述「同一输入 + 同一 profile」，不参与版本寻址（§20.7B），所以可以
    跨 revision 复用；oversize / embedding_disabled 的 chunk 不留 key。
    """
    from .token_chunking import embedding_key as _embedding_key
    profile = getattr(owner, "_embedding_profile", None)
    space = getattr(profile, "fingerprint", "") or ""
    template = getattr(profile, "preprocess_version", "") or "text-v1"
    for chunks in owner._chunks.values():
        for chunk in chunks:
            if chunk.metadata.get("embedding_disabled") or chunk.metadata.get("embedding_key"):
                continue
            chunk.metadata["embedding_key"] = _embedding_key(
                _document_input(owner, chunk.content), space, template,
                chunk.metadata.get("media_hashes", ()))


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
                   if not chunk.metadata.get("embedding_disabled") and not owner._chunk_has_vector(chunk)]
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
        # §20.7B：审批未通过（profile/space/切块代际变化且会付费）时外部请求数必须为 0。
        # 这里不能把每个文件都标 failed（那是「永久撒谎」），只标记暂停，等显式授权。
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
    return bool(chunks)


def run_sync(owner: MarkdownIndexer) -> list[Chunk]:
    """增量同步主执行引擎。

    前置条件：调用方必须已持有 owner._sync_lock。
    运行时通过 owner.* 就地变异 22 个内部状态属性。
    """
    failed_before = dict(owner.failed_files)
    matcher = owner._ignore_matcher()
    scan = scan_indexable_files(owner.vault_path, matcher, frozenset({".md", ".txt"}))
    revoked = False
    try:
        store = owner._existing_document_store()
        captured_seq = store.change_seq() if store is not None else 0
        documents = store.list_documents() if store is not None else []
    except Exception:
        store = None
        captured_seq = 0
        documents = []
        for source, chunks in list(owner._chunks.items()):
            if any(chunk.metadata.get("revision_id") for chunk in chunks):
                revoked = revoke_source(owner, source, reason="store_unavailable") or revoked
    hidden = {doc.source for doc in documents if doc.visibility != "active"}
    # §20.1「来源精确排除」：解析事实已进文档库的 source，其**同名旧镜像**不再作为
    # 物理文本重复进索引（否则一次检索同时命中虚拟文档与镜像，双份结果）。逐条按
    # source 推导镜像路径，不做目录级忽略；未入库的镜像与用户内容照旧可见。
    mirror_prefix = owner._ingest_mirror_prefix()
    excluded_mirrors: set[str] = set()
    for document in documents:
        if document.visibility != "active":
            continue
        source = document.source
        if source.lower().endswith((".md", ".markdown", ".txt")):
            excluded_mirrors.add(mirror_prefix + source)
        else:
            excluded_mirrors.add(mirror_prefix + source.rsplit(".", 1)[0] + ".md")
    for source in set(owner._chunks) | set(owner.failed_files):
        if (source in hidden or source in excluded_mirrors or matcher.is_ignored(source)[0]
                or any(source == path or source.startswith(path + "/") for path in scan.policy_pruned)):
            revoked = revoke_source(owner, source, reason="visibility/policy") or revoked
    found: set[str] = set()
    changed: list[tuple[str, str, list[Chunk], tuple[int, int, int], int]] = []
    # 时间戳刻度探测的样本：直接复用本循环本来就要做的 stat（零额外 I/O）。
    mtime_samples: list[int] = []
    files = [path for path in scan.found
             if owner._source(path) not in hidden and owner._source(path) not in excluded_mirrors]
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
        if (owner.vault_path / source).suffix.lower() in {".md", ".txt"}:
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
            signature = f"virtual:{revision.revision_id}:{raw_sha}:{revision.render_sha256}"
            if owner._signatures.get(source) == signature:
                continue
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
    # Disk-backed mode: persist newly embedded vectors and release RAM.
    owner._flush_vectors_to_disk()

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
            status = "ready" if scan.complete and not owner.failed_files and store.change_seq() == captured_seq else "stale"
            if (changed or removed or revoked or embed_did_work or generation is None
                    or generation.status != status or generation.change_seq != captured_seq):
                owner.document_store(write=True).mark_derived_generation(
                    owner._derived_profile_key(), change_seq=captured_seq,
                    chunker_fingerprint=owner._chunker_fingerprint(),
                    space_fingerprint=owner._derived_profile_key(), status=status,
                )
        except Exception:
            for source, chunks in list(owner._chunks.items()):
                if any(chunk.metadata.get("revision_id") for chunk in chunks):
                    revoke_source(owner, source, reason="derived_unavailable")
            owner._save_cache()
    return owner.all_chunks()
