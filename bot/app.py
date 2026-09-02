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

on_message routing, in order (see _route): !deploy anywhere, then a
follow-up inside a live Claude Code thread, then an explicit !code prefix,
then ordinary chat.
"""

import asyncio
import functools
import logging
import re

import discord

from cigboard import server as cigboard_server

from . import claude_bridge, config, deploy, geofence_server, llm_status_server, moderation, oauth_server, permissions
from .discord_client import client, display_name
from .llm import ask_llm
from .tools.reminders import reschedule_pending

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord-llm-bot")

_THREAD_TITLE_PREFIX = "Claude Code: "
_DISCORD_THREAD_NAME_LIMIT = 100
_THREAD_ARCHIVE_MINUTES = 1440

_LLM_ERROR = (
    "Sorry, something went wrong talking to the model. Check the server "
    "logs (`journalctl -u discord-llm-bot -n 50`)."
)
_BRIDGE_ERROR = (
    "Sorry, something went wrong running Claude Code. Check the server "
    "logs (`journalctl -u discord-llm-bot -n 50`)."
)

# The local HTTP sidecars, started once the gateway is up. Each serve() call
# just binds a socket, so these are awaited rather than fired off as tasks —
# a failure to bind should be logged, not swallowed into an orphaned task.
_SIDECARS = (
    ("OAuth callback", oauth_server.start),
    ("LLM status", llm_status_server.start),
    ("Cigboard", cigboard_server.start),
    ("Geofence webhook", geofence_server.start),
)


def chunk(text: str, limit: int = config.DISCORD_MESSAGE_LIMIT):
    """Split `text` into Discord-sized pieces, preferring line boundaries so
    code blocks and lists don't get cut mid-line. Falls back to a hard cut
    for a single line longer than the limit."""
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            yield text[:limit]
            text = text[limit:]
        else:
            yield text[:cut]
            text = text[cut + 1 :]
    if text:
        yield text


async def send_text(channel, text: str) -> None:
    for part in chunk(text):
        if part.strip():
            await channel.send(part)


def _thread_title(prompt: str) -> str:
    summary = prompt.strip().splitlines()[0]
    budget = _DISCORD_THREAD_NAME_LIMIT - len(_THREAD_TITLE_PREFIX)
    if len(summary) > budget:
        summary = summary[: budget - 1].rstrip() + "…"
    return _THREAD_TITLE_PREFIX + summary


@functools.lru_cache(maxsize=1)
def _mention_pattern(user_id: int) -> re.Pattern:
    return re.compile(rf"<@!?{user_id}>")


# ---------------------------------------------------------------------------
# Discord event handlers
# ---------------------------------------------------------------------------

_started = False


@client.event
async def on_ready():
    global _started
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)
    # on_ready can fire again after a dropped/reconnected gateway session;
    # only start things up once per process to avoid double-firing timers
    # and double-binding ports.
    if _started:
        return
    _started = True

    reschedule_pending()
    for name, start in _SIDECARS:
        try:
            await start()
        except Exception:
            log.exception("Failed to start the %s server", name)

    await _announce_restart()


async def _announce_restart() -> None:
    """Say "back online" in whichever channel asked for the !deploy that
    (probably) caused this startup. See deploy.py."""
    channel_id = deploy.consume_pending_notify()
    if channel_id is None:
        return
    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        await channel.send("✅ Back online.")
    except Exception:
        log.exception("Failed to announce restart in channel %s", channel_id)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    try:
        await _route(message)
    except Exception:
        log.exception("Unhandled error while handling message %s", message.id)


async def _route(message: discord.Message) -> None:
    # Inside a thread that already has a Claude Code session running, every
    # message from the owner continues it directly — no @mention or !code
    # prefix needed, since the thread itself is the dedicated scope for it.
    in_code_thread = isinstance(message.channel, discord.Thread) and claude_bridge.has_session(message.channel.id)
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = client.user in message.mentions

    if not (in_code_thread or is_dm or is_mention):
        return

    text = message.content
    if is_mention:
        text = _mention_pattern(client.user.id).sub("", text)
    text = text.strip()
    if not text:
        return

    log.info("Message from %s (dm=%s, mention=%s): %r", message.author, is_dm, is_mention, text)

    # Resolved once per message and threaded through everything below —
    # both the admin gate (claude_bridge.is_authorized) and role-gated tools
    # (see ToolContext.roles) need it, and it can involve a Discord API call
    # for a DM (see permissions.resolve_roles), so it's not worth resolving
    # more than once per message.
    roles = await permissions.resolve_roles(message)

    # Checked before the thread branch so a restart can still be asked for
    # from inside a Claude Code thread.
    if deploy.is_trigger(text):
        await _handle_deploy(message, text, roles)
    elif in_code_thread:
        await _handle_thread_followup(message, text, roles)
    elif claude_bridge.is_trigger(text):
        await _handle_code(message, text, roles)
    else:
        await _handle_chat(message, text, roles)


async def _patch_notes_channel() -> discord.abc.Messageable | None:
    """config.PATCH_NOTES_CHANNEL_ID resolved to an actual channel, or None
    if it's unset or can't be fetched. Tries the client's cache first (cheap,
    populated for any channel the bot has already seen) before falling back
    to a REST call."""
    if config.PATCH_NOTES_CHANNEL_ID is None:
        return None
    channel = client.get_channel(config.PATCH_NOTES_CHANNEL_ID)
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(config.PATCH_NOTES_CHANNEL_ID)
    except discord.HTTPException:
        log.exception("Failed to fetch patch-notes channel %s", config.PATCH_NOTES_CHANNEL_ID)
        return None


async def _broadcast_deploy_update(origin_channel, text: str, header: str | None = None) -> None:
    """Sends `text` to `origin_channel` (wherever !deploy was actually run),
    and mirrors it into config.PATCH_NOTES_CHANNEL_ID too, if configured and
    different from `origin_channel` — so that channel accumulates a running
    log of every deploy regardless of where each one was triggered from.
    `header` (e.g. who ran it) is prefixed only on the patch-notes copy,
    since the origin channel already has that context from the Discord
    message itself."""
    await origin_channel.send(text)
    patch_notes = await _patch_notes_channel()
    if patch_notes is None or patch_notes.id == origin_channel.id:
        return
    await patch_notes.send(f"{header}\n{text}" if header else text)


async def _handle_deploy(message: discord.Message, text: str, roles: frozenset[str]) -> None:
    if not claude_bridge.is_authorized(message.author.id, roles):
        await message.channel.send("You're not authorized to deploy.")
        return
    if not deploy.is_force(text):
        in_flight = claude_bridge.in_flight_count()
        if in_flight:
            await message.channel.send(
                f"Refusing to restart: {in_flight} `{claude_bridge.TRIGGER_PREFIX}` "
                f"session(s) still running — a restart right now would kill "
                f"them mid-task. Wait for them to finish, or say "
                f"`{deploy.TRIGGER_PREFIX} {deploy.FORCE_ARG}` to restart anyway."
            )
            return

    header = f"**Deploy by {display_name(message.author)}**"

    commit_summary = await asyncio.to_thread(
        deploy.commit_and_push, message.author.id, display_name(message.author)
    )
    if commit_summary:
        await _broadcast_deploy_update(message.channel, commit_summary, header)

    await _broadcast_deploy_update(
        message.channel, "🔄 Restarting now — back in a few seconds.", header if not commit_summary else None
    )
    deploy.restart(message.channel.id)


async def _handle_thread_followup(message: discord.Message, text: str, roles: frozenset[str]) -> None:
    if not claude_bridge.is_authorized(message.author.id, roles):
        return
    # The prefix is optional in here, but harmless if repeated out of habit.
    prompt = claude_bridge.strip_trigger(text)
    prompt = text if prompt is None else prompt
    if prompt:
        await _run_claude_bridge(message.channel, prompt, message.author.id, display_name(message.author))


async def _handle_code(message: discord.Message, text: str, roles: frozenset[str]) -> None:
    if not claude_bridge.is_authorized(message.author.id, roles):
        await message.channel.send("You're not authorized to use Claude Code through this bot.")
        return
    prompt = claude_bridge.strip_trigger(text)
    if not prompt:
        await message.channel.send(f"Usage: `{claude_bridge.TRIGGER_PREFIX} <prompt>`")
        return

    # Guild text channels get a dedicated thread so follow-ups don't need
    # the prefix repeated; DMs and other channel types can't have threads,
    # so they just keep responding in place.
    target = message.channel
    if isinstance(message.channel, discord.TextChannel):
        try:
            target = await message.create_thread(
                name=_thread_title(prompt), auto_archive_duration=_THREAD_ARCHIVE_MINUTES
            )
        except discord.HTTPException:
            log.exception("Failed to create thread for Claude Code bridge, replying inline instead")
            target = message.channel

    await _run_claude_bridge(target, prompt, message.author.id, display_name(message.author))


async def _run_claude_bridge(channel, prompt: str, user_id: int, user_name: str) -> None:
    log.info("Claude Code bridge prompt in channel %s: %r", channel.id, prompt)
    async with channel.typing():
        try:
            reply = await claude_bridge.run(channel.id, prompt, user_id, user_name)
        except Exception:
            log.exception("Claude Code bridge failed")
            await channel.send(_BRIDGE_ERROR)
            return
    await send_text(channel, reply)


async def _handle_chat(message: discord.Message, text: str, roles: frozenset[str]) -> None:
    verdict = await moderation.enforce(message, roles, text)
    if verdict is not None:
        await message.channel.send(verdict)
        return

    async with message.channel.typing():
        try:
            # ask_llm is synchronous and makes several blocking HTTP calls
            # (plus tool work) that can run for a minute or more; running it
            # inline would stall the gateway heartbeat and every other
            # channel, reminder timer and local HTTP server in this process.
            reply = await asyncio.to_thread(ask_llm, message, text, roles)
        except Exception:
            log.exception("LLM call failed")
            await message.channel.send(_LLM_ERROR)
            return
    await send_text(message.channel, reply)


def main():
    client.run(config.DISCORD_BOT_TOKEN)
