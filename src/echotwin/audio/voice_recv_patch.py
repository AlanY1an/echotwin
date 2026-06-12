"""Defensive monkey-patches for discord-ext-voice-recv 0.5.2a179 bugs.

Bug 1: VoiceRecvClient._remove_ssrc crashes on _MissingSentinel reader
=====================================================================

When Discord sends a "user disconnect / SSRC remove" gateway event,
voice_recv calls `vc._remove_ssrc(user_id=...)` which immediately accesses
`self._reader.speaking_timer.drop_ssrc(ssrc)`.

If `_reader` is still `discord.utils.MISSING` (sentinel for "not yet
listening" — e.g. between a disconnect and a re-listen, or during shutdown
race), this raises:

    AttributeError: '_MissingSentinel' object has no attribute 'speaking_timer'

The exception propagates out of the websocket poller and corrupts voice
state — packets stop flowing in either direction until /leave + /join.

Patch: wrap _remove_ssrc to silently no-op when _reader isn't ready.

Bug 2: UDPKeepAlive.run sendto fails on macOS (already-connected UDP)
====================================================================

reader.py:409 does `socket.sendto(packet, (ip, port))` on a UDP socket
that discord.py already called `connect()` on. macOS strictly rejects
this with `OSError: [Errno 56] Socket is already connected` (EISCONN).
Linux is permissive.

The exception path in voice_recv has no sleep, so it spins:
    sendto fails → wait_until_connected (returns instantly) → continue → sendto fails → ...
This wastes CPU AND no keepalives are actually sent. Discord's voice
gateway eventually times out the connection.

Patch: replace UDPKeepAlive.run with a version that tries
`sock.send(packet)` first (works on connected socket), falls back to
sendto, and backs off properly on persistent failures.
"""
from __future__ import annotations

import time as _t

from loguru import logger

try:
    from discord.ext.voice_recv.voice_client import VoiceRecvClient
    from discord.ext.voice_recv import reader as _voice_recv_reader
    from discord.utils import MISSING
    _HAS_DEPS = True
except Exception as _e:  # pragma: no cover
    logger.warning(f"voice_recv defensive patch unavailable: {_e}")
    _HAS_DEPS = False


_PATCHED = False


def _patched_keepalive_run(self):
    """Replacement for UDPKeepAlive.run — sends via sock.send (no addr)
    on connected UDP socket; falls back to sendto; backs off on errors."""
    self.voice_client.wait_until_connected()
    while not self._end_thread.is_set():
        vc = self.voice_client
        try:
            packet = self.counter.to_bytes(8, "big")
        except OverflowError:
            self.counter = 0
            continue

        sock = vc._connection.socket
        sent = False
        try:
            # On macOS, sendto on a connected UDP socket raises EISCONN.
            # send() works regardless of connect() state.
            sock.send(packet)
            sent = True
        except OSError:
            try:
                sock.sendto(
                    packet,
                    (vc._connection.endpoint_ip, vc._connection.voice_port),
                )
                sent = True
            except Exception as e:  # noqa: BLE001
                # Don't spin — back off below
                if self.counter % 50 == 0:
                    logger.debug(f"[voice_recv_patch] keepalive send failed: {e}")

        if sent:
            self.counter += 1
            _t.sleep(self.delay)
        else:
            # Persistent failure — back off so we don't burn CPU
            _t.sleep(max(1.0, self.delay))
            if not vc.is_connected():
                break


def apply_voice_recv_patches() -> bool:
    """Install all defensive monkey-patches. Idempotent."""
    global _PATCHED
    if _PATCHED:
        return True
    if not _HAS_DEPS:
        return False

    # Bug 1: safe _remove_ssrc
    original_remove_ssrc = VoiceRecvClient._remove_ssrc

    def safe_remove_ssrc(self, *, user_id):
        reader = getattr(self, "_reader", MISSING)
        if reader is MISSING or reader is None:
            logger.debug(
                f"[voice_recv_patch] _remove_ssrc(user_id={user_id}) skipped "
                f"— no active reader"
            )
            return
        try:
            return original_remove_ssrc(self, user_id=user_id)
        except AttributeError as e:
            logger.warning(
                f"[voice_recv_patch] _remove_ssrc(user_id={user_id}) "
                f"AttributeError: {e}; suppressed to keep voice WS alive"
            )
        except Exception as e:
            logger.warning(
                f"[voice_recv_patch] _remove_ssrc(user_id={user_id}) "
                f"unexpected: {type(e).__name__}: {e}; suppressed"
            )

    VoiceRecvClient._remove_ssrc = safe_remove_ssrc  # type: ignore[assignment]

    # Bug 2: macOS-safe keepalive
    _voice_recv_reader.UDPKeepAlive.run = _patched_keepalive_run  # type: ignore[assignment]

    # Bug 3: stop() should not kill the receiver
    # voice_recv overrides stop() to do BOTH stop_playing + stop_listening.
    # That's wrong for any caller that just wants to interrupt outbound audio
    # (barge-in, prior-playback-interrupt). Restore vanilla discord.py
    # semantics: stop() only stops playback. Receivers stay alive.
    def stop_play_only(self):
        self.stop_playing()
    VoiceRecvClient.stop = stop_play_only  # type: ignore[assignment]

    _PATCHED = True
    logger.info(
        "voice_recv defensive patches applied "
        "(safe _remove_ssrc + macOS-safe UDP keepalive + stop=play-only)"
    )
    return True
