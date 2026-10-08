from __future__ import annotations

import io
import hashlib
import math
import mmap
import tempfile
import shutil
import json
import re
import sqlite3
from dataclasses import replace
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
_SNAPSHOT_VERSION = 2
_SNAPSHOT_MEMBERS = frozenset({"manifest.json", "chunks.bin", "vectors.bin", "vectors.sqlite", "fts.sqlite", "docstore.sqlite"})
_V2_MEMBERS = frozenset({"manifest.json", "chunks.bin", "vectors.bin", "docstore.sqlite"})
_MB = 1024 * 1024
# 各成员的字节上限：正常库的缓存不会超过这些量级，超限 = 恶意构造。
# 上限即「单次导入的驻留内存/磁盘上限」——取 1~2GB 而不是 GB 级的两位数，
# 否则一个 16MB 的 zip（声明 16GB、比率 993 仍过闸）就能把服务吃光内存：
# 声明值本身是攻击者可控字段，真正兜底的是这个量级 + 流式读取预算。
_SNAPSHOT_MEMBER_LIMITS = {
    "manifest.json": 1 * _MB,
    "docstore.sqlite": 2048 * _MB,
    "chunks.bin": 1024 * _MB,
    "vectors.bin": 1024 * _MB,
    "vectors.sqlite": 2048 * _MB,
    "fts.sqlite": 2048 * _MB,
}
# 合计预算：5 个成员各自卡在上限仍能凑出两位数 GB。§20.2：在既有 4GiB 基础上
# **显式版本化到 6GiB**；导出与导入共用同一常量，manifest 记录预算版本，避免
# 两侧各写一套数字（此前导出写死 6144、导入写死 4096）。
_SNAPSHOT_TOTAL_BUDGET_MB = 6144
_SNAPSHOT_BUDGET_VERSION = "6gib-v1"
_SNAPSHOT_TOTAL_LIMIT = _SNAPSHOT_TOTAL_BUDGET_MB * _MB
# 快照成员是固定白名单（≤6 个），成员数与中央目录元数据上限按快照口径收紧：
# 中央目录准入在构造 ZipFile 之前完成（§20.7C）。
_SNAPSHOT_MAX_MEMBERS = 64
_SNAPSHOT_CD_METADATA_BYTES = 2 * _MB
# zip 头里的 file_size/compress_size 同属攻击者可控：比率门限只挡「小包大解」。
# 真实缓存里 sqlite 约 2~5x、.bin 自身已 zlib 压缩（≈1x），100 已足够宽松。
_MAX_COMPRESSION_RATIO = 100
_MEMBER_READ_BLOCK = 1 * _MB


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
    return _export_v2(owner, out_path)


