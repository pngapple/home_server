"""
Local-only HTTP server exposing an at-a-glance LLM metrics dashboard.

Bound to 127.0.0.1 — nginx reverse-proxies /llm/ to it from the
Tailscale-only interface, same pattern as /calendar/oauth/callback and the
Netdata dashboard at /status/ (see sites-available/status).

The page itself is static/llm.html, loaded through webserver.page(). It used
to be a 311-line string literal in this file, which left ~70 lines of actual
server here buried in markup.
"""

import html
import logging
import time

from aiohttp import web

from . import config, httpclient, metrics, webserver

log = logging.getLogger("discord-llm-bot.llm_status_server")

# OpenRouter's key-info endpoint is the authoritative source for credit
# usage/limit (rather than us estimating from per-call responses) — cached
# briefly so the 4s dashboard poll doesn't hit it every tick.
_OR_CREDITS_TTL_S = 60.0
_or_credits_cache: dict = {"data": None, "ts": 0.0}


async def _fetch_openrouter_credits() -> dict | None:
    now = time.monotonic()
    if _or_credits_cache["data"] is not None and now - _or_credits_cache["ts"] < _OR_CREDITS_TTL_S:
        return _or_credits_cache["data"]
    try:
        async with httpclient.session().get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    except Exception:
        log.exception("Failed to fetch OpenRouter credit info")
        return _or_credits_cache["data"]  # serve last-known value (or None) rather than fail the whole tick
    data = payload["data"]
    _or_credits_cache["data"] = data
    _or_credits_cache["ts"] = now
    return data
async def handle_index(request: web.Request) -> web.Response:
    page = webserver.page(__file__, "llm.html").replace(
        "__CHAT_MODEL__", html.escape(config.OPENROUTER_MODEL)
    )
    return web.Response(text=page, content_type="text/html")


async def handle_metrics(request: web.Request) -> web.Response:
    snap = metrics.snapshot()
    credits = await _fetch_openrouter_credits()
    snap["openrouter_credits"] = (
        {
            "limit": credits.get("limit"),
            "used": credits.get("usage"),
            "remaining": credits.get("limit_remaining"),
        }
        if credits
        else None
    )
    return web.json_response(snap)


async def start() -> None:
    await webserver.serve(
        "LLM status server",
        config.LLM_STATUS_SERVER_PORT,
        [web.get("/", handle_index), web.get("/api/metrics", handle_metrics)],
    )
