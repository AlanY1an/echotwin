"""get_time tool — current local time."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import Tool, ToolError


class GetTime(Tool):
    name = "get_time"
    description = (
        "获取当前时间。可选参数 timezone(IANA 时区名,如 Asia/Taipei、Asia/Shanghai、UTC),"
        "默认使用 bot 配置时区。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA 时区名,如 Asia/Taipei、Asia/Shanghai、UTC",
            }
        },
    }

    def __init__(self, default_timezone: str = "Asia/Taipei", lang: str = "zh"):
        self._default_tz = default_timezone
        self._lang = lang

    async def execute(self, args: dict) -> str:
        tz_name = args.get("timezone") or self._default_tz
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            raise ToolError(f"未知时区: {tz_name}" if self._lang == "zh" else f"Unknown timezone: {tz_name}")
        now = datetime.now(tz)
        if self._lang == "en":
            return now.strftime(f"It is %A, %B %-d, %Y, %-I:%M %p ({tz_name})")
        ampm = "上午" if now.hour < 12 else "下午"
        h12 = now.hour % 12 or 12
        return (
            f"现在 {now.year} 年 {now.month} 月 {now.day} 日 "
            f"{ampm} {h12} 点 {now.minute} 分({tz_name})"
        )