def import_snapshot(owner: MarkdownIndexer, snapshot: str | Path, force: bool = False,
                    trust_parsed_documents: bool = False, replace: bool = False,
                    confirm_replace: bool = False) -> dict[str, Any]:
    """从快照恢复索引缓存；随后一次 sync 应当 0 次 embedding API 调用。

    安全与兼容：

    * zip 成员按**白名单**精确匹配，多余的成员（含 ../ 穿越名）直接拒绝；
      落地路径全部来自本机缓存配置，从不使用压缩包内的名字拼路径。
    * .bin 里的 meta 含源机器的 cache key，导入时用本机 _chunks_meta() /
      _vectors_meta() 重写后再落地。
    * 向量层的 model/dimension 与本机配置不一致时拒绝导入（force=true 可
      强制，但此时只导入文本层，向量作废由本地重新 embedding——错维度的
      向量对检索是毒药）。
    * manifest 是**声明**，payload 才是事实：两个向量成员都按实际内容复验维度
      （.bin 逐条长度、.sqlite 读 vec0 表的 float[N]），不匹配直接拒绝。
    * 包内 chunk 的 signature 一律丢弃：本机 sync 的"内容未变"判据就是它，
      信任它等于让投毒正文躲过重读。代价是导入后首次 sync 重读全部文件重算
      签名（内容未变的切片仍按 content_hash 复用快照向量，不产生 embedding）。
    * 成员读取按真实字节数记账：zip 头声明的尺寸是攻击者可控字段，不能当上限。
    * 构造 ZipFile **之前**先做 EOCD/中央目录准入（§20.7C）：ZIP64/多 disk/
      声明与真实条目不符/成员数与目录元数据超限一律在分配前拒绝。
    * 未知来源包默认不 merge、不静默替换本机事实；显式 `replace=True` 且
      `confirm_replace=True` 才覆盖，被替换 generation 登记为 retained_backup。

    前提：本库缓存已启用、目录已注册（先 kb_init 再 kb_import）。
    """
    for key, value in (("force", force), ("trust_parsed_documents", trust_parsed_documents),
                       ("replace", replace), ("confirm_replace", confirm_replace)):
        if type(value) is not bool:
            raise ValueError(f"{key} must be bool")
    if replace and not confirm_replace:
        raise ValueError("replace=true requires explicit confirm_replace=true")
    if owner._chunks_cache_path is None:
        raise ValueError("cache is disabled; enable [cache] before importing a snapshot")
    src = Path(snapshot).expanduser()
    if not src.is_file():
        raise ValueError(f"snapshot not found: {src}")

    declared_members = _preflight_zip(src)
    with zipfile.ZipFile(src) as zf:
        if len(zf.namelist()) != declared_members:
            raise ValueError(
                f"snapshot central directory declares {declared_members} members but "
                f"ZipFile sees {len(zf.namelist())}"
            )
        names = set(zf.namelist())
        if len(names) != len(zf.namelist()):
            raise ValueError("snapshot contains duplicate members")
        version_hint = None
        try:
            version_hint = json.loads(_read_member_bytes(zf, "manifest.json")).get("format_version")
        except Exception:
            pass
        allowed = _V2_MEMBERS if version_hint == 2 else frozenset({"manifest.json", "chunks.bin", "vectors.bin", "vectors.sqlite", "fts.sqlite"})
        unknown = names - allowed
        if unknown:
            raise ValueError(f"snapshot contains unexpected members: {sorted(unknown)}")
        if "manifest.json" not in names or "chunks.bin" not in names:
            raise ValueError("snapshot is missing manifest.json or chunks.bin")
        # 解压炸弹防护：快照的用途就是从别人机器收文件，任何一个成员都可以
        # 是恶意构造的。白名单挡得住路径穿越，挡不住"1MB zip 解出几十 GB"。
        declared_total = 0
        for member in names:
            info = zf.getinfo(member)
            limit = _SNAPSHOT_MEMBER_LIMITS.get(member, 0)
            if info.file_size > limit:
                raise ValueError(
                    f"snapshot member {member} is too large "
                    f"({info.file_size} bytes > limit {limit})"
                )
            if info.compress_size > 0 and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
                raise ValueError(
                    f"snapshot member {member} has an implausible compression ratio "
                    f"({info.file_size}/{info.compress_size}); refusing to decompress"
                )
            declared_total += info.file_size
        if declared_total > _SNAPSHOT_TOTAL_LIMIT:
            raise ValueError(
                f"snapshot declares {declared_total} bytes across members, "
                f"above the {_SNAPSHOT_TOTAL_LIMIT} byte budget"
            )
        try:
            manifest = json.loads(_read_member_bytes(zf, "manifest.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"snapshot manifest is corrupt: {exc}") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != _SNAPSHOT_FORMAT
            or manifest.get("format_version") not in (1, 2)
        ):
            raise ValueError(
                f"not a {_SNAPSHOT_FORMAT} v{_SNAPSHOT_VERSION} archive; got "
                f"format={manifest.get('format')!r} version={manifest.get('format_version')!r}"
            )

        if manifest["format_version"] == 2:
            return _import_v2(owner, src, zf, manifest, force, trust_parsed_documents,
                              replace=replace, confirm_replace=confirm_replace)

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
            result = _import_snapshot_locked(
                owner, src, zf, vectors_member, skip_vectors, warnings
            )
        # 释放 mutation 之后才登记刷新（E04-a）。
        _register_post_import_refresh(owner, result)
        return result


def _register_post_import_refresh(owner: MarkdownIndexer, result: dict[str, Any]) -> None:
    """导入**释放 mutation 锁之后**登记一次刷新（E04-a）。

    导入不发布包内正文（§20.7F），文本层为空；此前导入路径从不登记刷新，索引
    会停在「空且无待办」的状态，只有下次外部触发才重建。这里只登记 pending、
    不拉起后台调度线程（避免为"登记"而启动一次后台 sync）。
    """
    from .watch import request_refresh

    try:
        accepted = bool(request_refresh(owner, immediate=True, start_scheduler=False))
    except Exception:
        accepted = False
    result["refresh_requested"] = accepted


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_MEMBER_READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _preflight_zip(src: Path) -> int:
    """构造 `ZipFile` **之前**的 EOCD/中央目录准入（§20.7C）。

    复用 mineru 的只读准入（不改 mineru.py）：拒 ZIP64/多 disk/声明≠真实/成员数/
    目录元数据超限。用 mmap 而非整文件读入内存——快照可达 GiB 级，不能为了一次
    准入把整个归档驻留内存。返回声明的成员数供构造后核对。
    """
    from ..ingest.mineru import MineruError, _inspect_central_directory
    from ..ingest.models import ResourceLimits

    if src.stat().st_size <= 0:
        raise ValueError(f"snapshot is empty: {src}")
    limits = ResourceLimits(
        max_members=_SNAPSHOT_MAX_MEMBERS,
        cd_metadata_max_bytes=_SNAPSHOT_CD_METADATA_BYTES,
    )
    try:
        with src.open("rb") as stream:
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                return _inspect_central_directory(mapped, limits)
    except MineruError as exc:
        raise ValueError(f"snapshot archive rejected before reading: {exc}") from exc


def _require_free_disk(directory: Path, declared_total: int) -> None:
    """staging 前的本机磁盘余量预检（§20.2）：解压成员 + 派生物重建留 25% 余量。"""
    if declared_total <= 0:
        return
    try:
        usage = shutil.disk_usage(directory)
    except OSError:
        return
    needed = int(declared_total * 1.25)
    if usage.free < needed:
        raise ValueError(
            f"insufficient local disk to stage snapshot: need ~{needed} bytes, "
            f"only {usage.free} free under {directory}"
        )


def _export_v2(owner: MarkdownIndexer, out_path: str | Path) -> dict[str, Any]:
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=out.parent) as temporary:
        root = Path(temporary)
        payloads = {"chunks.bin": owner._chunks_cache_path}
        vectors = owner._vector_backend.get_vectors(chunk.id for chunk in owner.all_chunks())
        if vectors:
            transport = root / "vectors.bin"
            _VectorsCodec.dump(transport, owner._vectors_meta(), vectors)
            payloads["vectors.bin"] = transport
        store = owner._existing_document_store()
        store_manifest = None
        if store is not None and store.generation_id:
            with store.pin_generation():
                document = root / "docstore.sqlite"
                store.backup_to(document)
                # Portable backups contain facts, never live task credentials.
                conn = sqlite3.connect(str(document))
                try:
                    conn.execute("DELETE FROM ingest_jobs")
                    conn.execute("DELETE FROM ingest_subjobs")
                    conn.commit()
                    meta = conn.execute("SELECT schema_version, change_seq, store_uuid FROM store_meta WHERE id=1").fetchone()
                    store_manifest = {"schema_version": meta[0], "change_seq": meta[1], "store_uuid": meta[2]}
                finally:
                    conn.close()
                payloads["docstore.sqlite"] = document
        members = {}
        for name, path in payloads.items():
            size = path.stat().st_size
            if size > _SNAPSHOT_MEMBER_LIMITS[name]:
                raise ValueError(f"snapshot {name} exceeds export budget")
            members[name] = {"size": size, "sha256": _sha_file(path)}
        if sum(info["size"] for info in members.values()) > _SNAPSHOT_TOTAL_LIMIT:
            raise ValueError("snapshot exceeds total export budget")
        manifest = {"format": _SNAPSHOT_FORMAT, "format_version": 2,
                    "chunks_meta": owner._chunks_meta(), "vectors_meta": owner._vectors_meta(),
                    "members": members, "docstore": store_manifest,
                    "total_budget_bytes": _SNAPSHOT_TOTAL_LIMIT,
                    "budget_version": _SNAPSHOT_BUDGET_VERSION,
                    "derived_status": "requires_local_source_verification",
                    "data_classification": ["parsed_text", "media_originals_and_audio"],
                    "stats": {"files": len(owner._chunks), "chunks": len(owner.all_chunks()), "vectors": len(vectors)}}
        staged = root / "archive.zip"
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for name, path in payloads.items():
                archive.write(path, name)
        staged.replace(out)
    return {"exported": True, "path": str(out), "format_version": 2,
            "contains_media": store_manifest is not None, **manifest["stats"]}


