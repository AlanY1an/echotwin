"""Per-user ring buffer of recent PCM frames; drained on VAD speech_start."""
from __future__ import annotations

from collections import deque


class PrerollRingBuffer:
    """Keep up to N most recent PCM frame chunks; drain returns concatenated bytes."""

    def __init__(self, max_frames: int = 15):
        self._dq: deque[bytes] = deque(maxlen=max(0, max_frames))

    def push(self, pcm_frame: bytes) -> None:
        if self._dq.maxlen == 0:
            return
        self._dq.append(pcm_frame)

    def drain(self) -> bytes:
        if not self._dq:
            return b""
        out = b"".join(self._dq)
        self._dq.clear()
        return out

    def clear(self) -> None:
        """Discard buffered frames. Call at utterance end so the previous
        utterance's tail doesn't get prepended to the next one."""
        self._dq.clear()

    def __len__(self) -> int:
        return len(self._dq)
