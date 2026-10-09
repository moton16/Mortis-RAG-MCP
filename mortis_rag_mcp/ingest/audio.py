"""Bounded core PCM WAV segmentation plus explicit decoder/transcript seams.

E09：本模块只做**有界**音频读取与段调度。非 PCM（MP3/M4A/FLAC/非 PCM WAV）必须由
**显式配置且显式注入**的本地解码组件处理；没有该组件时给出明确的能力错误——不隐式
启动 shell/ffmpeg、不联网安装、不下载模型、不猜服务协议、也不把音频送 MinerU 文档通道。
"""
from __future__ import annotations

import hashlib
import io
import json
import wave
from inspect import signature
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import ParseResult, ResourceLimits


class AudioUnsupported(ValueError):
    pass


class AudioSettings:
    """`IngestConfig` + 独立 `AudioConfig` 的只读合成视图（server→factory→worker→audio）。

    为什么需要它：只有把真实的 `AudioConfig` 沿装配链传下去，`_setting` 才读得到真实
    音频参数；但**不能因此丢掉** `ingest.audio_enabled` / `network_policy` 等 ingest 字段。
    本视图让 `audio.*` 由传入的 `AudioConfig` 提供、其余属性代理给 ingest 配置；`audio`
    为 None 时自动退回旧的 flat（`audio_segment_seconds` …）读取路径。
    """

    __slots__ = ("_ingest", "audio")

    def __init__(self, ingest: Any, audio: Any = None) -> None:
        object.__setattr__(self, "_ingest", ingest)
        object.__setattr__(self, "audio", audio)

    def __getattr__(self, name: str) -> Any:
        # 顶层读取顺序：audio 专用键优先（合成视图语义），再回 ingest 字段。
        if not name.startswith("_"):
            audio = object.__getattribute__(self, "audio")
            if audio is not None:
                try:
                    return getattr(audio, name)
                except AttributeError:
                    pass
        return getattr(object.__getattribute__(self, "_ingest"), name)


def audio_settings(config: Any, audio: Any = None) -> Any:
    """构造 audio 读取视图；已是带 `.audio` 的配置时不重复包装。"""
    if audio is None and getattr(config, "audio", None) is not None:
        return config
    return AudioSettings(config, audio)


_RIFF_PCM_TAGS = (1, 0xFFFE)  # PCM / WAVE_FORMAT_EXTENSIBLE
_ADMITTED_SAMPLE_RATES = {8000, 16000, 22050, 24000, 32000, 44100, 48000}


def detect_audio_format(path: Path) -> str:
    """按容器/编码头分流（只读头部，不猜）。

    返回 `wav_pcm` / `wav_other` / `mp3` / `m4a` / `flac` / `unknown`。只有 `wav_pcm`
    能走核心 `wave` 读取；其余格式必须有显式注入且声明该格式能力的解码组件。
    """
    with path.open("rb") as stream:
        head = stream.read(64)
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav_pcm" if _riff_is_pcm(head) else "wav_other"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    return "unknown"


def _riff_is_pcm(head: bytes) -> bool:
    """读 RIFF `fmt ` 块的 wFormatTag：只有 PCM/EXTENSIBLE 才是核心可读 PCM。"""
    index = 12
    while index + 8 <= len(head):
        chunk_id = head[index:index + 4]
        size = int.from_bytes(head[index + 4:index + 8], "little")
        if chunk_id == b"fmt ":
            if index + 10 > len(head):
                return False
            return int.from_bytes(head[index + 8:index + 10], "little") in _RIFF_PCM_TAGS
        index += 8 + size + (size & 1)
    return False


class AudioDecoder(Protocol):
    """显式注入的本地解码组件（E09 接缝）。

    `supported_formats` 必须声明它**真实**能解码的格式名（`detect_audio_format` 的返回值）；
    未声明的格式一律明确拒绝。`probe` 必须给出精确帧数（分段/限额/时间都依赖它，不能猜）。
    本仓库不内置任何解码器：MP3/M4A/FLAC 出口在注入受验证组件之前是 blocked。
    """

    name: str
    fingerprint: str
    supported_formats: frozenset[str]

    def probe(self, path: Path) -> dict[str, int]: ...

    def read_frames(self, path: Path, start_frame: int, count: int) -> bytes: ...