#: 活跃摄取任务：存在即默认拒绝导入（§20.7F），避免覆盖未完成资产。
_ACTIVE_JOB_STATES = frozenset({"queued", "parsing", "staged"})
_ACTIVE_JOB_PHASES = frozenset(
    {"send_intent", "submitted", "polling", "downloaded", "submission_unknown"}
)


def _require_no_active_ingest(store: Any) -> None:
    """存在活跃摄取任务 → IMPORT_BUSY，默认不导入（§20.7F）。只走公开读 API。"""
    from ..doc_store import StoreBusy

    try:
        jobs = store.list_jobs(limit=500)
    except Exception as exc:
        # 读不到队列 ≠ 没有队列：控制库被锁/损坏时旧实现直接当作「无活跃任务」放行，
        # 属安全门禁 fail-open。导入随后要打开同一个库，所以这里坚持 fail closed。
        raise StoreBusy(
            "IMPORT_BUSY: cannot read this vault's ingest queue; refusing to import while the "
            "queue state is unknown",
            fix="先确认控制库可读（或修复/恢复控制库）再导入。",
        ) from exc
    for job in jobs:
        state = str(getattr(job, "state", "") or "")
        phase = str(getattr(job, "phase", "") or "")
        if state in _ACTIVE_JOB_STATES or phase in _ACTIVE_JOB_PHASES:
            raise StoreBusy(
                "IMPORT_BUSY: this vault has active ingestion tasks; refusing to import by "
                "default so unfinished assets are not overwritten",
                fix="等待任务终结或人工确认损失清单后再导入。",
            )


def _active_generation_id(store: Any) -> str | None:
    """新鲜读取控制面当前活动 generation；**读不到返回 None**。

    `DocumentStore.generation_id` 是惰性缓存字段，异常态下可能是空串或旧值，
    不能当作「当前活动代」。调用方必须把 None 当「未知」处理，不得据此猜。
    """
    from ..doc_store import ControlStore

    ctrl = ControlStore(store.layout)
    try:
        ctrl.open(create=False, write=False)
        return str(ctrl.state().active_document_generation or "")
    except Exception:
        return None
    finally:
        ctrl.close()


