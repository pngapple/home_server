"""
The shared discord.Client instance.

Split out from app.py so tool modules (e.g. tools/reminders.py) can send
messages/DMs (fetch_user, .send(...)) without importing app.py and creating
a circular import.

The subclass exists only to hang shutdown on: the local HTTP sidecars and
the shared outbound session are started from on_ready, and close() is the
one hook discord.py gives us that runs on the way out.
"""

import logging

import discord

from . import httpclient, webserver

log = logging.getLogger("discord-llm-bot.discord_client")

intents = discord.Intents.default()
intents.message_content = True  # required to read message text; enable this
                                # "Privileged Gateway Intent" in the Discord
                                # Developer Portal too (see SETUP_GUIDE.md)


class Bot(discord.Client):
    async def close(self) -> None:
        await webserver.shutdown_all()
        await httpclient.close()
        await super().close()


client = Bot(intents=intents)


def display_name(user) -> str:
    """A human-readable label for a discord.Member/User, for anything
    user-facing (metrics attribution, grocery "added by" tags) that
    shouldn't just show a raw Discord id."""
    return getattr(user, "display_name", None) or str(user)
