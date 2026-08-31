"""
One shared aiohttp.ClientSession for every outbound HTTP call made from the
Discord client's event loop (OpenRouter credit lookups, Discord REST user
lookups).

Both of those callers used to open a fresh ClientSession per request, which
means a new TCP connection and TLS handshake every few seconds on a poll
loop. A single session keeps the connection pool warm across calls.

Only for loop-side code: the OpenRouter chat path (llm.py) runs in a worker
thread and keeps using `requests`, since a session is bound to the loop that
created it.
"""

import asyncio
import logging

import aiohttp

log = logging.getLogger("discord-llm-bot.httpclient")

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Keyed by loop so a session is never handed to a different event loop than
# the one it was created on (which aiohttp rejects at request time).
_sessions: dict[asyncio.AbstractEventLoop, aiohttp.ClientSession] = {}


def session() -> aiohttp.ClientSession:
    """The shared session for the running loop, created on first use."""
    loop = asyncio.get_running_loop()
    existing = _sessions.get(loop)
    if existing is not None and not existing.closed:
        return existing
    created = aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT)
    _sessions[loop] = created
    return created


async def close() -> None:
    for existing in _sessions.values():
        if not existing.closed:
            try:
                await existing.close()
            except Exception:
                log.exception("Failed to close shared HTTP session")
    _sessions.clear()