def _register_generation_state(store: Any, generation_id: str, state: str) -> None:
    """改写 generation 注册状态（`retained_backup` / `aborted`）。"""
    if not generation_id:
        return
    from ..doc_store import ControlStore

    ctrl = ControlStore(store.layout)
    try:
        ctrl.open(create=False, write=True)
        ctrl.register_generation(
            generation_id, store.layout.generations_dir / generation_id, "document", state
        )
    finally:
        ctrl.close()


def _live_derived_paths(owner: MarkdownIndexer) -> list[Path]:
    paths: list[Path] = []
    for candidate in (
        owner._chunks_cache_path,
        owner._vectors_cache_path,
        owner._vectors_db_path,
        owner._fts_cache_path,
    ):
        if candidate is not None:
            paths.append(Path(candidate))
    return paths


def _backup_live_derived(owner: MarkdownIndexer, root: Path) -> list[dict[str, Any]]:
    """发布前把会被覆盖的活派生文件复制到临时目录，失败时据此回滚。"""
    saved_dir = root / "rollback"
    saved_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for index, path in enumerate(_live_derived_paths(owner)):
        entry: dict[str, Any] = {
            "path": path, "existed": path.exists(), "saved": saved_dir / f"{index}_{path.name}"
        }
        if entry["existed"]:
            try:
                shutil.copy2(path, entry["saved"])
            except OSError:
                entry["existed"] = False
        entries.append(entry)
    return entries


def _restore_live_derived(owner: MarkdownIndexer, entries: list[dict[str, Any]]) -> None:
    """回滚：把活派生文件恢复到导入前内容，并让内存态与磁盘一致（§20.2/§20.5）。"""
    for entry in entries:
        path: Path = entry["path"]
        try:
            if entry["existed"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry["saved"], path)
            else:
                _unlink_quietly(path)
        except OSError:
            pass
    if owner._fts is not None:
        try:
            owner._fts.close()
        except Exception:
            pass
        owner._fts = None
    owner._chunks.clear()
    owner._signatures.clear()
    owner._pending_vectors.clear()
    owner._disk_vectors.clear()
    owner._load_chunks_cache()
    owner._load_failed_files()
    if not owner._vectors_on_disk:
        owner._load_vectors_cache()
    else:
        try:
            owner._vector_backend = create_vector_backend(
                owner.config.vector, owner, owner._vectors_db_path
            )
            owner._vectors_on_disk = bool(getattr(owner._vector_backend, "on_disk", False))
        except Exception:
            pass
    _recreate_fts(owner)
    owner._fts_ensure_populated()


def _stage_derived_layers(owner: MarkdownIndexer, text_target: Path, vectors: dict,
                          compatible: bool) -> None:
    """把已写好的临时派生层原子替换进活文件；调用方负责失败回滚。"""
    owner._pending_vectors.clear()
    if compatible:
        owner._pending_vectors.update(vectors)
    owner._disk_vectors.clear()
    text_target.replace(owner._chunks_cache_path)
    owner._chunks.clear()
    owner._signatures.clear()
    if owner._fts is not None:
        owner._fts.close()
        owner._fts = None
    _recreate_fts(owner)
    owner._fts_ensure_populated()
    if owner._vectors_on_disk and compatible:
        owner._vector_backend.delete_vectors(owner._vector_backend.list_ids())
        owner._vector_backend.upsert_vectors(vectors)
    elif owner._vectors_cache_path is not None:
        _VectorsCodec.dump(owner._vectors_cache_path, owner._vectors_meta(), vectors if compatible else {})


