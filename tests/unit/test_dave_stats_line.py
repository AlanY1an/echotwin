"""_dave_stats_line — the DAVE diagnostics segment of the [stats] log.

Purpose: when something breaks, tell apart at a glance "Discord isn't delivering packets"
(attempts flat), "packets arrive but decryption fails" (failures rising), and
"the receiver died" (reader=DEAD).
"""
from types import SimpleNamespace

from echotwin.bot import _dave_stats_line


def _fake_vc(ready=True, reader_active=True):
    sess = SimpleNamespace(
        ready=ready,
        epoch=3,
        get_user_ids=lambda: ["42", "43"],
        get_decryption_stats=lambda uid, media_type=None: SimpleNamespace(
            attempts=101, successes=98, failures=2, passthroughs=1
        ),
    )
    return SimpleNamespace(
        _connection=SimpleNamespace(dave_session=sess),
        _reader=SimpleNamespace(active=reader_active),
    )


def test_ready_session_reports_epoch_and_per_user_stats():
    line = _dave_stats_line(_fake_vc(), [42])
    assert "epoch=3" in line
    assert "reader=alive" in line
    assert "mls_users=2" in line
    assert "ok=98" in line and "fail=2" in line


def test_dead_reader_is_loudly_visible():
    line = _dave_stats_line(_fake_vc(reader_active=False), [42])
    assert "reader=DEAD" in line


def test_not_ready_session():
    line = _dave_stats_line(_fake_vc(ready=False), [42])
    assert "not-ready" in line


def test_none_vc_is_safe():
    assert isinstance(_dave_stats_line(None, [42]), str)
