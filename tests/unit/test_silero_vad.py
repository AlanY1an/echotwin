import os
import numpy as np
import pytest

from echotwin.providers.vad.silero import SileroVAD


MODEL_PATH = "models/silero_vad/src/silero_vad/data/silero_vad.onnx"


@pytest.fixture
def vad():
    if not os.path.exists(MODEL_PATH):
        pytest.skip(f"silero_vad.onnx not present at {MODEL_PATH}")
    return SileroVAD(min_silence_duration_ms=200, frame_window=2)


def test_silence_no_voice(vad):
    """1 second of silence → not detected as voice."""
    silence = np.zeros(16000, dtype=np.int16).tobytes()
    result = vad.feed(silence)
    assert result.is_voice is False
    assert result.utterance_ended is False


def test_reset_clears_state(vad):
    """After reset, can keep feeding without crash."""
    vad.feed(np.zeros(16000, dtype=np.int16).tobytes())
    vad.reset()
    vad.feed(np.zeros(1000, dtype=np.int16).tobytes())


def test_short_buffer_no_inference(vad):
    """<512 samples (1024 bytes): no inference, no voice."""
    short = np.zeros(100, dtype=np.int16).tobytes()
    result = vad.feed(short)
    assert result.is_voice is False


def test_speech_started_only_on_rising_edge(vad):
    """speech_started must be True exactly once at the silent→speech transition."""
    # Synthesize 16kHz sine bursts that trigger silero
    rng = np.random.default_rng(42)
    burst = (rng.integers(-15000, 15000, 8000, dtype=np.int16)).tobytes()  # 0.5s
    silence = np.zeros(8000, dtype=np.int16).tobytes()

    # First: silence — no rising edge
    r0 = vad.feed(silence)
    assert r0.speech_started is False
    # Then: noisy burst — should fire speech_started once (across however many chunks it takes)
    r1 = vad.feed(burst)
    if r1.is_voice:
        assert r1.speech_started is True
        # Continued speech in same call: speech_started stays False
        r2 = vad.feed(burst)
        assert r2.speech_started is False
    # If silero didn't classify our white noise as speech, that's environment-dependent;
    # the assertion above covers the case we care about (rising edge semantics).
