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
        # Barge-in ducking: 1.0 = full volume. When < 1.0, read() transcodes
        # each packet (decode → scale → re-encode) so the bot audibly yields
        # the moment the user starts talking. Float writes are atomic, so the
        # event-loop thread can set this while the player thread reads it.
        self._duck = 1.0
        self._codec = None  # lazy (Decoder, Encoder) — only built if ducking is used

    def is_opus(self) -> bool:
        return True

    def set_duck(self, factor: float) -> None:
        self._duck = max(0.0, min(1.0, float(factor)))

    @property
    def duck(self) -> float:
        return self._duck

    def _scaled(self, pkt: bytes) -> bytes:
        """Decode → scale → re-encode one mono 48k packet. Any codec error
        falls back to the original packet (better briefly loud than broken)."""
        try:
            import opuslib_next

            if self._codec is None:
                self._codec = (
                    opuslib_next.Decoder(48000, 1),
                    opuslib_next.Encoder(48000, 1, opuslib_next.APPLICATION_AUDIO),
                )
            dec, enc = self._codec
            pcm = dec.decode(pkt, 5760)
            import numpy as np

            arr = (np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * self._duck)
            return enc.encode(arr.astype(np.int16).tobytes(), len(arr))
        except Exception:
            return pkt

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
        if self._duck < 0.999:
            return self._scaled(item)
        return item

    def cleanup(self) -> None:
        self._eof = True
