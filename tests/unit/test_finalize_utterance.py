"""_finalize_utterance — utterance duration must use per-user counters, and empty ASR results must be logged.

Historical bugs:
1. The opus counter was session-global, and any new user's first speech reset it to zero —
   the current speaker's utterance was measured as 0ms and wrongly killed by the 600ms
   filter ("whenever a new person speaks up, someone else's sentence gets swallowed").
2. An empty ASR result returned silently — the only drop point in the detection chain
   with zero logging.
"""
from types import SimpleNamespace

from loguru import logger

from echotwin.bot import VoiceAgentBot
from echotwin.providers.asr.base import ASRResult
from echotwin.session import VoiceSession


class FakeASR:
    def __init__(self, text: str, spec_text: str | None = None):
        self._text = text
        self._spec_text = spec_text
        self._buffered = 100
        self.end_utterance_called = 0
        self.dropped = False

    def buffered_bytes(self):
        return self._buffered

    async def speculate(self):
        if self._spec_text is None:
            return (None, -1)
        return (ASRResult(text=self._spec_text), self._buffered)

    def drop_buffer(self):
        self.dropped = True
        self._buffered = 0

    async def end_utterance(self):
        self.end_utterance_called += 1
        if self._text is None:
            return None
        return ASRResult(text=self._text)


class FakePreTTS:
    def __init__(self):
        self.closed = False
        self.opened = False

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True


