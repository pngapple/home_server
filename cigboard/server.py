"""
Local-only HTTP server exposing the cigarette leaderboard.

Bound to 127.0.0.1 like bot/llm_status_server.py — reverse-proxy /cigboard/
to it from nginx the same way (see sites-available/status) if you want it
reachable outside the box.

The page itself is static/cigboard.html, loaded through webserver.page().
"""

import logging

from aiohttp import web

from bot import config, webserver

from . import discord_users, leaderboard

log = logging.getLogger("discord-llm-bot.cigboard.server")


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=webserver.page(__file__, "cigboard.html"), content_type="text/html")


async def handle_leaderboard(request: web.Request) -> web.Response:
    rows = leaderboard.compute()
    profiles = await discord_users.resolve_many([row["id"] for row in rows])
    users = [{**row, **profiles[row["id"]]} for row in rows]
    return web.json_response({"users": users})


async def start() -> None:
    await webserver.serve(
        "Cigboard server",
        config.CIGBOARD_SERVER_PORT,
        [web.get("/", handle_index), web.get("/api/leaderboard", handle_leaderboard)],
    )
