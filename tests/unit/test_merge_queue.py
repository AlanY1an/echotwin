"""Queue merging — multiple utterances backed up during playback are combined into one unified reply (real-device P3: 7s backlog)."""
import asyncio
from types import SimpleNamespace

from echotwin.bot import VoiceAgentBot
from echotwin.config import OrganicCfg
from echotwin.session import Utterance, VoiceSession


class FakeTTS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeSpec:
    def __init__(self):
        self.aborted = False

    async def abort(self):
        self.aborted = True


def _bot():
    bot = SimpleNamespace(loop=asyncio.get_event_loop())
    for m in ("_discard_utterance", "_abort_spec_llm", "_drain_merge_extras"):
        setattr(bot, m, getattr(VoiceAgentBot, m).__get__(bot))
    return bot


async def test_drain_extras_returns_lines_and_releases():
    bot = _bot()
    s = VoiceSession(guild_id=1, bot_id=999)
    primary = Utterance(user_id=111, user_name="Alan", text="主话语", spec_llm=FakeSpec())
    e1_tts, e1_spec = FakeTTS(), FakeSpec()
    s.utterance_queue.put_nowait(
        Utterance(user_id=222, user_name="小明", text="第二句", tts=e1_tts, spec_llm=e1_spec)
    )
    s.utterance_queue.put_nowait(Utterance(user_id=333, user_name="小雨", text="第三句"))

    merged = bot._drain_merge_extras(s, primary)

    assert merged == [(222, "小明", "第二句"), (333, "小雨", "第三句")]
    assert s.utterance_queue.qsize() == 0
    assert primary.spec_llm is None, "合并轮 payload 变了,主话语的投机流必须作废"
    await asyncio.sleep(0.01)  # the release tasks are fire-and-forget
    assert e1_tts.closed, "被合并话语的预开 TTS 必须关闭(否则泄漏 Fish WS)"
    assert e1_spec.aborted, "被合并话语的投机流必须 abort(否则一直计费)"


async def test_drain_extras_empty_keeps_primary_spec():
    bot = _bot()
    s = VoiceSession(guild_id=1, bot_id=999)
    spec = FakeSpec()
    primary = Utterance(user_id=111, user_name="Alan", text="主话语", spec_llm=spec)

    assert bot._drain_merge_extras(s, primary) == []
    assert primary.spec_llm is spec, "无合并时不得动投机流"
    assert not spec.aborted


async def test_merged_payload_reaches_llm_and_joins_window(monkeypatch):
    """Merged turn: queued_speakers go into the payload; merged speakers also join the conversation-state window."""
    import json

    import echotwin.pipeline.think_speak as think_speak
    from echotwin.providers.llm.base import MessageEnd, TextDelta

    captured = {}

    class CapturingLLM:
        def stream_chat(self, system, messages, tools=None):
            captured["messages"] = messages

            async def gen():
                yield TextDelta(text="都收到啦。")
                yield MessageEnd(stop_reason="end_turn")

            return gen()

    class TTS:
        async def open(self): ...
        async def push_text(self, t): ...
        async def flush(self): ...
        async def end_turn(self): ...
        async def cancel(self): ...
        async def close(self): ...
        async def packets(self):
            return
            yield b""

    monkeypatch.setattr(think_speak, "make_tts", lambda *a, **k: TTS())
    bot = SimpleNamespace(
        quota_guard=None,
        config=SimpleNamespace(
            bot=SimpleNamespace(
                history_window=20, filler_mode="off", filler_keywords=[],
                organic=OrganicCfg(enabled=True),
            )
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(id="p1"),
        llm=CapturingLLM(),
        tool_registry=None,
        loop=asyncio.get_running_loop(),
    )
    s = VoiceSession(guild_id=1, bot_id=999)

    class VC:
        def is_playing(self): return False
        def play(self, src, after=None):
            if after: after(None)
        def stop(self): ...

    await think_speak.respond_to_user(
        bot, s, VC(), user_id=111, user_name="Alan", user_text="你觉得呢",
        emotion="NEUTRAL", system_prompt="sys",
        merged=[(222, "小明", "我也想问"), (333, "小雨", "快说快说")],
    )
    payload = json.loads(captured["messages"][0]["content"])
    assert payload["content"] == "你觉得呢"
    assert payload["queued_speakers"] == ["小明: 我也想问", "小雨: 快说快说"]
    assert "note" in payload, "要告诉模型这是多人积压,一次性综合回应"
    for uid in (111, 222, 333):
        assert uid in s.organic_participants, "被合并的人也应进对话态窗口"
