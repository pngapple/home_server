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
# These tools switch real power. Off by default (anyone who can reach the
# bot can drive them, as has always been the case); set to 1 to restrict
# them to CLAUDE_CODE_OWNER_ID via the registry's owner_only gate.
KASA_OWNER_ONLY = _flag("KASA_OWNER_ONLY")

# Direct bridge to a real headless Claude Code session (bot/claude_bridge.py)
# — full shell/file access on this server, so restricted to a single owner
# Discord user id. Unset (None) means the bridge is disabled for everyone.
CLAUDE_CODE_OWNER_ID = _optional_int("CLAUDE_CODE_OWNER_ID")
CLAUDE_CODE_WORKDIR = os.environ.get("CLAUDE_CODE_WORKDIR", "/home/mjxu/home_server")
CLAUDE_CODE_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_CODE_TIMEOUT_SECONDS", "600"))
# Absolute path, not just "claude" — the systemd service's PATH doesn't
# include ~/.local/bin, where the CLI actually lives.
CLAUDE_CODE_BINARY = os.environ.get("CLAUDE_CODE_BINARY", "/home/mjxu/.local/bin/claude")

# Geofence webhook (bot/geofence_server.py) — iOS Shortcuts automations hit
# this on arrive/leave-home to deliver location-triggered reminders. Same
# local-only-server-behind-nginx pattern as the oauth/status/cigboard servers
# above. Secret is a shared token Shortcuts sends back, required to trigger.
GEOFENCE_SERVER_PORT = int(os.environ.get("GEOFENCE_SERVER_PORT", "8793"))
GEOFENCE_WEBHOOK_SECRET = os.environ.get("GEOFENCE_WEBHOOK_SECRET")
GEOFENCE_NOTIFY_USER_ID = _optional_int("GEOFENCE_NOTIFY_USER_ID") or CLAUDE_CODE_OWNER_ID
