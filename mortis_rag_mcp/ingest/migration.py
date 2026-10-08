"""Explicit read-only legacy mirror preflight and opt-in migration; no network."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..doc_store import (
    DocStoreError,
    DocumentStore,
    MediaOccurrenceSpec,
    is_within,
    normalize_source_path,
)

DEFAULT_MIRROR_DIRNAME = ".mortis-parsed"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

MIGRATION_VERSION = "legacy-mirror-v1"

#: 旧镜像里的图片引用：Markdown `![..](path)` 与 HTML `<img src="path">`。
_MEDIA_REF_PATTERN = re.compile(
    r"!\[[^\]]*\]\(\s*([^)\s]+)|<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE
)


def _image_mime(path: Path) -> str | None:
    """按 magic 头推断图像 MIME；认不出返回 None（不猜、不按扩展名。§20.1）。"""
    try:
        with path.open("rb") as stream:
            head = stream.read(16)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _looks_like_image(path: Path) -> bool:
    return _image_mime(path) is not None


def _verify_media_assets(body: str, mirror_path: Path, mirrors_root: Path,
                         quota: int) -> list[dict]:
    """逐项验证镜像引用的媒体资产（§20.1）：安全子树 + 配额 + magic，认不出即拒。

    旧镜像的图片相对镜像文件本身解析（worker 落在 `<stem>.assets/`）；任何一项
    无法证明归属都抛错 → 调用方标 pending_manual，绝不自动修复或上传。
    返回**已验证**的资产清单（含正文 anchor 与推断 MIME），供迁移时作为 Blob/Occurrence
    写入文档库——迁移后的历史图片因此可以经 `kb_read_media` 读出。
    """
    assets: list[dict] = []
    for match in _MEDIA_REF_PATTERN.finditer(body):
        ref = match.group(1) or match.group(2)
        if not ref:
            continue
        if ref.startswith(("http://", "https://", "data:")):
            raise ValueError("remote/data media refs cannot be proven from a legacy mirror")
        candidate = (mirror_path.parent / ref).resolve()
        if not is_within(candidate, mirrors_root) or not candidate.is_file():
            raise ValueError("media asset is missing or outside the safe mirror subtree")
        if candidate.stat().st_size > quota:
            raise ValueError("media asset exceeds quota")
        mime = _image_mime(candidate)
        if mime is None:
            raise ValueError("media asset magic is not a recognized image")
        assets.append({"ref": ref, "path": candidate, "mime_type": mime,
                       "anchor_start": match.start(), "anchor_end": match.end()})
    return assets


def _header(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise ValueError("not a legacy frontmatter mirror")
    header, separator, body = text[4:].partition("\n---\n")
    if not separator:
        raise ValueError("invalid frontmatter")
    values = {}
    for line in header.splitlines():
        key, colon, value = line.partition(":")
        if not colon:
            raise ValueError("unsupported frontmatter")
        value = value.strip()
        values[key.strip()] = json.loads(value) if value.startswith('"') else value
    if not isinstance(values.get("source_pdf"), str):
        raise ValueError("source_pdf must be a string")
    digest = values.get("source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("source_sha256 must be a SHA256 string")
    if not isinstance(values.get("parsed_by"), str) or not values["parsed_by"]:
        raise ValueError("parser provenance is missing")
    return values, body


@dataclass(frozen=True)
class MirrorOwnershipProof:
    """**单项**镜像归属证明（只读抽取；供 sync 的逐项镜像排除消费）。

    `legacy_mirror` 标记、路径吻合、或「文档库里恰好有同名 source」都**不是**
    证明。证明必须同时具备：镜像 frontmatter 声明的 source/SHA、库内物理源
    SHA 复核、唯一 ledger `done` 归属，以及（正文含媒体引用时）资产校验。
    """

    mirror: str
    source: str
    source_sha256: str
    parser: str
    media_assets: int


def load_legacy_ledger(mirrors_root: Path) -> dict[str, Any] | None:
    """读取旧 `.ingest_state.json`；缺失/损坏返回 None（不猜、不自动修复）。"""
    ledger_path = Path(mirrors_root) / ".ingest_state.json"
    if not ledger_path.is_file():
        return None
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return ledger if isinstance(ledger, dict) else None


def prove_mirror_ownership(root: Path, mirror: str, *, quota_bytes: int,
                           mirrors_root: Path | None = None,
                           ledger: Mapping[str, Any] | None = None) -> MirrorOwnershipProof:
    """逐项只读归属证明；任何一环无法证明即抛 `ValueError(reason)`。

    不做目录级判断、不写库、不改源文件/用户配置。调用方（sync）只按本函数
    返回的成功证明排除该**具体**镜像路径。
    """
    mirrors = Path(mirrors_root) if mirrors_root is not None else root / DEFAULT_MIRROR_DIRNAME
    path = root / mirror
    if path.is_symlink() or not is_within(path, mirrors):
        raise ValueError("unsafe mirror path")
    if not path.is_file():
        raise ValueError("mirror file is missing")
    if path.stat().st_size > quota_bytes:
        raise ValueError("mirror exceeds quota")
    fields, body = _header(path.read_text(encoding="utf-8"))
    if "\x00" in body:
        raise ValueError("mirror body contains NUL")
    source = normalize_source_path(fields["source_pdf"], root)
    physical = root / source
    if not physical.is_file() or _file_sha256(physical) != fields["source_sha256"]:
        raise ValueError("missing source or source SHA mismatch")
    assets: list[dict] = []
    if re.search(r"!\[[^\]]*\]\(|<img\b", body, re.I):
        assets = _verify_media_assets(body, path, mirrors, quota_bytes)
    if ledger is None:
        ledger = load_legacy_ledger(mirrors)
    if ledger is None:
        raise ValueError("legacy ledger provenance is missing")
    jobs = ledger.get("jobs", {})
    values = jobs.values() if isinstance(jobs, dict) else jobs
    matching = [j for j in values if isinstance(j, dict) and j.get("source") == source
                and j.get("state") == "done" and j.get("sha256") == fields["source_sha256"]]
    if len(matching) != 1:
        raise ValueError("mirror ownership cannot be proven by ledger")
    return MirrorOwnershipProof(mirror=str(mirror), source=source,
                                source_sha256=str(fields["source_sha256"]),
                                parser=MIGRATION_VERSION + ":" + str(fields["parsed_by"]),
                                media_assets=len(assets))


def resolve_excluded_mirrors(root: Path, candidates: Mapping[str, str], *, quota_bytes: int,
                             mirrors_root: Path | None = None) -> tuple[set[str], dict[str, str]]:
    """把 `source -> 候选镜像路径` 映射收敛为**已证明**镜像集合。

    返回 `(excluded, reasons)`；`reasons` 只用于诊断（未证明→保留为普通文件）。
    候选文件不存在时静默跳过；证明成立且 header 声明的 source 与候选 source
    一致才排除。整库 migration/apply 不由本函数触发。
    """
    mirrors = Path(mirrors_root) if mirrors_root is not None else root / DEFAULT_MIRROR_DIRNAME
    ledger = load_legacy_ledger(mirrors)
    excluded: set[str] = set()
    reasons: dict[str, str] = {}
    for source, mirror in candidates.items():
        if not (root / mirror).is_file():
            continue
        try:
            proof = prove_mirror_ownership(root, mirror, quota_bytes=quota_bytes,
                                           mirrors_root=mirrors, ledger=ledger)
        except (ValueError, OSError, UnicodeError, TypeError, DocStoreError) as exc:
            reasons[str(mirror)] = str(exc)
            continue
        if proof.source != source:
            reasons[str(mirror)] = "mirror header source does not match the indexed source"
            continue
        excluded.add(str(mirror))
    return excluded, reasons


def migrate_legacy_mirrors(store: DocumentStore, *, apply: bool = False) -> dict:
    if type(apply) is not bool:
        raise ValueError("apply must be bool")
    root = store.layout.vault_path
    mirrors = root / ".mortis-parsed"
    items = []
    if not mirrors.is_dir():
        return {"apply": apply, "items": [], "migrated": 0, "migrated_assets": 0,
                "excluded_sources": []}
    candidates = []
    for path in sorted(mirrors.rglob("*.md")):
        item = {"mirror": path.relative_to(root).as_posix(), "status": "pending_manual"}
        items.append(item)
        try:
            if not is_within(path, mirrors) or path.is_symlink():
                raise ValueError("unsafe mirror path")
            if path.stat().st_size > store._quota_limit_bytes():
                raise ValueError("mirror exceeds quota")
            fields, body = _header(path.read_text(encoding="utf-8"))
            if "\x00" in body:
                raise ValueError("mirror body contains NUL")
            # 源必须是库内相对安全路径（normalize_source_path 拒绝绝对/上跳/NUL）。
            source = normalize_source_path(fields["source_pdf"], root)
            physical = root / source
            if not physical.is_file() or _file_sha256(physical) != fields["source_sha256"]:
                raise ValueError("missing source or source SHA mismatch")
            # Legacy headers have no output checksum. Require the original ledger
            # provenance; image-bearing mirrors must prove asset ownership：引用的
            # 资产必须在安全镜像子树内、大小合规且 magic 是已知图像格式。
            assets: list[dict] = []
            if re.search(r"!\[[^\]]*\]\(|<img\b", body, re.I):
                assets = _verify_media_assets(body, path, mirrors, store._quota_limit_bytes())
            ledger_path = mirrors / ".ingest_state.json"
            if not ledger_path.is_file():
                raise ValueError("legacy ledger provenance is missing")
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            jobs = ledger.get("jobs", {})
            values = jobs.values() if isinstance(jobs, dict) else jobs
            matching = [j for j in values if isinstance(j, dict) and j.get("source") == source
                        and j.get("state") == "done" and j.get("sha256") == fields["source_sha256"]]
            if len(matching) != 1:
                raise ValueError("mirror ownership cannot be proven by ledger")
            item.update(source=source, source_sha256=fields["source_sha256"], bytes=path.stat().st_size,
                        status="ready", parser=MIGRATION_VERSION + ":" + fields["parsed_by"],
                        media_assets=len(assets))
            candidates.append((item, body, assets))
        except (ValueError, OSError, UnicodeError, TypeError, DocStoreError) as exc:
            item["reason"] = str(exc)
    counts = {}
    for item, _body, _assets in candidates:
        counts[item["source"]] = counts.get(item["source"], 0) + 1
    migrated = 0
    migrated_assets = 0
    # `excluded_sources` 是**逐条已迁移镜像**的库内相对路径（来源精确排除），
    # 供调用方按 per-source 机制登记；绝不返回 `.mortis-parsed/` 目录级忽略，
    # 否则整棵子树（含未迁移镜像与用户内容）会一起消失。未迁移镜像保旧兼容。
    excluded = []
    for item, body, assets in candidates:
        if counts[item["source"]] != 1:
            item.update(status="pending_manual", reason="conflicting mirrors for the same source")
            continue
        if not apply:
            continue
        active = store.get_active(item["source"])
        if active is not None and active.revision.source_sha256 == item["source_sha256"] and active.revision.parser_fingerprint == item["parser"]:
            item["status"] = "already_migrated"
            excluded.append(item["mirror"])
            continue
        candidate = store.stage_revision(source=item["source"], source_sha256=item["source_sha256"],
            render_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            parser_fingerprint=item["parser"], markdown=body,
            capabilities={"migration_version": MIGRATION_VERSION, "legacy_mirror": item["mirror"]})
        if assets:
            # 已验证资产作为真正 Blob/Occurrence 入库（§20.1）：迁移后的历史图片可经
            # kb_read_media 读取；anchor 来自正文引用位置，读路径据此做媒体邻接。
            specs = []
            for ordinal, asset in enumerate(assets, start=1):
                blob_id = store.put_media_blob(data=asset["path"].read_bytes(),
                                               mime_type=asset["mime_type"])
                specs.append(MediaOccurrenceSpec(
                    occurrence_id=f"mirror-{ordinal}", blob_id=blob_id, kind="image",
                    ordinal=ordinal, mime_type=asset["mime_type"],
                    metadata={"anchor_start": asset["anchor_start"], "anchor_end": asset["anchor_end"],
                              "name": asset["ref"], "legacy_mirror": item["mirror"]}))
            store.attach_occurrences(candidate.revision_id, specs)
            migrated_assets += len(specs)
        store.commit_revision(candidate.revision_id, source_sha256=item["source_sha256"])
        item["status"] = "migrated"
        migrated += 1
        excluded.append(item["mirror"])
    return {"apply": apply, "items": items, "migrated": migrated,
            "migrated_assets": migrated_assets, "excluded_sources": excluded}
