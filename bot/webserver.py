"""
Shared plumbing for the small local-only aiohttp servers this bot runs
alongside the Discord client: the Google OAuth callback, the geofence
webhook, the LLM status dashboard and the cigboard leaderboard.

They all follow the same shape — bind 127.0.0.1 on a config port, let nginx
reverse-proxy them from the Tailscale-only interface (see
/etc/nginx/sites-available/status) — so the AppRunner/TCPSite boilerplate
lives here once instead of four times. Each server module just declares its
routes and calls serve().
"""

import logging

from aiohttp import web

log = logging.getLogger("discord-llm-bot.webserver")

_runners: list[web.AppRunner] = []


async def serve(name: str, port: int, routes, host: str = "127.0.0.1") -> web.AppRunner:
    """Start one local HTTP server and keep its runner for shutdown_all()."""
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    _runners.append(runner)
    log.info("%s listening on %s:%d", name, host, port)
    return runner


async def shutdown_all() -> None:
    """Release every listening socket. Not needed for a systemd restart (the
    process dies outright), but lets an in-process shutdown rebind cleanly."""
    for runner in _runners:
        try:
            await runner.cleanup()
        except Exception:
            log.exception("Failed to shut down an HTTP server cleanly")
    _runners.clear()
