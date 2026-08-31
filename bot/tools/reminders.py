"""
Reminder tool: lets the LLM schedule a one-off DM to fire either at a future
local date/time, or the next time a geofence event (arriving/leaving home)
comes in from bot/geofence_server.py.

Stored as a flat JSON list on disk so they survive a restart. Deliberately
not a database: this is a handful of personal reminders, not a workload.

  [{"id": "...", "author_id": 123, "text": "Admin meeting",
    "due_at": "2026-08-27T22:00:00+00:00"},
   {"id": "...", "author_id": 123, "text": "Take out the trash",
    "trigger": "arrive"}, ...]

Time-based reminders have a "due_at"; location-based ones have a "trigger"
("arrive" or "leave") instead and sit untouched until geofence_server.py
calls pop_location_reminders() for a matching event.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime

from .. import config, jsonstore
from ..discord_client import client
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.reminders")

TRIGGERS = ("arrive", "leave")


def load_reminders() -> list[dict]:
    return jsonstore.read(config.REMINDERS_FILE, [])


def _add(reminder: dict) -> None:
    with jsonstore.update(config.REMINDERS_FILE, []) as reminders:
        reminders.append(reminder)


def _remove(reminder_id: str) -> None:
    with jsonstore.update(config.REMINDERS_FILE, []) as reminders:
        reminders[:] = [r for r in reminders if r.get("id") != reminder_id]


def parse_due_at(when_iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(when_iso)
    except ValueError:
        return None
    # Ignore any offset the model included (it can't reliably know DST
    # rules) — always trust our own tz database for the real offset.
    return dt.replace(tzinfo=config.LOCAL_TZ).astimezone(UTC)


def format_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(config.LOCAL_TZ).strftime("%a %b %d, %I:%M %p %Z")


def schedule_reminder(reminder: dict) -> None:
    """Set a one-shot timer that fires exactly at due_at, instead of
    polling. The JSON file is only there so reminders survive a restart
    (see reschedule_pending), not to be scanned periodically."""
    due_at = datetime.fromisoformat(reminder["due_at"])
    delay = max(0.0, (due_at - datetime.now(UTC)).total_seconds())
    # Tool handlers run in a worker thread (app.py hands ask_llm to
    # asyncio.to_thread), so there's no running loop here to create_task on —
    # hand the coroutine to the Discord client's loop explicitly. This is
    # also safe when called from the loop thread itself, as on_ready does.
    asyncio.run_coroutine_threadsafe(fire_after(reminder, delay), client.loop)


async def fire_after(reminder: dict, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        user = await client.fetch_user(reminder["author_id"])
        await user.send(f"⏰ Reminder: {reminder['text']}")
    except Exception:
        log.exception("Failed to deliver reminder %s", reminder.get("id"))
    finally:
        _remove(reminder.get("id"))


def reschedule_pending() -> None:
    """Call once on startup to re-arm timers for reminders that were saved
    before a restart."""
    timed = [r for r in load_reminders() if "due_at" in r]
    for reminder in timed:
        schedule_reminder(reminder)
    if timed:
        log.info("Rescheduled %d pending reminder(s)", len(timed))


def pop_location_reminders(trigger: str) -> list[dict]:
    """Remove and return all pending reminders waiting on this geofence
    trigger ('arrive' or 'leave'), for delivery by geofence_server.py."""
    with jsonstore.update(config.REMINDERS_FILE, []) as reminders:
        matched = [r for r in reminders if r.get("trigger") == trigger]
        reminders[:] = [r for r in reminders if r.get("trigger") != trigger]
    return matched


@tool(
    name="set_reminder",
    description=(
        "Schedule a one-off reminder that DMs the user at a specific future "
        "local date/time. Only call this once you know both a specific time "
        "and what to remind them about — ask the user first if either is "
        "missing or too vague (e.g. 'later')."
    ),
    properties={
        "when_iso": {
            "type": "string",
            "description": (
                "The target local date/time, ISO 8601 format WITHOUT a "
                "timezone offset or 'Z' suffix, e.g. '2026-08-27T22:00:00'. "
                "Resolve relative expressions ('tonight', 'in 20 minutes', "
                "'next Friday at 3pm') against the current local date/time "
                "given in the system prompt. Do not guess a UTC offset — the "
                "timezone is already known and applied separately."
            ),
        },
        "text": {
            "type": "string",
            "description": "Short description of what to remind about, e.g. 'Admin meeting'.",
        },
    },
    required=["when_iso", "text"],
)
def handle_set_reminder(arguments: dict, ctx: ToolContext) -> str:
    when_iso = arguments["when_iso"]

    due_at = parse_due_at(when_iso)
    if due_at is None:
        return f"Error: could not parse '{when_iso}' as a date/time. Ask the user to clarify."

    if due_at <= datetime.now(UTC):
        return (
            f"Error: {format_local(due_at)} is already in the past. "
            "Ask the user for a specific future time."
        )

    reminder = {
        "id": os.urandom(4).hex(),
        "author_id": ctx.user_id,
        "text": arguments["text"],
        "due_at": due_at.isoformat(),
    }
    _add(reminder)
    schedule_reminder(reminder)

    return f"Reminder set for {format_local(due_at)}: {arguments['text']}"


@tool(
    name="set_location_reminder",
    description=(
        "Schedule a one-off reminder that DMs the user the next time they "
        "arrive at or leave home, instead of at a fixed time. Only call this "
        "when the user's phrasing is location-based ('when I get home', "
        "'next time I leave the house') rather than time-based — use "
        "set_reminder for actual times."
    ),
    properties={
        "trigger": {
            "type": "string",
            "enum": list(TRIGGERS),
            "description": "'arrive' fires on arriving home, 'leave' fires on leaving home.",
        },
        "text": {
            "type": "string",
            "description": "Short description of what to remind about, e.g. 'Take out the trash'.",
        },
    },
    required=["trigger", "text"],
)
def handle_set_location_reminder(arguments: dict, ctx: ToolContext) -> str:
    trigger = arguments["trigger"]
    if trigger not in TRIGGERS:
        return f"Error: trigger must be one of {', '.join(TRIGGERS)}."

    _add(
        {
            "id": os.urandom(4).hex(),
            "author_id": ctx.user_id,
            "text": arguments["text"],
            "trigger": trigger,
        }
    )

    verb = "arrive home" if trigger == "arrive" else "leave home"
    return f"Reminder set for the next time you {verb}: {arguments['text']}"
