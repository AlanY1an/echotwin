import time

import pytest

from echotwin.cost.pricing import calc_cost
from echotwin.cost.tracker import CostTracker


def test_calc_cost_utf8():
    # 1M bytes of fish_audio_tts = $15
    assert calc_cost("fishaudio_tts", 1_000_000) == pytest.approx(15.0)
    # 100k bytes = $1.5
    assert calc_cost("fishaudio_tts", 100_000) == pytest.approx(1.5)


def test_calc_cost_seconds():
    # 1 hour = 3600s of fishaudio_asr = $0.36
    assert calc_cost("fishaudio_asr", 3600) == pytest.approx(0.36)


def test_calc_cost_unknown_returns_zero():
    assert calc_cost("nonexistent", 1000) == 0.0


@pytest.mark.asyncio
async def test_record_and_summary(tmp_path):
    db = tmp_path / "test_costs.db"
    tracker = CostTracker(db_path=str(db))
    await tracker.init()

    cost1 = await tracker.record("fishaudio_tts", 100_000, guild_id="g1")
    assert cost1 == pytest.approx(1.5)

    cost2 = await tracker.record("claude_haiku_4_5_input", 5000, guild_id="g1")
    assert cost2 == pytest.approx(0.005)

    summary = await tracker.summary(since=0.0)
    assert summary["fishaudio_tts"] == pytest.approx(1.5)
    assert summary["claude_haiku_4_5_input"] == pytest.approx(0.005)

    total = await tracker.total(since=0.0)
    assert total == pytest.approx(1.505)


@pytest.mark.asyncio
async def test_summary_filters_by_time(tmp_path):
    db = tmp_path / "filter.db"
    tracker = CostTracker(db_path=str(db))
    await tracker.init()
    await tracker.record("fishaudio_tts", 1000)
    future = time.time() + 1000
    summary = await tracker.summary(since=future)
    assert summary == {}
