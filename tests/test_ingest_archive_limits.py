"""T05：安全归档消费的限额与拒绝路径（C93）。

这些用例只碰 `mortis_rag_mcp.ingest.mineru` 的**纯归档**路径（无网络、无真实端点）：
- 限额边界 ±1 字节、伪 Content-Length
- CRC 失败、规范化重名、多 full.md、加密项、symlink、未知压缩算法
- 绝对/盘符/UNC/上跳成员名
- 声明≠实际大小
- 超限 ZIP 在**构造 ZipFile 之前**就被拒绝（分配前准入）
- 媒体只经 sink 落点，不在结果对象里留字节
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from mortis_rag_mcp.ingest.mineru import (
    _ALLOWED_COMPRESSION,  # noqa: F401  (显式确认白名单存在，便于人工核对)
    MineruError,
    _safe_extract_zip,
)
from mortis_rag_mcp.ingest.models import DictMediaSink, ResourceLimits

_CD_SIG = b"PK\x01\x02"
_CD_ENTRY_SIZE = 46


def _make_zip(files: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _patch_cd_entry(zip_bytes: bytes, member: str, offset: int, raw: bytes) -> bytes:
    """把 `member` 中央目录条目里相对条目起点的 `offset` 处改成 `raw`（用于伪造元数据）。"""
    cursor = 0
    while True:
        index = zip_bytes.find(_CD_SIG, cursor)
        if index < 0:
            raise AssertionError(f"central directory entry not found for {member!r}")
        name_len = int.from_bytes(zip_bytes[index + 28 : index + 30], "little")
        name = zip_bytes[index + _CD_ENTRY_SIZE : index + _CD_ENTRY_SIZE + name_len].decode("utf-8")
        if name == member:
            patched = bytearray(zip_bytes)
            patched[index + offset : index + offset + len(raw)] = raw
            return bytes(patched)
        cursor = index + _CD_ENTRY_SIZE + name_len


def _limits(**overrides) -> ResourceLimits:
    return ResourceLimits(**overrides)


# --------------------------------------------------------------- 限额边界


def test_markdown_limit_boundary():
    payload = b"A" * 100
    zip_bytes = _make_zip({"full.md": payload})
    # 等于上限：允许
    outcome = _safe_extract_zip(zip_bytes, limits=_limits(markdown_max_bytes=100), sink=None)
    assert outcome.markdown == "A" * 100
    # 少 1 字节：拒绝
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(markdown_max_bytes=99), sink=None)
    assert exc_info.value.code_str == "RESOURCE_LIMIT"


def test_media_limit_boundary():
    media = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    zip_bytes = _make_zip({"full.md": b"# t", "images/a.png": media})
    outcome = _safe_extract_zip(zip_bytes, limits=_limits(media_max_bytes=len(media)), sink=None)
    assert outcome.media and outcome.media[0].byte_size == len(media)
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(media_max_bytes=len(media) - 1), sink=None)
    assert exc_info.value.code_str == "RESOURCE_LIMIT"


def test_member_count_limit_is_rejected_before_zipfile_is_constructed(monkeypatch):
    """§20.7C：成员数超限必须在**创建 ZipFile 之前**拒绝，不能先分配再判。"""
    zip_bytes = _make_zip({"full.md": b"x", "a.txt": b"y", "b.txt": b"z"})

    def _forbidden(*args, **kwargs):  # pragma: no cover - 断言不应被调用
        raise AssertionError("ZipFile 不该在准入失败后被构造")

    monkeypatch.setattr(zipfile, "ZipFile", _forbidden)
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(max_members=2), sink=None)
    assert exc_info.value.code_str == "RESOURCE_LIMIT"


def test_media_count_limit():
    files = {"full.md": b"# t"}
    for index in range(3):
        files[f"images/{index}.png"] = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    zip_bytes = _make_zip(files)
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(max_media=2), sink=None)
    assert exc_info.value.code_str == "RESOURCE_LIMIT"


def test_image_pixel_budget_rejected_from_header_only():
    # IHDR 声明 20000x20000 = 4e8 像素 > 4e7 上限
    huge = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + (20000).to_bytes(4, "big")
        + (20000).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    zip_bytes = _make_zip({"full.md": b"# t", "images/huge.png": huge})
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(), sink=None)
    assert exc_info.value.code_str == "RESOURCE_LIMIT"
    assert "pixels" in str(exc_info.value)


# --------------------------------------------------------------- 归档损坏与元数据伪造


def test_crc_failure_rejects_whole_candidate():
    payload = b"# Chapter\n" + b"B" * 64
    zip_bytes = _make_zip({"full.md": payload}, compression=zipfile.ZIP_STORED)
    index = zip_bytes.find(payload)
    assert index > 0
    corrupted = bytearray(zip_bytes)
    corrupted[index + 3] ^= 0xFF
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(bytes(corrupted), limits=_limits(), sink=None)
    assert exc_info.value.code_str == "ARCHIVE_INVALID"
    assert "CRC" in str(exc_info.value) or "CRC/integrity" in str(exc_info.value)


def test_declared_size_mismatch_is_rejected():
    zip_bytes = _make_zip({"full.md": b"# Chapter\nABC"}, compression=zipfile.ZIP_STORED)
    patched = _patch_cd_entry(zip_bytes, "full.md", 24, (1).to_bytes(4, "little"))
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(patched, limits=_limits(), sink=None)
    assert exc_info.value.code_str == "ARCHIVE_INVALID"


def test_multiple_full_md_is_rejected():
    zip_bytes = _make_zip({"full.md": b"# a", "sub/full.md": b"# b"})
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(), sink=None)
    assert exc_info.value.code_str == "ARCHIVE_INVALID"
    assert "full.md" in str(exc_info.value)


def test_missing_full_md_is_contract_error():
    zip_bytes = _make_zip({"images/a.png": b"x"})
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(), sink=None)
    assert "full.md not found" in str(exc_info.value)
    assert exc_info.value.retryable is False


def test_duplicate_member_after_normalization_is_rejected():
    zip_bytes = _make_zip({"images/A.png": b"x", "images/a.png": b"y"})
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(zip_bytes, limits=_limits(), sink=None)
    assert exc_info.value.code_str == "ARCHIVE_INVALID"
    assert "duplicate" in str(exc_info.value)


def test_encrypted_member_is_rejected():
    zip_bytes = _make_zip({"full.md": b"# a", "images/a.png": b"x"})
    patched = _patch_cd_entry(zip_bytes, "images/a.png", 8, (0x1).to_bytes(2, "little"))
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(patched, limits=_limits(), sink=None)
    assert "encrypted" in str(exc_info.value)


def test_symlink_member_is_rejected():
    zip_bytes = _make_zip({"full.md": b"# a", "images/a.png": b"x"})
    patched = _patch_cd_entry(zip_bytes, "images/a.png", 38, (0o120777 << 16).to_bytes(4, "little"))
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(patched, limits=_limits(), sink=None)
    assert "symlink" in str(exc_info.value)


def test_unknown_compression_method_is_rejected():
    zip_bytes = _make_zip({"full.md": b"# a", "images/a.png": b"x"})
    patched = _patch_cd_entry(zip_bytes, "images/a.png", 10, (99).to_bytes(2, "little"))
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(patched, limits=_limits(), sink=None)
    assert "compression" in str(exc_info.value)


@pytest.mark.parametrize(
    "name",
    ["../evil.md", "a/../../b.md", "//server/share/x.png", "C:/x.png", "a/./b.png"],
)
def test_unsafe_member_names_are_rejected(name: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("full.md", b"# a")
        zf.writestr(zipfile.ZipInfo(name), b"x")
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(buf.getvalue(), limits=_limits(), sink=None)
    assert exc_info.value.code_str == "ARCHIVE_INVALID"


def test_zip64_is_rejected_not_guessed(monkeypatch):
    zip_bytes = _make_zip({"full.md": b"# a"})
    # 伪造 ZIP64 定位符存在（真实 ZIP64 需要大归档，这里只验证「拒」而不是「猜」）
    forged = zip_bytes + b"PK\x06\x07" + b"\x00" * 16
    with pytest.raises(MineruError) as exc_info:
        _safe_extract_zip(forged, limits=_limits(), sink=None)
    assert exc_info.value.code_str == "ARCHIVE_INVALID"


# --------------------------------------------------------------- 媒体 sink


def test_media_is_delivered_to_sink_and_not_retained_by_outcome():
    media = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    zip_bytes = _make_zip({"full.md": b"# t", "images/a.png": media})
    sink = DictMediaSink()
    outcome = _safe_extract_zip(zip_bytes, limits=_limits(), sink=sink)
    assert sink.images["images/a.png"] == media
    assert outcome.media[0].occurrence_id
    assert not hasattr(outcome.media[0], "data")


def test_no_files_are_written_to_disk(tmp_path: Path):
    """归档消费**不得**落任何临时文件（回归「零磁盘污染」）。"""
    zip_bytes = _make_zip({"full.md": b"# t", "images/a.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 24})
    before = set(tmp_path.iterdir())
    _safe_extract_zip(zip_bytes, limits=_limits(), sink=DictMediaSink())
    assert set(tmp_path.iterdir()) == before