def _import_v2(owner: MarkdownIndexer, src: Path, archive: zipfile.ZipFile,
               manifest: dict, force: bool, trusted: bool, *,
               replace: bool = False, confirm_replace: bool = False) -> dict[str, Any]:
    from ..doc_store import StoreConflict, normalize_source_path
    declarations = manifest.get("members")
    names = set(archive.namelist()) - {"manifest.json"}
    if not isinstance(declarations, dict) or set(declarations) != names:
        raise ValueError("snapshot member declarations do not match payloads")
    parent = owner._chunks_cache_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    declared_total = sum(
        int(info["size"]) for info in declarations.values()
        if isinstance(info, dict) and isinstance(info.get("size"), int)
    )
    _require_free_disk(parent, declared_total)

    # 只读打开本机库：无 active generation 时**不会**创建（item 1 的零状态变化）。
    existing = owner._existing_document_store()
    if existing is not None:
        _require_no_active_ingest(existing)

    with tempfile.TemporaryDirectory(prefix="import-", dir=parent) as temporary:
        root = Path(temporary)
        # ---- 1) 校验与 staging 全部在临时目录完成，不改任何活动状态 ----------
        for name in names:
            path = root / name
            size = _stream_member(archive, name, path)
            declared = declarations[name]
            if not isinstance(declared, dict) or declared.get("size") != size or declared.get("sha256") != _sha_file(path):
                raise ValueError(f"snapshot {name} checksum/size mismatch")
        loaded = _CacheCodec.load(root / "chunks.bin")
        if loaded is None:
            raise ValueError("snapshot chunks.bin is corrupt")
        _meta, files = loaded
        chunks_mismatch = ({k: v for k, v in manifest.get("chunks_meta", {}).items() if k != "key"} !=
                           {k: v for k, v in owner._chunks_meta().items() if k != "key"})
        vectors_mismatch = ({k: v for k, v in manifest.get("vectors_meta", {}).items() if k != "key"} !=
                            {k: v for k, v in owner._vectors_meta().items() if k != "key"})
        compatible = not (chunks_mismatch or vectors_mismatch)
        if not compatible and not force:
            if chunks_mismatch:
                raise ValueError(
                    "snapshot was chunked with different parameters than this machine "
                    "(chunk_size / chunker / inject_image_captions mismatch); pass force=true "
                    "to import anyway (next sync will re-chunk and re-embed)"
                )
            raise ValueError(
                "snapshot vectors were built with a different profile than this machine; "
                "pass force=true to import the text layer only and re-embed"
            )
        vectors = {}
        if "vectors.bin" in names:
            decoded = _VectorsCodec.load(root / "vectors.bin")
            if decoded is None:
                raise ValueError("snapshot vectors.bin is corrupt")
            vectors = decoded[1]
            dimension = manifest.get("vectors_meta", {}).get("dimension")
            chunk_ids = {chunk.id for _signature, chunks in files.values() for chunk in chunks}
            for key, vector in vectors.items():
                if key not in chunk_ids or vector is None or len(vector) != dimension or not all(math.isfinite(x) for x in vector):
                    raise ValueError("snapshot vector key/numeric/dimension contract mismatch")
        # 包内正文一律**不发布**（与 v1 同口径，§20.7F）：快照来自不可信来源，把包内
        # chunk 写进活动 chunks cache 等于让投毒正文在下一次本机物理核验之前就能被检索。
        # 旧实现只在**当前进程**清空 `_chunks`（所以"导入后立即可见性为假"的断言能过），
        # 但重启或第二个会话调用 `_load_chunks_cache()` 会把包内正文原样载回索引。
        # 这里仍逐条校验 source 合法性与 chunk 结构（畸形 metadata 会拖垮下游硬索引），
        # 只用于计数、不落盘；向量仍按 chunk.id 进补挂池，下次 sync 重建可信文本时复用。
        packaged_files = 0
        packaged_chunks = 0
        for source, (_signature, chunks) in files.items():
            normalize_source_path(source, owner.vault_path)
            for chunk in chunks:
                _sanitize_chunk(owner, chunk)
            packaged_files += 1
            packaged_chunks += len(chunks)
        text_target = root / "verified-chunks.bin"
        _CacheCodec.dump(text_target, owner._chunks_meta(), {})

        # ---- 2) docstore staging（仍不切 active） -----------------------------
        store = None
        prepared = None
        change_seq_before = 0
        if "docstore.sqlite" in names:
            committed = bool(existing is not None and existing.list_documents())
            if committed and not (replace and confirm_replace):
                raise StoreConflict(
                    "IMPORT_CONFLICT: this vault already holds committed parsed documents; "
                    "refusing to merge an unknown snapshot. Pass replace=true and "
                    "confirm_replace=true to replace them (the replaced generation is kept "
                    "as a recoverable retained_backup).",
                    fix="先展示损失清单，再显式 replace+confirm_replace；默认不静默替换最新事实。",
                )
            store = owner.document_store(write=False)
            change_seq_before = store.change_seq()
            prepared = store.prepare_import(root / "docstore.sqlite", trust_parsed_documents=trusted)

        # ---- 3) 发布阶段：锁内再校验；派生层先落地，最后 publish ---------------
        with owner._sync_lock:
            if store is not None:
                _require_no_active_ingest(store)
                if store.change_seq() != change_seq_before:
                    raise StoreConflict(
                        "IMPORT_CONFLICT: document store changed during import validation; "
                        "nothing was published and existing assets remain readable"
                    )
            backups = _backup_live_derived(owner, root)
            published = False
            try:
                _stage_derived_layers(owner, text_target, vectors, compatible)
                if prepared is not None:
                    prev_generation = str(prepared.get("expected_generation") or "")
                    store.publish_import(prepared, snapshot_sha256=_sha_file(src), trusted=trusted)
                    published = True
                    _register_generation_state(store, prev_generation, "retained_backup")
            except BaseException:
                _restore_live_derived(owner, backups)
                if prepared is not None:
                    prev_generation = str(prepared.get("expected_generation") or "")
                    imported_generation = str(prepared.get("generation_id") or "")
                    active_now = _active_generation_id(store)
                    # `published` 只在 `publish_import` **返回**后为真：控制面 CAS 已经
                    # 提交、随后重新打开失败的窗口里它为假，但活动代其实已经切过去了。
                    # 用一次新鲜控制面读重判，否则派生层回滚了、docstore 却留在导入代。
                    actually_published = published or (
                        active_now is not None and imported_generation != ""
                        and active_now == imported_generation
                    )
                    active_after = active_now
                    if actually_published and prev_generation:
                        try:
                            store.restore_generation(prev_generation)
                            active_after = prev_generation
                        except Exception:
                            active_after = _active_generation_id(store)
                    # 没有成为活动事实的 staged generation 必须标成 aborted：否则它永远
                    # 停在 'validated'（看起来像一份可用快照），而且当前没有任何回收
                    # 路径 —— 每次失败都静默留下这份拷贝。状态登记失败不得掩盖原始异常。
                    #
                    # 只给**能确认不再是活动代**的 generation 打标：读不到活动代（None）
                    # 时不猜，避免把活动事实标成 aborted（标签侧 fail closed）。
                    if (imported_generation and active_after is not None
                            and active_after != imported_generation):
                        try:
                            _register_generation_state(store, imported_generation, "aborted")
                        except Exception:
                            pass
                raise
        result = {"imported": True, "path": str(src), "format_version": 2,
                  "files": 0, "chunks": 0,
                  "packaged_files": packaged_files, "packaged_chunks": packaged_chunks,
                  "text_published": False,
                  "vectors": len(vectors) if compatible else 0, "vectors_imported": compatible,
                  "parsed_documents_trusted": trusted, "replaced": bool(published and replace),
                  "warnings": ["local source reconciliation required"]}
        # 释放 mutation 之后才登记刷新（E04-a）。
        _register_post_import_refresh(owner, result)
        return result


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
    # 补挂池同样先清空——它是给"文本层将被重建，但向量还能按 id 复用"用的；
    # 放在这里清而不是放在后面，是为了让文本层的向量导入（第 2 步）能把
    # 导入的向量放进池里（见 _import_vectors_bin_member）。
    owner._pending_vectors.clear()

    # 1) 文本层（v1）：**只解码校验，不发布**（§20.7F）。包内正文来自不可信快照，
    #    导入即公开等于先公开毒正文再等后台重读——这里一律不落 _chunks/FTS。
    chunk_count, file_count = _import_chunks_member(owner, zf)

    # 2) 向量层：.bin 重写 meta；sqlite 原样搬运（关连接 -> 换文件 -> 重开）。
    #    这一层只落"可复用"状态：向量按 chunk.id 进补挂池，下一次 sync 重建文本后复用。
    vector_count: int | None = None
    if vectors_member is not None and not skip_vectors:
        if vectors_member == "vectors.bin":
            vector_count = _import_vectors_bin_member(owner, zf)
        else:
            vector_count = _import_vectors_sqlite_member(owner, zf)

    # 3) 文本层与 FTS 一律置空：包内 fts.sqlite（v1 允许）不安装，旧 FTS 也丢弃，
    #    由下一次 sync 从本机物理源重读重建可信 chunks。chunks cache 写空，
    #    保证重启后不会把包内投毒正文重新载回索引。
    owner._chunks.clear()
    owner._signatures.clear()
    owner.failed_files.clear()
    if owner._chunks_cache_path is not None:
        _CacheCodec.dump(owner._chunks_cache_path, owner._chunks_meta(), {})  # type: ignore[arg-type]
    if owner._fts_cache_path is not None:
        _unlink_quietly(owner._fts_cache_path)
    if not owner._vectors_on_disk:
        owner._load_vectors_cache()
    _recreate_fts(owner)
    owner._fts_ensure_populated()

    return {
        "imported": True,
        "path": str(src),
        "files": len(owner._chunks),
        "chunks": 0,
        "packaged_chunks": chunk_count,
        "file_count": 0,
        "packaged_files": file_count,
        "vectors": vector_count,
        "vectors_imported": vectors_member is not None and not skip_vectors,
        "text_published": False,
        "backend": getattr(owner._vector_backend, "name", owner.config.vector.backend),
        "warnings": (warnings or []) + [
            "snapshot text isolated; re-run sync to rebuild trusted chunks from local sources"
        ],
    }


