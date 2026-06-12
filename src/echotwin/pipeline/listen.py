"""Stage 1: Discord voice receive → per-user audio routing.

Endpoint detection is NOT in this layer — it's handled entirely by the
wallclock watchdog in bot.py:_speech_watchdog_loop. The
voice_member_speaking_stop event was tried and proven unreliable in our
setup (verified empirically: did not fire even after 17s of silence).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from discord.ext import voice_recv
from loguru import logger


class VoiceListener(voice_recv.AudioSink):
    """Discord audio sink. Forwards every voice packet to on_user_audio."""

    def __init__(
        self,
        bot_id: int,
        loop: asyncio.AbstractEventLoop,
        on_user_audio: Callable[[int, str, bytes], Awaitable[None]],
    ):
        super().__init__()
        self._bot_id = bot_id
        self._loop = loop
        self._on_user_audio = on_user_audio
        self._unknown_user_drops = 0

    def wants_opus(self) -> bool:
        return True

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None:
            # ssrc→user mapping missing (lost SPEAKING gateway event): this
            # user is effectively deaf to us. Rate-limited log so the failure
            # mode is visible instead of silently dropping their audio.
            self._unknown_user_drops += 1
            if self._unknown_user_drops == 1 or self._unknown_user_drops % 500 == 0:
                logger.warning(
                    f"[listen] dropping packet from unknown user "
                    f"(ssrc unmapped, count={self._unknown_user_drops}) — "
                    f"missing SPEAKING event?"
                )
            return
        if user.id == self._bot_id:
            return
        if data.opus is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._on_user_audio(user.id, user.display_name, data.opus),
                self._loop,
            )
            # The future carries any exception raised inside on_user_audio
            # (opus decode / VAD / ASR). Dropping it makes those errors —
            # and the audio frame — vanish with zero log output.
            fut.add_done_callback(self._log_pipeline_error)
        except Exception as e:
            logger.warning(f"VoiceListener.write schedule error: {e}")

    @staticmethod
    def _log_pipeline_error(fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.opt(exception=exc).error(f"on_user_audio failed: {exc!r}")

    def cleanup(self) -> None:
        pass
