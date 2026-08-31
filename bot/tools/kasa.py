"""
Kasa smart plug on/off control via python-kasa's local protocol. Devices are
looked up by alias (the name given in the Kasa app) via LAN discovery, with
just the host/alias/state (plain data) cached briefly so repeated commands
don't re-discover every time. Newer firmware needs the Kasa *account*
credentials for the local auth handshake (config.KASA_USERNAME/PASSWORD)
even though actual on/off traffic never leaves the LAN — one shared
credential for the household, held only here.

Live `Device` objects are never cached across calls: each one holds a
network connection tied to the asyncio event loop that created it, and each
call below gets its own fresh loop via asyncio.run(), so a cached device
from an earlier call breaks with "attached to a different loop" / "Event
loop is closed". Always connect fresh, act, and disconnect within a single
asyncio.run() coroutine.

asyncio.run() only works here because tool handlers run in a worker thread
with no loop of its own (app.py hands ask_llm to asyncio.to_thread); calling
one of these from the Discord event loop thread would raise.
"""

import asyncio
import logging
import threading
import time
from typing import NamedTuple

from kasa import Discover

from .. import config
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.kasa")

# NOTE: these tools perform physical actions (cutting power to whatever is
# plugged in). Who may actuate hardware is a policy call, so it's a config
# switch rather than a silent assumption: set KASA_OWNER_ONLY=1 to restrict
# them to CLAUDE_CODE_OWNER_ID. Left off by default, which is the behaviour
# this has always had — anyone who can DM the bot or @mention it in a shared
# server can drive these through the model.
_OWNER_ONLY = config.KASA_OWNER_ONLY

_DISCOVERY_TIMEOUT_S = 3
_CACHE_TTL_SECONDS = 120


class Plug(NamedTuple):
    host: str
    alias: str
    is_on: bool


# Rebound wholesale on refresh rather than mutated in place, so a lookup
# running on another tool worker thread never observes a half-filled cache.
_CACHE: dict[str, Plug] = {}
_CACHE_TS = 0.0
# Serializes discovery so two concurrent tool calls don't each pay for a
# full LAN scan.
_REFRESH_LOCK = threading.Lock()


def _normalize(s: str) -> str:
    # Discord messages use a plain ASCII apostrophe; Kasa app aliases (e.g.
    # "Max's Desk Lights") often use a curly one. Fold both to the same form
    # so name matching doesn't silently fail on that alone.
    return s.strip().casefold().replace("’", "'").replace("‘", "'")


async def _discover_all() -> list[Plug]:
    found = await Discover.discover(
        username=config.KASA_USERNAME,
        password=config.KASA_PASSWORD,
        discovery_timeout=_DISCOVERY_TIMEOUT_S,
    )
    # The discovery broadcast reply alone doesn't carry full sysinfo for
    # KLAP-authenticated devices — alias/state are only populated after an
    # authenticated update() call. That call is per-device and can fail on
    # its own (a device that rejects the shared credentials, one that's
    # dropped off wifi mid-scan), so keep the rest of the scan rather than
    # losing every plug to one bad one.
    plugs = []
    for host, dev in found.items():
        try:
            await dev.update()
        except Exception:
            log.warning("Skipping Kasa device at %s: update failed", host, exc_info=True)
        else:
            plugs.append(Plug(host, dev.alias, dev.is_on))
        finally:
            await dev.disconnect()
    return plugs


def _refresh_cache() -> None:
    global _CACHE, _CACHE_TS
    with _REFRESH_LOCK:
        plugs = asyncio.run(_discover_all())
        _CACHE = {_normalize(p.alias): p for p in plugs}
        _CACHE_TS = time.time()


def _lookup(name: str) -> Plug | None:
    cache = _CACHE  # one read: _refresh_cache may rebind it mid-lookup
    target = _normalize(name)
    hit = cache.get(target)
    if hit is not None:
        return hit
    # Fall back to a substring match (either direction) if it's unambiguous,
    # so close-enough names ("desk lights" for "Max's Desk Lights") work too.
    matches = [v for k, v in cache.items() if target in k or k in target]
    return matches[0] if len(matches) == 1 else None