def _import_chunks_member(owner: MarkdownIndexer, zf: zipfile.ZipFile) -> tuple[int, int]:
    """解码校验包内文本层，但**不落地**：v1 包内正文一律不公开（§20.7F）。"""
    loaded = _decode_member(owner, zf, "chunks.bin", _CacheCodec.load)
    if loaded is None:
        raise ValueError("snapshot chunks.bin is corrupt")
    _meta, files = loaded
    # chunks 层必须无向量（向量只属于向量层）；导入时不信任包内数据，统一剥离。
    # 清洗在这里仍执行（结构畸形的 metadata 会拖垮下游硬索引），但结果不落盘。
    clean: dict[str, tuple[str, list[Chunk]]] = {
        source: (signature, [_sanitize_chunk(owner, chunk) for chunk in chunks])
        for source, (signature, chunks) in files.items()
    }
    total = sum(len(chunks) for _, chunks in clean.values())
    return total, len(clean)


def _import_vectors_bin_member(owner: MarkdownIndexer, zf: zipfile.ZipFile) -> int:
    loaded = _decode_member(owner, zf, "vectors.bin", _VectorsCodec.load)
    if loaded is None:
        raise ValueError("snapshot vectors.bin is corrupt")
    meta, vectors = loaded
    # 包内 meta 由下一行用本机 meta 重写（cache key 派生自路径，换机必然不同），
    # 所以维度只能按 payload 的实际长度校验：否则 manifest 只要声明得与本机一致，
    # 错长度向量就会被盖上"本机身份"落地，检索侧的维度不一致只会静默给出 0.0
    # 相似度（numpy 形状报错 → 回退 _cosine → min(len) 截断）。
    dimension = _local_vector_dimension(owner)
    if dimension is not None:
        mismatched = [
            chunk_id
            for chunk_id, vector in vectors.items()
            if vector is not None and len(vector) != dimension
        ]
        if mismatched:
            raise ValueError(
                f"snapshot vectors.bin carries {len(mismatched)} embedding(s) whose dimension "
                f"is not {dimension} (first: {mismatched[0]!r}); refusing to import mismatched vectors"
            )
    vectors = {chunk_id: vector for chunk_id, vector in vectors.items() if vector is not None}
    _VectorsCodec.dump(owner._vectors_cache_path, owner._vectors_meta(), vectors)  # type: ignore[arg-type]
    # 包内签名会被丢弃（见 _import_snapshot_locked），首次 sync 必然重建全部文本层。
    # 这些向量按 chunk.id 放进"重建后补挂"池：内容未变的切片 id 不变 → 直接复用，
    # 沿用 chunker 提升时同一条路径（tests/test_aliases.py 的铁律），0 次 embedding；
    # 内容对不上的切片 id 不同 → 照常重嵌。内存后端才需要：磁盘后端的记账集由
    # _ensure_disk_vectors_migrated 从 vec0 表重建，天然按 id 判"已有向量"。
    owner._pending_vectors.update(vectors)
    return len(vectors)


