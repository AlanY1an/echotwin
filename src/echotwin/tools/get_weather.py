"""get_weather tool — uses wttr.in (free, no key, no host hassle).

wttr.in returns weather data for any city in JSON via ?format=j1.
Lookup tolerates Chinese city names (台北 → Taipei via geocoding inside wttr).
"""
from __future__ import annotations

import aiohttp
from loguru import logger

from .base import Tool, ToolError

_BASE = "https://wttr.in"


# Translate common Chinese city aliases to ASCII so wttr.in geocodes them
# reliably. wttr.in does accept some Unicode but ASCII is safer.
_CITY_ALIAS = {
    "台北": "Taipei",
    "臺北": "Taipei",
    "台中": "Taichung",
    "高雄": "Kaohsiung",
    "北京": "Beijing",
    "上海": "Shanghai",
    "广州": "Guangzhou",
    "深圳": "Shenzhen",
    "杭州": "Hangzhou",
    "成都": "Chengdu",
    "重庆": "Chongqing",
    "南京": "Nanjing",
    "西安": "Xian",
    "天津": "Tianjin",
    "武汉": "Wuhan",
    "苏州": "Suzhou",
    "长沙": "Changsha",
    "青岛": "Qingdao",
    "厦门": "Xiamen",
    "东京": "Tokyo",
    "纽约": "New York",
}


def _resolve_city(name: str) -> str:
    return _CITY_ALIAS.get(name.strip(), name.strip())


class GetWeather(Tool):
    name = "get_weather"
    description = (
        "获取天气。参数 city(城市名,中文/英文均可,如 台北、Tokyo),"
        "when(today=今天 / tomorrow=明天 / 3day=未来三天),默认 today。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名,中文或英文"},
            "when": {
                "type": "string",
                "enum": ["today", "tomorrow", "3day"],
                "description": "查询时间窗口,默认 today",
            },
        },
    }

    def __init__(self, default_city: str = "台北"):
        self._default_city = default_city

    async def _fetch(self, city: str) -> dict:
        url = f"{_BASE}/{city}?format=j1"
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as r:
                    if r.status != 200:
                        raise ToolError(f"天气查询失败 (HTTP {r.status})")
                    # wttr.in returns JSON with text/plain mimetype; disable strict check
                    return await r.json(content_type=None)
        except aiohttp.ClientError as e:
            raise ToolError(f"网络错误: {e}")

    async def execute(self, args: dict) -> str:
        raw_city = (args.get("city") or self._default_city).strip()
        when = (args.get("when") or "today").strip().lower()
        if when not in {"today", "tomorrow", "3day"}:
            when = "today"
        city = _resolve_city(raw_city)
        data = await self._fetch(city)

        # Always show the user's original city name — wttr.in's nearest_area is
        # often a small district (e.g. "台北" → "Tingtungshih"), which is confusing
        display_name = raw_city

        if when == "today":
            cur_list = data.get("current_condition") or []
            if not cur_list:
                raise ToolError(f"找不到 {raw_city} 的天气数据")
            cur = cur_list[0]
            desc_list = cur.get("weatherDesc") or []
            desc = desc_list[0].get("value", "") if desc_list else ""
            return (
                f"{display_name} 现在 {desc} {cur.get('temp_C', '?')}°C, "
                f"{cur.get('winddir16Point', '')}风, 湿度 {cur.get('humidity', '')}%"
            )

        weather_list = data.get("weather") or []
        if when == "tomorrow":
            if len(weather_list) < 2:
                raise ToolError("没有明天的预报数据")
            d = weather_list[1]
            return (
                f"{display_name} 明天 ({d.get('date', '')}) "
                f"{d.get('mintempC')}~{d.get('maxtempC')}°C, "
                f"日最高紫外指数 {d.get('uvIndex', '?')}"
            )

        # 3day
        parts = []
        for d in weather_list[:3]:
            mid = d.get("hourly", [{}])[len(d.get("hourly", [])) // 2 if d.get("hourly") else 0]
            desc_list = mid.get("weatherDesc") or []
            desc = desc_list[0].get("value", "") if desc_list else ""
            parts.append(
                f"{d.get('date', '')} {desc} {d.get('mintempC')}~{d.get('maxtempC')}°C"
            )
        return f"{display_name} 未来三天: " + "; ".join(parts)
