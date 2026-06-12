"""Bridge producer (thread-safe queue of Opus packets) to discord.py's
synchronous AudioSource interface. With is_opus()=True, discord.py treats
each read() return value as a complete Opus packet and forwards it directly
into RTP — no PCM↔Opus conversion happens, enabling mono pass-through.

Uses a sync queue.Queue (not asyncio.Queue) so read() — which runs on
discord.py's player thread — can wait with a real timeout without
spawning orphaned get() coroutines that would silently consume packets
intended for the next read() call.
"""
from __future__ import annotations

import queue as _queue
from typing import Optional

import discord

# 3-byte Opus silence frame (CELT 20ms mono, used by discord.py's own
# silence emitter; safe to send during transient producer underruns).
SILENCE_OPUS: bytes = b"\xf8\xff\xfe"

# Hard cap on how long read() waits for a packet before returning silence.
# Must be < 20ms so discord.py's player loop stays in cadence.
_READ_BUDGET_S = 0.015


class StreamingOpusAudioSource(discord.AudioSource):
    def __init__(self, frame_queue: "_queue.Queue[Optional[bytes]]") -> None:
        self._queue = frame_queue
        self._eof = False

    def is_opus(self) -> bool:
        return True

    def read(self) -> bytes:
        if self._eof:
            return b""
        try:
            item = self._queue.get(timeout=_READ_BUDGET_S)
        except _queue.Empty:
            return SILENCE_OPUS

        if item is None:
            self._eof = True
            return b""
        return item

    def cleanup(self) -> None:
        self._eof = True
