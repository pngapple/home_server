"""
Kasa smart plug on/off control via python-kasa's local protocol. Devices are
looked up by alias (the name given in the Kasa app) via LAN discovery, with
just the host/alias (plain data) cached briefly so repeated commands don't
re-discover every time. Newer firmware needs the Kasa *account* credentials
for the local auth handshake (config.KASA_USERNAME/PASSWORD) even though
actual on/off traffic never leaves the LAN — one shared credential for the
household, held only here.

Live `Device` objects are never cached across calls: each one holds a
network connection tied to the asyncio event loop that created it, and
_run_async() gives every call its own fresh loop (see below), so a cached
device from an earlier call breaks with "attached to a different loop" /
"Event loop is closed". Always connect fresh, act, and disconnect within a
single _run_async() coroutine.
"""

import asyncio
import logging
import threading
import time

from kasa import Discover

from .. import config
from . import ToolContext, register

log = logging.getLogger("discord-llm-bot.tools.kasa")

# normalized alias -> (host, display alias, is_on as of last refresh)
_CACHE: dict[str, tuple[str, str, bool]] = {}
_CACHE_TS = 0.0
_CACHE_TTL_SECONDS = 120


def _run_async(coro):
    """ask_llm() runs synchronously on the Discord client's own event-loop
    thread, so we can't asyncio.run() directly here (a loop's already
    running in this thread) or await (tool handlers are plain sync
    functions). Run the coroutine on its own thread with its own fresh loop
    instead, and block until it's done."""
    result: dict = {}

    def runner():
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


def _normalize(s: str) -> str:
    # Discord messages use a plain ASCII apostrophe; Kasa app aliases (e.g.
    # "Max's Desk Lights") often use a curly one. Fold both to the same form
    # so name matching doesn't silently fail on that alone.
    return s.strip().casefold().replace("’", "'").replace("‘", "'")


def _refresh_cache() -> None:
    global _CACHE_TS

    async def _discover_all():
        found = await Discover.discover(
            username=config.KASA_USERNAME,
            password=config.KASA_PASSWORD,
            discovery_timeout=3,
        )
        # The discovery broadcast reply alone doesn't carry full sysinfo for
        # KLAP-authenticated devices — alias/state are only populated after
        # an authenticated update() call.
        info = []
        for host, dev in found.items():
            await dev.update()
            info.append((host, dev.alias, dev.is_on))
            await dev.disconnect()
        return info

    info = _run_async(_discover_all())
    _CACHE.clear()
    for host, alias, is_on in info:
        _CACHE[_normalize(alias)] = (host, alias, is_on)
    _CACHE_TS = time.time()


def _lookup(name: str) -> tuple[str, str, bool] | None:
    target = _normalize(name)
    hit = _CACHE.get(target)
    if hit is not None:
        return hit
    # Fall back to a substring match (either direction) if it's unambiguous,
    # so close-enough names ("desk lights" for "Max's Desk Lights") work too.
    matches = [v for k, v in _CACHE.items() if target in k or k in target]
    return matches[0] if len(matches) == 1 else None


def _find(name: str) -> tuple[str, str, bool] | None:
    if time.time() - _CACHE_TS > _CACHE_TTL_SECONDS:
        _refresh_cache()
    hit = _lookup(name)
    if hit is None:
        # Might be new/renamed since the cache was built — one retry.
        _refresh_cache()
        hit = _lookup(name)
    return hit


def _known_plugs_str() -> str:
    return ", ".join(alias for _, alias, _ in _CACHE.values()) or "(none found)"


LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_smart_plugs",
        "description": (
            "List all Kasa smart plugs on the network by name and their "
            "current on/off state. Call this if you don't know the exact "
            "device name the user means."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

SET_POWER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_plug_power",
        "description": (
            "Turn a Kasa smart plug on or off by name. Use "
            "list_smart_plugs first if you're not sure of the exact device "
            "name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "The plug's name/alias, e.g. 'Max's Desk Lights'.",
                },
                "state": {
                    "type": "string",
                    "enum": ["on", "off"],
                    "description": "Desired power state.",
                },
            },
            "required": ["device", "state"],
        },
    },
}

GET_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_plug_status",
        "description": "Check whether a specific Kasa smart plug is currently on or off.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "The plug's name/alias."},
            },
            "required": ["device"],
        },
    },
}


def handle_list(arguments: dict, ctx: ToolContext) -> str:
    if not config.KASA_USERNAME or not config.KASA_PASSWORD:
        return "Error: Kasa credentials aren't configured on the server."
    try:
        _refresh_cache()
    except Exception:
        log.exception("Kasa discovery failed")
        return "Error: couldn't reach any Kasa devices on the network."
    if not _CACHE:
        return "No Kasa smart plugs found on the network."
    lines = [f"- {alias}: {'on' if is_on else 'off'}" for _, alias, is_on in _CACHE.values()]
    return "\n".join(lines)


async def _set_power(host: str, turn_on: bool) -> None:
    dev = await Discover.discover_single(
        host, username=config.KASA_USERNAME, password=config.KASA_PASSWORD
    )
    try:
        await dev.update()
        await (dev.turn_on() if turn_on else dev.turn_off())
    finally:
        await dev.disconnect()


async def _get_status(host: str) -> bool:
    dev = await Discover.discover_single(
        host, username=config.KASA_USERNAME, password=config.KASA_PASSWORD
    )
    try:
        await dev.update()
        return dev.is_on
    finally:
        await dev.disconnect()


def handle_set_power(arguments: dict, ctx: ToolContext) -> str:
    if not config.KASA_USERNAME or not config.KASA_PASSWORD:
        return "Error: Kasa credentials aren't configured on the server."
    name = arguments.get("device")
    state = arguments.get("state")
    if not name or state not in ("on", "off"):
        return "Error: both device and state ('on'/'off') are required."

    hit = _find(name)
    if hit is None:
        return f"Error: no plug named '{name}' found. Known plugs: {_known_plugs_str()}"
    host, alias, _ = hit

    try:
        _run_async(_set_power(host, state == "on"))
    except Exception:
        log.exception("Failed to set power for %s", name)
        return f"Error: couldn't reach '{alias}' to change its power state."

    return f"'{alias}' turned {state}."


def handle_get_status(arguments: dict, ctx: ToolContext) -> str:
    if not config.KASA_USERNAME or not config.KASA_PASSWORD:
        return "Error: Kasa credentials aren't configured on the server."
    name = arguments.get("device")
    if not name:
        return "Error: device is required."

    hit = _find(name)
    if hit is None:
        return f"Error: no plug named '{name}' found. Known plugs: {_known_plugs_str()}"
    host, alias, _ = hit

    try:
        is_on = _run_async(_get_status(host))
    except Exception:
        log.exception("Failed to refresh status for %s", name)
        return f"Error: couldn't reach '{alias}' to check its status."

    return f"'{alias}' is currently {'on' if is_on else 'off'}."


register("list_smart_plugs", LIST_SCHEMA, handle_list)
register("set_plug_power", SET_POWER_SCHEMA, handle_set_power)
register("get_plug_status", GET_STATUS_SCHEMA, handle_get_status)
