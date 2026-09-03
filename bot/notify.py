"""
Sending a Discord DM from wherever you happen to be running.

Two things used to be copy-pasted at every site that wanted to DM someone:
the fetch_user/send/log-on-failure coroutine (reminders, groceries and
geofence_server each had their own), and the hop onto the Discord client's
event loop, since tool handlers run in asyncio.to_thread workers (see
app.py) and have no running loop of their own to create_task on.

run_coroutine_threadsafe is used unconditionally rather than branching on
which context we're in — it's correct from a worker thread and from the loop
thread itself, so there's nothing to detect.
"""

import asyncio
import logging

from .discord_client import client

log = logging.getLogger("discord-llm-bot.notify")


async def send_dm(user_id: int, text: str) -> bool:
    """DM `user_id`, swallowing (but logging) any failure. Awaitable form,
    for callers already on the client's loop that want the result."""
    try:
        user = await client.fetch_user(user_id)
        await user.send(text)
        return True
    except Exception:
        log.exception("Failed to DM user %s", user_id)
        return False


def dm(user_id: int, text: str) -> None:
    """Fire-and-forget a DM from any thread. Returns as soon as the
    coroutine is handed to the client's loop, without waiting for delivery.

    Never raises. Callers reach here *after* committing whatever the DM is
    announcing — checking off a housemate's grocery item, say — so letting a
    delivery problem escape would turn a completed action into a failed tool
    call and tell the user nothing happened when it did."""
    try:
        from_loop(send_dm(user_id, text))
    except Exception:
        log.exception("Could not hand a DM for user %s to the client loop", user_id)


def from_loop(coro) -> object:
    """Run `coro` on the Discord client's event loop from any thread, and
    hand back the concurrent.futures.Future for it (which callers who need
    to cancel later — see tools/reminders.py's recurring loops — keep)."""
    return asyncio.run_coroutine_threadsafe(coro, client.loop)
