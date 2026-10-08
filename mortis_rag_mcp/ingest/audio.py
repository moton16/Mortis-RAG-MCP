"""Bounded core PCM WAV segmentation. No implicit decoder or paid protocol."""
from __future__ import annotations

import hashlib
import io
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import ParseResult, ResourceLimits


class AudioUnsupported(ValueError):
    pass


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


def inspect_wav(path: Path, config: Any) -> dict[str, int]:
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
            channels, width, rate, frames = source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()
            if source.getcomptype() != "NONE" or width not in {1, 2, 3, 4}:
                raise AudioUnsupported("unsupported PCM width/compression")
            if not 1 <= channels <= _integer(config, "max_channels", 2):
                raise AudioUnsupported("audio channels outside admitted contract")
            if rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000} or rate > _integer(config, "max_sample_rate", 48000):
                raise AudioUnsupported("audio sample rate outside admitted contract; no resampling")
            if frames <= 0 or frames > rate * _integer(config, "max_duration_seconds", 3600):
                raise AudioUnsupported("empty audio or duration budget exceeded")
            if frames > _integer(config, "max_frames", 172800000):
                raise AudioUnsupported("audio frame budget exceeded")
            step = _integer(config, "segment_seconds", 30) - _integer(config, "overlap_seconds", 5, 0)
            if step <= 0:
                raise AudioUnsupported("overlap must be smaller than segment")
            segments = 1 + max(0, (frames - rate * _integer(config, "segment_seconds", 30) + rate * step - 1) // (rate * step))
            if segments > _integer(config, "max_segments", 1000):
                raise AudioUnsupported("audio segment count budget exceeded")
            return {"channels": channels, "sample_width": width, "sample_rate": rate, "frames": frames,
                    "duration_ms": frames * 1000 // rate, "segments": segments}
    except (wave.Error, EOFError) as exc:
        raise AudioUnsupported("invalid/non-PCM WAV header") from exc


def iter_segments(path: Path, config: Any, *, cancelled=lambda: False):
    metadata = inspect_wav(path, config)
    rate, frames = metadata["sample_rate"], metadata["frames"]
    size = rate * _integer(config, "segment_seconds", 30)
    step = size - rate * _integer(config, "overlap_seconds", 5, 0)
    max_bytes = _integer(config, "max_segment_mb", 8) * 1024 * 1024
    if min(size, frames) * metadata["channels"] * metadata["sample_width"] + 44 > max_bytes:
        raise AudioUnsupported("PCM segment exceeds byte budget; lower segment_seconds")
    with wave.open(str(path), "rb") as source:
        start = 0
        ordinal = 1
        while start < frames:
            if cancelled():
                raise AudioUnsupported("audio cancelled")
            end = min(start + size, frames)
            source.setpos(start)
            raw = source.readframes(end - start)
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
                cancelled=lambda: False, checkpoint=None, resume=None) -> ParseResult:
    """PCM WAV → 代理 chunks（§16）。

    `resume`：已持久化的片段 checkpoint，映射 `{ordinal: {"sha256":..., "text":...}}`。
    **只补缺失片段**：当且仅当**片段内容 SHA 与记录一致**时跳过转录与 sink 落库（幂等，
    不重复存储、不重新计费）；时间范围仍计入 coverage。无 checkpoint 时全量处理。
    `checkpoint(segment, transcript)`：每处理完一个**新**片段回调一次，供 worker 持久化
    （partial coverage / resume 的事实来源）。
    """
    metadata = inspect_wav(path, config)
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
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    parent_sha = digest.hexdigest()
    lines = [f"# Audio: {path.stem}", "", f"Duration: {metadata['duration_ms']} ms", ""]
    coverage = []
    completed: list[int] = []
    resumed: list[int] = []
    for segment in iter_segments(path, config, cancelled=cancelled):
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
                response = adapter.transcribe(segment)
                transcript = str(response.get("text", ""))
                if len(transcript.encode("utf-8")) > limits.markdown_max_bytes:
                    raise AudioUnsupported("transcript output byte budget exceeded")
            if sink is not None:
                sink.add(name=f"segment-{ordinal}.wav", data=segment.data, kind="audio",
                         ordinal=ordinal, mime_type="audio/wav", caption=transcript,
                         t_start_ms=segment.t_start_ms, t_end_ms=segment.t_end_ms,
                         metadata={"parent_source_sha256": parent_sha, "segment_sha256": segment.input_sha256,
                                   "start_frame": segment.start_frame, "end_frame": segment.end_frame,
                                   "preprocess_version": getattr(adapter, "fingerprint", "core-pcm-v1")})
        if transcript:
            lines.extend([f"[{segment.t_start_ms}-{segment.t_end_ms} ms] {transcript}", ""])
        coverage.append([segment.t_start_ms, segment.t_end_ms])
        completed.append(ordinal)
        if not already_done and checkpoint is not None:
            checkpoint(segment, transcript)
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > limits.markdown_max_bytes:
        raise AudioUnsupported("audio markdown budget exceeded")
    return ParseResult(text, "audio-pcm-v1:" + getattr(adapter, "fingerprint", "metadata_only"),
                       "local", "core_pcm", quality="partial",
                       capabilities={"audio": True, "semantic_audio": False, "transcript": adapter is not None,
                                     "status": "transcript" if adapter else "metadata_only",
                                     "line_basis": "transcript" if adapter else "metadata",
                                     "coverage_ms": coverage, "completed_ordinals": sorted(completed),
                                     "resumed_ordinals": sorted(resumed), "total_segments": metadata["segments"],
                                     "audio_metadata": metadata},
                       warnings=[] if adapter else ["metadata_only: speech content is not searchable"])
