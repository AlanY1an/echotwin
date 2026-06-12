"""HTTP health endpoint for container orchestration / uptime monitoring."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiohttp import web
from loguru import logger

if TYPE_CHECKING:
    from echotwin.bot import VoiceAgentBot


def make_app(bot: "VoiceAgentBot") -> web.Application:
    app = web.Application()

    async def healthz(_request):
        if not bot.is_ready():
            return web.Response(text="discord_not_ready", status=503)
        return web.Response(text="ok", status=200)

    async def readyz(_request):
        if not bot.is_ready():
            return web.Response(text="not_ready", status=503)
        return web.Response(text="ok", status=200)

    async def stats(_request):
        return web.json_response({
            "uptime_seconds": int(time.time() - bot.start_time),
            "guilds": len(bot.guilds),
            "active_sessions": len(bot.sessions),
        })

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/stats.json", stats)
    return app


async def start_health_server(bot: "VoiceAgentBot", port: int) -> web.AppRunner:
    app = make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health endpoints on http://0.0.0.0:{port}/healthz /readyz /stats.json")
    return runner
