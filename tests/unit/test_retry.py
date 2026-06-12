import asyncio

import pytest

from echotwin.utils.retry import async_retry


@pytest.mark.asyncio
async def test_succeeds_first_try():
    async def fn():
        return "ok"

    assert (await async_retry(fn)) == "ok"


@pytest.mark.asyncio
async def test_retries_on_failure():
    counter = {"n": 0}

    async def fn():
        counter["n"] += 1
        if counter["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    res = await async_retry(fn, attempts=5, base_delay=0.001, backoff=1.0)
    assert res == "ok"
    assert counter["n"] == 3


@pytest.mark.asyncio
async def test_exhausts_and_raises():
    async def fn():
        raise ConnectionError("never")

    with pytest.raises(ConnectionError):
        await async_retry(fn, attempts=2, base_delay=0.001)


@pytest.mark.asyncio
async def test_does_not_retry_unlisted():
    counter = {"n": 0}

    async def fn():
        counter["n"] += 1
        raise ValueError("don't retry me")

    with pytest.raises(ValueError):
        await async_retry(fn, attempts=5, base_delay=0.001, retry_on=(ConnectionError,))
    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_backoff_grows():
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d):
        delays.append(d)
        await real_sleep(0)

    counter = {"n": 0}

    async def fn():
        counter["n"] += 1
        raise ConnectionError("x")

    import echotwin.utils.retry as retry_mod

    orig = retry_mod.asyncio.sleep
    retry_mod.asyncio.sleep = fake_sleep
    try:
        with pytest.raises(ConnectionError):
            await async_retry(fn, attempts=4, base_delay=0.5, backoff=2.0)
    finally:
        retry_mod.asyncio.sleep = orig

    assert delays == [0.5, 1.0, 2.0]
