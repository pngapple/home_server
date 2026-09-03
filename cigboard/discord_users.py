"""
Display names and avatars for the leaderboard, read from the shared profile
store (bot/users.py).

This module used to fetch each id from Discord's REST API and cache the
result, because the leaderboard runs as a plain aiohttp server with no
gateway cache of its own to read from. It was making authenticated HTTP
requests to Discord from inside the very process that already handles those
users on every message — cigboard/server.py is started from bot/app.py's
_SIDECARS list, on the same event loop as the Discord client.

Now the bot records each person's name and avatar on their profile whenever
they speak (bot/users.touch), so this is a dict lookup: no HTTP, no cache to
expire, no rate limit to respect, and correct across restarts. What stays
here is the default-avatar arithmetic, which is genuinely presentation:
somebody who has never set an avatar still needs a URL to render.
"""

import logging

from bot import users

log = logging.getLogger("discord-llm-bot.cigboard.discord_users")


def _default_avatar_url(user_id: str) -> str:
    """Discord's own fallback avatars. For post-discriminator accounts the
    index is derived from the id itself (six variants); the old
    discriminator-based scheme had five."""
    try:
        index = (int(user_id) >> 22) % 6
    except (TypeError, ValueError):
        index = 0
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def profile_for(user_id: str) -> dict:
    """The leaderboard-facing shape for one id: {id, display_name, avatar_url}."""
    profile = users.get(int(user_id))
    return {
        "id": user_id,
        "display_name": profile.display_name,
        "avatar_url": profile.avatar_url or _default_avatar_url(user_id),
    }


async def resolve_many(user_ids: list[str]) -> dict[str, dict]:
    """Returns {user_id: {id, display_name, avatar_url}}.

    Still a coroutine so cigboard/server.py's handler is unchanged; there's
    simply nothing to await any more.
    """
    return {uid: profile_for(uid) for uid in user_ids}
