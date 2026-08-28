"""
Local-only HTTP server that receives the Google OAuth redirect.

Bound to 127.0.0.1 — nginx reverse-proxies /calendar/oauth/callback to it
from the Tailscale-only interface, same pattern as the Netdata dashboard.
Not reachable from the LAN or the internet.
"""

import logging

from aiohttp import web

from . import config, google_oauth

log = logging.getLogger("discord-llm-bot.oauth_server")

_PAGE = """<!doctype html><html><body style="font-family:sans-serif;max-width:32em;margin:4em auto">
<h2>{heading}</h2><p>{body}</p></body></html>"""


async def handle_callback(request: web.Request) -> web.Response:
    error = request.query.get("error")
    if error:
        return web.Response(
            text=_PAGE.format(heading="Not connected", body=f"Google reported: {error}. You can close this tab."),
            content_type="text/html",
        )

    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return web.Response(status=400, text="Missing code/state.")

    discord_user_id = google_oauth.exchange_code(state, code)
    if discord_user_id is None:
        return web.Response(
            text=_PAGE.format(
                heading="Link expired",
                body="That authorization link is no longer valid — ask the bot to connect your calendar again.",
            ),
            content_type="text/html",
        )

    log.info("Google Calendar connected for Discord user %s", discord_user_id)
    return web.Response(
        text=_PAGE.format(heading="Connected!", body="Your Google Calendar is linked. You can close this tab."),
        content_type="text/html",
    )


async def start() -> None:
    app = web.Application()
    app.router.add_get("/calendar/oauth/callback", handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", config.GOOGLE_OAUTH_SERVER_PORT)
    await site.start()
    log.info("OAuth callback server listening on 127.0.0.1:%d", config.GOOGLE_OAUTH_SERVER_PORT)
