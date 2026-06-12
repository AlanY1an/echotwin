from unittest.mock import MagicMock

from echotwin.config_watcher import _diff_reloadable


def _mk(active="a", inactivity=120, hard=300, empty=300, silence=600, tick=100):
    cfg = MagicMock()
    cfg.bot = MagicMock(
        active_persona=active,
        inactivity_timeout_seconds=inactivity,
        hard_timeout_seconds=hard,
        empty_channel_timeout_seconds=empty,
        endpoint_silence_ms=silence,
        endpoint_tick_ms=tick,
    )
    return cfg


def test_diff_detects_persona_change():
    old = _mk(active="a")
    new = _mk(active="b")
    diff = _diff_reloadable(old, new)
    assert diff["bot.active_persona"] == ("a", "b")


def test_diff_detects_timeout_change():
    old = _mk(inactivity=120)
    new = _mk(inactivity=60)
    diff = _diff_reloadable(old, new)
    assert "bot.inactivity_timeout_seconds" in diff
    assert diff["bot.inactivity_timeout_seconds"] == (120, 60)


def test_diff_empty_when_equal():
    old = _mk()
    new = _mk()
    assert _diff_reloadable(old, new) == {}


def test_diff_multiple_changes():
    old = _mk(active="a", inactivity=120, hard=300)
    new = _mk(active="b", inactivity=60, hard=200)
    diff = _diff_reloadable(old, new)
    assert set(diff.keys()) == {
        "bot.active_persona",
        "bot.inactivity_timeout_seconds",
        "bot.hard_timeout_seconds",
    }