def _find(name: str) -> Plug | None:
    fresh = time.time() - _CACHE_TS <= _CACHE_TTL_SECONDS
    if not fresh:
        _refresh_cache()
    hit = _lookup(name)
    if hit is None and fresh:
        # Might be new/renamed since the cache was built — one retry, but
        # only if we haven't just scanned (each scan costs a LAN broadcast
        # plus an authenticated update() per device).
        _refresh_cache()
        hit = _lookup(name)
    return hit


def _known_plugs_str() -> str:
    return ", ".join(p.alias for p in _CACHE.values()) or "(none found)"


def _credentials_missing() -> str | None:
    if not config.KASA_USERNAME or not config.KASA_PASSWORD:
        return "Error: Kasa credentials aren't configured on the server."
    return None


async def _set_power(host: str, turn_on: bool) -> None:
    dev = await Discover.discover_single(host, username=config.KASA_USERNAME, password=config.KASA_PASSWORD)
    try:
        await dev.update()
        await (dev.turn_on() if turn_on else dev.turn_off())
    finally:
        await dev.disconnect()


async def _get_status(host: str) -> bool:
    dev = await Discover.discover_single(host, username=config.KASA_USERNAME, password=config.KASA_PASSWORD)
    try:
        await dev.update()
        return dev.is_on
    finally:
        await dev.disconnect()


@tool(
    name="list_smart_plugs",
    description=(
        "List all Kasa smart plugs on the network by name and their current "
        "on/off state. Call this if you don't know the exact device name the "
        "user means."
    ),
    owner_only=_OWNER_ONLY,
)
def handle_list(arguments: dict, ctx: ToolContext) -> str:
    if error := _credentials_missing():
        return error
    try:
        _refresh_cache()
    except Exception:
        log.exception("Kasa discovery failed")
        return "Error: couldn't reach any Kasa devices on the network."
    if not _CACHE:
        return "No Kasa smart plugs found on the network."
    return "\n".join(f"- {p.alias}: {'on' if p.is_on else 'off'}" for p in _CACHE.values())


@tool(
    name="set_plug_power",
    description=(
        "Turn a Kasa smart plug on or off by name. Use list_smart_plugs "
        "first if you're not sure of the exact device name."
    ),
    properties={
        "device": {"type": "string", "description": "The plug's name/alias, e.g. 'Max's Desk Lights'."},
        "state": {"type": "string", "enum": ["on", "off"], "description": "Desired power state."},
    },
    required=["device", "state"],
    owner_only=_OWNER_ONLY,
)
def handle_set_power(arguments: dict, ctx: ToolContext) -> str:
    if error := _credentials_missing():
        return error
    name, state = arguments["device"], arguments["state"]
    if state not in ("on", "off"):
        return "Error: state must be 'on' or 'off'."

    plug = _find(name)
    if plug is None:
        return f"Error: no plug named '{name}' found. Known plugs: {_known_plugs_str()}"

    try:
        asyncio.run(_set_power(plug.host, state == "on"))
    except Exception:
        log.exception("Failed to set power for %s", name)
        return f"Error: couldn't reach '{plug.alias}' to change its power state."

    return f"'{plug.alias}' turned {state}."


@tool(
    name="get_plug_status",
    description="Check whether a specific Kasa smart plug is currently on or off.",
    properties={"device": {"type": "string", "description": "The plug's name/alias."}},
    required=["device"],
    owner_only=_OWNER_ONLY,
)
def handle_get_status(arguments: dict, ctx: ToolContext) -> str:
    if error := _credentials_missing():
        return error
    name = arguments["device"]

    plug = _find(name)
    if plug is None:
        return f"Error: no plug named '{name}' found. Known plugs: {_known_plugs_str()}"

    try:
        is_on = asyncio.run(_get_status(plug.host))
    except Exception:
        log.exception("Failed to refresh status for %s", name)
        return f"Error: couldn't reach '{plug.alias}' to check its status."

    return f"'{plug.alias}' is currently {'on' if is_on else 'off'}."
