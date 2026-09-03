"""
Admin tool: onboards a new resident's phone for location-based reminders
(bot/geofence_server.py, tools/reminders.py) without an admin editing .env
by hand — generates that resident a fresh webhook secret, appends it to
GEOFENCE_USERS in .env, and DMs them the secret plus the Shortcuts setup
steps.

Deliberately does NOT restart the service itself: config.GEOFENCE_USERS is
only read at process startup (see config.py's load_dotenv()), same as every
other .env value, so the new secret only takes effect once someone runs
!deploy — restarting from inside a tool call would kill this very
tool-calling loop before it could reply (see deploy.py's docstring for why
that restart is handled as its own explicit, human-triggered step instead).
"""

import asyncio
import logging
import os
import re
import secrets
import threading

from .. import config
from ..discord_client import client
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.geofence_admin")

_env_lock = threading.Lock()

_GEOFENCE_USERS_RE = re.compile(r"^GEOFENCE_USERS=(.*)$", re.MULTILINE)

_SETUP_INSTRUCTIONS = """\
You're set up for location-based reminders. In the Shortcuts app on your phone:

1. Automation tab -> + -> Create Personal Automation -> Arrive -> pick your \
home, tap Next.
2. Add action "Get Contents of URL":
   URL: http://raspberrypi.tail4ce93a.ts.net/geofence/webhook
   Method: POST (tap "Show More" to change it)
   Request Body: Form, with two fields:
     event = arrive
     secret = {secret}
3. Turn OFF "Ask Before Running" (and "Notify When Run", if you don't want \
Shortcuts' own popup — the bot DMs you instead).
4. Repeat steps 1-3 for a second automation using "Leave" instead of \
"Arrive", with event = leave instead of arrive.

Once both are set up, just ask me things like "remind me when I get home to \
take out the trash" or "add dishes to my todo list and remind me every 30 \
minutes while I'm home until it's done"."""


def _env_path() -> str:
    return os.path.join(config.CLAUDE_CODE_WORKDIR, ".env")


def _read_env() -> str:
    with open(_env_path()) as f:
        return f.read()


def _write_env(text: str) -> None:
    path = _env_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _existing_secret(env_text: str, user_id: int) -> str | None:
    match = _GEOFENCE_USERS_RE.search(env_text)
    if not match:
        return None
    for entry in match.group(1).split(","):
        entry = entry.strip()
        if not entry:
            continue
        entry_secret, _, entry_user_id = entry.partition(":")
        if entry_user_id == str(user_id):
            return entry_secret
    return None


def _add_geofence_user(user_id: int, secret: str) -> None:
    """Appends `secret:user_id` to GEOFENCE_USERS in .env (creating the key
    if it's missing entirely). Locked so two concurrent registrations can't
    clobber each other's read-modify-write of the same line."""
    new_entry = f"{secret}:{user_id}"
    with _env_lock:
        text = _read_env()
        match = _GEOFENCE_USERS_RE.search(text)
        if match:
            current = match.group(1).strip()
            updated_value = f"{current},{new_entry}" if current else new_entry
            text = text[: match.start(1)] + updated_value + text[match.end(1) :]
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"GEOFENCE_USERS={new_entry}\n"
        _write_env(text)


async def _dm_setup(user_id: int, secret: str) -> None:
    try:
        user = await client.fetch_user(user_id)
        await user.send(_SETUP_INSTRUCTIONS.format(secret=secret))
    except Exception:
        log.exception("Failed to DM geofence setup instructions to user %s", user_id)


@tool(
    name="register_location_user",
    description=(
        "Onboard a new resident's phone for location-based reminders: "
        "generates them a fresh geofence webhook secret, adds it to the "
        "server's config, and DMs them their secret plus the Shortcuts "
        "setup steps. Restricted to admins. A !deploy restart is still "
        "needed afterward before the new secret actually takes effect — "
        "say so in your reply."
    ),
    properties={
        "discord_user_id": {
            "type": "string",
            "description": (
                "The Discord user id of the resident to register — pull the "
                "numeric id out of an @mention if that's what was given."
            ),
        },
    },
    required=["discord_user_id"],
    owner_only=True,
)
def handle_register_location_user(arguments: dict, ctx: ToolContext) -> str:
    digits = re.sub(r"\D", "", arguments["discord_user_id"])
    if not digits:
        return "Error: discord_user_id must contain a numeric Discord user id."
    user_id = int(digits)

    existing = _existing_secret(_read_env(), user_id)
    if existing is not None:
        secret = existing
        status = f"<@{user_id}> was already registered — resent their setup instructions."
    else:
        secret = secrets.token_urlsafe(24)
        _add_geofence_user(user_id, secret)
        status = (
            f"Registered <@{user_id}> for location reminders and DMed them setup "
            "instructions. Run `!deploy` to activate it — the new secret won't "
            "work until the service restarts."
        )

    asyncio.run_coroutine_threadsafe(_dm_setup(user_id, secret), client.loop)
    return status
