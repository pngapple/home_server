"""
Local-only HTTP server that receives geofence events from an iOS Shortcuts
automation (Automation tab -> Arrive/Leave a location -> Get Contents of
URL) and turns them into Discord DMs.

Bound to 127.0.0.1 — nginx reverse-proxies /geofence/webhook to it from the
Tailscale-only interface, same pattern as /calendar/oauth/callback and the
other local dashboards (see /etc/nginx/sites-available/status). The phone
needs Tailscale connected for the request to land.

Each resident's phone sends its own secret (config.GEOFENCE_USERS maps
secret -> Discord user id) rather than one shared secret for the household —
that's what identifies whose arrive/leave event this is, so one resident
getting home doesn't fire another resident's location reminders. Keep the
secrets out of source control (.env, not committed).
"""

import hmac
import logging

from aiohttp import web

from . import config, webserver
from .discord_client import client
from .tools import reminders

log = logging.getLogger("discord-llm-bot.geofence_server")

_ARRIVE_MESSAGE = "🏠 Welcome home."
_LEAVE_MESSAGE = "🚪 Left home."


async def _notify(user_id: int, text: str) -> None:
    try:
        user = await client.fetch_user(user_id)
        await user.send(text)
    except Exception:
        log.exception("Failed to deliver geofence notification to user %s", user_id)


def _match_user(secret: bytes) -> int | None:
    """Which resident this secret belongs to, or None if it matches nobody.
    Checks every configured secret rather than stopping at the first
    mismatch, so response timing can't be used to narrow down which secret
    (if any) is close to correct."""
    matched = None
    for candidate_secret, candidate_user_id in config.GEOFENCE_USERS.items():
        # Compare as bytes: hmac.compare_digest raises TypeError on str
        # inputs containing non-ASCII, which an arbitrary query string can
        # easily have.
        if hmac.compare_digest(secret, candidate_secret.encode()):
            matched = candidate_user_id
    return matched


async def handle_webhook(request: web.Request) -> web.Response:
    if not config.GEOFENCE_USERS:
        return web.Response(status=503, text="Geofence webhook not configured.")

    params = dict(request.query)
    if request.method == "POST" and request.can_read_body:
        try:
            params.update(await request.post())
        except Exception:
            log.warning("Ignoring unparseable geofence POST body", exc_info=True)

    user_id = _match_user(str(params.get("secret", "")).encode())
    if user_id is None:
        return web.Response(status=403, text="Bad secret.")

    trigger = params.get("event")
    if trigger not in ("arrive", "leave"):
        return web.Response(status=400, text="event must be 'arrive' or 'leave'.")

    log.info("Geofence event: %s for user %s", trigger, user_id)
    await _notify(user_id, _ARRIVE_MESSAGE if trigger == "arrive" else _LEAVE_MESSAGE)

    reminders.record_geofence_event(user_id, trigger)
    reminders.sync_recurring_for_event(user_id, trigger)

    for reminder in reminders.pop_location_reminders(user_id, trigger):
        await _notify(reminder["author_id"], f"⏰ Reminder: {reminder['text']}")

    return web.Response(text="ok")


async def start() -> None:
    await webserver.serve(
        "Geofence webhook server",
        config.GEOFENCE_SERVER_PORT,
        [
            web.post("/geofence/webhook", handle_webhook),
            # Shortcuts defaults to GET unless changed. Note a GET puts the
            # secret in the query string, where nginx logs it in plain
            # text — prefer configuring the Shortcut to POST a form body.
            web.get("/geofence/webhook", handle_webhook),
        ],
    )
