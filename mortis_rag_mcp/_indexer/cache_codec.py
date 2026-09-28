"""双层磁盘缓存编解码（v0.8.0 自 indexer.py 逐字提取）。

格式契约（P2 闸门 ``tests/test_cache_codec_roundtrip.py`` 锁定，
变更 = 全量缓存失效 + 全库重嵌）：
- 文本层 ``.chunks.bin``：magic ``VMCPC`` + 版本字节 + JSON meta + 逐文件
  (sha256 签名 + 逐 chunk: id/content/title/JSON metadata/float32 embedding)，
  zlib 压缩；meta 相等性 + ``Chunk`` **位置序**构造是兼容性核心；
- 向量层 ``.vectors.bin``：magic ``VMCPV`` + 版本字节 + JSON meta +
  ``{chunk_id: float32 array}``，zlib 压缩；与文本层独立失效；
- 解码失败（magic/版本/截断/损坏）一律静默返回 ``None``，上层据此全库
  静默重建，绝不抛异常；
- 写入一律 ``tmp + replace`` 原子替换。
"""
from __future__ import annotations

import json
import struct
import zlib
from array import array
from pathlib import Path
from typing import Any

from .models import Chunk, _EMB_DTYPE

_CACHE_MAGIC = b"VMCPC"
_CACHE_VERSION = 1
# 解压上限：缓存文件会随 vault 分发（[cache] placement = "vault"），所以这条读
# 路径必须自己兜住内存——"解压后有多大"不能由文件自己说了算。
_MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_DECOMPRESS_BLOCK = 1 * 1024 * 1024


def _decompress_bounded(path: Path) -> bytes | None:
    """流式解压缓存文件并施加字节上限。

    超限与格式错误一律返回 ``None``（本模块"解码失败即静默重建"的契约）：
    解压前先按块读，避免为了解压再额外驻留一份压缩字节。
    """
    limit = _MAX_DECOMPRESSED_BYTES
    engine = zlib.decompressobj()
    out = bytearray()
    try:
        with path.open("rb") as src:
            while True:
                block = src.read(_DECOMPRESS_BLOCK)
                if not block:
                    break
                remaining = limit - len(out)
                if remaining <= 0:
                    return None
                out += engine.decompress(block, remaining)
                if engine.unconsumed_tail:
                    # 顶到上限而输入还没消化完 = 载荷超出上限，直接判失败，
                    # 不给出"截断但看起来合法"的半份数据。
                    return None
        out += engine.flush()
    except (OSError, zlib.error):
        return None
    if len(out) > limit:
        return None
    return bytes(out)


def _pack_str(buf: bytearray, text: str) -> None:
    data = text.encode("utf-8")
    buf += struct.pack("<I", len(data))
    buf += data


def _pack_u32(buf: bytearray, value: int) -> None:
    buf += struct.pack("<I", value)


