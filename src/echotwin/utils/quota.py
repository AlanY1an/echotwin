"""QuotaGuard — translate cost-tracker totals into block/allow decisions."""
from __future__ import annotations

import time
from enum import Enum

from loguru import logger


class QuotaStatus(Enum):
    OK = "ok"
    EXCEEDED_DAILY = "exceeded_daily"
    EXCEEDED_MONTHLY = "exceeded_monthly"


class QuotaGuard:
    """Wrap the CostTracker; cache results briefly so we don't query DB per turn."""

    _CACHE_TTL = 5.0  # seconds — quota check cache

    def __init__(
        self,
        tracker,
        daily_usd: float,
        monthly_usd: float,
        on_exceed: str = "warn",
    ):
        self._tracker = tracker
        self._daily = daily_usd
        self._monthly = monthly_usd
        self._on_exceed = on_exceed
        self._cached_status: QuotaStatus = QuotaStatus.OK
        self._cached_at: float = 0.0

    async def check(self) -> QuotaStatus:
        now = time.time()
        if now - self._cached_at < self._CACHE_TTL:
            return self._cached_status
        daily = await self._tracker.total(now - 86400)
        monthly = await self._tracker.total(now - 30 * 86400)
        if daily >= self._daily:
            status = QuotaStatus.EXCEEDED_DAILY
        elif monthly >= self._monthly:
            status = QuotaStatus.EXCEEDED_MONTHLY
        else:
            status = QuotaStatus.OK
        self._cached_status = status
        self._cached_at = now
        if status != QuotaStatus.OK:
            logger.warning(
                f"[quota] {status.value}: daily=${daily:.4f}/{self._daily}, "
                f"monthly=${monthly:.4f}/{self._monthly}"
            )
        return status

    async def should_block(self) -> bool:
        # Always run the check so "warn" mode actually logs the warning
        # (check() emits it); only "shutdown" mode converts it into a block.
        status = await self.check()
        if self._on_exceed != "shutdown":
            return False
        return status != QuotaStatus.OK
