"""start_listening — reader death must raise an alarm and rebuild automatically.

Historical bug: voice_recv's AudioReader stops receiving permanently when feed_rtp raises any
exception, and vc.listen() passed no after= callback → the bot was unaware, sitting deaf in the
channel (similar community report: "No listening thereafter").
"""
import asyncio
from types import SimpleNamespace

from loguru import logger

from echotwin.bot import VoiceAgentBot


class FakeVC:
    def __init__(self, connected=True):
        self._connected = connected
        self.listen_calls: list = []  # (listener, after)

    def listen(self, listener, *, after=None):
        self.listen_calls.append((listener, after))

    def is_connected(self):
        return self._connected


def _fake_bot():
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        loop=asyncio.get_event_loop(),
        on_user_audio=None,
        _reader_restarts={},
    )
    # On the real bot start_listening is an instance method; the fake must bind it manually,
    # otherwise self.start_listening inside the restart closure cannot be found
    bot.start_listening = lambda vc, gid, **kw: VoiceAgentBot.start_listening(
        bot, vc, gid, **kw
    )
    return bot


async def test_reader_death_triggers_restart():
    bot = _fake_bot()
    bot.loop = asyncio.get_running_loop()
    vc = FakeVC()

    VoiceAgentBot.start_listening(bot, vc, guild_id=1)
    assert len(vc.listen_calls) == 1
    _, after = vc.listen_calls[0]
    assert after is not None, "vc.listen 必须注册 after 回调,否则 reader 死亡无人知晓"

    after(RuntimeError("boom in feed_rtp"))  # simulate the reader dying abnormally (called by the stopper thread)
    await asyncio.sleep(0.05)

    assert len(vc.listen_calls) == 2, "reader 异常死亡后必须自动重新 listen"


async def test_clean_stop_does_not_restart():
    bot = _fake_bot()
    bot.loop = asyncio.get_running_loop()
    vc = FakeVC()

    VoiceAgentBot.start_listening(bot, vc, guild_id=1)
    _, after = vc.listen_calls[0]
    after(None)  # clean stop (/leave, re-listen)
    await asyncio.sleep(0.05)

    assert len(vc.listen_calls) == 1, "正常停止不能触发重启"


async def test_restart_is_capped():
    """No infinite restart storm when the underlying cause persists."""
    bot = _fake_bot()
    bot.loop = asyncio.get_running_loop()
    vc = FakeVC()

    VoiceAgentBot.start_listening(bot, vc, guild_id=1)
    for _ in range(10):
        _, after = vc.listen_calls[-1]
        after(RuntimeError("persistent failure"))
        await asyncio.sleep(0.02)

    # 1 initial + at most 5 restarts
    assert len(vc.listen_calls) <= 6, f"重启必须封顶,实际 listen 了 {len(vc.listen_calls)} 次"
