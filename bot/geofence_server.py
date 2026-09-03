"""
Local-only HTTP server that receives geofence events from an iOS Shortcuts
automation (Automation tab -> Arrive/Leave a location -> Get Contents of
URL) and turns them into Discord DMs.

Bound to 127.0.0.1 — nginx reverse-proxies /geofence/webhook to it from the
Tailscale-only interface, same pattern as /calendar/oauth/callback and the
other local dashboards (see /etc/nginx/sites-available/status). The phone
needs Tailscale connected for the request to land.

Each resident's phone sends its own secret (stored on their profile — see
users.by_geofence_secret) rather than one shared secret for the household —
that's what identifies whose arrive/leave event this is, so one resident
getting home doesn't fire another resident's location reminders. Keep
profiles.json out of source control: it holds those secrets, and is written
0600 for the same reason.
"""

import logging

from aiohttp import web

from . import config, notify, users, webserver
from .tools import reminders

log = logging.getLogger("discord-llm-bot.geofence_server")

_ARRIVE_MESSAGE = "🏠 Welcome home."
_LEAVE_MESSAGE = "🚪 Left home."


async def handle_webhook(request: web.Request) -> web.Response:
    if not users.any_geofence_users():
        return web.Response(status=503, text="Geofence webhook not configured.")

    params = dict(request.query)
    if request.method == "POST" and request.can_read_body:
        try:
            params.update(await request.post())
        except Exception:
            log.warning("Ignoring unparseable geofence POST body", exc_info=True)

    profile = users.by_geofence_secret(str(params.get("secret", "")))
    if profile is None:
        return web.Response(status=403, text="Bad secret.")
    user_id = profile.user_id

    trigger = params.get("event")
    if trigger not in ("arrive", "leave"):
        return web.Response(status=400, text="event must be 'arrive' or 'leave'.")

    log.info("Geofence event: %s for %s (%s)", trigger, profile.display_name, user_id)
    await notify.send_dm(user_id, _ARRIVE_MESSAGE if trigger == "arrive" else _LEAVE_MESSAGE)

    reminders.record_geofence_event(user_id, trigger)
    reminders.sync_recurring_for_event(user_id, trigger)

    for reminder in reminders.pop_location_reminders(user_id, trigger):
        await notify.send_dm(reminder["author_id"], f"⏰ Reminder: {reminder['text']}")

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
