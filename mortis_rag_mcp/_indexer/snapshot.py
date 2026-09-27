from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING
import zipfile

from .cache_codec import _CacheCodec, _VectorsCodec
from ..fts import FtsIndex
from ..vector import create_vector_backend

if TYPE_CHECKING:
    from ..indexer import MarkdownIndexer
    from .models import Chunk


# 快照（kb_export / kb_import）：把两个缓存层打包成 zip 随库迁移，换机不再
# 全量重新 embedding。导入端会按本机 _cache_key() 重命名落地，并对 .bin 里
# 的 meta 做本地重写（cache key 是路径派生的，跨机器必然不同）。
_SNAPSHOT_FORMAT = "vault-mcp-snapshot"
_SNAPSHOT_VERSION = 1
_SNAPSHOT_MEMBERS = frozenset({"manifest.json", "chunks.bin", "vectors.bin", "vectors.sqlite", "fts.sqlite"})
# 各成员解压后的字节上限：正常库的缓存不会超过这些量级，超限 = 恶意构造。
_SNAPSHOT_MEMBER_LIMITS = {
    "manifest.json": 1 * 1024 * 1024,
    "chunks.bin": 4 * 1024 * 1024 * 1024,
    "vectors.bin": 16 * 1024 * 1024 * 1024,
    "vectors.sqlite": 16 * 1024 * 1024 * 1024,
    "fts.sqlite": 16 * 1024 * 1024 * 1024,
}


def export_snapshot(owner: MarkdownIndexer, out_path: str | Path) -> dict[str, Any]:
    """把本库的索引缓存（chunks + 向量 + FTS）打包成 zip 快照。

    快照是缓存层原样搬运，不含任何机器相关路径；导入端按自己的 cache key
    重命名落地。前提是缓存已启用且做过至少一次 sync（否则没有可导出的东西）。
    """
    # 与 import 对称地持 _sync_lock：否则并发 sync 的 tmp.replace 会让
    # 快照里 chunks 与 vectors 来自不同时刻（撕裂快照）。
    with owner._sync_lock:
        return _export_snapshot_locked(owner, out_path)