def _fake_bot():
    import asyncio

    bot = SimpleNamespace(
        addressee_detector=None,
        wake_matcher=None,
        fast_cache=None,
        get_guild=lambda gid: None,
        _dequeue_user=lambda session, user_id: None,
        config=SimpleNamespace(
            bot=SimpleNamespace(barge_in_mode="addressee_only"),
            asr=SimpleNamespace(emotion_sidecar=False),
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(id="p1"),
        loop=asyncio.get_event_loop(),
    )
    bot._abort_spec_llm = lambda spec: VoiceAgentBot._abort_spec_llm(bot, spec)
    return bot


async def test_new_speaker_does_not_zero_other_users_utterance():
    """A spoke 50 packets (1000ms) while B appeared for the first time — A's utterance must not be dropped as 0ms."""
    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50  # A's own packet count
    session.opus_ok[222] = 1   # B just appeared, only 1 packet
    setattr(session, "_utt_opus_ok_111", 0)  # snapshot at the start of A's utterance (A's own counter)
    session.asrs[111] = FakeASR("帮我查一下天气怎么样")

    await VoiceAgentBot._finalize_utterance(
        _fake_bot(), session, 1, 111, "A", source="watchdog"
    )

    assert session.utterance_queue.qsize() == 1, (
        "A 的 1000ms 话语被丢弃了——时长计算被其他用户的计数器污染"
    )
    utt = session.utterance_queue.get_nowait()
    assert utt.user_id == 111


async def test_speculation_hit_skips_end_utterance():
    """Speculation result is valid (no new audio) → adopt it directly, skip the second inference."""
    import asyncio

    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    asr = FakeASR("正常结果不该用到", spec_text="帮我查一下天气怎么样")
    session.asrs[111] = asr
    setattr(session, "_spec_task_111", asyncio.create_task(asr.speculate()))

    await VoiceAgentBot._finalize_utterance(
        _fake_bot(), session, 1, 111, "A", source="watchdog"
    )

    assert asr.end_utterance_called == 0, "投机命中后不应再跑 end_utterance"
    assert asr.dropped is True, "采用投机结果后必须 drop_buffer"
    assert session.utterance_queue.qsize() == 1
    assert session.utterance_queue.get_nowait().text == "帮我查一下天气怎么样"


async def test_speculation_stale_when_user_resumed():
    """User spoke again after speculation (buffered amount changed) → speculation is void, rerun normally."""
    import asyncio

    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    asr = FakeASR("完整的最终结果哦", spec_text="不完整的投机结果")
    session.asrs[111] = asr
    setattr(session, "_spec_task_111", asyncio.create_task(asr.speculate()))
    await asyncio.sleep(0)  # let the speculation task run first and capture fed=100
    asr._buffered += 50  # user keeps talking

    await VoiceAgentBot._finalize_utterance(
        _fake_bot(), session, 1, 111, "A", source="watchdog"
    )

    assert asr.end_utterance_called == 1, "投机失效必须正常重跑"
    assert session.utterance_queue.qsize() == 1
    assert session.utterance_queue.get_nowait().text == "完整的最终结果哦"


def _fast_path_bot(played: list, aborted_via_queue: bool = False):
    """A bot wired with the full set of fakes needed for the wake-word fast path."""
    from pathlib import Path

    async def fake_fast_play(voice_client, session, user_id, cached):
        played.append(cached)

    async def fake_get_random():
        return Path("fake.ogg")

    import asyncio

    bot = SimpleNamespace(
        addressee_detector=None,
        wake_matcher=SimpleNamespace(match_only=lambda t: True),
        fast_cache=SimpleNamespace(get_random=fake_get_random),
        get_guild=lambda gid: SimpleNamespace(
            voice_client=SimpleNamespace(channel=SimpleNamespace(members=[]))
        ),
        _dequeue_user=lambda session, user_id: None,
        _fast_path_play=fake_fast_play,
        config=SimpleNamespace(
            bot=SimpleNamespace(barge_in_mode="addressee_only"),
            asr=SimpleNamespace(emotion_sidecar=False),
        ),
        active_voice_id=lambda: "v1",
        persona=SimpleNamespace(id="p1"),
        loop=asyncio.get_event_loop(),
    )
    bot._abort_spec_llm = lambda spec: VoiceAgentBot._abort_spec_llm(bot, spec)
    return bot


async def test_fast_path_skipped_while_bot_is_responding():
    """Saying "宝宝?" while a reply is in progress (PROCESSING) must not take the fast path —
    that would stop() the playing reply and commit the truncated reply to history as if
    complete; it should go through normal barge-in instead."""
    import asyncio
    from echotwin.session import SessionState

    session = VoiceSession(guild_id=1, bot_id=2)
    session.state = SessionState.PROCESSING
    session.current_addressee_id = 111
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("宝宝在吗?")
    played: list = []

    await VoiceAgentBot._finalize_utterance(
        _fast_path_bot(played), session, 1, 111, "A", source="watchdog"
    )
    await asyncio.sleep(0.02)  # give the (should-not-exist) fast path task a chance to run

    assert played == [], "PROCESSING 期间不能触发 fast path"
    assert session.client_abort is True, "应走正常 barge-in(abort)"
    assert session.utterance_queue.qsize() == 1, "话语应正常入队等 LLM 处理"


async def test_fast_path_still_works_when_idle():
    import asyncio

    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("宝宝?")
    played: list = []

    await VoiceAgentBot._finalize_utterance(
        _fast_path_bot(played), session, 1, 111, "A", source="watchdog"
    )
    await asyncio.sleep(0.02)

    assert len(played) == 1, "空闲时纯唤醒词应走 fast path"
    assert session.utterance_queue.qsize() == 0


async def test_pre_opens_tts_when_dispatch_is_imminent(monkeypatch):
    """Queue empty and not PROCESSING → pre-open the TTS WS at endpoint enqueue time (hidden inside the queueing delay)."""
    import asyncio
    import echotwin.bot as bot_mod

    made = []
    monkeypatch.setattr(bot_mod, "make_tts", lambda *a, **k: made.append(FakePreTTS()) or made[-1])
    bot = _fake_bot()
    bot.loop = asyncio.get_running_loop()
    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("帮我查一下天气怎么样")

    await VoiceAgentBot._finalize_utterance(bot, session, 1, 111, "A", source="watchdog")
    await asyncio.sleep(0)

    utt = session.utterance_queue.get_nowait()
    assert utt.tts is made[0], "入队的 utterance 必须携带预开的 TTS"
    assert utt.tts_open_task is not None and made[0].opened


async def test_no_pre_open_while_processing(monkeypatch):
    """Currently replying (PROCESSING) → this utterance has to queue, so don't pre-open (Fish closes idle connections)."""
    import echotwin.bot as bot_mod
    from echotwin.session import SessionState

    made = []
    monkeypatch.setattr(bot_mod, "make_tts", lambda *a, **k: made.append(FakePreTTS()) or made[-1])
    bot = _fake_bot()
    session = VoiceSession(guild_id=1, bot_id=2)
    session.state = SessionState.PROCESSING
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("帮我查一下天气怎么样")

    await VoiceAgentBot._finalize_utterance(bot, session, 1, 111, "A", source="watchdog")

    utt = session.utterance_queue.get_nowait()
    assert utt.tts is None and made == [], "排队中的话语不应预开 WS"


async def test_dequeue_user_discards_preopened_tts():
    """A queued utterance displaced by a newer one must have its pre-opened WS closed."""
    import asyncio
    from echotwin.session import Utterance

    session = VoiceSession(guild_id=1, bot_id=2)
    old_tts = FakePreTTS()
    session.utterance_queue.put_nowait(
        Utterance(user_id=111, user_name="A", text="旧话", tts=old_tts)
    )
    bot = SimpleNamespace(loop=asyncio.get_running_loop())
    bot._discard_utterance = lambda utt: VoiceAgentBot._discard_utterance(bot, utt)

    VoiceAgentBot._dequeue_user(bot, session, 111)
    await asyncio.sleep(0.01)

    assert session.utterance_queue.qsize() == 0
    assert old_tts.closed, "被顶掉的话语的预开 TTS 泄漏了"


async def test_cleanup_session_discards_queued_tts():
    """On /leave, pre-opened WS of utterances left in the queue must be closed."""
    import asyncio
    from echotwin.session import Utterance

    session = VoiceSession(guild_id=1, bot_id=2)
    old_tts = FakePreTTS()
    session.utterance_queue.put_nowait(
        Utterance(user_id=111, user_name="A", text="残留", tts=old_tts)
    )
    bot = SimpleNamespace(loop=asyncio.get_running_loop(), sessions={1: session})
    bot._discard_utterance = lambda utt: VoiceAgentBot._discard_utterance(bot, utt)
    bot._cancel_spec_asr = lambda s, uid: None

    await VoiceAgentBot.cleanup_session(bot, 1)
    await asyncio.sleep(0.01)

    assert old_tts.closed, "cleanup_session 留下了打开的预开 WS"


class FakeSpecLLM:
    def __init__(self, match=True):
        self._match = match
        self.aborted = False

    def matches(self, text, dialogue_len):
        return self._match

    async def abort(self):
        self.aborted = True


async def test_spec_llm_carried_into_utterance_when_matching():
    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("帮我查一下天气怎么样")
    spec = FakeSpecLLM(match=True)
    setattr(session, "_spec_llm_111", spec)

    await VoiceAgentBot._finalize_utterance(_fake_bot(), session, 1, 111, "A", source="watchdog")

    utt = session.utterance_queue.get_nowait()
    assert utt.spec_llm is spec, "匹配的投机流必须随 Utterance 携带"
    assert spec.aborted is False
    assert getattr(session, "_spec_llm_111", None) is None, "session 引用必须清空"


async def test_spec_llm_aborted_on_drop_path():
    """Utterance dropped (empty ASR) → the speculative stream must be aborted, not drift into the next turn."""
    import asyncio

    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("")  # empty result → drop path
    spec = FakeSpecLLM(match=True)
    setattr(session, "_spec_llm_111", spec)

    await VoiceAgentBot._finalize_utterance(_fake_bot(), session, 1, 111, "A", source="watchdog")
    await asyncio.sleep(0.01)

    assert session.utterance_queue.qsize() == 0
    assert spec.aborted is True, "丢弃路径上的投机流泄漏了(Anthropic 在白烧钱)"
    assert getattr(session, "_spec_llm_111", None) is None


async def test_spec_llm_aborted_on_mismatch():
    import asyncio

    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("帮我查一下天气怎么样")
    spec = FakeSpecLLM(match=False)
    setattr(session, "_spec_llm_111", spec)

    await VoiceAgentBot._finalize_utterance(_fake_bot(), session, 1, 111, "A", source="watchdog")
    await asyncio.sleep(0.01)

    utt = session.utterance_queue.get_nowait()
    assert utt.spec_llm is None, "不匹配的投机流不得被采用"
    assert spec.aborted is True


async def test_empty_asr_result_is_logged_not_silent():
    session = VoiceSession(guild_id=1, bot_id=2)
    session.opus_ok[111] = 50
    setattr(session, "_utt_opus_ok_111", 0)
    session.asrs[111] = FakeASR("")  # pure tags / noise → empty text

    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        await VoiceAgentBot._finalize_utterance(
            _fake_bot(), session, 1, 111, "A", source="watchdog"
        )
    finally:
        logger.remove(handler_id)

    assert session.utterance_queue.qsize() == 0
    assert any("empty ASR" in m or "空" in m for m in messages), (
        f"ASR 空结果的丢弃必须留日志,实际日志: {messages}"
    )
