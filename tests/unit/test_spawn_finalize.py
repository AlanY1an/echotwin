"""_spawn_finalize — finalize runs concurrently without freezing the watchdog; same-user deferrals are not lost."""
import asyncio
from types import SimpleNamespace

from echotwin.bot import VoiceAgentBot


async def test_finalize_runs_concurrently_and_defers_per_user():
    calls: list[tuple[int, int]] = []  # (uid, ok_at_start)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_finalize(session, gid, uid, name, *, source, ok_at_start=None):
        calls.append((uid, ok_at_start))
        started.set()
        await release.wait()

    session = SimpleNamespace()
    setattr(session, "_utt_opus_ok_111", 37)  # synchronous snapshot value at spawn time
    bot = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        _finalize_utterance=fake_finalize,
    )
    VoiceAgentBot._spawn_finalize(bot, session, 1, 111, "A")
    VoiceAgentBot._spawn_finalize(bot, session, 1, 222, "B")  # different user → runs concurrently
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0.01)
    assert sorted(u for u, _ in calls) == [111, 222], "不同用户必须并发执行"
    assert (111, 37) in calls, (
        "ok_at_start 必须是 spawn 时的同步快照,不能等 task 执行时再读"
    )

    # Same user still running → don't drop: restore in_speech so the watchdog re-triggers later
    setattr(session, "_in_speech_111", False)  # simulate the watchdog having reset it
    VoiceAgentBot._spawn_finalize(bot, session, 1, 111, "A")
    assert getattr(session, "_in_speech_111") is True, (
        "去重命中时必须恢复 in_speech,否则第二句话被静默吞掉"
    )
    assert len(calls) == 2  # no third execution

    release.set()
    await asyncio.sleep(0.01)
    # After the first batch finishes, the same user can finalize again
    VoiceAgentBot._spawn_finalize(bot, session, 1, 111, "A")
    await asyncio.sleep(0.01)
    assert sum(1 for u, _ in calls if u == 111) == 2
