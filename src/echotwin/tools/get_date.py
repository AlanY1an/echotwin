"""get_date tool — today / past / future date."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import Tool, ToolError


_WEEKDAY_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


class GetDate(Tool):
    name = "get_date"
    description = (
        "获取日期(年月日 + 星期几)。可选参数 offset_days(整数,正数=未来,负数=过去),"
        "默认 0=今天,1=明天,-1=昨天。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "offset_days": {
                "type": "integer",
                "description": "相对今天的天数偏移。0=今天,1=明天,-1=昨天",
            },
            "timezone": {
                "type": "string",
                "description": "可选时区 IANA 名,默认使用 bot 配置时区",
            },
        },
    }

    def __init__(self, default_timezone: str = "Asia/Taipei"):
        self._default_tz = default_timezone

    async def execute(self, args: dict) -> str:
        offset = int(args.get("offset_days") or 0)
        tz_name = args.get("timezone") or self._default_tz
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            raise ToolError(f"未知时区: {tz_name}")
        target = datetime.now(tz) + timedelta(days=offset)
        wd = _WEEKDAY_ZH[target.weekday()]
        return f"{target.year} 年 {target.month} 月 {target.day} 日 {wd}"
