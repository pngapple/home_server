"""
Bare-bones Discord <-> LLM bridge.

Flow:
  You DM the bot, or @mention it in a server channel
    -> bot sends your message (plus a little recent history) to OpenRouter
    -> OpenRouter routes it to whatever model you picked
    -> bot posts the reply back in Discord

Deliberately simple: one file, in-memory history (lost on restart), no
database, no slash commands. Good starting point to build on.
"""

import os
import logging
from collections import defaultdict, deque

import discord
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()  # reads .env in this directory when run manually (not needed
               # under systemd if you use EnvironmentFile=, but harmless either way)

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Any model slug from https://openrouter.ai/models works here.
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord-llm-bot")

# channel_id -> deque of {"role": ..., "content": ...} dicts
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))

intents = discord.Intents.default()
intents.message_content = True  # required to read message text; enable this
                                  # "Privileged Gateway Intent" in the Discord
                                  # Developer Portal too (see SETUP_GUIDE.md)

client = discord.Client(intents=intents)


# ---------------------------------------------------------------------------
# OpenRouter call
# ---------------------------------------------------------------------------

def ask_llm(channel_id: int, user_text: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[channel_id])
    messages.append({"role": "user", "content": user_text})

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Optional but recommended by OpenRouter for attribution/rate-limit purposes:
            "X-Title": "home-server-discord-bot",
        },
        json={"model": OPENROUTER_MODEL, "messages": messages},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    reply = data["choices"][0]["message"]["content"]

    history[channel_id].append({"role": "user", "content": user_text})
    history[channel_id].append({"role": "assistant", "content": reply})
    return reply


def chunk(text: str, limit: int = DISCORD_MESSAGE_LIMIT):
    for i in range(0, len(text), limit):
        yield text[i : i + limit]


# ---------------------------------------------------------------------------
# Discord event handlers
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = client.user in message.mentions

    if not (is_dm or is_mention):
        return

    text = message.content
    if is_mention:
        text = text.replace(f"<@{client.user.id}>", "").strip()
    if not text:
        return

    async with message.channel.typing():
        try:
            reply = ask_llm(message.channel.id, text)
        except Exception:
            log.exception("LLM call failed")
            await message.channel.send(
                "Sorry, something went wrong talking to the model. Check the "
                "server logs (`journalctl -u discord-llm-bot -n 50`)."
            )
            return

    for part in chunk(reply):
        await message.channel.send(part)


if __name__ == "__main__":
    client.run(DISCORD_BOT_TOKEN)