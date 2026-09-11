"""
Live mic-activity dashboard: what the voice listener (voice/listen.py, a
separate process/venv — see its module docstring) is doing right now —
idle/listening, wake word heard, recording, speaker-checking, transcribing,
or replied — rendered as one glanceable page instead of tailing
`journalctl -u voice-listener`.

State lives in memory only, same call as metrics.py's uptime: this answers
"what's happening right now", and a stale answer surviving a restart would
be actively misleading. The listener pushes a state update on every phase
transition plus a periodic heartbeat while idle-listening (see listen.py) —
POST /api/status, reusing VOICE_SERVER_SECRET since it's the same "only the
listener process is allowed to say this" trust boundary as the command
webhook. "Online" isn't a field the listener sets; it's derived here from
how long ago the last update landed, so a crashed/killed listener shows as
offline instead of frozen on its last real phase forever.
"""

import hmac
import logging
import time

from aiohttp import web

from . import config, webserver

log = logging.getLogger("discord-llm-bot.voice_status_server")

# No update in this long -> the listener isn't there to have sent one,
# whether it crashed, lost network, or the Pi rebooted. Comfortably above
# the idle heartbeat interval in listen.py so one dropped heartbeat doesn't
# flap the dashboard to "offline" and back.
_STALE_S = 45.0

_state = {
    "phase": "offline",
    "detail": {},
    "updated_at": 0.0,
    "phase_since": 0.0,
}


async def handle_index(request: web.Request) -> web.Response:
    page = webserver.page(__file__, "voice.html")
    return web.Response(text=page, content_type="text/html")


async def handle_get_status(request: web.Request) -> web.Response:
    now = time.time()
    online = (now - _state["updated_at"]) < _STALE_S if _state["updated_at"] else False
    return web.json_response(
        {
            "phase": _state["phase"] if online else "offline",
            "detail": _state["detail"],
            "online": online,
            "updated_at": _state["updated_at"],
            "phase_since": _state["phase_since"],
        }
    )


async def handle_post_status(request: web.Request) -> web.Response:
    if not config.VOICE_SERVER_SECRET:
        return web.Response(status=503, text="Voice commands aren't configured on the server.")
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad request: expected JSON.")

    secret = str(body.get("secret", ""))
    if not hmac.compare_digest(secret.encode(), config.VOICE_SERVER_SECRET.encode()):
        return web.Response(status=403, text="Bad secret.")

    phase = str(body.get("phase", "")).strip()
    if not phase:
        return web.Response(status=400, text="Bad request: 'phase' is required.")

    now = time.time()
    if phase != _state["phase"]:
        _state["phase_since"] = now
    _state["phase"] = phase
    _state["detail"] = body.get("detail") or {}
    _state["updated_at"] = now
    return web.json_response({"ok": True})


async def start() -> None:
    await webserver.serve(
        "Voice status dashboard",
        config.VOICE_STATUS_SERVER_PORT,
        [
            web.get("/", handle_index),
            web.get("/api/status", handle_get_status),
            web.post("/api/status", handle_post_status),
        ],
    )
