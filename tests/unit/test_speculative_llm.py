"""SpeculativeLLM — LLM stream pre-opened before the endpoint: events are buffered, attach only when text matches.

Review constraints: must carry tools; matches() validates both text and the dialogue snapshot length;
abort must not leak the task; after attach, the live continuation loses nothing and duplicates nothing.
"""
import asyncio

import pytest

from echotwin.pipeline.speculative import SpeculativeLLM
from echotwin.providers.llm.base import MessageEnd, TextDelta


class SlowLLM:
    """Genuinely yields the loop between two events, exposing races at the attach boundary."""

    def __init__(self):
        self.calls = []

    def stream_chat(self, system, messages, tools=None):
        self.calls.append({"system": system, "messages": messages, "tools": tools})

        async def gen():
            yield TextDelta(text="今天")
            await asyncio.sleep(0.02)
            yield TextDelta(text="晴。")
            await asyncio.sleep(0.02)
            yield MessageEnd(stop_reason="end_turn")

        return gen()


async def test_buffer_then_attach_replays_everything():
    llm = SlowLLM()
    spec = SpeculativeLLM(
        llm, "sys", [{"role": "user", "content": "p"}],
        user_text="天气", user_payload="p", tools=[{"name": "get_weather"}],
        dialogue_len=4,
    )
    await asyncio.sleep(0.03)  # some events already buffered, stream still in flight
    events = [ev async for ev in spec.events()]
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["今天", "晴。"], "缓冲回放 + live 续流不得丢失或重复"
    assert isinstance(events[-1], MessageEnd)
    # tools must be passed through (otherwise tool-round speculation hallucinates answers)
    assert llm.calls[0]["tools"] == [{"name": "get_weather"}]


async def test_attach_after_stream_complete():
    llm = SlowLLM()
    spec = SpeculativeLLM(llm, "sys", [], user_text="天气", user_payload="p",
                          tools=None, dialogue_len=0)
    await asyncio.sleep(0.1)  # stream fully finished into the buffer
    events = [ev async for ev in spec.events()]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["今天", "晴。"]


async def test_matches_requires_text_and_dialogue_snapshot():
    llm = SlowLLM()
    spec = SpeculativeLLM(llm, "sys", [], user_text="今天天气怎么样",
                          user_payload="p", tools=None, dialogue_len=4)
    try:
        assert spec.matches("今天天气怎么样", 4)
        assert not spec.matches("今天天气怎么样", 6), "历史变了(插了别的轮次)必须否决"
        assert not spec.matches("今天天气怎样", 4), "文本不同必须否决"
        assert spec.matches(" 今天天气怎么样 ", 4), "首尾空白不应否决"
    finally:
        await spec.abort()


async def test_abort_cancels_without_leak():
    class HangLLM:
        def stream_chat(self, system, messages, tools=None):
            async def gen():
                yield TextDelta(text="开")
                await asyncio.sleep(60)
                yield MessageEnd(stop_reason="end_turn")  # pragma: no cover

            return gen()

    spec = SpeculativeLLM(HangLLM(), "sys", [], user_text="x", user_payload="p",
                          tools=None, dialogue_len=0)
    await asyncio.sleep(0.01)
    await spec.abort()
    assert spec._task.done()
    # aborting again after abort is safe
    await spec.abort()


async def test_think_speak_attaches_spec_and_records_spec_payload(monkeypatch):
    """respond_to_user with spec_llm: round 0 uses the speculative stream (bot.llm is not called),
    and history records the speculative payload (the one the LLM actually saw)."""
    from types import SimpleNamespace

    import echotwin.pipeline.think_speak as think_speak
    from echotwin.session import SessionState, VoiceSession

    class FakeTTS:
        closed = False

        async def open(self): ...
        async def push_text(self, t): ...
        async def flush(self): ...
        async def end_turn(self): ...
        async def cancel(self): ...
        async def close(self):
            self.closed = True

        async def packets(self):
            return
            yield b""  # pragma: no cover

    class MustNotBeCalled:
        def stream_chat(self, *a, **k):
            raise AssertionError("投机命中时不得新开 LLM 流")

    spec_payload = '{"speaker": "u", "emotion": "HAPPY", "content": "hi"}'
    spec = SpeculativeLLM(
        SlowLLM(), "sys",
        [{"role": "user", "content": spec_payload}],
        user_text="hi", user_payload=spec_payload, tools=None, dialogue_len=0,
    )

    fake_tts = FakeTTS()
    monkeypatch.setattr(think_speak, "make_tts", lambda *a, **k: fake_tts)
    bot = SimpleNamespace(
        quota_guard=None,
        config=SimpleNamespace(
            bot=SimpleNamespace(history_window=20, filler_mode="off", filler_keywords=[])
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(id="p1"),
        llm=MustNotBeCalled(),
        tool_registry=None,
        loop=asyncio.get_running_loop(),
    )
    session = VoiceSession(guild_id=1, bot_id=2)

    class VC:
        def is_playing(self):
            return False

        def play(self, source, after=None):
            if after:
                after(None)

        def stop(self): ...

    await think_speak.respond_to_user(
        bot, session, VC(),
        user_id=42, user_name="u", user_text="hi",
        emotion="NEUTRAL", system_prompt="sys", spec_llm=spec,
    )

    assert session.dialogue[0]["content"] == spec_payload, "历史必须记录投机 payload"
    assert session.dialogue[1]["content"] == "今天晴。"
    assert session.state == SessionState.IDLE
    assert spec._task.done(), "finally 必须收尾投机任务"


async def test_exception_in_stream_surfaces_at_events():
    class BoomLLM:
        def stream_chat(self, system, messages, tools=None):
            async def gen():
                yield TextDelta(text="开")
                raise RuntimeError("api down")

            return gen()

    spec = SpeculativeLLM(BoomLLM(), "sys", [], user_text="x", user_payload="p",
                          tools=None, dialogue_len=0)
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError, match="api down"):
        async for _ in spec.events():
            pass
