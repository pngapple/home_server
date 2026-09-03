"""
Admin tool: onboards a new resident's phone for location-based reminders
(bot/geofence_server.py, tools/reminders.py) — generates that resident a
fresh webhook secret and DMs them the secret plus the Shortcuts setup steps.

The secret lives on the resident's profile (see ../users.py). It used to be
appended to GEOFENCE_USERS in .env by regex-rewriting that file in place,
which meant registration didn't actually take effect until someone ran
!deploy, because config.py only parses .env at import. Storing it with the
rest of that person's settings makes registration live immediately, and
takes about seventy lines of string surgery on a config file out of this
module.

Secrets already in .env from before that change are migrated into the
profile store at startup by users.seed_geofence_from_env(), so nobody has to
re-register a phone.
"""

import logging
import re

from .. import config, notify, users
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.geofence_admin")

_SETUP_INSTRUCTIONS = """\
You're set up for location-based reminders. In the Shortcuts app on your phone:

1. Automation tab -> + -> Create Personal Automation -> Arrive -> pick your \
home, tap Next.
2. Add action "Get Contents of URL":
   URL: {webhook_url}
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


def _webhook_url() -> str:
    """Derived from the OAuth redirect URI so the host only has to be
    configured once — both are served by nginx off the same Tailscale-only
    interface (see sites-available/status)."""
    base = config.GOOGLE_REDIRECT_URI.split("/calendar/")[0]
    return f"{base}/geofence/webhook"


@tool(
    name="register_location_user",
    description=(
        "Onboard a new resident's phone for location-based reminders: "
        "generates them a fresh geofence webhook secret, saves it to their "
        "profile, and DMs them their secret plus the Shortcuts setup steps. "
        "Takes effect immediately — no restart needed. Restricted to admins."
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

    secret, created = users.ensure_geofence_secret(user_id)
    notify.dm(user_id, _SETUP_INSTRUCTIONS.format(secret=secret, webhook_url=_webhook_url()))

    if created:
        log.info("Registered user %s for location reminders", user_id)
        return (
            f"Registered <@{user_id}> for location reminders and DMed them setup "
            "instructions. It's active right away — no restart needed."
        )
    return f"<@{user_id}> was already registered — resent their setup instructions."
