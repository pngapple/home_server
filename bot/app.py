"""
Bare-bones Discord <-> LLM bridge.

Flow:
  You DM the bot, or @mention it in a server channel
    -> bot sends your message (plus a little recent history) to OpenRouter
    -> OpenRouter routes it to whatever model you picked
    -> the model replies, optionally calling a tool (see tools/) along the way
    -> bot posts the final reply back in Discord

Deliberately simple: in-memory history (lost on restart), no database, no
slash commands. See llm.py for the chat/tool-calling loop, tools/ for the
individual tools, config.py for settings. Run via `python -m bot` (see
__main__.py).
"""

import logging
import re

import discord

from . import config
from .discord_client import client
from .llm import ask_llm
from .tools.reminders import reschedule_pending

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord-llm-bot")


def chunk(text: str, limit: int = config.DISCORD_MESSAGE_LIMIT):
    for i in range(0, len(text), limit):
        yield text[i : i + limit]


# ---------------------------------------------------------------------------
# Discord event handlers
# ---------------------------------------------------------------------------

_pending_rescheduled = False


@client.event
async def on_ready():
    global _pending_rescheduled
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)
    # on_ready can fire again after a dropped/reconnected gateway session;
    # only reschedule from disk once per process to avoid double-firing.
    if not _pending_rescheduled:
        _pending_rescheduled = True
        reschedule_pending()


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
        text = re.sub(rf"<@!?{client.user.id}>", "", text).strip()
    if not text:
        return

    log.info("Message from %s (dm=%s, mention=%s): %r", message.author, is_dm, is_mention, text)

    async with message.channel.typing():
        try:
            reply = ask_llm(message, text)
        except Exception:
            log.exception("LLM call failed")
            await message.channel.send(
                "Sorry, something went wrong talking to the model. Check the "
                "server logs (`journalctl -u discord-llm-bot -n 50`)."
            )
            return

    for part in chunk(reply):
        await message.channel.send(part)


def main():
    client.run(config.DISCORD_BOT_TOKEN)
