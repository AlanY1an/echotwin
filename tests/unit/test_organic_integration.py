"""Organic integration — finalize verdict dispatch / open-floor yielding / clarify cooldown / ambient injection."""
import asyncio
from types import SimpleNamespace

import echotwin.bot as bot_mod
from echotwin.bot import VoiceAgentBot
from echotwin.config import OrganicCfg
from echotwin.providers.asr.base import ASRResult
from echotwin.session import SessionState, VoiceSession


class FakeASR:
    def __init__(self, text):
        self._text = text

    def buffered_bytes(self):
        return 100

    async def speculate(self):
        return (None, -1)

    def drop_buffer(self):
        pass

    async def end_utterance(self):
        return ASRResult(text=self._text) if self._text else None


class FakeTTS:
    async def open(self): ...
    async def close(self): ...


def _bot(monkeypatch=None, clarify_path="x.ogg"):
    played = []
    if monkeypatch:
        monkeypatch.setattr(bot_mod, "make_tts", lambda *a, **k: FakeTTS())

    async def fake_play(vc, path):
        played.append(path)

    bot = SimpleNamespace(
        addressee_detector=SimpleNamespace(
            is_addressed=lambda *a, **k: False, strip_wake_word=lambda t: t
        ),
        wake_matcher=None,
        fast_cache=None,
        user=SimpleNamespace(id=999),
        get_guild=lambda gid: SimpleNamespace(
            voice_client=SimpleNamespace(
                channel=SimpleNamespace(members=[1, 2, 3], name="测试频道")
            )
        ),
        _dequeue_user=lambda session, user_id: None,
        config=SimpleNamespace(
            bot=SimpleNamespace(
                barge_in_mode="addressee_only",
                organic=OrganicCfg(
                    enabled=True, open_floor_wait_ms=200, gray_zone="heuristic"
                ),
            ),
            asr=SimpleNamespace(emotion_sidecar=False),
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(
            id="p1", name="Hinata酱", wake_words=["Hinata", "宝宝"],
            system_prompt="测试人设", language="zh",
        ),
        loop=asyncio.get_event_loop(),
        _play_cached_opus=fake_play,
        pick_clarify_path=lambda: clarify_path,
        _played=played,
    )
    for m in ("_ambient_note", "_arm_open_floor", "_abort_spec_llm", "_build_organic_ctx"):
        setattr(bot, m, getattr(VoiceAgentBot, m).__get__(bot))
    return bot


def _session(**names):
    s = VoiceSession(guild_id=1, bot_id=999)
    s.opus_ok[111] = 50
    setattr(s, "_utt_opus_ok_111", 0)
    s.user_names.update({111: "Alan", 222: "小明"})
    return s


async def test_reject_goes_to_ambient(monkeypatch):
    bot = _bot(monkeypatch)
    s = _session()
    s.asrs[111] = FakeASR("昨天那把全靠我carry好吧")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 0
    assert len(s.ambient) == 1 and s.ambient[0]["speaker"] == "Alan"
    assert s.last_voice_event == 111


async def test_wake_word_accepts_and_joins_window(monkeypatch):
    bot = _bot(monkeypatch)
    s = _session()
    s.asrs[111] = FakeASR("宝宝今天天气怎么样呀")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 1
    assert 111 in s.organic_participants


async def test_clarify_plays_once_then_cooldown(monkeypatch):
    import time
    bot = _bot(monkeypatch)
    s = _session()
    s.organic_participants[111] = time.time()  # inside window
    s.last_bot_reply = "我觉得可以试试速攻打法"
    s.last_voice_event = "bot"
    s.asrs[111] = FakeASR("不行吧")  # gray zone (short reaction +1, no topic linkage)
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    await asyncio.sleep(0.01)
    assert s.utterance_queue.qsize() == 0
    assert bot._played == ["x.ogg"], "灰区应播一次反问"
    assert 111 in s.clarify_pending_at

    s.asrs[111] = FakeASR("不行吧")  # another gray-zone utterance, within cooldown
    s.clarify_pending_at.pop(111)  # simulate: 10s pending window elapsed but 60s cooldown has not
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    await asyncio.sleep(0.01)
    assert bot._played == ["x.ogg"], "冷却内不得重复反问"


async def test_clarify_followup_accepts(monkeypatch):
    import time
    bot = _bot(monkeypatch)
    s = _session()
    s.organic_participants[111] = time.time()
    s.clarify_pending_at[111] = time.time()  # clarify question was just asked
    s.asrs[111] = FakeASR("对啊就是问你")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 1, "反问后确认必须接"


async def test_open_floor_yields_to_human(monkeypatch):
    bot = _bot(monkeypatch)
    s = _session()
    s.asrs[111] = FakeASR("有人知道现在几点了吗")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    setattr(s, "_in_speech_222", True)  # real human Xiaoming started speaking
    await asyncio.sleep(0.35)
    assert s.utterance_queue.qsize() == 0, "有人接话就该让位"


async def test_open_floor_self_selects_when_silent(monkeypatch):
    bot = _bot(monkeypatch)
    s = _session()
    s.asrs[111] = FakeASR("有人知道现在几点了吗")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    await asyncio.sleep(0.35)  # nobody takes the floor (wait_ms=200)
    assert s.utterance_queue.qsize() == 1, "空场无人接,bot 应自荐"
    assert s.utterance_queue.get_nowait().text == "有人知道现在几点了吗"


async def test_ambient_injected_into_payload(monkeypatch):
    """think_speak: ambient chat goes into the current turn's payload (fresh within 120s); stripped when committed to history (in-the-moment reference, not memory)."""
    import json
    import time
    import echotwin.pipeline.think_speak as think_speak
    from echotwin.providers.llm.base import MessageEnd, TextDelta

    captured = {}

    class CapturingLLM:
        def stream_chat(self, system, messages, tools=None):
            captured["messages"] = messages

            async def gen():
                yield TextDelta(text="好。")
                yield MessageEnd(stop_reason="end_turn")

            return gen()

    class TTS:
        closed = False
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
    now = time.time()
    s.ambient.append({"speaker": "老王", "text": "三分钟前的旧话题", "ts": now - 300})
    s.ambient.append({"speaker": "小明", "text": "这把太难了", "ts": now - 5})
    s.last_bot_reply_ts = 0.0

    class VC:
        def is_playing(self): return False
        def play(self, src, after=None):
            if after: after(None)
        def stop(self): ...

    await think_speak.respond_to_user(
        bot, s, VC(), user_id=111, user_name="Alan", user_text="你觉得呢",
        emotion="NEUTRAL", system_prompt="sys",
    )
    payload = json.loads(captured["messages"][0]["content"])
    assert payload["recent_room_chat"] == ["小明: 这把太难了"], "超过 120s 的旁听不得注入"
    assert s.last_bot_reply == "好。"  # topic-linkage signal recorded after the reply
    # Ambient chat is in-the-moment reference, not memory: the user message committed to history must have recent_room_chat stripped
    committed = json.loads(s.dialogue[0]["content"])
    assert s.dialogue[0]["role"] == "user"
    assert "recent_room_chat" not in committed, "历史里不得保留旁听快照"
    assert committed["content"] == "你觉得呢"


async def test_mention_rate_zero_goes_ambient(monkeypatch):
    """Phase 2: a mention at rate=0 (default) only goes to ambient, never gets taken up."""
    import time
    bot = _bot(monkeypatch)
    s = _session()
    s.organic_participants[111] = time.time()
    s.asrs[111] = FakeASR("我觉得Hinata挺聪明的")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 0
    assert len(s.ambient) == 1


async def test_mention_rate_one_accepts(monkeypatch):
    import time
    bot = _bot(monkeypatch)
    bot.config.bot.organic.mention_reply_rate = 1.0
    s = _session()
    s.organic_participants[111] = time.time()
    s.asrs[111] = FakeASR("我觉得Hinata挺聪明的")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 1, "rate=1 时提及必接"


async def test_clarify_llm_yes_enqueues(monkeypatch):
    """Phase 2: with clarify_llm enabled, LLM answers "yes" → utterance gets picked up after all."""
    import time
    from echotwin.providers.llm.base import MessageEnd, TextDelta

    class YesLLM:
        def stream_chat(self, system, messages, tools=None):
            async def gen():
                yield TextDelta(text="是的")
                yield MessageEnd(stop_reason="end_turn")
            return gen()

    bot = _bot(monkeypatch)
    bot.config.bot.organic.clarify_llm = True
    bot.llm = YesLLM()
    bot._clarify_via_llm = VoiceAgentBot._clarify_via_llm.__get__(bot)
    s = _session()
    s.organic_participants[111] = time.time()
    s.last_bot_reply = "我觉得可以试试速攻打法"
    s.last_voice_event = "bot"
    s.asrs[111] = FakeASR("不行吧")  # gray zone
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    await asyncio.sleep(0.1)
    assert s.utterance_queue.qsize() == 1, "LLM 判'是'必须补接"
    assert bot._played == [], "clarify_llm 模式不播反问音频"


async def test_spec_llm_gated_by_pre_verdict(monkeypatch):
    """Found on real hardware: every line of human-to-human chitchat was pointlessly opening a paid speculative stream. Must pass the addressee pre-verdict before triggering."""
    spawned = []

    class StreamASR:
        def partial_text(self):
            return self._partial

        def pipeline_drained(self):
            return True

    bot = _bot(monkeypatch)
    bot.llm = SimpleNamespace(
        stream_chat=lambda *a, **k: spawned.append(1) or iter(())
    )
    bot.tool_registry = None
    bot._build_organic_ctx = VoiceAgentBot._build_organic_ctx.__get__(bot)
    bot._maybe_spawn_spec_llm = VoiceAgentBot._maybe_spawn_spec_llm.__get__(bot)
    s = _session()
    asr = StreamASR()
    s.asrs[111] = asr

    asr._partial = "他们那个比赛规模不大呀"  # human-to-human chitchat → no speculation
    bot._maybe_spawn_spec_llm(s, 1, 111)
    assert getattr(s, "_spec_llm_111", None) is None, "闲聊不得开投机流"

    asr._partial = "是"  # too short → no speculation
    bot._maybe_spawn_spec_llm(s, 1, 111)
    assert getattr(s, "_spec_llm_111", None) is None

    asr._partial = "宝宝今天天气怎么样"  # vocative → speculate
    bot._maybe_spawn_spec_llm(s, 1, 111)
    spec = getattr(s, "_spec_llm_111", None)
    assert spec is not None, "受话 partial 必须照常投机"
    await spec.abort()


async def test_arbiter_reject_goes_ambient(monkeypatch):
    """gray_zone=llm: arbiter rejects a gray-zone utterance → ambient; accept → enqueued."""
    from echotwin.providers.llm.base import MessageEnd, TextDelta

    class ArbiterLLM:
        def __init__(self, verdict):
            self._v = verdict

        def stream_chat(self, system, messages, tools=None):
            async def gen():
                yield TextDelta(text=f'{{"verdict":"{self._v}","reason":"测"}}')
                yield MessageEnd(stop_reason="end_turn")
            return gen()

    import time
    bot = _bot(monkeypatch)
    bot.config.bot.organic.gray_zone = "llm"
    bot.cost_tracker = None
    s = _session()
    s.organic_participants[111] = time.time()  # inside window → gray zone

    bot.llm = ArbiterLLM("reject")
    s.asrs[111] = FakeASR("你能不能在这版本的结束啊")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 0
    assert len(s.ambient) == 1, "仲裁 reject 必须旁听"

    bot.llm = ArbiterLLM("accept")
    s.asrs[111] = FakeASR("你能不能帮我查个东西呀")
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 1, "仲裁 accept 必须入队"


async def test_arbiter_failure_falls_back_to_heuristic(monkeypatch):
    """Arbiter broke (no JSON) → fall back to heuristic scoring, pipeline stays alive."""
    from echotwin.providers.llm.base import MessageEnd, TextDelta

    class BrokenLLM:
        def stream_chat(self, system, messages, tools=None):
            async def gen():
                yield TextDelta(text="我也不知道")
                yield MessageEnd(stop_reason="end_turn")
            return gen()

    import time
    bot = _bot(monkeypatch)
    bot.config.bot.organic.gray_zone = "llm"
    bot.cost_tracker = None
    bot.llm = BrokenLLM()
    s = _session()
    s.organic_participants[111] = time.time()
    s.last_bot_reply = "我觉得可以试试速攻打法"
    s.last_voice_event = "bot"
    s.asrs[111] = FakeASR("昨天那把全靠我carry好吧")  # heuristic will reject
    await VoiceAgentBot._finalize_utterance(bot, s, 1, 111, "Alan", source="watchdog")
    assert s.utterance_queue.qsize() == 0
    assert len(s.ambient) == 1, "兜底启发式必须接管"


async def test_arbiter_rate_limit_falls_back(monkeypatch):
    """Per-minute arbiter cap: over the limit, skip the LLM and use the heuristic fallback."""
    import time
    from echotwin.providers.llm.base import MessageEnd, TextDelta

    calls = []

    class CountingLLM:
        def stream_chat(self, system, messages, tools=None):
            calls.append(1)

            async def gen():
                yield TextDelta(text='{"verdict":"reject","reason":"x"}')
                yield MessageEnd(stop_reason="end_turn")
            return gen()

    bot = _bot(monkeypatch)
    bot.config.bot.organic.gray_zone = "llm"
    bot.config.bot.organic.arbiter_max_per_min = 3
    bot.cost_tracker = None
    bot.llm = CountingLLM()
    s = _session()
    s.organic_participants[111] = time.time()
    for i in range(5):
        s.asrs[111] = FakeASR("他们那个比赛规模不大呀对吧各位")
        await VoiceAgentBot._finalize_utterance(
            bot, s, 1, 111, "Alan", source="watchdog"
        )
    assert len(calls) == 3, f"超频后不得继续调 LLM,实际调了 {len(calls)} 次"
    assert len(s.ambient) == 5, "超频的句子走兜底,仍然旁听"
