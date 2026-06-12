"""Cleanup guarantees of respond_to_user — every exit path (exception / task cancellation) must:
close the TTS WS, reset session.is_audible / state / current_addressee_id, and roll back the uncommitted user message.

Historical bug: the cleanup code was straight-line code outside a finally block, so when
voice_client.play() raised ClientException or the consumer was cancelled it was all skipped
→ is_audible stuck True (all short utterances in that guild permanently dropped by the ACK filter),
state stuck PROCESSING, TTS WS leaked.
"""
import asyncio
from types import SimpleNamespace

import discord
import pytest

import echotwin.pipeline.think_speak as think_speak
from echotwin.session import SessionState, VoiceSession


class FakeTTS:
    def __init__(self):
        self.closed = False
        self.cancelled = False

    async def open(self):
        pass

    async def push_text(self, text):
        pass

    async def flush(self):
        pass

    async def end_turn(self):
        pass

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True

    async def packets(self):
        await asyncio.sleep(30)
        yield b""  # pragma: no cover


class HangingLLM:
    """stream_chat never produces an event — simulates a stuck LLM so an external cancel lands on the await."""

    def stream_chat(self, system_prompt, messages, tools=None):
        async def gen():
            await asyncio.sleep(60)
            yield None  # pragma: no cover

        return gen()


class HappyLLM:
    """A normal round: one chunk of text + end_turn."""

    def stream_chat(self, system_prompt, messages, tools=None):
        async def gen():
            from echotwin.providers.llm.base import MessageEnd, TextDelta

            yield TextDelta(text="好的。")
            yield MessageEnd(stop_reason="end_turn")

        return gen()


