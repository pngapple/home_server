"""
Local-only HTTP server that receives the Google OAuth redirect.

Bound to 127.0.0.1 — nginx reverse-proxies /calendar/oauth/callback to it
from the Tailscale-only interface, same pattern as the Netdata dashboard.
Not reachable from the LAN or the internet.
"""

import asyncio
import html
import logging

from aiohttp import web

from . import config, google_oauth, webserver

log = logging.getLogger("discord-llm-bot.oauth_server")

_PAGE = """<!doctype html><html><body style="font-family:sans-serif;max-width:32em;margin:4em auto">
<h2>{heading}</h2><p>{body}</p></body></html>"""

_RETRY_HINT = "ask the bot to connect your calendar again."


def _page(heading: str, body: str) -> web.Response:
    return web.Response(text=_PAGE.format(heading=heading, body=body), content_type="text/html")


async def handle_callback(request: web.Request) -> web.Response:
    error = request.query.get("error")
    if error:
        return _page("Not connected", f"Google reported: {html.escape(error)}. You can close this tab.")

    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return web.Response(status=400, text="Missing code/state.")

    try:
        # exchange_code does a blocking HTTPS round trip to Google; this
        # handler shares the Discord client's event loop, so keep it off it.
        discord_user_id = await asyncio.to_thread(google_oauth.exchange_code, state, code)
    except Exception:
        log.exception("Google token exchange failed")
        return _page("Something went wrong", f"Google wouldn't complete the link — {_RETRY_HINT}")
    if discord_user_id is None:
        return _page("Link expired", f"That authorization link is no longer valid — {_RETRY_HINT}")

    log.info("Google Calendar connected for Discord user %s", discord_user_id)
    return _page("Connected!", "Your Google Calendar is linked. You can close this tab.")


async def start() -> None:
    await webserver.serve(
        "OAuth callback server",
        config.GOOGLE_OAUTH_SERVER_PORT,
        [web.get("/calendar/oauth/callback", handle_callback)],
    )
