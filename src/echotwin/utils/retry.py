"""async_retry — generic retry with exponential backoff."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from loguru import logger

T = TypeVar("T")


async def async_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    backoff: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
        OSError,
    ),
    name: str = "op",
) -> T:
    last_exc: BaseException | None = None
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as e:
            last_exc = e
            if attempt == attempts:
                logger.warning(f"[retry] {name} failed after {attempts} attempts: {e}")
                raise
            logger.info(f"[retry] {name} attempt {attempt} failed ({e}); sleeping {delay}s")
            await asyncio.sleep(delay)
            delay *= backoff
    assert last_exc is not None
    raise last_exc
