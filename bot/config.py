"""
Environment/config loading, shared across modules.
"""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()  # reads .env in this directory when run manually (not needed
               # under systemd if you use EnvironmentFile=, but harmless either way)

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Any model slug from https://openrouter.ai/models works here. Must support
# tool calling for the reminder/tool features to work.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku")

# How many past turns (user+assistant pairs) to keep per channel/DM, so the
# bot has some memory of the conversation without growing forever.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "6"))

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful assistant running on a home server. Keep replies "
    "concise and practical.",
)

DISCORD_MESSAGE_LIMIT = 2000  # Discord's hard cap per message

# Timezone used to interpret "tonight", "tomorrow at 3pm", etc. Must be an
# IANA name (see `timedatectl list-timezones`).
TIMEZONE = os.environ.get("TIMEZONE", "America/Indiana/Indianapolis")
LOCAL_TZ = ZoneInfo(TIMEZONE)

REMINDERS_FILE = os.environ.get("REMINDERS_FILE", "reminders.json")

# Google Calendar OAuth (per-Discord-user linking, see tools/calendar.py).
GOOGLE_CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
GOOGLE_CALENDAR_TOKENS_FILE = os.environ.get("GOOGLE_CALENDAR_TOKENS_FILE", "calendar_tokens.json")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "http://raspberrypi.tail4ce93a.ts.net/calendar/oauth/callback"
)
GOOGLE_OAUTH_SERVER_PORT = int(os.environ.get("GOOGLE_OAUTH_SERVER_PORT", "8788"))
