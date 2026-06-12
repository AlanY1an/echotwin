"""VoiceSession.trim_history — history trimming must keep the user-first paired structure.

The Anthropic Messages API requires the first message to be a user message; think_speak's call
order is append the new user message first, then trim, so the trimmed list must never start with assistant.
"""
import asyncio
from types import SimpleNamespace

from echotwin.bot import VoiceAgentBot
from echotwin.session import VoiceSession


def _make_session() -> VoiceSession:
    return VoiceSession(guild_id=1, bot_id=2)


def _fill_pairs(session: VoiceSession, n_pairs: int) -> None:
    for i in range(n_pairs):
        session.dialogue.append({"role": "user", "content": f"u{i}"})
        session.dialogue.append({"role": "assistant", "content": f"a{i}"})


def test_trim_noop_when_under_limit():
    s = _make_session()
    _fill_pairs(s, 3)
    s.trim_history(20)
    assert len(s.dialogue) == 6
    assert s.dialogue[0] == {"role": "user", "content": "u0"}


def test_trim_after_appending_user_keeps_user_first():
    """Reproduces the bug: 20 history pairs + new user message = 41 entries; trimmed to 40 it started with assistant."""
    s = _make_session()
    _fill_pairs(s, 20)  # 40 messages
    s.dialogue.append({"role": "user", "content": "new"})  # 41 — think_speak's order
    s.trim_history(20)

    assert len(s.dialogue) <= 40
    assert s.dialogue[0]["role"] == "user", (
        f"trim 后第一条必须是 user,实际是 {s.dialogue[0]}"
    )
    # the newest user message must remain at the end
    assert s.dialogue[-1] == {"role": "user", "content": "new"}


async def test_new_session_activity_time_uses_loop_clock():
    """Historical bug: the dataclass default was the time.time() wall clock (~1.7e9), while
    _check_idle compares with loop.time() (small monotonic values) → elapsed was a huge negative
    number, so after /join with nobody speaking the 120s/300s idle timeouts never fired."""
    loop = asyncio.get_running_loop()
    bot = SimpleNamespace(
        user=SimpleNamespace(id=1),
        loop=loop,
        sessions={},
        _consumer_loop=lambda gid: asyncio.sleep(0),
    )
    session = VoiceAgentBot.get_or_create_session(bot, guild_id=7)
    try:
        assert abs(session.last_activity_time - loop.time()) < 5.0, (
            f"last_activity_time={session.last_activity_time} 不是 loop 时钟 "
            f"(loop.time()={loop.time():.1f})——空闲超时将永不触发"
        )
    finally:
        if session.consumer_task:
            session.consumer_task.cancel()


def test_trim_never_starts_with_assistant_even_from_broken_state():
    """Self-healing from a broken state: history already starting with assistant (dirty data left by the old bug) must also be repaired."""
    s = _make_session()
    s.dialogue.append({"role": "assistant", "content": "orphan"})
    _fill_pairs(s, 2)
    s.trim_history(20)
    assert s.dialogue[0]["role"] == "user"
    assert len(s.dialogue) == 4


async def test_abort_does_not_wake_a_sleeping_session():
    """abort() unconditionally setting state=IDLE would undo /sleep — it may only reset from PROCESSING."""
    from echotwin.session import SessionState

    s = VoiceSession(guild_id=1, bot_id=2)
    s.state = SessionState.SLEEPING
    await s.abort()
    assert s.client_abort is True
    assert s.state == SessionState.SLEEPING, "/sleep 不能被 abort 静默撤销"

    s2 = VoiceSession(guild_id=1, bot_id=2)
    s2.state = SessionState.PROCESSING
    await s2.abort()
    assert s2.state == SessionState.IDLE  # normal barge-in behavior unchanged
