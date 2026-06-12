"""Cost tracker: record provider usage events to SQLite, query summaries."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite
from loguru import logger

from .pricing import calc_cost


class CostTracker:
    def __init__(self, db_path: str = "data/costs.db"):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._initialized = False

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS cost_events (
                    id INTEGER PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    guild_id TEXT,
                    user_id TEXT,
                    sentence_id TEXT,
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_time ON cost_events(timestamp)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cost_guild ON cost_events(guild_id)"
            )
            await db.commit()
        self._initialized = True
        logger.debug(f"cost_tracker initialized at {self.db_path}")

    async def record(
        self,
        kind: str,
        amount: float,
        *,
        guild_id: str = "",
        user_id: str = "",
        sentence_id: str = "",
    ) -> float:
        if not self._initialized:
            await self.init()
        cost = calc_cost(kind, amount)
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO cost_events (timestamp, guild_id, user_id, sentence_id, kind, amount, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), guild_id, user_id, sentence_id, kind, amount, cost),
                )
                await db.commit()
        return cost

    async def summary(self, since: float) -> dict[str, float]:
        if not self._initialized:
            await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT kind, SUM(cost_usd) FROM cost_events WHERE timestamp >= ? GROUP BY kind",
                (since,),
            )
            rows = await cur.fetchall()
        return {row[0]: float(row[1] or 0.0) for row in rows}

    async def total(self, since: float) -> float:
        s = await self.summary(since)
        return sum(s.values())
