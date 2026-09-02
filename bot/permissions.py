"""
Discord role-based permission checks, layered on top of the single-owner
model config.CLAUDE_CODE_OWNER_ID already used everywhere sensitive.

Two roles are recognized by name (config.ADMIN_ROLE_NAME /
config.HOUSEHOLD_ROLE_NAME), created and assigned directly in the Discord
server rather than hardcoded here, so who's in each group is managed
entirely through Discord's own UI:

  Administrator — everything CLAUDE_CODE_OWNER_ID could already do alone:
                  !code, !deploy, Kasa plugs when KASA_OWNER_ONLY=1. See
                  is_admin(), used by tools/__init__.py's owner_only gate
                  and claude_bridge.is_authorized().
  Home Resident — gates the household-facing tools (todos, groceries,
                  reminders, calendar) so randoms in the server/DMs can't
                  use them — see the `required_role` tool registry option
                  in tools/__init__.py.

Roles only ride along with a message's author when it comes from a guild
channel or thread — discord.Member.roles is populated straight off the
gateway event for those, no privileged Members intent needed. A DM's author
is a discord.User with no role information at all (DMs aren't attached to
any guild), so resolve_roles() falls back to a REST member lookup against
one specific guild for that case: config.DISCORD_GUILD_ID if set, or the
bot's one-and-only guild if it's only ever in one. With neither, DMs get no
roles at all — the CLAUDE_CODE_OWNER_ID bypass in is_admin() still works
there, same as always, but nothing gets Home Resident access over DM.
"""

import logging

import discord

from . import config
from .discord_client import client

log = logging.getLogger("discord-llm-bot.permissions")

_warned_no_guild = False


async def resolve_roles(message: discord.Message) -> frozenset[str]:
    """The set of role names the message's author holds, resolved however
    is possible for the channel this message came from."""
    if message.guild is not None and isinstance(message.author, discord.Member):
        return frozenset(role.name for role in message.author.roles)
    return await _resolve_dm_roles(message.author.id)


def _dm_guild() -> discord.Guild | None:
    global _warned_no_guild
    if config.DISCORD_GUILD_ID is not None:
        guild = client.get_guild(config.DISCORD_GUILD_ID)
        if guild is None:
            log.warning("DISCORD_GUILD_ID=%s but the bot isn't in that guild", config.DISCORD_GUILD_ID)
        return guild

    if len(client.guilds) == 1:
        return client.guilds[0]

    if not _warned_no_guild:
        log.warning(
            "Can't resolve roles for DMs: bot is in %d guild(s) and "
            "DISCORD_GUILD_ID isn't set to disambiguate. Role-gated tools "
            "will deny everyone over DM except CLAUDE_CODE_OWNER_ID.",
            len(client.guilds),
        )
        _warned_no_guild = True
    return None


async def _resolve_dm_roles(user_id: int) -> frozenset[str]:
    guild = _dm_guild()
    if guild is None:
        return frozenset()

    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        return frozenset()
    except discord.HTTPException:
        log.exception("Failed to fetch member %s in guild %s for DM role lookup", user_id, guild.id)
        return frozenset()

    return frozenset(role.name for role in member.roles)


def is_admin(user_id: int, roles: frozenset[str]) -> bool:
    """True if `user_id`/`roles` should get the same access
    CLAUDE_CODE_OWNER_ID has always had alone — either by being that legacy
    owner id, or by holding the Administrator role."""
    if config.CLAUDE_CODE_OWNER_ID is not None and user_id == config.CLAUDE_CODE_OWNER_ID:
        return True
    return config.ADMIN_ROLE_NAME in roles
