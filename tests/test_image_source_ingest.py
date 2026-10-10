"""E09：独立图片源显式摄取（NEW）。

真实 server 工厂 + 真实 store 发布；PNG/JPEG/WebP fixture 只含真实容器头（不解码像素）。
钉住：显式提交才摄取、不进扫描面、不做 OCR/自动 caption、原文件不改写、
宽高只进 width/height（绝不冒充正文锚点）、缺能力明确拒绝。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mortis_rag_mcp.ingest.images import IMAGE_ROUTE_UNSUPPORTED, ImageUnsupported
from mortis_rag_mcp.ingest.images import parse_image, validate_image_source
from mortis_rag_mcp.ingest.models import ResourceLimits
from mortis_rag_mcp.server import VaultMcpServer


@pytest.fixture()
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reg_file = str(tmp_path / "vaults.toml")
    monkeypatch.setenv("MORTIS_RAG_REGISTRY", reg_file)
    monkeypatch.setenv("VAULT_MCP_REGISTRY", reg_file)


def png(width: int = 4, height: int = 4, pad: int = 64) -> bytes:
    """结构合法的最小 PNG 头（签名 + IHDR），无需像素数据。"""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00" + b"\x00" * pad)


def jpeg(pad: int = 64) -> bytes:
    # SOF0 段：高度/宽度 4x4
    return (b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00\x01\x00\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xc0" + b"\x00\x11\x08" + (4).to_bytes(2, "big") + (4).to_bytes(2, "big")
            + b"\x01\x11\x00" + b"\x00" * pad)


def webp() -> bytes:
    return b"RIFF" + (40).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 32


def build_server(tmp_path: Path, *, storage: str = "virtual", auto_watch: bool = False,
                 media_max_mb: int = 8):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        'mode = "static"\n\n[ingest]\nenabled = true\nstorage = "virtual"\n'
        f'auto_watch = {str(auto_watch).lower()}\nmedia_max_mb = {media_max_mb}\n',
        encoding="utf-8",
    )
    server = VaultMcpServer(config_path)
    server.config.cache.dir = str(tmp_path / "cache")
    server.config.cache.enabled = True
    server.config.cache.placement = "home"
    server.registry.add(str(vault), name="Vault")
    if storage != "virtual":
        server.config.ingest.storage = storage
    return server, vault


def put(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / "vault" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def submit_and_wait(server: VaultMcpServer, vault: Path, sources: list[str], **kwargs) -> dict:
    result = server.call_tool("kb_ingest", {"action": "submit", "sources": sources, **kwargs})
    data = json.loads(result["content"][0]["text"])
    assert data["submitted"] == 1, data
    manager = server._ingest_manager_for(str(vault))
    manager._worker.join(timeout=10.0)
    status = manager.status()["jobs"][0]
    assert status["state"] == "done", status
    return manager


def test_png_explicit_submit_publishes_blob_and_occurrence(isolated_registry, tmp_path):
    path = put(tmp_path, "photo.png", png())
    original_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    server, vault = build_server(tmp_path)
    manager = submit_and_wait(server, vault, ["photo.png"], caption="实验装置照片")

    store = manager._store()
    active = store.get_active("photo.png")
    assert active is not None
    markdown = active.revision.parsed_markdown
    assert "# 实验装置照片" in markdown and "- source: photo.png" in markdown
    caps = active.revision.capabilities
    assert caps["image"] is True and caps["ocr"] is False
    assert caps["line_basis"] == "user_metadata" and caps["anchor"] is False
    assert "no OCR" in " ".join(caps["warnings"])

    media = list(store.iter_media("photo.png", revision_id=active.revision.revision_id))
    assert len(media) == 1
    occ = media[0]
    assert occ["kind"] == "image" and occ["caption"] == "实验装置照片"
    assert occ["page"] is None, "独立图片源没有页码，不得编造"
    meta = occ["metadata"] or {}
    assert meta.get("line_basis") == "user_metadata" and meta.get("ocr") is False
    assert occ["t_start_ms"] is None, "图片没有时间范围，不得冒充音频"

    # 原文件不改写：SHA 保持
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_sha
    # 图片不进扫描面：auto/pending 都不会把它带回队列
    assert manager.scan_pending() == []


def test_jpeg_and_captionless_stem_title(isolated_registry, tmp_path):
    put(tmp_path, "figure.jpeg", jpeg())
    server, vault = build_server(tmp_path)
    manager = submit_and_wait(server, vault, ["figure.jpeg"])
    store = manager._store()
    active = store.get_active("figure.jpeg")
    assert "# figure" in active.revision.parsed_markdown
    assert active.revision.capabilities["width"] == 4
    media = list(store.iter_media("figure.jpeg", revision_id=active.revision.revision_id))
    assert media[0]["caption"] == "figure", "无用户说明时回退文件名（真实事实），不猜内容"


def test_webp_without_measurable_dimensions_publishes_honestly(isolated_registry, tmp_path):
    put(tmp_path, "logo.webp", webp())
    server, vault = build_server(tmp_path)
    manager = submit_and_wait(server, vault, ["logo.webp"], caption="logo")
    store = manager._store()
    caps = store.get_active("logo.webp").revision.capabilities
    assert caps["width"] is None and caps["height"] is None
    assert any("image_dimensions_unavailable" in warning for warning in caps["warnings"])
    media = list(store.iter_media("logo.webp", revision_id=active_revision(store, "logo.webp")))
    assert media[0]["caption"] == "logo"


def active_revision(store, source: str):
    return store.get_active(source).revision.revision_id


@pytest.mark.parametrize("name,payload", [
    ("photo.bmp", png()),
    ("photo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
])
def test_unsupported_image_suffix_rejected(isolated_registry, tmp_path, name, payload):
    put(tmp_path, name, payload)
    server, vault = build_server(tmp_path)
    # 沙箱后缀白名单先拦（.bmp/.svg 不在显式提交面）；错误必须可见而非静默跳过。
    with pytest.raises(ValueError, match="ingestible document|IMAGE_ROUTE_UNSUPPORTED"):
        server.call_tool("kb_ingest", {"action": "submit", "sources": [name]})
    manager = server._ingest_manager_for(str(vault))
    assert manager._store().queue_depth() == 0


def test_magic_mismatch_rejected(isolated_registry, tmp_path):
    put(tmp_path, "fake.png", b"\xff\xd8\xff\xe0" + b"\x00" * 64)  # JPEG 字节冒 PNG 后缀
    server, _ = build_server(tmp_path)
    with pytest.raises(ValueError, match="suffix/magic mismatch"):
        server.call_tool("kb_ingest", {"action": "submit", "sources": ["fake.png"]})


def test_byte_budget_enforced(isolated_registry, tmp_path):
    put(tmp_path, "big.png", png(pad=2 * 1024 * 1024))
    server, vault = build_server(tmp_path, media_max_mb=1)
    with pytest.raises(ValueError, match="media byte budget"):
        server.call_tool("kb_ingest", {"action": "submit", "sources": ["big.png"]})
    manager = server._ingest_manager_for(str(vault))
    assert manager._store().queue_depth() == 0
    # 像素上限（max_image_pixels=40M 默认）在单元合同里验证。


def test_images_never_enter_auto_watch_scan(isolated_registry, tmp_path):
    """显式提交与 auto_watch 扫描面分离：支持图片≠开启后台全库图片扫描。"""
    from mortis_rag_mcp.ingest.worker import _ingest_exts

    put(tmp_path, "photo.png", png())
    put(tmp_path, "logo.webp", webp())
    server, vault = build_server(tmp_path, auto_watch=True)
    manager = server._ingest_manager_for(str(vault))
    assert manager.scan_pending() == [], "图片绝不进入扫描面"
    assert not ({".png", ".jpg", ".jpeg", ".webp"} & _ingest_exts(server.config.ingest))
    auto = manager.auto_submit()
    assert auto["submitted"] == 0
    assert manager._store().queue_depth() == 0


def test_legacy_storage_rejects_image_submit(isolated_registry, tmp_path):
    put(tmp_path, "photo.png", png())
    server, _ = build_server(tmp_path, storage="legacy")
    with pytest.raises(ValueError, match=IMAGE_ROUTE_UNSUPPORTED):
        server.call_tool("kb_ingest", {"action": "submit", "sources": ["photo.png"]})


def test_parse_image_unit_contract(tmp_path):
    path = put(tmp_path, "u.png", png(6, 7))
    limits = ResourceLimits()
    info = validate_image_source(path, limits)
    assert info["mime_type"] == "image/png" and (info["width"], info["height"]) == (6, 7)
    result = parse_image(path, limits=limits, title="t", description="d")
    assert result.capabilities["line_basis"] == "user_metadata"
    assert "# t" in result.markdown and "d" in result.markdown
    with pytest.raises(ImageUnsupported, match="pixel budget"):
        validate_image_source(put(tmp_path, "v.png", png(9999, 9999)), limits)
