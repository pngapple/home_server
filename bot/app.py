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

import asyncio
import logging
import re

import discord

from . import claude_bridge, config, deploy, geofence_server, llm_status_server, oauth_server
from cigboard import server as cigboard_server
from .discord_client import client
from .llm import ask_llm
from .tools.reminders import reschedule_pending

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord-llm-bot")


def chunk(text: str, limit: int = config.DISCORD_MESSAGE_LIMIT):
    for i in range(0, len(text), limit):
        yield text[i : i + limit]


_THREAD_TITLE_PREFIX = "Claude Code: "
_DISCORD_THREAD_NAME_LIMIT = 100


def _thread_title(prompt: str) -> str:
    summary = prompt.strip().splitlines()[0]
    budget = _DISCORD_THREAD_NAME_LIMIT - len(_THREAD_TITLE_PREFIX)
    if len(summary) > budget:
        summary = summary[: budget - 1].rstrip() + "…"
    return _THREAD_TITLE_PREFIX + summary


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
        client.loop.create_task(oauth_server.start())
        client.loop.create_task(llm_status_server.start())
        client.loop.create_task(cigboard_server.start())
        client.loop.create_task(geofence_server.start())

        notify_channel_id = deploy.consume_pending_notify()
        if notify_channel_id is not None:
            channel = client.get_channel(notify_channel_id)
            if channel is None:
                channel = await client.fetch_channel(notify_channel_id)
            await channel.send("✅ Back online.")


async def _run_claude_bridge(channel, prompt: str) -> None:
    log.info("Claude Code bridge prompt in channel %s: %r", channel.id, prompt)
    async with channel.typing():
        try:
            reply = await claude_bridge.run(channel.id, prompt)
        except Exception:
            log.exception("Claude Code bridge failed")
            await channel.send(
                "Sorry, something went wrong running Claude Code. Check the "
                "server logs (`journalctl -u discord-llm-bot -n 50`)."
            )
            return
    for part in chunk(reply):
        await channel.send(part)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Inside a thread that already has a Claude Code session running, every
    # message from the owner continues it directly — no @mention or !code
    # prefix needed, since the thread itself is the dedicated scope for it.
    if isinstance(message.channel, discord.Thread) and claude_bridge.has_session(message.channel.id):
        if not claude_bridge.is_authorized(message.author.id):
            return
        if message.content.strip():
            await _run_claude_bridge(message.channel, message.content)
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

    if deploy.is_trigger(text):
        if not claude_bridge.is_authorized(message.author.id):
            await message.channel.send("You're not authorized to deploy.")
            return
        force = deploy.is_force(text)
        if not force:
            in_flight = claude_bridge.in_flight_count()
            if in_flight:
                await message.channel.send(
                    f"Refusing to restart: {in_flight} `{claude_bridge.TRIGGER_PREFIX}` "
                    f"session(s) still running — a restart right now would kill "
                    f"them mid-task. Wait for them to finish, or say "
                    f"`{deploy.TRIGGER_PREFIX} {deploy.FORCE_ARG}` to restart anyway."
                )
                return
        await message.channel.send("🔄 Restarting now — back in a few seconds.")
        deploy.restart(message.channel.id)
        return

    if claude_bridge.is_trigger(text):
        if not claude_bridge.is_authorized(message.author.id):
            await message.channel.send("You're not authorized to use Claude Code through this bot.")
            return
        prompt = claude_bridge.strip_trigger(text)
        if not prompt:
            await message.channel.send(f"Usage: `{claude_bridge.TRIGGER_PREFIX} <prompt>`")
            return

        # Guild text channels get a dedicated thread so follow-ups don't need
        # the prefix repeated; DMs and other channel types can't have
        # threads, so they just keep responding in place.
        target = message.channel
        if isinstance(message.channel, discord.TextChannel):
            try:
                target = await message.create_thread(
                    name=_thread_title(prompt), auto_archive_duration=1440
                )
            except discord.HTTPException:
                log.exception("Failed to create thread for Claude Code bridge, replying inline instead")
                target = message.channel

        await _run_claude_bridge(target, prompt)
        return

    async with message.channel.typing():
        try:
            # ask_llm is synchronous and makes several blocking HTTP calls
            # (plus tool work) that can run for a minute or more; running it
            # inline would stall the gateway heartbeat and every other
            # channel, reminder timer and local HTTP server in this process.
            reply = await asyncio.to_thread(ask_llm, message, text)
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
