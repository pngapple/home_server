"""
Local-only HTTP endpoint that a separate voice-listener process (wake word +
speech-to-text, see voice/ at the repo root) POSTs transcribed commands to.
Two paths from there:

  - A small set of Kasa on/off commands ("turn on desk lights") are matched
    by a regex here and dispatched straight to bot/tools/kasa.py's
    set_plug_power, skipping the LLM entirely — that's the latency win over
    the text bot, which always pays for an OpenRouter round trip.
  - Everything else falls back to the same ask_llm() pipeline a DM would
    take, run for the fixed household-owner identity
    (config.CLAUDE_CODE_OWNER_ID). This endpoint has no notion of "who is
    speaking" beyond the shared secret plus the listener's own speaker
    verification upstream of ever reaching here, so it only ever acts as
    that one owner.

Every reply — fast-path or not — is also DMed to the owner via
notify.send_dm(), so there's always a durable, readable record of what a
voice command did even before (or if) the listener's local TTS speaks it
back.

Bound to 127.0.0.1, same as the other sidecars (see webserver.py) —
loopback-only since the listener runs on this same Pi.
"""

import asyncio
import hmac
import logging
import re

from aiohttp import web

from . import cards, config, metrics, notify, webserver
from .discord_client import client, display_name
from .llm import ask_llm
from .tools import ToolContext, dispatch_result

log = logging.getLogger("discord-llm-bot.voice_server")

# Negative, and therefore guaranteed never to collide with a real Discord
# snowflake (always a positive 64-bit int) — keeps voice-command history in
# llm.py's in-memory per-(channel, user) store separate from the owner's
# real DM history instead of interleaving the two.
_VOICE_CHANNEL_ID = -1

# Matches "turn on the desk lights", "turn desk lights off", "switch off
# Max's Desk Lights", etc. Anything that doesn't match falls back to the
# full LLM pipeline below, so this only needs to catch the common phrasings
# — it doesn't need to be exhaustive.
_PLUG_COMMAND = re.compile(
    r"""^\s*(?:please\s+)?(?:turn|switch)\s+
        (?:
            (?P<state1>on|off)\s+(?:the\s+)?(?P<device1>.+?)
          | (?:the\s+)?(?P<device2>.+?)\s+(?P<state2>on|off)
        )
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def match_plug_command(text: str) -> tuple[str, str] | None:
    """`(device, state)` if `text` is a simple "turn X on/off" command,
    else None — the signal to fall back to the full LLM pipeline."""
    m = _PLUG_COMMAND.match(text)
    if not m:
        return None
    device = m.group("device1") or m.group("device2")
    state = (m.group("state1") or m.group("state2")).lower()
    return device.strip(), state


class _FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id


class _FakeMessage:
    """Minimal duck-typed stand-in for discord.Message. ask_llm() and every
    currently-registered tool handler only ever touch .author, .author.id,
    .channel.id, and (optionally) .id — never message.guild or anything
    else — so this is sufficient without a real Discord message existing."""

    def __init__(self, author, channel_id: int):
        self.author = author
        self.channel = _FakeChannel(channel_id)
        self.id = None


def _strip_card_preamble(reply: str) -> str:
    return reply.replace(cards.RELAY_PREAMBLE, "").strip()


async def handle_command(request: web.Request) -> web.Response:
    if not config.VOICE_SERVER_SECRET or config.CLAUDE_CODE_OWNER_ID is None:
        return web.Response(status=503, text="Voice commands aren't configured on the server.")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad request: expected JSON.")

    secret = str(body.get("secret", ""))
    if not hmac.compare_digest(secret.encode(), config.VOICE_SERVER_SECRET.encode()):
        log.warning("Rejected voice command with a bad secret")
        return web.Response(status=403, text="Bad secret.")

    transcript = str(body.get("transcript", "")).strip()
    if not transcript:
        return web.Response(status=400, text="Bad request: 'transcript' is required.")

    owner_id = config.CLAUDE_CODE_OWNER_ID
    author = await client.fetch_user(owner_id)
    roles = frozenset({config.HOUSEHOLD_ROLE_NAME, config.ADMIN_ROLE_NAME})
    message = _FakeMessage(author, _VOICE_CHANNEL_ID)

    # The listener includes these whenever it successfully transcribed via
    # Groq (i.e. always, on this path) — recorded the same way llm.py logs
    # OpenRouter spend, so Groq shows up in the same `calls` table/dashboard
    # instead of being invisible cost. Missing/old listener build: skip
    # rather than record a bogus zero-cost row.
    stt_duration_s = body.get("stt_duration_s")
    if stt_duration_s is not None:
        await asyncio.to_thread(
            metrics.record,
            source="groq",
            model=str(body.get("stt_model", "")),
            input_tokens=0,
            output_tokens=0,
            duration_s=float(stt_duration_s),
            user_id=owner_id,
            user_name=display_name(author),
            cost_usd=float(body.get("stt_cost_usd", 0.0)),
        )

    plug_command = match_plug_command(transcript)
    if plug_command is not None:
        device, state = plug_command
        ctx = ToolContext(message=message, roles=roles)
        reply = dispatch_result("set_plug_power", {"device": device, "state": state}, ctx).content
    else:
        reply = await asyncio.to_thread(ask_llm, message, transcript, roles)

    reply = _strip_card_preamble(reply)
    await notify.send_dm(owner_id, f'\U0001f399️ "{transcript}"\n\n{reply}')
    return web.json_response({"reply": reply})


async def start() -> None:
    await webserver.serve("Voice command webhook", config.VOICE_SERVER_PORT, [web.post("/voice/command", handle_command)])
