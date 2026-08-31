"""
Resolves Discord user ids (the only thing cigarettes.json stores) to a
display name + avatar URL for the leaderboard, via Discord's REST API
directly — this runs as a plain aiohttp server, not a discord.py Client, so
it has no gateway cache to read from.

Results are cached in-memory since names/avatars rarely change and the
leaderboard polls frequently; a 404/failed lookup is cached too (briefly)
so one bad id doesn't get hit on every poll.
"""

import asyncio
import logging
import time

import aiohttp

from bot import config, httpclient

log = logging.getLogger("discord-llm-bot.cigboard.discord_users")

_API_BASE = "https://discord.com/api/v10"
_OK_TTL_S = 3600.0  # names/avatars change rarely
_FAIL_TTL_S = 60.0  # but retry failures reasonably soon

# user_id -> (expires_at monotonic, profile)
_cache: dict[str, tuple[float, dict]] = {}


def _default_avatar_url(user_id: str, discriminator: str) -> str:
    if discriminator and discriminator != "0":
        index = int(discriminator) % 5
    else:
        index = (int(user_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def _fallback(user_id: str) -> dict:
    return {
        "id": user_id,
        "display_name": f"User {user_id[-4:]}",
        "avatar_url": _default_avatar_url(user_id, "0"),
    }


async def _fetch_one(session: aiohttp.ClientSession, user_id: str) -> tuple[dict, bool]:
    try:
        async with session.get(
            f"{_API_BASE}/users/{user_id}",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                log.warning("Discord user lookup for %s failed: HTTP %s", user_id, resp.status)
                return _fallback(user_id), False
            payload = await resp.json()
    except Exception:
        log.exception("Discord user lookup for %s failed", user_id)
        return _fallback(user_id), False

    avatar = payload.get("avatar")
    if avatar:
        ext = "gif" if avatar.startswith("a_") else "png"
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{ext}?size=128"
    else:
        avatar_url = _default_avatar_url(user_id, payload.get("discriminator", "0"))

    profile = {
        "id": user_id,
        "display_name": payload.get("global_name") or payload.get("username") or f"User {user_id[-4:]}",
        "avatar_url": avatar_url,
    }
    return profile, True


async def resolve_many(user_ids: list[str]) -> dict[str, dict]:
    """Returns {user_id: {id, display_name, avatar_url}}, using and refreshing the cache."""
    now = time.monotonic()
    # dict.fromkeys, not a set: dedupes while keeping a stable order, so a
    # repeated id in `user_ids` is only fetched once.
    stale = list(dict.fromkeys(uid for uid in user_ids if now >= _cache.get(uid, (0.0, None))[0]))
    if stale:
        session = httpclient.session()
        results = await asyncio.gather(*(_fetch_one(session, uid) for uid in stale))
        for uid, (profile, ok) in zip(stale, results):
            _cache[uid] = (now + (_OK_TTL_S if ok else _FAIL_TTL_S), profile)
    return {uid: _cache[uid][1] for uid in user_ids}
