"""
Local-only HTTP server that receives geofence events from an iOS Shortcuts
automation (Automation tab -> Arrive/Leave a location -> Get Contents of
URL) and turns them into Discord DMs.

Bound to 127.0.0.1 — nginx reverse-proxies /geofence/webhook to it from the
Tailscale-only interface, same pattern as /calendar/oauth/callback and the
other local dashboards (see /etc/nginx/sites-available/status). The phone
needs Tailscale connected for the request to land; the shared secret is the
only auth, so keep it out of source control (.env, not committed).
"""

import hmac
import logging

from aiohttp import web

from . import config
from .discord_client import client
from .tools.reminders import pop_location_reminders

log = logging.getLogger("discord-llm-bot.geofence_server")

_ARRIVE_MESSAGE = "🏠 Welcome home."
_LEAVE_MESSAGE = "🚪 Left home."


async def _notify(user_id: int, text: str) -> None:
    try:
        user = await client.fetch_user(user_id)
        await user.send(text)
    except Exception:
        log.exception("Failed to deliver geofence notification to user %s", user_id)


async def handle_webhook(request: web.Request) -> web.Response:
    if not config.GEOFENCE_WEBHOOK_SECRET:
        return web.Response(status=503, text="Geofence webhook not configured.")

    params = dict(request.query)
    if request.method == "POST" and request.can_read_body:
        try:
            params.update(await request.post())
        except Exception:
            log.warning("Ignoring unparseable geofence POST body", exc_info=True)

    # Compare as bytes: hmac.compare_digest raises TypeError on str inputs
    # containing non-ASCII, which an arbitrary query string can easily have.
    secret = str(params.get("secret", "")).encode()
    if not hmac.compare_digest(secret, config.GEOFENCE_WEBHOOK_SECRET.encode()):
        return web.Response(status=403, text="Bad secret.")

    trigger = params.get("event")
    if trigger not in ("arrive", "leave"):
        return web.Response(status=400, text="event must be 'arrive' or 'leave'.")

    if config.GEOFENCE_NOTIFY_USER_ID is None:
        log.warning("Geofence event %r received but GEOFENCE_NOTIFY_USER_ID is unset", trigger)
        return web.Response(status=503, text="No notify user configured.")

    log.info("Geofence event: %s", trigger)
    await _notify(config.GEOFENCE_NOTIFY_USER_ID, _ARRIVE_MESSAGE if trigger == "arrive" else _LEAVE_MESSAGE)

    for reminder in pop_location_reminders(trigger):
        await _notify(reminder["author_id"], f"⏰ Reminder: {reminder['text']}")

    return web.Response(text="ok")


async def start() -> None:
    app = web.Application()
    app.router.add_post("/geofence/webhook", handle_webhook)
    # Shortcuts defaults to GET unless changed. Note a GET puts the shared
    # secret in the query string, where nginx logs it in plain text — prefer
    # configuring the Shortcut to POST a form body.
    app.router.add_get("/geofence/webhook", handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", config.GEOFENCE_SERVER_PORT)
    await site.start()
    log.info("Geofence webhook server listening on 127.0.0.1:%d", config.GEOFENCE_SERVER_PORT)
