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
from .scanning import _MTIME_TICK_PROBE_MAX_SAMPLES

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


def reuse_vectors_by_content_hash(owner: MarkdownIndexer) -> int:
    """把已有向量按 content_hash 复用到内容相同但还没有向量的 chunk 上。

    典型场景：库里有一份整目录的备份（教材/ 与 教材_Raw_Backup/），两处
    正文逐字相同，但 chunk.id 因为 source 不同而不一样——与其把同一段文本
    送进 embedding API 两次，不如直接复用已经算出来的向量。返回复用条数。

    只读使用向量，所以多个 chunk 可以安全共享同一个 array 对象。
    """
    missing: list[Chunk] = []
    donors: dict[str, Chunk] = {}
    for chunks in owner._chunks.values():
        for chunk in chunks:
            digest = chunk.metadata.get("content_hash")
            if not digest:
                continue
            if owner._chunk_has_vector(chunk):
                donors.setdefault(digest, chunk)
            else:
                missing.append(chunk)
    if not missing or not donors:
        return 0

    needed = {chunk.metadata.get("content_hash") for chunk in missing}
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
        vector = reusable.get(chunk.metadata.get("content_hash"))
        if vector is None:
            continue
        chunk.embedding = vector
        reused += 1
    return reused


def embed_one_file(source: str, chunks: list[Chunk], provider: EmbeddingProvider) -> None:
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
    # 先做一轮内容哈希复用：已经算过的内容不再花钱重算一次。
    reused = owner._reuse_vectors_by_content_hash()

    failed_before = dict(owner.failed_files)
    pending: dict[str, list[Chunk]] = {}
    for source, chunks in owner._chunks.items():
        missing = [chunk for chunk in chunks if not owner._chunk_has_vector(chunk)]
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

    if owner.config.embedding.mode != "external":
        for source, chunks in pending.items():
            try:
                vectors = owner.embedding_provider.embed([chunk.content for chunk in chunks])
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
    if max_workers <= 1 or len(tasks) <= 1:
        for source, chunks in tasks:
            try:
                owner._embed_one_file(source, chunks, owner.embedding_provider)
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
            pool.submit(owner._embed_one_file, source, chunks, owner.embedding_provider): source
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


def run_sync(owner: MarkdownIndexer) -> list[Chunk]:
    """增量同步主执行引擎。

    前置条件：调用方必须已持有 owner._sync_lock。
    运行时通过 owner.* 就地变异 22 个内部状态属性。
    """
    owner.vault_path.mkdir(parents=True, exist_ok=True)
    failed_before = dict(owner.failed_files)
    found: set[str] = set()
    changed: list[tuple[str, str, list[Chunk], tuple[int, int, int], int]] = []
    # 时间戳刻度探测的样本：直接复用本循环本来就要做的 stat（零额外 I/O）。
    mtime_samples: list[int] = []
    files = list(owner._markdown_files())
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
            owner.failed_files[source] = str(exc)
            owner._chunks.pop(source, None)
            owner._signatures.pop(source, None)
            owner._stat_cache.pop(source, None)
            owner._stat_seen_ns.pop(source, None)
            owner._stat_confirmations.pop(source, None)
            owner._fts_delete(source)
        finally:
            owner._sync_progress["files_done"] = i + 1

    # 扫描结束即收敛刻度探测结果。安全：快速路径只可能在 _stat_cache 非空时
    # 命中，而它是进程内存量、每进程首次 sync 必然为空，故首次扫描不会用未
    # 探测的余量做跳过决定。
    owner._finalize_mtime_tick_probe(mtime_samples)

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

    removed: set[str] = set(owner._chunks) - found
    for source in removed:
        removed_ids = [chunk.id for chunk in owner._chunks.get(source, [])]
        owner._chunks.pop(source, None)
        owner._signatures.pop(source, None)
        owner._stat_cache.pop(source, None)
        owner._stat_seen_ns.pop(source, None)
        owner._stat_confirmations.pop(source, None)
        owner.fast_path_warnings.pop(source, None)
        owner.failed_files.pop(source, None)
        owner._fts_delete(source)
        if removed_ids:
            try:
                owner._vector_backend.delete_vectors(removed_ids)
                owner._disk_vectors.difference_update(removed_ids)
            except Exception:
                pass
    owner.last_sync = time.time()
    # 本轮扫描的结束时刻（纳秒），仅供观测/诊断使用，不参与快速路径判据
    # （判据见 _fast_path_is_trustworthy，基于 _stat_seen_ns）。
    owner._scan_completed_ns = time.time_ns()
    # 什么都没变时跳过缓存重写：原生监听（[cache] placement = "vault"）下，
    # 每次写缓存都会再次触发文件事件，无变化也重写等于自激的同步死循环。
    if changed or removed or embed_did_work or owner.failed_files != failed_before:
        owner._save_cache()
    return owner.all_chunks()
