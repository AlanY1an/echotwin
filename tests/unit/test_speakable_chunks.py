"""Speakability guardrail — empty chunks (\\n / tag-only / punctuation-only) must not be pushed to Fish.

On real hardware 2026-06-11: when an LLM reply contained \\n\\n paragraph breaks, the chunker cut
out a chunk containing only newlines; Fish answered empty content with "Inference backend returned
empty audio" and finished the whole stream, silencing all subsequent text
("a \\n causes the rest of the reply to go unspoken").
"""
import asyncio
from types import SimpleNamespace

import pytest

from echotwin.utils.sentence_chunker import speakable


@pytest.mark.parametrize(
    "text,expect",
    [
        ("\n", False),
        ("\n\n", False),
        ("[chuckle]", False),
        ("[chuckle]。", False),
        ("[volume up]~\n", False),
        ("", False),
        ("。!?", False),
        ("好。", True),
        ("嗯\n", True),
        ("[laughing]哈,这什么鬼啦!", True),
        ("你可以搜一下~", True),
    ],
)
def test_speakable(text, expect):
    assert speakable(text) is expect


async def test_empty_chunks_not_pushed(monkeypatch):
    """A reply split by \\n\\n: the newline-only empty chunk is skipped, the second half is pushed as usual."""
    import echotwin.pipeline.think_speak as think_speak
    from echotwin.providers.llm.base import MessageEnd, TextDelta

    pushed = []

    class TTS:
        async def open(self): ...
        async def push_text(self, t): pushed.append(t)
        async def flush(self): ...
        async def end_turn(self): ...
        async def cancel(self): ...
        async def close(self): ...
        async def packets(self):
            return
            yield b""

    class LLM:
        def stream_chat(self, system, messages, tools=None):
            async def gen():
                yield TextDelta(text="有的。\n\n要我细说吗?")
                yield MessageEnd(stop_reason="end_turn")
            return gen()

    monkeypatch.setattr(think_speak, "make_tts", lambda *a, **k: TTS())
    bot = SimpleNamespace(
        quota_guard=None,
        config=SimpleNamespace(
            bot=SimpleNamespace(
                history_window=20, filler_mode="off", filler_keywords=[],
                organic=None,
            )
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(id="p1"),
        llm=LLM(),
        tool_registry=None,
        loop=asyncio.get_running_loop(),
    )
    from echotwin.session import VoiceSession
    s = VoiceSession(guild_id=1, bot_id=999)

    class VC:
        def is_playing(self): return False
        def play(self, src, after=None):
            if after: after(None)
        def stop(self): ...

    await think_speak.respond_to_user(
        bot, s, VC(), user_id=111, user_name="Alan", user_text="世界杯有喝水暂停吗",
        emotion="NEUTRAL", system_prompt="sys",
    )
    assert all(speakable(p) for p in pushed), f"推送了空块: {pushed!r}"
    assert any("要我细说吗" in p for p in pushed), "\\n 之后的内容必须照常推送"
