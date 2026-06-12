from unittest.mock import AsyncMock, MagicMock

import pytest

from echotwin.tools.base import ToolError
from echotwin.tools.get_weather import GetWeather


@pytest.fixture
def tool():
    return GetWeather(default_city="台北")


def _mock_response(json_payload: dict, status: int = 200):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


_TAIPEI_J1 = {
    "nearest_area": [{"areaName": [{"value": "Taipei"}]}],
    "current_condition": [
        {
            "temp_C": "26",
            "weatherDesc": [{"value": "Partly cloudy"}],
            "humidity": "70",
            "winddir16Point": "NE",
        }
    ],
    "weather": [
        {"date": "2026-05-09", "mintempC": "22", "maxtempC": "28", "uvIndex": "8",
         "hourly": [{"weatherDesc": [{"value": "Sunny"}]}]},
        {"date": "2026-05-10", "mintempC": "23", "maxtempC": "29", "uvIndex": "9",
         "hourly": [{"weatherDesc": [{"value": "Cloudy"}]}]},
        {"date": "2026-05-11", "mintempC": "24", "maxtempC": "30", "uvIndex": "10",
         "hourly": [{"weatherDesc": [{"value": "Rain"}]}]},
    ],
}


@pytest.mark.asyncio
async def test_today_weather(tool, monkeypatch):
    captured: list[str] = []

    def fake_get(self, url, **kw):
        captured.append(url)
        return _mock_response(_TAIPEI_J1)

    monkeypatch.setattr("aiohttp.ClientSession.get", fake_get)
    result = await tool.execute({"city": "台北"})
    assert "Partly cloudy" in result
    assert "26" in result
    assert "70" in result
    # Chinese alias should be translated to ASCII for the URL
    assert "Taipei" in captured[0]


@pytest.mark.asyncio
async def test_default_city_used(tool, monkeypatch):
    captured: list[str] = []

    def fake_get(self, url, **kw):
        captured.append(url)
        return _mock_response(_TAIPEI_J1)

    monkeypatch.setattr("aiohttp.ClientSession.get", fake_get)
    result = await tool.execute({})
    assert "Taipei" in captured[0]
    assert "26" in result


@pytest.mark.asyncio
async def test_tomorrow_query(tool, monkeypatch):
    def fake_get(self, url, **kw):
        return _mock_response(_TAIPEI_J1)

    monkeypatch.setattr("aiohttp.ClientSession.get", fake_get)
    result = await tool.execute({"city": "台北", "when": "tomorrow"})
    assert "明天" in result
    assert "23" in result and "29" in result


@pytest.mark.asyncio
async def test_3day_query(tool, monkeypatch):
    def fake_get(self, url, **kw):
        return _mock_response(_TAIPEI_J1)

    monkeypatch.setattr("aiohttp.ClientSession.get", fake_get)
    result = await tool.execute({"city": "台北", "when": "3day"})
    assert "未来三天" in result
    # Three dates joined by "; "
    assert result.count(";") >= 2


@pytest.mark.asyncio
async def test_api_500_raises(tool, monkeypatch):
    def fake_get(self, url, **kw):
        return _mock_response({}, status=500)

    monkeypatch.setattr("aiohttp.ClientSession.get", fake_get)
    with pytest.raises(ToolError):
        await tool.execute({"city": "台北"})


@pytest.mark.asyncio
async def test_unknown_city_no_data(tool, monkeypatch):
    def fake_get(self, url, **kw):
        return _mock_response({"current_condition": []})

    monkeypatch.setattr("aiohttp.ClientSession.get", fake_get)
    with pytest.raises(ToolError):
        await tool.execute({"city": "Mars"})
