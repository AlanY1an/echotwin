"""Filler decision + OGG packet pre-fill — a perceived-latency optimization that masks LLM TTFT."""
import queue as sync_queue
from pathlib import Path

import pytest

from echotwin.pipeline.filler import enqueue_filler_packets, should_play_filler

KW = ["天气", "几点", "查"]


def test_smart_mode_only_fillers_slow_turns():
    assert should_play_filler("今天天气怎么样", "smart", KW) is True
    assert should_play_filler("你好呀", "smart", KW) is False


def test_always_and_off_modes():
    assert should_play_filler("你好", "always", KW) is True
    assert should_play_filler("今天天气怎么样", "off", KW) is False


def _local_ogg() -> Path | None:
    d = Path("data/wake_responses")
    if not d.exists():
        return None
    return next(d.rglob("*.ogg"), None)


@pytest.mark.skipif(_local_ogg() is None, reason="needs a locally cached ogg (data/ is machine-local)")
def test_enqueue_filler_packets_from_real_ogg():
    q: sync_queue.Queue = sync_queue.Queue(maxsize=200)
    n = enqueue_filler_packets(_local_ogg(), q)
    assert n > 0 and q.qsize() == n
    assert all(isinstance(q.get_nowait(), bytes) for _ in range(n))


def test_enqueue_filler_missing_file_is_safe():
    q: sync_queue.Queue = sync_queue.Queue()
    assert enqueue_filler_packets(Path("/nonexistent/x.ogg"), q) == 0