class _CacheCodec:
    """Compact binary cache format: signatures + chunks + float32 embeddings (zlib)."""

    @staticmethod
    def dump(path: Path, meta: dict[str, Any], files: dict[str, tuple[str, list[Chunk]]]) -> None:
        buf = bytearray()
        buf += _CACHE_MAGIC
        buf += struct.pack("<B", _CACHE_VERSION)
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        _pack_u32(buf, len(meta_bytes))
        buf += meta_bytes
        _pack_u32(buf, len(files))
        for source in sorted(files):
            signature, chunks = files[source]
            _pack_str(buf, source)
            _pack_str(buf, signature)
            _pack_u32(buf, len(chunks))
            for chunk in chunks:
                _pack_str(buf, chunk.id)
                _pack_str(buf, chunk.content)
                _pack_str(buf, chunk.title)
                _pack_str(buf, json.dumps(chunk.metadata, ensure_ascii=False))
                emb = chunk.embedding
                if emb is not None and len(emb):
                    _pack_u32(buf, len(emb))
                    buf += emb.tobytes()
                else:
                    _pack_u32(buf, 0)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(zlib.compress(bytes(buf), 6))
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> tuple[dict[str, Any], dict[str, tuple[str, list[Chunk]]]] | None:
        if not path.exists():
            return None
        raw = _decompress_bounded(path)
        if raw is None:
            return None
        # 长度判据必须与 magic 同句：magic 匹配但总长不足 6 字节的截断文件会让
        # 下方 raw[pos] 抛 IndexError，违反本模块「解码失败一律静默返回 None」契约。
        if raw[: len(_CACHE_MAGIC)] != _CACHE_MAGIC or len(raw) <= len(_CACHE_MAGIC):
            return None
        pos = len(_CACHE_MAGIC)
        version = raw[pos]
        pos += 1
        if version != _CACHE_VERSION:
            return None
        try:
            (meta_len,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            meta = json.loads(raw[pos : pos + meta_len].decode("utf-8"))
            pos += meta_len
            (file_count,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            files: dict[str, tuple[str, list[Chunk]]] = {}
            for _ in range(file_count):
                (s_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                source = raw[pos : pos + s_len].decode("utf-8")
                pos += s_len
                (sig_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                signature = raw[pos : pos + sig_len].decode("utf-8")
                pos += sig_len
                (chunk_count,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                chunks: list[Chunk] = []
                for _ in range(chunk_count):
                    (c_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    chunk_id = raw[pos : pos + c_len].decode("utf-8")
                    pos += c_len
                    (content_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    content = raw[pos : pos + content_len].decode("utf-8")
                    pos += content_len
                    (title_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    title = raw[pos : pos + title_len].decode("utf-8")
                    pos += title_len
                    (meta_len2,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    metadata = json.loads(raw[pos : pos + meta_len2].decode("utf-8"))
                    pos += meta_len2
                    (emb_len,) = struct.unpack_from("<I", raw, pos)
                    pos += 4
                    embedding: array | None = None
                    if emb_len:
                        embedding = array(_EMB_DTYPE)
                        embedding.frombytes(raw[pos : pos + emb_len * 4])
                        # 尾部截断但字节数是 4 的倍数时 frombytes 不报错，会静默产出
                        # 短向量（挂到 chunk 上即"看起来像回事"的错分值），必须自校验。
                        if len(embedding) != emb_len:
                            return None
                        pos += emb_len * 4
                    chunks.append(Chunk(chunk_id, content, source, title, metadata, embedding=embedding))
                files[source] = (signature, chunks)
            return meta, files
        except (struct.error, UnicodeDecodeError, json.JSONDecodeError, ValueError, OverflowError):
            return None


class _VectorsCodec:
    """Vector-layer cache: chunk id -> float32 embedding (zlib).

    Kept separate from the chunks layer so embedding model/dimension changes
    only invalidate vectors while the text chunks stay reusable.
    """

    _MAGIC = b"VMCPV"
    _VERSION = 1

    @staticmethod
    def dump(path: Path, meta: dict[str, Any], vectors: dict[str, array]) -> None:
        buf = bytearray()
        buf += _VectorsCodec._MAGIC
        buf += struct.pack("<B", _VectorsCodec._VERSION)
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        _pack_u32(buf, len(meta_bytes))
        buf += meta_bytes
        _pack_u32(buf, len(vectors))
        for chunk_id in sorted(vectors):
            embedding = vectors[chunk_id]
            _pack_str(buf, chunk_id)
            _pack_u32(buf, len(embedding))
            if len(embedding):
                buf += embedding.tobytes()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(zlib.compress(bytes(buf), 6))
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> tuple[dict[str, Any], dict[str, array]] | None:
        if not path.exists():
            return None
        raw = _decompress_bounded(path)
        if raw is None:
            return None
        # 同 _CacheCodec：截断文件不得抛 IndexError（静默返回 None 是契约）
        if raw[: len(_VectorsCodec._MAGIC)] != _VectorsCodec._MAGIC or len(raw) <= len(_VectorsCodec._MAGIC):
            return None
        pos = len(_VectorsCodec._MAGIC)
        version = raw[pos]
        pos += 1
        if version != _VectorsCodec._VERSION:
            return None
        try:
            (meta_len,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            meta = json.loads(raw[pos : pos + meta_len].decode("utf-8"))
            pos += meta_len
            (count,) = struct.unpack_from("<I", raw, pos)
            pos += 4
            vectors: dict[str, array] = {}
            for _ in range(count):
                (cid_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                chunk_id = raw[pos : pos + cid_len].decode("utf-8")
                pos += cid_len
                (emb_len,) = struct.unpack_from("<I", raw, pos)
                pos += 4
                embedding: array | None = None
                if emb_len:
                    embedding = array(_EMB_DTYPE)
                    embedding.frombytes(raw[pos : pos + emb_len * 4])
                    # 同 _CacheCodec：截断的尾部向量不得静默降级为短向量
                    if len(embedding) != emb_len:
                        return None
                    pos += emb_len * 4
                vectors[chunk_id] = embedding
            return meta, vectors
        except (struct.error, UnicodeDecodeError, json.JSONDecodeError, ValueError, OverflowError):
            return None