def _make_bot(fake_tts, monkeypatch):
    monkeypatch.setattr(think_speak, "make_tts", lambda *a, **k: fake_tts)
    return SimpleNamespace(
        quota_guard=None,
        config=SimpleNamespace(
            bot=SimpleNamespace(history_window=20, filler_mode="off", filler_keywords=[])
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(id="p1"),
        llm=HangingLLM(),
        tool_registry=None,
        loop=asyncio.get_event_loop(),
    )


def _make_session():
    return VoiceSession(guild_id=1, bot_id=2)


class RaisingVoiceClient:
    def is_playing(self):
        return False

    def play(self, source, after=None):
        raise discord.ClientException("not connected to voice")

    def stop(self):
        pass


async def test_play_exception_still_cleans_up(monkeypatch):
    fake_tts = FakeTTS()
    bot = _make_bot(fake_tts, monkeypatch)
    bot.loop = asyncio.get_running_loop()
    session = _make_session()

    with pytest.raises(discord.ClientException):
        await think_speak.respond_to_user(
            bot, session, RaisingVoiceClient(),
            user_id=42, user_name="u", user_text="hi",
            emotion="NEUTRAL", system_prompt="sys",
        )

    assert fake_tts.closed, "TTS WS 必须在异常路径上被关闭"
    assert session.is_audible is False, "is_audible 卡 True 会让 ACK 过滤器吞掉所有短话语"
    assert session.state == SessionState.IDLE
    assert session.current_addressee_id is None
    assert session.dialogue == [], "未完成的 user 消息必须回滚"


class IdleVoiceClient:
    def __init__(self):
        self._playing = False

    def is_playing(self):
        return self._playing

    def play(self, source, after=None):
        self._playing = True

    def stop(self):
        self._playing = False


async def test_consumer_cancellation_still_cleans_up(monkeypatch):
    fake_tts = FakeTTS()
    bot = _make_bot(fake_tts, monkeypatch)
    bot.loop = asyncio.get_running_loop()
    session = _make_session()

    task = asyncio.create_task(
        think_speak.respond_to_user(
            bot, session, IdleVoiceClient(),
            user_id=42, user_name="u", user_text="hi",
            emotion="NEUTRAL", system_prompt="sys",
        )
    )
    await asyncio.sleep(0.1)  # let it reach the stuck LLM stream
    assert session.state == SessionState.PROCESSING  # precondition: actually processing
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake_tts.closed, "TTS WS 必须在取消路径上被关闭"
    assert session.is_audible is False
    assert session.state == SessionState.IDLE
    assert session.current_addressee_id is None
    assert session.dialogue == [], "未完成的 user 消息必须回滚"


class CompletingVoiceClient:
    """Fires the after callback immediately on play, simulating playback finishing instantly."""

    def is_playing(self):
        return False

    def play(self, source, after=None):
        if after is not None:
            after(None)

    def stop(self):
        pass


async def test_normal_turn_commits_history(monkeypatch):
    fake_tts = FakeTTS()
    bot = _make_bot(fake_tts, monkeypatch)
    bot.loop = asyncio.get_running_loop()
    bot.llm = HappyLLM()
    session = _make_session()

    await think_speak.respond_to_user(
        bot, session, CompletingVoiceClient(),
        user_id=42, user_name="u", user_text="hi",
        emotion="NEUTRAL", system_prompt="sys",
    )

    assert [m["role"] for m in session.dialogue] == ["user", "assistant"]
    assert session.dialogue[-1]["content"] == "好的。"
    assert fake_tts.closed
    assert session.is_audible is False
    assert session.state == SessionState.IDLE
    assert session.last_addressee_id == 42


async def test_llm_starts_before_tts_handshake_completes(monkeypatch):
    """The TTS WS handshake (150-400ms) must not serially block the LLM."""
    timeline: dict = {}

    class SlowOpenTTS(FakeTTS):
        async def open(self):
            timeline["open_start"] = asyncio.get_running_loop().time()
            await asyncio.sleep(0.15)
            timeline["open_done"] = asyncio.get_running_loop().time()

    class TimedLLM(HappyLLM):
        def stream_chat(self, *a, **k):
            timeline["llm_start"] = asyncio.get_running_loop().time()
            return super().stream_chat(*a, **k)

    fake_tts = SlowOpenTTS()
    bot = _make_bot(fake_tts, monkeypatch)
    bot.loop = asyncio.get_running_loop()
    bot.llm = TimedLLM()
    session = _make_session()

    await think_speak.respond_to_user(
        bot, session, CompletingVoiceClient(),
        user_id=42, user_name="u", user_text="hi",
        emotion="NEUTRAL", system_prompt="sys",
    )

    assert timeline["llm_start"] < timeline["open_done"], (
        f"LLM 必须与 TTS 握手并行启动: {timeline}"
    )
    # the reply still completes normally
    assert [m["role"] for m in session.dialogue] == ["user", "assistant"]


async def test_journey_line_logged_on_normal_turn(monkeypatch):
    from loguru import logger as _logger
    from echotwin.utils.latency import LatencyJourney

    fake_tts = FakeTTS()
    bot = _make_bot(fake_tts, monkeypatch)
    bot.loop = asyncio.get_running_loop()
    bot.llm = HappyLLM()
    session = _make_session()

    messages = []
    handler_id = _logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        await think_speak.respond_to_user(
            bot, session, CompletingVoiceClient(),
            user_id=42, user_name="u", user_text="hi",
            emotion="NEUTRAL", system_prompt="sys",
            journey=LatencyJourney("endpoint"),
        )
    finally:
        _logger.remove(handler_id)

    lat = [m for m in messages if "[latency]" in m]
    assert lat, "正常轮次必须输出延迟旅程日志"
    assert "llm_first_delta" in lat[0]


async def test_sleep_during_turn_survives_turn_end(monkeypatch):
    """/sleep issued mid-reply (state=SLEEPING) — the end of this turn must not flip it back to IDLE."""
    fake_tts = FakeTTS()
    bot = _make_bot(fake_tts, monkeypatch)
    bot.loop = asyncio.get_running_loop()
    session = _make_session()

    task = asyncio.create_task(
        think_speak.respond_to_user(
            bot, session, IdleVoiceClient(),
            user_id=42, user_name="u", user_text="hi",
            emotion="NEUTRAL", system_prompt="sys",
        )
    )
    await asyncio.sleep(0.1)
    assert session.state == SessionState.PROCESSING
    session.state = SessionState.SLEEPING  # simulate /sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.state == SessionState.SLEEPING, (
        "/sleep 在回复结束时被静默撤销了"
    )
