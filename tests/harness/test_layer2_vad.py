"""Layer 2 (Silero VAD) tests.

Speed:    feed() latency P50/P95
Quality:  per-fixture: detected speech_started, utterance_ended, speech duration
Robust:   noisy / quiet / double-utterance / long-gap behavior

These tests are read-only on real fixtures; no API calls. Skip if Silero
ONNX model isn't downloaded.
"""
from __future__ import annotations

import os

import pytest

from tests.harness._utils import (
    SAMPLE_RATE,
    Stat,
    chunk_pcm_at,
    load_pcm48k_mono,
    resample_48k_to_16k,
    time_ms,
)
from echotwin.providers.vad.silero import SileroVAD

MODEL_PATH = "models/silero_vad/src/silero_vad/data/silero_vad.onnx"
pytestmark = pytest.mark.skipif(
    not os.path.exists(MODEL_PATH),
    reason="silero VAD model not downloaded",
)


def _new_vad() -> SileroVAD:
    """Match production config (config.yaml current values)."""
    return SileroVAD(
        threshold=0.4,
        threshold_low=0.2,
        min_silence_duration_ms=800,
        frame_window=2,
    )


def _run_vad_on_fixture(tag: str, tail_silence_ms: int = 1500) -> dict:
    """Feed a fixture through VAD frame-by-frame; return event timeline + timing.

    Appends extra silence at end so VAD always has a chance to fire
    utterance_ended (fixture tail padding alone may not exceed threshold).
    """
    import numpy as np

    vad = _new_vad()
    pcm_48k = load_pcm48k_mono(tag)
    pcm_16k = resample_48k_to_16k(pcm_48k)
    # Append silence to ensure VAD sees enough silent chunks to endpoint
    silence_16k = np.zeros(int(16000 * tail_silence_ms / 1000), dtype=np.int16)
    pcm_16k = np.concatenate([pcm_16k, silence_16k])

    feed_stat = Stat(name=f"vad_feed_{tag}")
    starts: list[int] = []  # frame indices where speech_started
    ends: list[int] = []    # frame indices where utterance_ended
    voice_frames = 0

    for i, frame in enumerate(chunk_pcm_at(pcm_16k, 16000, frame_ms=20)):
        with time_ms() as t:
            r = vad.feed(frame)
        feed_stat.add(t.elapsed_ms / 1000.0)
        if r.speech_started:
            starts.append(i)
        if r.utterance_ended:
            ends.append(i)
        if r.is_voice:
            voice_frames += 1

    return {
        "tag": tag,
        "starts": starts,
        "ends": ends,
        "voice_frames": voice_frames,
        "total_frames": len(pcm_16k) // (16000 // 50),  # 20ms = 16000/50 samples
        "feed_stat": feed_stat,
    }


# --- Speed ------------------------------------------------------------

def test_vad_feed_latency_under_5ms():
    """A single feed() call (320 samples = 20ms PCM) should be <5ms P95."""
    r = _run_vad_on_fixture("medium")
    s = r["feed_stat"]
    print(f"\n  {s.fmt_ms()}")
    assert s.p95 < 0.005, f"VAD feed P95 too slow: {s.p95*1000:.1f}ms"


# --- Quality ----------------------------------------------------------

@pytest.mark.parametrize("tag", ["short", "medium", "long", "wake_query"])
def test_vad_detects_one_utterance_in_clean(tag):
    """Each clean single-utterance fixture should fire exactly 1 START + 1 END."""
    r = _run_vad_on_fixture(tag)
    print(
        f"\n  {tag}: starts={r['starts']} ends={r['ends']} "
        f"voice_frames={r['voice_frames']}/{r['total_frames']}"
    )
    assert len(r["starts"]) == 1, f"expected 1 SPEECH_START, got {len(r['starts'])}"
    assert len(r["ends"]) == 1, f"expected 1 utterance_ended, got {len(r['ends'])}"
    # Voice should be detected for at least 10% of frames (we append 1.5s silence)
    voice_ratio = r["voice_frames"] / r["total_frames"]
    assert voice_ratio > 0.1, f"voice ratio too low: {voice_ratio:.2f}"


def test_vad_detects_pre_roll_window():
    """Verify start-of-speech fires within reasonable delay of actual audio start.

    Fixture has 200ms head silence padding; VAD should fire START within
    first ~500ms (200ms silence + a few hundred ms to confirm).
    """
    r = _run_vad_on_fixture("medium")
    assert r["starts"], "no SPEECH_START fired"
    start_frame = r["starts"][0]
    start_ms = start_frame * 20
    print(f"\n  medium START at frame {start_frame} ({start_ms}ms)")
    assert 100 <= start_ms <= 800, f"START fired at {start_ms}ms (expected 100-800ms)"


# --- Robustness -------------------------------------------------------

def test_vad_long_gap_splits_into_two():
    """1500ms silence between two utterances should produce 2 separate START+END."""
    r = _run_vad_on_fixture("long_gap")
    print(f"\n  long_gap: starts={r['starts']} ends={r['ends']}")
    assert len(r["starts"]) == 2, f"expected 2 SPEECH_START events, got {len(r['starts'])}"
    assert len(r["ends"]) == 2, f"expected 2 utterance_ended events, got {len(r['ends'])}"


def test_vad_short_gap_with_padding_splits():
    """short_gap composite has medium(600ms tail) + 300ms gap + short(200ms head)
    = 1100ms effective silence between utterances. Above 800ms threshold → splits."""
    r = _run_vad_on_fixture("short_gap")
    print(f"\n  short_gap: starts={r['starts']} ends={r['ends']}")
    # 1100ms effective gap > 800ms threshold → DOES split
    assert len(r["starts"]) == 2, f"got {len(r['starts'])} starts (expected 2)"


def test_vad_handles_noisy_fixture():
    """Noisy fixture (SNR 12dB) should still detect the utterance."""
    r = _run_vad_on_fixture("noisy")
    print(f"\n  noisy: starts={r['starts']} ends={r['ends']} voice={r['voice_frames']}")
    assert r["starts"], "noisy fixture: no SPEECH_START detected"


def test_vad_handles_quiet_fixture_or_skips():
    """Quiet fixture (-18dB): document behavior — may or may not trigger.
    Test passes either way; we just record what happens.
    """
    r = _run_vad_on_fixture("quiet")
    print(
        f"\n  quiet: starts={r['starts']} ends={r['ends']} "
        f"voice={r['voice_frames']}/{r['total_frames']} "
        f"(detected={'YES' if r['starts'] else 'NO'})"
    )
    # Pass unconditionally; result is informational