def _export_snapshot_locked(owner: MarkdownIndexer, out_path: str | Path) -> dict[str, Any]:
    # 必须持 _sync_lock：打包过程逐文件读取缓存，并发的 sync 可能恰好
    # tmp.replace 其中一个文件 —— Windows 上直接 PermissionError，非失败
    # 交错则产出「chunks 来自 sync 前、vectors 来自 sync 后」的撕裂快照，
    # 导入端会把这对不一致数据当作一致状态恢复。
    if owner._chunks_cache_path is None:
        raise ValueError("cache is disabled; enable [cache] before exporting a snapshot")
    if not owner._chunks:
        raise ValueError("index is empty; run a sync (or kb_rebuild) before exporting")

    # 把当前内存态刷进缓存文件再打包，保证快照 = 此刻的索引。
    owner._save_cache()

    vectors_member: str | None = None
    if owner._vectors_on_disk:
        if owner._vectors_db_path is not None and owner._vectors_db_path.exists():
            vectors_member = "vectors.sqlite"
    elif owner._vectors_cache_path is not None and owner._vectors_cache_path.exists():
        vectors_member = "vectors.bin"

    vector_count = sum(
        1 for chunk in owner.all_chunks() if owner._chunk_has_vector(chunk)
    )
    manifest = {
        "format": _SNAPSHOT_FORMAT,
        "format_version": _SNAPSHOT_VERSION,
        "cache_key": owner._cache_key(),
        "chunks_meta": owner._chunks_meta(),
        "vectors_meta": owner._vectors_meta(),
        "backend": getattr(owner._vector_backend, "name", owner.config.vector.backend),
        "stats": {
            "files": len(owner._chunks),
            "chunks": len(owner.all_chunks()),
            "vectors": vector_count,
        },
    }

    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.write(owner._chunks_cache_path, "chunks.bin")
            if vectors_member is not None:
                source = owner._vectors_db_path if vectors_member == "vectors.sqlite" else owner._vectors_cache_path
                zf.write(source, vectors_member)
            if owner._fts is not None and owner._fts_cache_path is not None and owner._fts_cache_path.exists():
                zf.write(owner._fts_cache_path, "fts.sqlite")
        tmp.replace(out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return {
        "exported": True,
        "path": str(out),
        "backend": manifest["backend"],
        **manifest["stats"],
    }


def import_snapshot(owner: MarkdownIndexer, snapshot: str | Path, force: bool = False) -> dict[str, Any]:
    """从快照恢复索引缓存；随后一次 sync 应当 0 次 embedding API 调用。

    安全与兼容：

    * zip 成员按**白名单**精确匹配，多余的成员（含 ../ 穿越名）直接拒绝；
      落地路径全部来自本机缓存配置，从不使用压缩包内的名字拼路径。
    * .bin 里的 meta 含源机器的 cache key，导入时用本机 _chunks_meta() /
      _vectors_meta() 重写后再落地。
    * 向量层的 model/dimension 与本机配置不一致时拒绝导入（force=true 可
      强制，但此时只导入文本层，向量作废由本地重新 embedding——错维度的
      向量对检索是毒药）。

    前提：本库缓存已启用、目录已注册（先 kb_init 再 kb_import）。
    """
    if owner._chunks_cache_path is None:
        raise ValueError("cache is disabled; enable [cache] before importing a snapshot")
    src = Path(snapshot).expanduser()
    if not src.is_file():
        raise ValueError(f"snapshot not found: {src}")

    with zipfile.ZipFile(src) as zf:
        names = set(zf.namelist())
        unknown = names - _SNAPSHOT_MEMBERS
        if unknown:
            raise ValueError(f"snapshot contains unexpected members: {sorted(unknown)}")
        if "manifest.json" not in names or "chunks.bin" not in names:
            raise ValueError("snapshot is missing manifest.json or chunks.bin")
        # 解压炸弹防护：快照的用途就是从别人机器收文件，任何一个成员都可以
        # 是恶意构造的。白名单挡得住路径穿越，挡不住"1MB zip 解出几十 GB"。
        for member in names:
            info = zf.getinfo(member)
            if info.file_size > _SNAPSHOT_MEMBER_LIMITS.get(member, 0):
                raise ValueError(
                    f"snapshot member {member} is too large "
                    f"({info.file_size} bytes > limit {_SNAPSHOT_MEMBER_LIMITS.get(member)})"
                )
            if info.compress_size > 0 and info.file_size / info.compress_size > 1000:
                raise ValueError(
                    f"snapshot member {member} has an implausible compression ratio "
                    f"({info.file_size}/{info.compress_size}); refusing to decompress"
                )
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"snapshot manifest is corrupt: {exc}") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != _SNAPSHOT_FORMAT
            or manifest.get("format_version") != _SNAPSHOT_VERSION
        ):
            raise ValueError(
                f"not a {_SNAPSHOT_FORMAT} v{_SNAPSHOT_VERSION} archive; got "
                f"format={manifest.get('format')!r} version={manifest.get('format_version')!r}"
            )

        local_vectors_meta = owner._vectors_meta()
        snapshot_vectors_meta = manifest.get("vectors_meta") or {}
        vectors_member = "vectors.sqlite" if "vectors.sqlite" in names else ("vectors.bin" if "vectors.bin" in names else None)
        skip_vectors = vectors_member is None
        warnings: list[str] = []

        # 分块参数（chunk_size / chunk_overlap / chunker 代际 / 图片注入）不匹配时，
        # chunk.id（sha1 of source+index+content）对不上，导入的向量一条都挂不上，
        # 等于白导——而且 vectors.bin 的 meta 导入时会被本机 meta 重写，事后看不出
        # 原因。此前 manifest 里导出的 chunks_meta 从不比对。
        # 注意只比切块相关字段，不比 cache_key：cache_key 含 vault 路径，换机迁移
        # 时必然不同，拿它当判据会把正当的「免重嵌迁移」也拒掉。
        def _chunking_fingerprint(meta: dict[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in (meta or {}).items() if key != "key"}

        snapshot_chunks_meta = manifest.get("chunks_meta")
        # 只在 manifest 确实带 chunks_meta（0.5.0+ 快照）时才做比对：
        # 老格式快照没有该字段，直接拒掉会给出误导性的"参数不匹配"报错。
        chunks_mismatch = isinstance(snapshot_chunks_meta, dict) and _chunking_fingerprint(
            snapshot_chunks_meta
        ) != _chunking_fingerprint(owner._chunks_meta())
        if chunks_mismatch and not force:
            raise ValueError(
                "snapshot was chunked with different parameters than this machine "
                "(chunk_size / chunker / inject_image_captions mismatch); pass force=true "
                "to import anyway (next sync will re-chunk and re-embed)"
            )

        # 模型/维度校验对两个向量成员一视同仁。此前只查 vectors.bin，vectors.sqlite
        # 整文件替换、零校验——错误维度的向量装进去后 numpy 路径抛错被吞，
        # 标量 _cosine 用 min(len) 截断，返回"看起来很像回事"的垃圾相似度。
        model_mismatch = (
            snapshot_vectors_meta.get("embedding_model") != local_vectors_meta["embedding_model"]
            or snapshot_vectors_meta.get("dimension") != local_vectors_meta["dimension"]
        )
        if vectors_member is not None and model_mismatch:
            if not force:
                raise ValueError(
                    "snapshot vectors were built with model="
                    f"{snapshot_vectors_meta.get('embedding_model')!r} dimension={snapshot_vectors_meta.get('dimension')!r} "
                    "but this config uses model="
                    f"{local_vectors_meta['embedding_model']!r} dimension={local_vectors_meta['dimension']!r}; "
                    "pass force=true to import the text layer only and re-embed"
                )
            skip_vectors = True
            warnings.append(
                "vectors skipped: snapshot model/dimension differs from this config"
            )
        if chunks_mismatch and force:
            warnings.append(
                "chunking parameters differ from this machine; imported vectors may not "
                "match chunk ids and will be re-embedded"
            )

        # 锁序必须与 sync() 一致（_sync_lock → _cache_lock）。此前这里是
        # _cache_lock → _sync_lock 的反向嵌套，kb_import 撞上 watcher 的
        # 30s 对账 sync 就是 ABBA 死锁：两个线程永久互等，之后所有
        # kb_search / kb_list_files 排队在 _sync_lock 上，整个 MCP 服务冻结。
        # 导入体本身会在锁内从磁盘重载全部状态，无需外层再持 _cache_lock。
        with owner._sync_lock:
            return _import_snapshot_locked(
                owner, src, zf, vectors_member, skip_vectors, warnings
            )


def _import_snapshot_locked(
    owner: MarkdownIndexer,
    src: Path,
    zf: zipfile.ZipFile,
    vectors_member: str | None,
    skip_vectors: bool,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    # 磁盘记账集必须清空：否则旧模型的 id 还在集合里，导入后凡是命中的
    # chunk 都被判为"已嵌入"永不重嵌（而 vec 库里躺的是旧语料的向量）。
    owner._disk_vectors.clear()

    # 1) 文本层：解码 -> 按本机 meta 重写 -> 原子落地。
    chunk_count, file_count = _import_chunks_member(owner, zf)

    # 2) 向量层：.bin 重写 meta；sqlite 原样搬运（关连接 -> 换文件 -> 重开）。
    vector_count: int | None = None
    if vectors_member is not None and not skip_vectors:
        if vectors_member == "vectors.bin":
            vector_count = _import_vectors_bin_member(owner, zf)
        else:
            vector_count = _import_vectors_sqlite_member(owner, zf)

    # 3) FTS：能搬就搬；无论搬没搬，都按导入后的 chunk 集对账一次。
    if "fts.sqlite" in zf.namelist() and owner._fts_cache_path is not None:
        _replace_live_file(owner, owner._fts_cache_path, zf.read("fts.sqlite"), close_fts=True)

    # 4) 用导入后的缓存文件重建内存态（向量按 id 挂回 chunk）。
    #    注意 FTS 对账必须放在 _chunks.clear() 与 _load_chunks_cache() 之后：
    #    此前先执行，用的是导入前的旧 chunk 集，会把旧语料全部 upsert 进
    #    刚导入的 FTS 库——两套语料混在一起，且后续 sync 因签名未变永不修复。
    owner._chunks.clear()
    owner._signatures.clear()
    owner._pending_vectors.clear()
    owner.failed_files.clear()
    owner._load_chunks_cache()
    owner._load_failed_files()
    if not owner._vectors_on_disk:
        owner._load_vectors_cache()
    _recreate_fts(owner)
    owner._fts_ensure_populated()

    return {
        "imported": True,
        "path": str(src),
        "files": len(owner._chunks),
        "chunks": chunk_count,
        "file_count": file_count,
        "vectors": vector_count,
        "vectors_imported": vectors_member is not None and not skip_vectors,
        "backend": getattr(owner._vector_backend, "name", owner.config.vector.backend),
        "warnings": warnings or [],
    }


def _import_chunks_member(owner: MarkdownIndexer, zf: zipfile.ZipFile) -> tuple[int, int]:
    loaded = _decode_member(owner, zf, "chunks.bin", _CacheCodec.load)
    if loaded is None:
        raise ValueError("snapshot chunks.bin is corrupt")
    meta, files = loaded
    # chunks 层必须无向量（向量只属于向量层）；导入时不信任包内数据，统一剥离。
    clean: dict[str, tuple[str, list[Chunk]]] = {
        source: (signature, [owner._strip_embedding(chunk) for chunk in chunks])
        for source, (signature, chunks) in files.items()
    }
    _CacheCodec.dump(owner._chunks_cache_path, owner._chunks_meta(), clean)  # type: ignore[arg-type]
    total = sum(len(chunks) for _, chunks in clean.values())
    return total, len(clean)


def _import_vectors_bin_member(owner: MarkdownIndexer, zf: zipfile.ZipFile) -> int:
    loaded = _decode_member(owner, zf, "vectors.bin", _VectorsCodec.load)
    if loaded is None:
        raise ValueError("snapshot vectors.bin is corrupt")
    meta, vectors = loaded
    _VectorsCodec.dump(owner._vectors_cache_path, owner._vectors_meta(), vectors)  # type: ignore[arg-type]
    return len(vectors)


def _import_vectors_sqlite_member(owner: MarkdownIndexer, zf: zipfile.ZipFile) -> int:
    if owner._vectors_db_path is None:
        raise ValueError("sqlite_vec vector store is unavailable for this vault")
    closer = getattr(owner._vector_backend, "close", None)
    if closer is not None:
        closer()
    # try/finally 是必须的：close 之后 owner._vector_backend 就是一个被关闭
    # 的对象，任何一步失败（磁盘满、文件被占用、sqlite_vec 不可用）都会
    # 让它永久停在"已关闭"状态 —— query() 恒返回 []、upsert 全部 no-op，
    # 而 _flush_vectors_to_disk 还在盲记账，语义检索静默归零且不可自愈。
    try:
        _replace_live_file(owner, owner._vectors_db_path, zf.read("vectors.sqlite"), close_fts=False)
        backend = create_vector_backend(owner.config.vector, owner, owner._vectors_db_path)
        if not getattr(backend, "available", False):
            raise ValueError("sqlite_vec could not open the imported vector store")
    except Exception:
        # 失败即回滚：把旧 backend 重新打开（旧文件可能已被替换，能开成
        # 什么样算什么样——至少不能留一个"已关闭"的对象给后续所有调用）。
        try:
            owner._vector_backend = create_vector_backend(owner.config.vector, owner, owner._vectors_db_path)
            owner._vectors_on_disk = bool(getattr(owner._vector_backend, "on_disk", False))
            owner._disk_vectors = set()
        except Exception:
            pass
        raise
    # 重开后由导入的数据接管；磁盘记账集在下一次 sync 的
    # _ensure_disk_vectors_migrated 里按库内实际 id 重建。
    owner._vector_backend = backend
    owner._vectors_on_disk = bool(getattr(backend, "on_disk", False))
    owner._disk_vectors = set()
    return len(backend.list_ids())


def _decode_member(owner: MarkdownIndexer, zf: zipfile.ZipFile, member: str, loader: Callable[[Path], Any]) -> Any:
    """把 zip 成员写到缓存目录的临时文件后用既有 loader 解码。

    _CacheCodec / _VectorsCodec 只认 Path，而它们真正的校验对象（meta）要
    在解码之后由导入逻辑重写，所以这里只负责把字节安全地交给 loader。
    """
    scratch_dir = (owner._chunks_cache_path or owner._vectors_cache_path).parent  # type: ignore[union-attr]
    tmp = scratch_dir / (member + ".importing")
    try:
        tmp.write_bytes(zf.read(member))
        return loader(tmp)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _replace_live_file(owner: MarkdownIndexer, target: Path, payload: bytes, *, close_fts: bool) -> None:
    """原子替换一个可能正被本实例打开的缓存文件（sqlite）。"""
    if close_fts and owner._fts is not None:
        try:
            owner._fts.close()
        except Exception:
            pass
        owner._fts = None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".importing")
    tmp.write_bytes(payload)
    tmp.replace(target)


def _recreate_fts(owner: MarkdownIndexer) -> None:
    if owner._fts is not None:
        try:
            owner._fts.close()
        except Exception:
            pass
        owner._fts = None
    if owner.config.use_hybrid and owner._fts_cache_path is not None:
        try:
            fts = FtsIndex(owner._fts_cache_path)
            owner._fts = fts if fts.available else None
        except Exception:
            owner._fts = None
