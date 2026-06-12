"""VoiceListener — exceptions raised inside the receive callback must be logged, not vanish silently.

Historical bug: write() discarded the future returned by run_coroutine_threadsafe,
so any exception inside on_user_audio (opus decode / VAD / ASR) evaporated silently —
audio frames were lost with zero logging, a suspected source of the
"speaking gets no reaction" flakiness.
"""
import asyncio
from types import SimpleNamespace

from loguru import logger

from echotwin.pipeline.listen import VoiceListener


class _FakeUser:
    id = 42
    display_name = "tester"


def _fake_data(opus: bytes = b"\x01\x02\x03\x04"):
    return SimpleNamespace(opus=opus)


async def test_exception_in_on_user_audio_is_logged():
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    try:
        async def exploding_handler(user_id, user_name, opus):
            raise RuntimeError("boom in pipeline")

        listener = VoiceListener(
            bot_id=999,
            loop=asyncio.get_running_loop(),
            on_user_audio=exploding_handler,
        )
        listener.write(_FakeUser(), _fake_data())
        # Give the scheduled coroutine time to run and trigger the done callback
        await asyncio.sleep(0.05)
    finally:
        logger.remove(handler_id)

    assert any("boom in pipeline" in m for m in messages), (
        f"管线异常必须出现在 ERROR 日志里,实际日志: {messages}"
    )


async def test_normal_frame_does_not_log_errors():
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    try:
        received = []

        async def ok_handler(user_id, user_name, opus):
            received.append((user_id, user_name, opus))

        listener = VoiceListener(
            bot_id=999,
            loop=asyncio.get_running_loop(),
            on_user_audio=ok_handler,
        )
        listener.write(_FakeUser(), _fake_data())
        await asyncio.sleep(0.05)
    finally:
        logger.remove(handler_id)

    assert received == [(42, "tester", b"\x01\x02\x03\x04")]
    assert messages == []


async def test_unknown_user_packet_is_logged():
    """When the ssrc→user mapping is missing (speaking event lost), user=None packets must not
    vanish silently — this is the zero-log path behind "permanently deaf to one person"."""
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        async def handler(user_id, user_name, opus):  # pragma: no cover
            pass

        listener = VoiceListener(
            bot_id=999,
            loop=asyncio.get_running_loop(),
            on_user_audio=handler,
        )
        listener.write(None, _fake_data())
        await asyncio.sleep(0.02)
    finally:
        logger.remove(handler_id)

    assert any("unknown user" in m or "user=None" in m for m in messages), (
        f"user=None 的丢包必须有日志,实际: {messages}"
    )