def _import_vectors_sqlite_member(owner: MarkdownIndexer, zf: zipfile.ZipFile) -> int:
    if owner._vectors_db_path is None:
        raise ValueError("sqlite_vec vector store is unavailable for this vault")
    # 先落地到暂存文件再校验：payload 的 vec0 表声明维度就是检索时的真实维度，
    # 而换进去之后 backend 的 CREATE ... IF NOT EXISTS 对已存在的表是 no-op，
    # 本机维度只活在文件名里——不校验等于让 payload 借用本机身份。
    staged = _stage_member(zf, "vectors.sqlite", owner._vectors_db_path)
    try:
        _validate_vector_sqlite(staged, _local_vector_dimension(owner))
    except Exception:
        _unlink_quietly(staged)
        raise
    closer = getattr(owner._vector_backend, "close", None)
    if closer is not None:
        closer()
    # try/finally 是必须的：close 之后 owner._vector_backend 就是一个被关闭
    # 的对象，任何一步失败（磁盘满、文件被占用、sqlite_vec 不可用）都会
    # 让它永久停在"已关闭"状态 —— query() 恒返回 []、upsert 全部 no-op，
    # 而 _flush_vectors_to_disk 还在盲记账，语义检索静默归零且不可自愈。
    try:
        _replace_live_file(owner, owner._vectors_db_path, staged, close_fts=False)
        backend = create_vector_backend(owner.config.vector, owner, owner._vectors_db_path)
        if not getattr(backend, "available", False):
            raise ValueError("sqlite_vec could not open the imported vector store")
    except Exception:
        _unlink_quietly(staged)
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
    """把 zip 成员流式写到缓存目录的临时文件后用既有 loader 解码。

    _CacheCodec / _VectorsCodec 只认 Path，而它们真正的校验对象（meta）要
    在解码之后由导入逻辑重写，所以这里只负责把字节安全地交给 loader。
    """
    scratch_dir = (owner._chunks_cache_path or owner._vectors_cache_path).parent  # type: ignore[union-attr]
    tmp = scratch_dir / (member + ".importing")
    try:
        _stream_member(zf, member, tmp)
        return loader(tmp)
    finally:
        _unlink_quietly(tmp)


def _stage_member(zf: zipfile.ZipFile, member: str, target: Path) -> Path:
    """把成员流式解出到 target 旁的 .importing 暂存文件（带字节预算）。"""
    tmp = target.with_suffix(target.suffix + ".importing")
    _stream_member(zf, member, tmp)
    return tmp


def _replace_live_file(owner: MarkdownIndexer, target: Path, staged: Path, *, close_fts: bool) -> None:
    """用已落地的暂存文件原子替换一个可能正被本实例打开的缓存文件（sqlite）。"""
    if close_fts and owner._fts is not None:
        try:
            owner._fts.close()
        except Exception:
            pass
        owner._fts = None
    target.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(target)


