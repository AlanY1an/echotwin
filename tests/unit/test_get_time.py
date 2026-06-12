import pytest

from echotwin.tools.base import ToolError
from echotwin.tools.get_time import GetTime


@pytest.mark.asyncio
async def test_default_timezone():
    t = GetTime(default_timezone="Asia/Taipei")
    out = await t.execute({})
    assert "年" in out and ("点" in out or ":" in out)


@pytest.mark.asyncio
async def test_custom_timezone():
    t = GetTime(default_timezone="Asia/Taipei")
    out = await t.execute({"timezone": "UTC"})
    assert "UTC" in out


@pytest.mark.asyncio
async def test_unknown_timezone_raises():
    t = GetTime(default_timezone="Asia/Taipei")
    with pytest.raises(ToolError):
        await t.execute({"timezone": "Mars/Olympus"})
