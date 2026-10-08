from types import SimpleNamespace
import wave

import pytest

from mortis_rag_mcp.ingest.audio import AudioUnsupported, inspect_wav, iter_segments, parse_audio


def config(**kwargs):
    return SimpleNamespace(audio_enabled=True, audio_segment_seconds=2, audio_overlap_seconds=1, **kwargs)


def wav(path, frames=24000):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * frames)
    return path


def test_metadata_only_and_overlap_no_lost_tail(tmp_path):
    path = wav(tmp_path / "sound.wav", 28001)
    segments = list(iter_segments(path, config()))
    assert [(s.start_frame, s.end_frame) for s in segments] == [(0, 16000), (8000, 24000), (16000, 28001)]
    assert segments[-1].end_frame == 28001
    result = parse_audio(path, config())
    assert result.capabilities["status"] == "metadata_only"
    assert result.capabilities["semantic_audio"] is False
    assert result.capabilities["transcript"] is False
    assert result.capabilities["line_basis"] == "metadata"


def test_disabled_and_unverified_endpoint(tmp_path):
    path = wav(tmp_path / "sound.wav")
    cfg = config()
    cfg.audio_enabled = False
    with pytest.raises(AudioUnsupported, match="authorization"):
        inspect_wav(path, cfg)
    with pytest.raises(AudioUnsupported, match="unverified"):
        parse_audio(path, config(audio_adapter="transcript"))


def test_truncated_audio_and_frame_budget(tmp_path):
    path = wav(tmp_path / "sound.wav")
    path.write_bytes(path.read_bytes()[:-4])
    with pytest.raises(AudioUnsupported, match="truncated"):
        list(iter_segments(path, config()))
    with pytest.raises(AudioUnsupported, match="frame budget"):
        inspect_wav(path, config(audio_max_frames=10))


def test_cancel_and_segment_budget(tmp_path):
    path = wav(tmp_path / "sound.wav")
    with pytest.raises(AudioUnsupported, match="cancelled"):
        list(iter_segments(path, config(), cancelled=lambda: True))
    with pytest.raises(AudioUnsupported, match="segment count"):
        inspect_wav(path, config(audio_max_segments=1))


def test_verified_local_transcript_time_metadata(tmp_path):
    path = wav(tmp_path / "sound.wav")
    class Adapter:
        evidence_reference = "test-fixture-only"
        fingerprint = "fixture-v1"
        local_only = True
        def transcribe(self, segment):
            return {"text": "same words"}
    class Sink:
        def __init__(self):
            self.items = []
        def add(self, **kwargs):
            self.items.append(kwargs)
    sink = Sink()
    result = parse_audio(path, config(), adapter=Adapter(), sink=sink)
    assert result.capabilities["line_basis"] == "transcript"
    assert sink.items[0]["t_start_ms"] == 0
    assert sink.items[0]["t_end_ms"] == 2000
    assert sink.items[0]["metadata"]["parent_source_sha256"]
    assert "[0-2000 ms] same words" in result.markdown
    assert result.capabilities["semantic_audio"] is False


class _RecordingAdapter:
    evidence_reference = "test-fixture-only"
    fingerprint = "fixture-v1"
    local_only = True

    def __init__(self):
        self.calls = []

    def transcribe(self, segment):
        self.calls.append(int(segment.ordinal))
        return {"text": f"words-{segment.ordinal}"}


class _RecordingSink:
    def __init__(self):
        self.items = []

    def add(self, **kwargs):
        self.items.append(kwargs)


def test_partial_checkpoint_idempotent_and_resume_only_missing(tmp_path):
    path = wav(tmp_path / "sound.wav", 28001)  # 3 段：(0,16000)(8000,24000)(16000,28001)
    cfg = config()
    records = {}

    def checkpoint(segment, transcript):
        records[int(segment.ordinal)] = {
            "ordinal": int(segment.ordinal),
            "sha256": segment.input_sha256,
            "t_start_ms": segment.t_start_ms,
            "t_end_ms": segment.t_end_ms,
            "text": transcript,
        }

    adapter1, sink1 = _RecordingAdapter(), _RecordingSink()
    first = parse_audio(path, cfg, adapter=adapter1, sink=sink1, checkpoint=checkpoint)
    assert adapter1.calls == [1, 2, 3]
    assert len(sink1.items) == 3
    assert sink1.items[0]["t_start_ms"] == 0 and sink1.items[0]["t_end_ms"] == 2000
    assert first.capabilities["completed_ordinals"] == [1, 2, 3]
    assert first.capabilities["resumed_ordinals"] == []
    assert len(first.capabilities["coverage_ms"]) == 3

    # 全量 resume：不重转录、不重落库、markdown/时间锚定幂等
    adapter2, sink2 = _RecordingAdapter(), _RecordingSink()
    marks = []
    second = parse_audio(path, cfg, adapter=adapter2, sink=sink2, resume=records,
                         checkpoint=lambda segment, transcript: marks.append(segment.ordinal))
    assert adapter2.calls == []
    assert sink2.items == []
    assert marks == []  # 已完成片段不重记 checkpoint
    assert second.capabilities["resumed_ordinals"] == [1, 2, 3]
    assert second.markdown == first.markdown

    # 部分 resume（只完成 ordinal 1）：只补 2、3
    adapter3, sink3 = _RecordingAdapter(), _RecordingSink()
    third = parse_audio(path, cfg, adapter=adapter3, sink=sink3, resume={1: records[1]})
    assert adapter3.calls == [2, 3]
    assert [item["ordinal"] for item in sink3.items] == [2, 3]
    assert third.capabilities["resumed_ordinals"] == [1]
    assert third.markdown == first.markdown


def test_resume_sha_mismatch_recomputes_segment(tmp_path):
    """片段内容 SHA 与 checkpoint 不符（配置变化/源重写）必须重算，绝不复用旧转录。"""
    path = wav(tmp_path / "sound.wav", 28001)
    stale = {1: {"ordinal": 1, "sha256": "not-the-current-segment", "text": "stale words"}}
    adapter, sink = _RecordingAdapter(), _RecordingSink()
    result = parse_audio(path, config(), adapter=adapter, sink=sink, resume=stale)
    assert adapter.calls == [1, 2, 3]
    assert result.capabilities["resumed_ordinals"] == []
    assert "stale words" not in result.markdown
