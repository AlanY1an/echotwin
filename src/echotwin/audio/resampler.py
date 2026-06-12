"""Streaming audio resampler (e.g. 48kHz → 16kHz int16 PCM)."""
from __future__ import annotations

import numpy as np
import soxr


class Resampler:
    def __init__(self, src_rate: int = 48000, dst_rate: int = 16000):
        self._stream = soxr.ResampleStream(src_rate, dst_rate, 1, dtype="int16")

    def feed(self, pcm_int16: bytes) -> bytes:
        if not pcm_int16:
            return b""
        arr = np.frombuffer(pcm_int16, dtype=np.int16)
        out = self._stream.resample_chunk(arr)
        return out.tobytes()

    def flush(self) -> bytes:
        out = self._stream.resample_chunk(np.array([], dtype=np.int16), last=True)
        return out.tobytes()
