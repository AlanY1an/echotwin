"""respond_to_user must report LLM token usage and TTS UTF-8 bytes to CostTracker.

Historical bug: CostTracker.record had zero call sites in the codebase → costs.db
stayed empty forever, budgets/quota never triggered, /admin cost always showed $0.
"""
import asyncio
from types import SimpleNamespace

import echotwin.pipeline.think_speak as think_speak
from echotwin.providers.llm.base import MessageEnd, TextDelta
from echotwin.session import VoiceSession


class FakeTTS:
    def __init__(self):
        self.closed = False
        self.pushed: list[str] = []

    async def open(self):
        pass

    async def push_text(self, text):
        self.pushed.append(text)

    async def flush(self):
        pass

    async def end_turn(self):
        pass

    async def cancel(self):
        pass

    async def close(self):
        self.closed = True

    async def packets(self):
        return
        yield b""  # pragma: no cover


class UsageLLM:
    def stream_chat(self, system_prompt, messages, tools=None):
        async def gen():
            yield TextDelta(text="好的。")
            yield MessageEnd(
                stop_reason="end_turn",
                input_tokens=100,
                output_tokens=50,
                cache_creation_input_tokens=5,
                cache_read_input_tokens=10,
            )

        return gen()


class FakeTracker:
    def __init__(self):
        self.records: list[tuple] = []

    async def record(self, kind, amount, *, guild_id="", user_id="", sentence_id=""):
        self.records.append((kind, amount, guild_id, user_id))
        return 0.0


class CompletingVoiceClient:
    def is_playing(self):
        return False

    def play(self, source, after=None):
        if after is not None:
            after(None)

    def stop(self):
        pass


async def test_turn_records_llm_and_tts_costs(monkeypatch):
    fake_tts = FakeTTS()
    tracker = FakeTracker()
    monkeypatch.setattr(think_speak, "make_tts", lambda *a, **k: fake_tts)
    bot = SimpleNamespace(
        quota_guard=None,
        config=SimpleNamespace(
            bot=SimpleNamespace(history_window=20, filler_mode="off", filler_keywords=[])
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(id="p1"),
        llm=UsageLLM(),
        tool_registry=None,
        loop=asyncio.get_running_loop(),
        cost_tracker=tracker,
    )
    session = VoiceSession(guild_id=7, bot_id=2)

    await think_speak.respond_to_user(
        bot, session, CompletingVoiceClient(),
        user_id=42, user_name="u", user_text="hi",
        emotion="NEUTRAL", system_prompt="sys",
    )

    by_kind = {kind: amount for kind, amount, *_ in tracker.records}
    assert by_kind.get("claude_haiku_4_5_input") == 100
    assert by_kind.get("claude_haiku_4_5_output") == 50
    assert by_kind.get("claude_haiku_4_5_cache_write") == 5
    assert by_kind.get("claude_haiku_4_5_cache_read") == 10
    # "好的。" = 9 UTF-8 bytes
    assert by_kind.get("fishaudio_tts") == len("好的。".encode("utf-8"))
    # guild/user attribution
    assert all(g == "7" and u == "42" for _, _, g, u in tracker.records)
