"""缓存编解码 round-trip 金测（P2 ``_indexer/models`` + ``cache_codec`` 提取闸门）。

契约（修正后的真实兼容契约，取代已被双声部评审证伪的 pickle 条款）：
1. ``VMCPC``/``VMCPV`` 二进制格式与 ``_CACHE_VERSION`` 不变；
2. ``Chunk`` 构造参数序不变（``_CacheCodec.load`` 位置序重建，字段序即契约）；
3. ``dump → load → dump`` 字节级恒等；
4. 解码失败静默返回 ``None``（上层据此全库静默重建），绝不抛异常。
"""
from __future__ import annotations

import dataclasses
import zlib
from array import array
from pathlib import Path

from mortis_rag_mcp.indexer import Chunk, _CacheCodec, _VectorsCodec


def test_chunk_field_order_lock():
    """字段序即磁盘兼容契约：``_CacheCodec.load`` 按位置序重建 Chunk。"""
    names = [f.name for f in dataclasses.fields(Chunk)]
    assert names == ["id", "content", "source", "title", "metadata", "score", "embedding"], (
        f"Chunk 字段序漂移（缓存位置序重建将静默错位）: {names}"
    )
    chunk = Chunk("i", "内容", "a.md", "标题", {"start_line": 1})
    assert chunk.score == 0.0 and chunk.embedding is None


def test_chunks_bin_roundtrip_byte_identical(tmp_path: Path):
    meta = {"key": "golden", "chunk_size": 1200, "chunk_overlap": 0, "chunker": 5}
    files = {
        "a.md": (
            "sig-a",
            [
                Chunk("id1", "内容甲", "a.md", "标题甲", {"start_line": 1, "end_line": 3, "content_hash": "h1"}),
                Chunk("id2", "内容乙", "a.md", "标题乙", {"start_line": 4, "end_line": 6, "content_hash": "h2"}),
            ],
        ),
        "b.txt": ("sig-b", [Chunk("id3", "text", "b.txt", "", {"start_line": 1, "end_line": 1})]),
    }
    p1 = tmp_path / "v.chunks.bin"
    _CacheCodec.dump(p1, meta, files)
    loaded = _CacheCodec.load(p1)
    assert loaded is not None
    meta2, files2 = loaded
    assert meta2 == meta
    assert files2["a.md"][0] == "sig-a"
    assert [c.id for c in files2["a.md"][1]] == ["id1", "id2"]
    assert files2["a.md"][1][0].content == "内容甲"
    assert files2["a.md"][1][0].metadata["start_line"] == 1
    p2 = tmp_path / "v2.chunks.bin"
    _CacheCodec.dump(p2, meta2, files2)
    assert p2.read_bytes() == p1.read_bytes(), "dump(load(x)) != x：编解码非字节恒等"


def test_vectors_bin_roundtrip_byte_identical(tmp_path: Path):
    meta = {"key": "golden", "mode": "static", "model": "", "dimension": 8}
    vectors = {"id1": array("f", [0.5, -0.25, 0.125, 0.0, 1.0, -1.0, 0.75, 0.25])}
    p1 = tmp_path / "v.vectors.bin"
    _VectorsCodec.dump(p1, meta, vectors)
    loaded = _VectorsCodec.load(p1)
    assert loaded is not None
    meta2, vectors2 = loaded
    assert meta2 == meta
    assert vectors2["id1"].tolist() == vectors["id1"].tolist()
    p2 = tmp_path / "v2.vectors.bin"
    _VectorsCodec.dump(p2, meta2, vectors2)
    assert p2.read_bytes() == p1.read_bytes()


def test_corrupt_cache_silently_rebuilds(tmp_path: Path):
    """解码失败静默返回 None（上层据此全库重建），绝不抛异常。"""
    bad = tmp_path / "bad.chunks.bin"
    bad.write_bytes(b"NOT-VMCPC-GARBAGE")
    assert _CacheCodec.load(bad) is None
    good = tmp_path / "good.chunks.bin"
    _CacheCodec.dump(good, {"k": 1}, {"a.md": ("s", [Chunk("i", "c", "a.md", "t", {})])})
    truncated = tmp_path / "trunc.chunks.bin"
    raw = good.read_bytes()
    truncated.write_bytes(raw[: len(raw) // 2])
    assert _CacheCodec.load(truncated) is None
    assert _VectorsCodec.load(bad) is None


def test_short_payload_after_zlib_returns_none_without_indexerror(tmp_path: Path):
    """C2 回归：magic 匹配但总长不足 6 字节的载荷不得抛 IndexError。

    关键：载荷必须先通过 ``zlib.decompress``（直接写裸 ``b"VMCPC"`` 会先撞
    zlib.error 分支，测不到新增的长度守卫），因此写 ``zlib.compress(...)``。
    """
    for name, magic, loader in (
        ("short.chunks.bin", b"VMCPC", _CacheCodec.load),
        ("short.vectors.bin", b"VMCPV", _VectorsCodec.load),
    ):
        path = tmp_path / name
        path.write_bytes(zlib.compress(magic))  # 解压后恰好 5 字节 = magic
        assert loader(path) is None

    # 边界：magic + 版本字节（6 字节）恰好跨过长度守卫，版本对但 meta 缺失 → struct.error 兜底
    boundary = tmp_path / "boundary.chunks.bin"
    boundary.write_bytes(zlib.compress(b"VMCPC\x01"))
    assert _CacheCodec.load(boundary) is None
