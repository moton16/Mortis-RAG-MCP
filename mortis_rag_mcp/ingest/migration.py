"""Explicit read-only legacy mirror preflight and opt-in migration; no network."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..doc_store import (
    DocStoreError,
    DocumentStore,
    MediaOccurrenceSpec,
    is_within,
    normalize_source_path,
)


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
