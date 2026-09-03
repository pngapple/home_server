"""
Environment/config loading, shared across modules.
"""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()  # reads .env in this directory when run manually (not needed
               # under systemd if you use EnvironmentFile=, but harmless either way)

_TRUTHY = {"1", "true", "yes", "on"}


def _required(name: str) -> str:
    """A must-have setting. Fails at import with a message that says what to
    do, rather than a bare KeyError traceback out of systemd."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — add it to .env (see SETUP_GUIDE.md) and restart the service.")
    return value


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in _TRUTHY


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    return int(raw) if raw else None


DISCORD_BOT_TOKEN = _required("DISCORD_BOT_TOKEN")
OPENROUTER_API_KEY = _required("OPENROUTER_API_KEY")

# Any model slug from https://openrouter.ai/models works here. Must support
# tool calling for the reminder/tool features to work.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")

# When set, only routes requests to providers with a Zero Data Retention
# policy (https://openrouter.ai/docs/guides/features/zdr).
OPENROUTER_ZDR = os.environ.get("OPENROUTER_ZDR", "").lower() in ("1", "true", "yes")

# Cap on completion length per request. Required, not just a nicety: some
# providers (e.g. GMICloud, which serves qwen3-235b) default an unset
# max_tokens to the model's *entire* context window as the completion
# budget rather than clamping it to what's left after the input, so even a
# one-word prompt overflows the context length and every call 400s.
OPENROUTER_MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "2048"))

# How many past turns (user+assistant pairs) to keep per channel/DM, so the
# bot has some memory of the conversation without growing forever.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "6"))

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful assistant running on a home server. Keep replies "
    "concise and practical.",
)

DISCORD_MESSAGE_LIMIT = 2000  # Discord's hard cap per message

# The systemd unit this process runs as. Used by deploy.py to restart it and
# by claude_bridge.py to deny a bridge session from restarting it — one
# constant so those two can't drift apart (see discord-llm-bot.service).
SERVICE_NAME = os.environ.get("SERVICE_NAME", "discord-llm-bot")

# Timezone used to interpret "tonight", "tomorrow at 3pm", etc. Must be an
# IANA name (see `timedatectl list-timezones`).
TIMEZONE = os.environ.get("TIMEZONE", "America/Indiana/Indianapolis")
LOCAL_TZ = ZoneInfo(TIMEZONE)

REMINDERS_FILE = os.environ.get("REMINDERS_FILE", "reminders.json")

# Per-user cigarette counter (see tools/cigarettes.py).
CIGARETTES_FILE = os.environ.get("CIGARETTES_FILE", "cigarettes.json")

# Per-user todo list (see tools/todos.py).
TODOS_FILE = os.environ.get("TODOS_FILE", "todos.json")

# Personal + shared grocery lists (see tools/groceries.py).
GROCERIES_FILE = os.environ.get("GROCERIES_FILE", "groceries.json")

# Google Calendar OAuth (per-Discord-user linking, see tools/calendar.py).
GOOGLE_CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
GOOGLE_CALENDAR_TOKENS_FILE = os.environ.get("GOOGLE_CALENDAR_TOKENS_FILE", "calendar_tokens.json")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "http://raspberrypi.tail4ce93a.ts.net/calendar/oauth/callback"
)
GOOGLE_OAUTH_SERVER_PORT = int(os.environ.get("GOOGLE_OAUTH_SERVER_PORT", "8788"))

# LLM metrics dashboard (bot/llm_status_server.py), reverse-proxied at /llm/
# the same way the oauth callback and Netdata are — see sites-available/status.
LLM_STATUS_SERVER_PORT = int(os.environ.get("LLM_STATUS_SERVER_PORT", "8791"))
LLM_METRICS_FILE = os.environ.get("LLM_METRICS_FILE", "llm_metrics.json")

# Cigarette leaderboard (see cigboard/), same local-only-server-behind-nginx
# pattern as the LLM status dashboard above.
CIGBOARD_SERVER_PORT = int(os.environ.get("CIGBOARD_SERVER_PORT", "8792"))

# Kasa smart plugs (see tools/kasa.py). One shared account for the whole
# household — nobody but the server ever needs these credentials, since
# control happens through the bot, not the Kasa app.
KASA_USERNAME = os.environ.get("KASA_USERNAME")
KASA_PASSWORD = os.environ.get("KASA_PASSWORD")
# These tools switch real power, so they're always restricted to the
# HOUSEHOLD_ROLE_NAME role below (see tools/kasa.py). Set this to 1 to
# further restrict them to admins only (CLAUDE_CODE_OWNER_ID or the
# ADMIN_ROLE_NAME role below) via the registry's owner_only gate — e.g. if
# even some residents shouldn't cut power to shared equipment.
KASA_OWNER_ONLY = _flag("KASA_OWNER_ONLY")

# Direct bridge to a real headless Claude Code session (bot/claude_bridge.py)
# — full shell/file access on this server, so restricted to admins
# (CLAUDE_CODE_OWNER_ID or the ADMIN_ROLE_NAME role below). Unset (None)
# means CLAUDE_CODE_OWNER_ID itself grants no access — the role can still
# grant it, and needs at least one guild member to actually be usable.
CLAUDE_CODE_OWNER_ID = _optional_int("CLAUDE_CODE_OWNER_ID")
CLAUDE_CODE_WORKDIR = os.environ.get("CLAUDE_CODE_WORKDIR", "/home/mjxu/home_server")
CLAUDE_CODE_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_CODE_TIMEOUT_SECONDS", "600"))
# Absolute path, not just "claude" — the systemd service's PATH doesn't
# include ~/.local/bin, where the CLI actually lives.
CLAUDE_CODE_BINARY = os.environ.get("CLAUDE_CODE_BINARY", "/home/mjxu/.local/bin/claude")

# Every !deploy's commit summary and restart notice is mirrored into this
# channel on top of wherever !deploy was actually run, so there's one place
# with a running log of what's shipped regardless of which channel/DM
# triggered each deploy. Right-click the channel in Discord (Developer Mode
# on) > Copy Channel ID. Unset (None, the default) means deploy updates only
# go to the triggering channel, same as before this existed.
PATCH_NOTES_CHANNEL_ID = _optional_int("PATCH_NOTES_CHANNEL_ID")

# Discord role-based permissions (bot/permissions.py). Create these roles in
# Discord's Server Settings > Roles and assign them to whoever should have
# that tier of access — nothing else to configure unless you used different
# role names than these defaults.
#   Administrator — same access as CLAUDE_CODE_OWNER_ID (!code, !deploy,
#                   and Kasa plugs on top of Home Resident when
#                   KASA_OWNER_ONLY=1).
#   Home Resident — required for the household tools (groceries, reminders,
#                   calendar) and the Kasa smart plug tools.
ADMIN_ROLE_NAME = os.environ.get("ADMIN_ROLE_NAME", "Administrator")
HOUSEHOLD_ROLE_NAME = os.environ.get("HOUSEHOLD_ROLE_NAME", "Home Resident")
# Which guild's roles to check when a message comes from a DM, since a DM
# has no guild of its own to read roles from. Auto-detected and fine to
# leave unset if the bot is only ever in one server (the common case here).
DISCORD_GUILD_ID = _optional_int("DISCORD_GUILD_ID")

# Auto-moderation (bot/moderation.py). Every ordinary chat message (not
# !code/!deploy, and never from an admin — see permissions.is_admin) is run
# through a cheap classification call before reaching the main model; a
# flagged message is refused, and repeat offenses within
# MODERATION_STRIKE_WINDOW_DAYS escalate to a real Discord timeout (requires
# the bot to have the "Timeout Members" permission in the guild — DMs can't
# be timed out, so those just get the warning/refusal).
MODERATION_ENABLED = _flag("MODERATION_ENABLED", default=True)
# Reuses OPENROUTER_MODEL by default; override to route classification to a
# separate (e.g. cheaper/faster) model without affecting normal chat.
MODERATION_MODEL = os.environ.get("MODERATION_MODEL") or OPENROUTER_MODEL
MODERATION_STRIKES_FILE = os.environ.get("MODERATION_STRIKES_FILE", "moderation.json")
MODERATION_STRIKE_WINDOW_DAYS = int(os.environ.get("MODERATION_STRIKE_WINDOW_DAYS", "7"))
# 1st flagged message in the window: refused, no timeout. 2nd: timed out for
# TIER2 minutes. 3rd and beyond: TIER3 minutes.
MODERATION_TIMEOUT_MINUTES_TIER2 = int(os.environ.get("MODERATION_TIMEOUT_MINUTES_TIER2", "10"))
MODERATION_TIMEOUT_MINUTES_TIER3 = int(os.environ.get("MODERATION_TIMEOUT_MINUTES_TIER3", "60"))

# Geofence webhook (bot/geofence_server.py) — each resident's phone runs its
# own iOS Shortcuts automation that hits this on arrive/leave-home, to
# deliver location-triggered reminders. Same local-only-server-behind-nginx
# pattern as the oauth/status/cigboard servers above.
GEOFENCE_SERVER_PORT = int(os.environ.get("GEOFENCE_SERVER_PORT", "8793"))


def _parse_geofence_users(raw: str) -> dict[str, int]:
    """GEOFENCE_USERS maps each resident's own webhook secret to their
    Discord user id — format 'secret1:discord_id1,secret2:discord_id2,...',
    one entry per phone. Per-person secrets (rather than one shared secret)
    are what let one resident's phone arriving/leaving only ever affect
    their own reminders, never a housemate's — see geofence_server.py."""
    users: dict[str, int] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        secret, _, user_id = entry.partition(":")
        if not secret or not user_id:
            raise RuntimeError(f"Malformed GEOFENCE_USERS entry {entry!r} — expected 'secret:discord_id'.")
        users[secret] = int(user_id)
    return users


GEOFENCE_USERS = _parse_geofence_users(os.environ.get("GEOFENCE_USERS", ""))

# Persists the last-seen geofence event (arrive/leave) per resident — see
# tools/reminders.record_geofence_event — so a process restart knows
# whether a recurring location reminder should already be active, instead
# of waiting for the next webhook to find out.
GEOFENCE_STATE_FILE = os.environ.get("GEOFENCE_STATE_FILE", "geofence_state.json")
