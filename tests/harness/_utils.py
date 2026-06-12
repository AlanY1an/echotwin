"""Shared utilities for layered pipeline tests.

- WAV loading + chunking
- Timer / Stat helpers
- Mock VoiceSession for addressee tests
- Fixture path lookup
"""
from __future__ import annotations

import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
import soundfile as sf

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_RATE = 48000  # all fixtures are 48k mono int16


def fixture_path(tag: str) -> Path:
    """Resolve fixture by tag (e.g. 'medium' → fixtures/medium.wav)."""
    p = FIXTURE_DIR / f"{tag}.wav"
    if not p.exists():
        raise FileNotFoundError(
            f"Fixture {tag!r} not found at {p}. "
            f"Run: python -m tests.harness.fixtures.gen_fixtures"
        )
    return p


def load_pcm48k_mono(tag_or_path) -> np.ndarray:
    """Load fixture as 48k mono int16 numpy array."""
    p = fixture_path(tag_or_path) if isinstance(tag_or_path, str) else Path(tag_or_path)
    audio, sr = sf.read(str(p), dtype="int16")
    assert sr == SAMPLE_RATE, f"expected 48k, got {sr}"
    if audio.ndim == 2:
        audio = audio.mean(axis=1).astype(np.int16)
    return audio


def chunk_pcm(pcm: np.ndarray, frame_ms: int = 20) -> Iterator[bytes]:
    """Slice into 20ms frames (matching Discord's typical packet size)."""
    samples_per_frame = SAMPLE_RATE * frame_ms // 1000
    for i in range(0, len(pcm), samples_per_frame):
        chunk = pcm[i : i + samples_per_frame]
        if len(chunk) == samples_per_frame:  # drop trailing partial frame
            yield chunk.tobytes()


def chunk_pcm_at(pcm: np.ndarray, sample_rate: int, frame_ms: int = 20) -> Iterator[bytes]:
    """Same as chunk_pcm but explicit sample rate (use for downsampled 16k arrays)."""
    samples_per_frame = sample_rate * frame_ms // 1000
    for i in range(0, len(pcm), samples_per_frame):
        chunk = pcm[i : i + samples_per_frame]
        if len(chunk) == samples_per_frame:
            yield chunk.tobytes()


def resample_48k_to_16k(pcm_48k_int16: np.ndarray) -> np.ndarray:
    """Production code uses soxr in `audio/resampler.py`; this matches that."""
    import soxr
    f32 = pcm_48k_int16.astype(np.float32) / 32768.0
    out_f32 = soxr.resample(f32, 48000, 16000)
    return (out_f32 * 32768.0).clip(-32768, 32767).astype(np.int16)


@dataclass
class Stat:
    """Collect a list of measurements; report P50/P95/max/mean."""
    name: str
    samples: list[float] = field(default_factory=list)

    def add(self, v: float) -> None:
        self.samples.append(v)

    @property
    def p50(self) -> float:
        return statistics.median(self.samples) if self.samples else float("nan")

    @property
    def p95(self) -> float:
        if not self.samples:
            return float("nan")
        s = sorted(self.samples)
        idx = max(0, int(len(s) * 0.95) - 1)
        return s[idx]

    @property
    def max(self) -> float:
        return max(self.samples) if self.samples else float("nan")

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else float("nan")

    def fmt_ms(self) -> str:
        return (
            f"{self.name}: "
            f"P50={self.p50*1000:.1f}ms "
            f"P95={self.p95*1000:.1f}ms "
            f"max={self.max*1000:.1f}ms "
            f"(N={len(self.samples)})"
        )


@contextmanager
def time_ms():
    """Context manager that records elapsed milliseconds.

    Usage:
        with time_ms() as t:
            do_work()
        print(t.elapsed_ms)
    """
    class _T:
        elapsed_ms: float = 0.0
    rec = _T()
    t0 = time.perf_counter()
    try:
        yield rec
    finally:
        rec.elapsed_ms = (time.perf_counter() - t0) * 1000.0


class FakeSession:
    """Stand-in for VoiceSession with the fields AddresseeDetector reads."""
    def __init__(self, last_bot_speak_time: float = 0.0, last_addressee_id: int | None = None):
        self.last_bot_speak_time = last_bot_speak_time
        self.last_addressee_id = last_addressee_id


# Expected ASR transcripts for fixtures (used in WER / quality checks).
# Empty string means "no transcript expected" (e.g. quiet fixture might fail to recognize).
EXPECTED_TEXT: dict[str, str] = {
    "short": "你好",
    "medium": "现在几点了",
    "long": "今天天气怎么样,我想出门跑步顺便买点东西",
    "wake_only": "一点点点",
    "wake_query": "一点点点 现在几点",
    "code_switch": "hello, 现在几点了",
    "quiet": "现在几点了",
    "noisy": "现在几点了",
    # Composites are concatenations
    "short_gap": "现在几点了 你好",
    "long_gap": "现在几点了 你好",
}


def char_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein-distance / len(ref). Punctuation-stripped, lowercased.

    Returns 0.0 for perfect match, 1.0 for completely wrong.
    """
    import re
    norm = lambda s: re.sub(r"[\s,。!?、:;\"\'《》【】()()「」.!?\-_~`]+", "", s).lower()
    r, h = norm(reference), norm(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    # Levenshtein
    m, n = len(r), len(h)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            tmp = dp[j]
            if r[i - 1] == h[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j - 1], dp[j])
            prev = tmp
    return dp[n] / m