def _stream_member(zf: zipfile.ZipFile, member: str, dest: Path | Any) -> int:
    """把 zip 成员按块写入 dest（Path 或文件对象），并施加字节预算。

    zip 头里声明的 file_size 与 compress_size 都是攻击者可控字段，而
    ``zf.read(member)`` 会在内存里一次性展开整个成员——两者都不能当上限用。
    这里按真实读到的字节数记账，越界立刻中止（此时最多只多驻留一个块）。
    """
    limit = _SNAPSHOT_MEMBER_LIMITS.get(member, 0)
    if isinstance(dest, Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest.open("wb") as sink, zf.open(member) as src:
                return _copy_member(src, sink, member, limit)
        except Exception:
            _unlink_quietly(dest)
            raise
    with zf.open(member) as src:
        return _copy_member(src, dest, member, limit)


def _copy_member(src: Any, sink: Any, member: str, limit: int) -> int:
    """按块搬运并记账；超过成员上限立即中止。"""
    written = 0
    while True:
        block = src.read(_MEMBER_READ_BLOCK)
        if not block:
            break
        written += len(block)
        if written > limit:
            raise ValueError(
                f"snapshot member {member} exceeds the {limit} byte limit while reading"
            )
        sink.write(block)
    return written


def _read_member_bytes(zf: zipfile.ZipFile, member: str) -> bytes:
    buffer = io.BytesIO()
    _stream_member(zf, member, buffer)
    return buffer.getvalue()


def _unlink_quietly(path: Path) -> None:
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError:
        pass


def _local_vector_dimension(owner: MarkdownIndexer) -> int | None:
    """本机配置的向量维度；拿不到（未配置维度）时返回 None = 跳过维度校验。"""
    dimension = owner._vectors_meta().get("dimension")
    if isinstance(dimension, int) and not isinstance(dimension, bool) and dimension > 0:
        return dimension
    return None


_VEC0_DDL_PATTERN = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


def _validate_vector_sqlite(path: Path, dimension: int | None) -> None:
    """校验 payload 的 vec0 表声明维度与本机配置一致（换掉活动文件之前）。

    表不存在 = 空库，重开时按本机维度建表，无风险；表存在则 payload 自带的
    ``float[N]`` 决定检索时的真实维度（backend 的 ``CREATE ... IF NOT EXISTS``
    对已存在的表是 no-op），与本机不一致就是毒向量，直接拒。

    与 backend 一样先加载本地 sqlite_vec：vec0 是虚拟表，schema 解析需要模块
    在位，否则连 sqlite_master 都读不出来。缺模块 / 读不出维度一律 fail-closed
    ——"验不了"不能当成"没问题"。
    """
    if dimension is None or not path.exists():
        return
    try:
        import sqlite_vec  # type: ignore
    except Exception as exc:
        raise ValueError(
            f"sqlite_vec is required to verify a sqlite vector snapshot: {exc}"
        ) from exc
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError(f"snapshot vectors.sqlite cannot be opened: {exc}") from exc
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'vec0_chunks'").fetchone()
    except (sqlite3.Error, OSError) as exc:
        raise ValueError(f"snapshot vectors.sqlite cannot be inspected: {exc}") from exc
    finally:
        conn.close()
    if row is None or not row[0]:
        return
    match = _VEC0_DDL_PATTERN.search(str(row[0]))
    if match is None:
        raise ValueError("snapshot vectors.sqlite has an unrecognized vec0 schema")
    declared = int(match.group(1))
    if declared != dimension:
        raise ValueError(
            f"snapshot vectors.sqlite declares float[{declared}] but this config uses "
            f"dimension={dimension}; refusing to install mismatched vectors"
        )


def _sanitize_chunk(owner: MarkdownIndexer, chunk: Chunk) -> Chunk:
    """导入边界统一清洗切片：剥掉向量 + 规范化 metadata。

    metadata 来自不可信快照，而下游对它既有硬索引（融合排序的 chunk_index、
    kb_read 的行号）也有数值比较——结构畸形的切片会让整个检索工具报错，
    而不是只影响那一条。
    """
    return owner._strip_embedding(replace(chunk, metadata=_normalize_chunk_metadata(chunk.metadata)))


# 下游硬索引/数值比较依赖的字段：缺失即报错，不是"降级"，所以必须补默认值。
_REQUIRED_INT_METADATA = {"start_line": 1, "end_line": 1, "chunk_index": 0}


def _normalize_chunk_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(_REQUIRED_INT_METADATA)
    meta: dict[str, Any] = {str(key): value for key, value in raw.items()}
    for key, default in _REQUIRED_INT_METADATA.items():
        try:
            meta[key] = int(meta.get(key, default))
        except (TypeError, ValueError):
            meta[key] = default
    if meta["end_line"] < meta["start_line"]:
        meta["end_line"] = meta["start_line"]
    if not isinstance(meta.get("content_hash"), str):
        # 非字符串摘要会污染 sync 的向量复用映射（按 content_hash 建索引）。
        meta.pop("content_hash", None)
    if "heading" in meta and not isinstance(meta["heading"], str):
        meta["heading"] = str(meta["heading"])
    if "mtime" in meta and not isinstance(meta["mtime"], (int, float)):
        meta.pop("mtime", None)
    for key in ("tags", "aliases"):
        value = meta.get(key)
        if value is None:
            meta.pop(key, None)
        elif isinstance(value, str):
            meta[key] = [value] if value.strip() else []
        elif isinstance(value, (list, tuple)):
            meta[key] = [str(item) for item in value if item is not None]
        else:
            meta.pop(key, None)
    return meta


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
