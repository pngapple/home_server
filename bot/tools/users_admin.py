"""
Admin tools over the profile store (../users.py).

These exist because until profiles there was no answer to "who uses this
server?" — you'd have had to union the keys of seven JSON files, and
removing someone who'd moved out meant hand-editing every one of them plus
.env. Both are one call now, so they're worth exposing.

forget_resident is destructive and irreversible, hence owner_only and a
required confirm argument: the model has to have been told explicitly to do
it, rather than inferring it from someone grumbling about a housemate.
"""

import logging
import re
from datetime import UTC, datetime

from .. import users
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.users_admin")


def _parse_user_id(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw or "")
    return int(digits) if digits else None


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        seen = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "unknown"
    delta = datetime.now(UTC) - seen
    days = delta.days
    if days > 0:
        return f"{days}d ago"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours}h ago"
    return f"{max(1, delta.seconds // 60)}m ago"


@tool(
    name="list_residents",
    description=(
        "List everyone the server knows about — their display name, when "
        "they were last seen, and which features they've set up (location "
        "reminders, a custom timezone). Use this when asked who uses the "
        "bot, who's registered, or to look up someone's Discord id."
    ),
    owner_only=True,
)
def handle_list_residents(arguments: dict, ctx: ToolContext) -> str:
    profiles = users.all()
    if not profiles:
        return "No profiles recorded yet — nobody has messaged the bot since profiles were introduced."

    lines = []
    for p in profiles:
        features = []
        if p.geofence_secret:
            features.append("location reminders")
        if p.timezone:
            features.append(f"tz={p.timezone}")
        suffix = f" — {', '.join(features)}" if features else ""
        lines.append(f"{p.display_name} ({p.user_id}), last seen {_ago(p.last_seen)}{suffix}")

    # Ids present in some other store but with no profile: someone an admin
    # registered who has never actually messaged the bot, or leftover data.
    orphans = users.known_ids() - {p.user_id for p in profiles}
    if orphans:
        lines.append(
            "Also holding data for, but never seen: " + ", ".join(str(i) for i in sorted(orphans))
        )

    return "\n".join(lines)


@tool(
    name="forget_resident",
    description=(
        "Permanently erase everything the server stores about one person — "
        "their profile, todos, groceries, cigarette log, reminders, "
        "moderation strikes and geofence registration. Irreversible. Only "
        "call this when an admin has clearly asked to remove a specific "
        "person (e.g. a housemate who moved out), and pass confirm='yes' "
        "only if they've actually said so."
    ),
    properties={
        "discord_user_id": {
            "type": "string",
            "description": "The Discord user id to erase — pull the numeric id out of an @mention if given one.",
        },
        "confirm": {
            "type": "string",
            "description": "Must be exactly 'yes'. Only set it if the admin explicitly asked to delete this person's data.",
        },
    },
    required=["discord_user_id", "confirm"],
    owner_only=True,
)
def handle_forget_resident(arguments: dict, ctx: ToolContext) -> str:
    if arguments["confirm"].strip().lower() != "yes":
        return "Error: forget_resident needs confirm='yes'. Ask the admin to confirm they want this data deleted."

    user_id = _parse_user_id(arguments["discord_user_id"])
    if user_id is None:
        return "Error: discord_user_id must contain a numeric Discord user id."

    name = users.name(user_id)
    erased = users.forget(user_id)
    if not erased:
        return f"Nothing stored for {user_id} — nothing to erase."
    return f"Erased all data for {name} ({user_id}) from: {', '.join(erased)}."
