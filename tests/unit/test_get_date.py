import pytest

from echotwin.tools.get_date import GetDate


@pytest.mark.asyncio
async def test_today():
    d = GetDate(default_timezone="Asia/Taipei")
    out = await d.execute({})
    assert "年" in out and "月" in out and "日" in out
    assert "星期" in out


@pytest.mark.asyncio
async def test_offset_tomorrow_differs():
    d = GetDate(default_timezone="Asia/Taipei")
    today = await d.execute({})
    tomorrow = await d.execute({"offset_days": 1})
    assert today != tomorrow


@pytest.mark.asyncio
async def test_negative_offset():
    d = GetDate(default_timezone="Asia/Taipei")
    out = await d.execute({"offset_days": -1})
    assert "年" in out