def decoder_fingerprint(decoder: Any) -> str:
    """解码器处理代际（进 metadata/指纹；不用于伪装格式支持）。"""
    if decoder is None:
        return "core-pcm-v1"
    return f"{getattr(decoder, 'name', 'decoder')}:{getattr(decoder, 'fingerprint', '') or 'unversioned'}"


@dataclass(frozen=True)
class AudioSegment:
    ordinal: int
    start_frame: int
    end_frame: int
    t_start_ms: int
    t_end_ms: int
    input_sha256: str
    data: bytes


class VerifiedTranscriptAdapter(Protocol):
    evidence_reference: str
    fingerprint: str
    local_only: bool

    def transcribe(self, segment: AudioSegment) -> dict[str, Any]: ...


def _setting(config: Any, key: str, default: Any):
    audio = getattr(config, "audio", None)
    return getattr(audio, key, getattr(config, "audio_" + key, default))


def _integer(config: Any, key: str, default: int, minimum: int = 1):
    value = _setting(config, key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AudioUnsupported(f"invalid audio.{key}")
    return value


def audio_profile_fingerprint(config: Any, *, adapter: Any = None, decoder: Any = None) -> str:
    """音频处理 profile 指纹：参数/解码组件/转录 adapter 任一变化都不得复用旧任务或旧片段。

    该指纹进 job `parser_fingerprint` 与 `ParseResult.parser_fingerprint`：不同分段/重叠/
    解码/转录 profile 因此既不会命中同一任务合并，也不会被当成同一个已确认事实。
    """
    payload = {
        "segment_seconds": _setting(config, "segment_seconds", 30),
        "overlap_seconds": _setting(config, "overlap_seconds", 5),
        "max_segments": _setting(config, "max_segments", 1000),
        "max_channels": _setting(config, "max_channels", 2),
        "max_sample_rate": _setting(config, "max_sample_rate", 48000),
        "max_duration_seconds": _setting(config, "max_duration_seconds", 3600),
        "max_segment_mb": _setting(config, "max_segment_mb", 8),
        "decoder": str(_setting(config, "decoder", "core_pcm")),
        "decoder_impl": decoder_fingerprint(decoder),
        "adapter": str(_setting(config, "adapter", "")),
        "adapter_impl": str(getattr(adapter, "fingerprint", "") or ""),
        "adapter_evidence": str(getattr(adapter, "evidence_reference", "") or ""),
        "transcription_model": str(_setting(config, "transcription_model", "")),
        "language": str(getattr(config, "language", "") or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


def _validate_pcm_metadata(config: Any, *, fmt: str, channels: int, sample_width: int,
                           rate: int, frames: int) -> dict[str, Any]:
    """PCM 元数据限额准入（核心 WAV 与注入解码器共用同一合同，不因来源放宽）。"""
    if sample_width not in {1, 2, 3, 4}:
        raise AudioUnsupported("unsupported PCM width/compression")
    if not 1 <= channels <= _integer(config, "max_channels", 2):
        raise AudioUnsupported("audio channels outside admitted contract")
    if rate not in _ADMITTED_SAMPLE_RATES or rate > _integer(config, "max_sample_rate", 48000):
        raise AudioUnsupported("audio sample rate outside admitted contract; no resampling")
    if frames <= 0 or frames > rate * _integer(config, "max_duration_seconds", 3600):
        raise AudioUnsupported("empty audio or duration budget exceeded")
    if frames > _integer(config, "max_frames", 172800000):
        raise AudioUnsupported("audio frame budget exceeded")
    step = _integer(config, "segment_seconds", 30) - _integer(config, "overlap_seconds", 5, 0)
    if step <= 0:
        raise AudioUnsupported("overlap must be smaller than segment")
    segments = 1 + max(0, (frames - rate * _integer(config, "segment_seconds", 30)
                           + rate * step - 1) // (rate * step))
    if segments > _integer(config, "max_segments", 1000):
        raise AudioUnsupported("audio segment count budget exceeded")
    return {"format": fmt, "channels": channels, "sample_width": sample_width, "sample_rate": rate,
            "frames": frames, "duration_ms": frames * 1000 // rate, "segments": segments}


def inspect_wav(path: Path, config: Any) -> dict[str, Any]:
    """核心 PCM WAV 检查（保留旧签名；格式分流入口是 `inspect_audio`）。"""
    if not getattr(config, "audio_enabled", False):
        raise AudioUnsupported("audio ingest requires explicit audio_enabled authorization")
    if _setting(config, "decoder", "core_pcm") != "core_pcm":
        raise AudioUnsupported("audio decoder requires a verified injected adapter")
    if path.suffix.lower() != ".wav":
        raise AudioUnsupported("core decoder supports PCM WAV only; no implicit ffmpeg")
    if path.stat().st_size > _integer(config, "max_input_mb", 20) * 1024 * 1024:
        raise AudioUnsupported("audio source byte budget exceeded")
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.getnframes()
            compression = source.getcomptype()
    except (wave.Error, EOFError) as exc:
        raise AudioUnsupported("invalid/non-PCM WAV header") from exc
    if compression != "NONE":
        raise AudioUnsupported("unsupported PCM width/compression")
    return _validate_pcm_metadata(config, fmt="wav_pcm", channels=channels,
                                  sample_width=width, rate=rate, frames=frames)


def _decode_metadata(path: Path, config: Any, decoder: Any, fmt: str) -> dict[str, Any]:
    """非 PCM 格式：必须显式配置解码器**并**注入声明该格式能力的组件。"""
    declared = str(_setting(config, "decoder", "core_pcm"))
    if declared == "core_pcm":
        raise AudioUnsupported(
            f"{fmt} requires an explicitly configured decoder; core_pcm decodes PCM WAV only"
        )
    if decoder is None:
        raise AudioUnsupported(
            f"decoder {declared!r} declared but no verified decoder component injected; "
            f"{fmt} is blocked (no implicit shell/ffmpeg)"
        )
    if fmt not in set(getattr(decoder, "supported_formats", ()) or ()):
        raise AudioUnsupported(
            f"injected decoder {getattr(decoder, 'name', '?')!r} does not declare {fmt} capability"
        )
    if path.stat().st_size > _integer(config, "max_input_mb", 20) * 1024 * 1024:
        raise AudioUnsupported("audio source byte budget exceeded")
    try:
        probed = dict(decoder.probe(path))
    except AudioUnsupported:
        raise
    except Exception as exc:
        raise AudioUnsupported(f"decoder probe failed for {fmt}: {exc}") from exc
    try:
        channels = int(probed.get("channels") or 0)
        sample_width = int(probed.get("sample_width") or 0)
        rate = int(probed.get("sample_rate") or 0)
        frames = int(probed.get("frames") or 0)
    except (TypeError, ValueError) as exc:
        raise AudioUnsupported(f"decoder {fmt} probe must report exact integer PCM metadata") from exc
    return _validate_pcm_metadata(config, fmt=fmt, channels=channels,
                                  sample_width=sample_width, rate=rate, frames=frames)


def inspect_audio(path: Path, config: Any, *, decoder: Any = None) -> dict[str, Any]:
    """按**实际格式**分流的能力/限额检查（入队前与解析前同一入口，避免无条件按 WAV 拒）。

    - `wav_pcm`：核心 `wave` 读取；
    - `mp3`/`m4a`/`flac`/`wav_other`：需要显式配置解码器 + 注入声明该格式的组件，否则明确
      能力错误（既不先送 `inspect_wav` 猜，也不送 MinerU 文档通道冒音频支持）；
    - `unknown`：不是可识别的音频容器，明确拒绝。
    """
    if not getattr(config, "audio_enabled", False):
        raise AudioUnsupported("audio ingest requires explicit audio_enabled authorization")
    fmt = detect_audio_format(path)
    if fmt == "wav_pcm":
        return inspect_wav(path, config)
    if fmt == "unknown":
        raise AudioUnsupported(
            f"unrecognized audio container ({path.suffix.lower() or 'no suffix'}): "
            "no decoder capability"
        )
    return _decode_metadata(path, config, decoder, fmt)


class _PcmReader:
    """统一的有界 PCM 读面：核心 WAV 与注入解码器都归结为 `read(start_frame, count)`。"""

    def __init__(self, path: Path, metadata: dict[str, Any], decoder: Any) -> None:
        self._path = path
        self._metadata = metadata
        self._decoder = decoder
        self._stream: Any = None

    def __enter__(self) -> "_PcmReader":
        if self._metadata["format"] == "wav_pcm":
            self._stream = wave.open(str(self._path), "rb")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def read(self, start: int, count: int) -> bytes:
        if self._stream is not None:
            self._stream.setpos(start)
            return self._stream.readframes(count)
        return bytes(self._decoder.read_frames(self._path, start, count) or b"")


def iter_segments(path: Path, config: Any, *, cancelled=lambda: False, decoder: Any = None):
    metadata = inspect_audio(path, config, decoder=decoder)
    rate, frames = metadata["sample_rate"], metadata["frames"]
    size = rate * _integer(config, "segment_seconds", 30)
    step = size - rate * _integer(config, "overlap_seconds", 5, 0)
    max_bytes = _integer(config, "max_segment_mb", 8) * 1024 * 1024
    if min(size, frames) * metadata["channels"] * metadata["sample_width"] + 44 > max_bytes:
        raise AudioUnsupported("PCM segment exceeds byte budget; lower segment_seconds")
    with _PcmReader(path, metadata, decoder) as source:
        start = 0
        ordinal = 1
        while start < frames:
            if cancelled():
                raise AudioUnsupported("audio cancelled")
            end = min(start + size, frames)
            raw = source.read(start, end - start)
            if len(raw) != (end - start) * metadata["channels"] * metadata["sample_width"]:
                raise AudioUnsupported("truncated PCM frames; no partial frame publication")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as target:
                target.setnchannels(metadata["channels"])
                target.setsampwidth(metadata["sample_width"])
                target.setframerate(rate)
                target.writeframes(raw)
            data = buffer.getvalue()
            yield AudioSegment(ordinal, start, end, start * 1000 // rate, end * 1000 // rate,
                               hashlib.sha256(data).hexdigest(), data)
            if end == frames:
                break
            start += step
            ordinal += 1


def parse_audio(path: Path, config: Any, *, sink: Any = None, adapter: Any = None,
                cancelled=lambda: False, checkpoint=None, resume=None,
                decoder: Any = None) -> ParseResult:
    """PCM WAV（或显式解码源）→ 代理 chunks（§16）。

    `resume`：已持久化的片段 checkpoint，映射 `{ordinal: {"sha256":..., "text":...}}`。
    **只补缺失片段**：当且仅当**片段内容 SHA 与记录一致**时跳过转录与 sink 落库（幂等，
    不重复存储、不重新计费）；时间范围仍计入 coverage。无 checkpoint 时全量处理。
    `checkpoint(segment, transcript)`：每处理完一个**新**片段回调一次，供 worker 持久化
    （partial coverage / resume 的事实来源）。

    非 PCM 源必须传 `decoder`（显式注入、声明该格式能力）；否则 `inspect_audio` 报明确
    能力错误。转录 adapter 与解码器是**两个**独立接缝：解码成功不等于转录成功。
    """
    metadata = inspect_audio(path, config, decoder=decoder)
    mode = _setting(config, "adapter", "") or "metadata_only"
    if mode != "metadata_only" and adapter is None:
        raise AudioUnsupported("transcript/native adapter contract unverified; unsupported")
    if adapter is not None:
        if not getattr(adapter, "evidence_reference", "") or not getattr(adapter, "fingerprint", ""):
            raise AudioUnsupported("adapter requires evidence reference and preprocessing fingerprint")
        if getattr(config, "network_policy", "configured") == "local_only" and not getattr(adapter, "local_only", False):
            raise AudioUnsupported("local_only forbids remote audio adapter")
        # Paid scheduling needs persistent send intents, not a guessed HTTP implementation.
        if not getattr(adapter, "local_only", False):
            raise AudioUnsupported("remote audio adapter requires durable paid-request scheduler")
    resume_map = dict(resume or {})
    limits = ResourceLimits.from_config(config)
    # 转录 timeout：`audio.transcription_timeout` 必须真正被消费。adapter 显式声明
    # `timeout` 形参时透传；旧 1 参 adapter 保持兼容（不强行改签名）。
    _adapter_timeout: float | None = None
    if adapter is not None:
        try:
            if "timeout" in signature(adapter.transcribe).parameters:
                _adapter_timeout = float(_setting(config, "transcription_timeout", 30.0))
        except (TypeError, ValueError):
            _adapter_timeout = None
    # 兼容旧的 2 参 checkpoint 回调（不强制消费媒体定位）。
    try:
        _checkpoint_accepts_media = len(signature(checkpoint).parameters) >= 3
    except (TypeError, ValueError):
        _checkpoint_accepts_media = False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    parent_sha = digest.hexdigest()
    fmt = str(metadata.get("format") or "wav_pcm")
    decoder_impl = decoder_fingerprint(decoder)
    profile = audio_profile_fingerprint(config, adapter=adapter, decoder=decoder)
    preprocess = (getattr(adapter, "fingerprint", "") or decoder_impl) if adapter else decoder_impl
    lines = [f"# Audio: {path.stem}", "", f"Duration: {metadata['duration_ms']} ms", ""]
    coverage = []
    completed: list[int] = []
    resumed: list[int] = []
    for segment in iter_segments(path, config, cancelled=cancelled, decoder=decoder):
        ordinal = int(segment.ordinal)
        prior = resume_map.get(ordinal)
        # 仅当内容 SHA 一致才算「已完成」：片段内容漂移（配置变化/文件重写）必须重算。
        already_done = bool(prior) and str(prior.get("sha256", "")) == segment.input_sha256
        transcript = ""
        if already_done:
            transcript = str(prior.get("text", ""))
            resumed.append(ordinal)
        else:
            if adapter is not None:
                response = (adapter.transcribe(segment) if _adapter_timeout is None
                            else adapter.transcribe(segment, timeout=_adapter_timeout))
                transcript = str(response.get("text", ""))
                if len(transcript.encode("utf-8")) > limits.markdown_max_bytes:
                    raise AudioUnsupported("transcript output byte budget exceeded")
            media_ref: dict[str, Any] | None = None
            if sink is not None:
                occurrence_id = sink.add(
                    name=f"segment-{ordinal}.wav", data=segment.data, kind="audio",
                    ordinal=ordinal, mime_type="audio/wav", caption=transcript,
                    t_start_ms=segment.t_start_ms, t_end_ms=segment.t_end_ms,
                    metadata={"parent_source_sha256": parent_sha, "segment_sha256": segment.input_sha256,
                              "start_frame": segment.start_frame, "end_frame": segment.end_frame,
                              "source_format": fmt, "source_sha256": parent_sha,
                              "decoder": decoder_impl, "profile_fingerprint": profile,
                              "preprocess_version": preprocess})
                # E02：媒体定位必须进 checkpoint —— 否则恢复时省了转录却丢了媒体。
                items = getattr(sink, "items", None)
                blob_id = ""
                if items:
                    blob_id = str(getattr(items[-1], "blob_id", "") or "")
                media_ref = {"occurrence_id": str(occurrence_id or ""), "blob_id": blob_id,
                             "kind": "audio", "mime_type": "audio/wav",
                             "t_start_ms": int(segment.t_start_ms), "t_end_ms": int(segment.t_end_ms),
                             "caption": transcript}
        if transcript:
            lines.extend([f"[{segment.t_start_ms}-{segment.t_end_ms} ms] {transcript}", ""])
        coverage.append([segment.t_start_ms, segment.t_end_ms])
        completed.append(ordinal)
        if not already_done and checkpoint is not None:
            if _checkpoint_accepts_media:
                checkpoint(segment, transcript, media_ref)
            else:
                checkpoint(segment, transcript)
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > limits.markdown_max_bytes:
        raise AudioUnsupported("audio markdown budget exceeded")
    return ParseResult(text, f"audio-{fmt}-v1:{profile}",
                       "local", str(_setting(config, "decoder", "core_pcm")), quality="partial",
                       capabilities={"audio": True, "semantic_audio": False, "transcript": adapter is not None,
                                     "status": "transcript" if adapter else "metadata_only",
                                     "line_basis": "transcript" if adapter else "metadata",
                                     "source_format": fmt, "decoder": decoder_impl,
                                     "profile_fingerprint": profile,
                                     "coverage_ms": coverage, "completed_ordinals": sorted(completed),
                                     "resumed_ordinals": sorted(resumed), "total_segments": metadata["segments"],
                                     "audio_metadata": metadata},
                       warnings=[] if adapter else ["metadata_only: speech content is not searchable"])
