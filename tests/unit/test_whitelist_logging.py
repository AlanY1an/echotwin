"""Whitelist rejections must be visible per-user.

Historical bug: _wl_skip_logged emitted only one log line for the whole process → with the
whitelist active, "can't hear a specific person" was nearly impossible to pin down
(one of the biggest suspects in this investigation).
"""
from types import SimpleNamespace

from loguru import logger

from echotwin.bot import VoiceAgentBot


def _fake_bot(whitelist):
    return SimpleNamespace(
        config=SimpleNamespace(bot=SimpleNamespace(listen_only_users=whitelist)),
        _trace_packet=lambda *a, **k: None,
    )


async def test_each_blocked_user_logged_once():
    bot = _fake_bot([42])
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        # Two different non-whitelisted users, two frames each
        for uid in (100, 100, 200, 200):
            await VoiceAgentBot.on_user_audio(bot, 1, uid, f"user{uid}", b"\x01\x02\x03\x04")
    finally:
        logger.remove(handler_id)

    wl_logs = [m for m in messages if "whitelist" in m]
    assert len(wl_logs) == 2, f"每个被拒用户应各打一条日志,实际: {wl_logs}"
    assert any("100" in m for m in wl_logs)
    assert any("200" in m for m in wl_logs)
