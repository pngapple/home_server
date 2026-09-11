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

A "replied" update can carry a synthesized WAV clip (base64, under
`audio_b64`) — the reply is spoken through *this* page in the browser
rather than through whatever's plugged into the Pi, which is why TTS lives
here and not just as a local aplay call in voice/tts.py. Only the latest
clip is kept: this is "what should I be playing right now", not a
transcript archive (bot/voice_server.py's DM to the owner is that record).
`audio_id` is a monotonic counter, not a hash or timestamp of the clip
itself, so the dashboard can cheaply tell "there's a new clip" from "same
clip, poll again" without re-fetching audio bytes on every tick.

`_history` is a short, separate record of finished turns (replied/rejected/
error only — not the in-between phases like "transcribing", which are
live-progress noise once the turn is over, not something worth remembering)
so the dashboard can show what happened recently instead of only ever the
current instant. Bounded and in-memory for the same reason `_state` is:
this is a glance-at-it dashboard, not an audit log — bot/voice_server.py's
DM to the owner is still the durable record of every command.
"""

import base64
import hmac
import logging
import time
from collections import deque

from aiohttp import web

from . import config, webserver

log = logging.getLogger("discord-llm-bot.voice_status_server")

# No update in this long -> the listener isn't there to have sent one,
# whether it crashed, lost network, or the Pi rebooted. Comfortably above
# the idle heartbeat interval in listen.py so one dropped heartbeat doesn't
# flap the dashboard to "offline" and back.
_STALE_S = 45.0

# Phases worth a line in the activity feed — the terminal outcome of a
# turn, not the mechanical steps (awoken/recording/transcribing/sending)
# that led up to it. Those still drive the live orb via _state, just not
# _history.
_HISTORY_PHASES = frozenset({"replied", "rejected", "error"})
_HISTORY_MAXLEN = 20

_state = {
    "phase": "offline",
    "detail": {},
    "updated_at": 0.0,
    "phase_since": 0.0,
    "audio_id": 0,
}

_history: deque = deque(maxlen=_HISTORY_MAXLEN)

_audio: bytes | None = None


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
            "audio_id": _state["audio_id"],
            # Newest-first, matching llm.html's "recent" convention.
            "history": list(reversed(_history)),
        }
    )


async def handle_get_audio(request: web.Request) -> web.Response:
    if _audio is None:
        return web.Response(status=404, text="No audio yet.")
    return web.Response(body=_audio, content_type="audio/wav")


async def handle_post_status(request: web.Request) -> web.Response:
    global _audio
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
    detail = body.get("detail") or {}
    _state["detail"] = detail
    _state["updated_at"] = now

    if phase in _HISTORY_PHASES:
        _history.append({"ts": now, "phase": phase, "detail": detail})

    audio_b64 = body.get("audio_b64")
    if audio_b64:
        try:
            _audio = base64.b64decode(audio_b64)
            _state["audio_id"] += 1
        except Exception:
            log.warning("Dropped an unparseable audio_b64 on a status update")

    return web.json_response({"ok": True})


async def start() -> None:
    await webserver.serve(
        "Voice status dashboard",
        config.VOICE_STATUS_SERVER_PORT,
        [
            web.get("/", handle_index),
            web.get("/api/status", handle_get_status),
            web.post("/api/status", handle_post_status),
            web.get("/api/audio", handle_get_audio),
        ],
    )
