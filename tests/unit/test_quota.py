from unittest.mock import AsyncMock, MagicMock

import pytest

from echotwin.utils.quota import QuotaGuard, QuotaStatus


def make_tracker(daily=0.0, monthly=0.0):
    """tracker.total(since) returns the total since the timestamp.
    For our quota check we ask for last 86400s (daily) and last 30*86400s (monthly).
    Simulate by returning daily for the first call and monthly for the second."""
    t = MagicMock()
    calls = {"n": 0}

    async def total(since):
        calls["n"] += 1
        # First call = daily window, second = monthly window
        return daily if calls["n"] == 1 else monthly

    t.total = total
    return t


@pytest.mark.asyncio
async def test_under_limits_ok():
    g = QuotaGuard(make_tracker(2.0, 30.0), daily_usd=5.0, monthly_usd=50.0, on_exceed="shutdown")
    assert (await g.check()) == QuotaStatus.OK


@pytest.mark.asyncio
async def test_daily_exceeded():
    g = QuotaGuard(make_tracker(6.0, 30.0), daily_usd=5.0, monthly_usd=50.0, on_exceed="shutdown")
    assert (await g.check()) == QuotaStatus.EXCEEDED_DAILY


@pytest.mark.asyncio
async def test_monthly_exceeded_when_daily_ok():
    g = QuotaGuard(make_tracker(2.0, 60.0), daily_usd=5.0, monthly_usd=50.0, on_exceed="shutdown")
    assert (await g.check()) == QuotaStatus.EXCEEDED_MONTHLY


@pytest.mark.asyncio
async def test_warn_mode_does_not_block():
    g = QuotaGuard(make_tracker(6.0, 30.0), daily_usd=5.0, monthly_usd=50.0, on_exceed="warn")
    assert (await g.check()) == QuotaStatus.EXCEEDED_DAILY
    assert (await g.should_block()) is False


@pytest.mark.asyncio
async def test_shutdown_mode_blocks():
    g = QuotaGuard(make_tracker(6.0, 30.0), daily_usd=5.0, monthly_usd=50.0, on_exceed="shutdown")
    assert (await g.should_block()) is True


@pytest.mark.asyncio
async def test_warn_mode_actually_warns():
    """Historical bug: in warn mode should_block() returned early and never called check(),
    making the warning log inside check() unreachable — "warn" was equivalent to "off"."""
    from loguru import logger

    class OverBudgetTracker:
        async def total(self, since):
            return 999.0

    g = QuotaGuard(OverBudgetTracker(), daily_usd=5.0, monthly_usd=50.0, on_exceed="warn")
    messages = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        blocked = await g.should_block()
    finally:
        logger.remove(handler_id)

    assert blocked is False  # warn mode does not block
    assert any("quota" in m for m in messages), (
        f"warn 模式超预算必须打 WARNING,实际: {messages}"
    )
