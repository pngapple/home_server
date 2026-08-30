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
import json
import logging
import os
from datetime import datetime, timezone as dt_timezone

from .. import config, jsonstore
from ..discord_client import client
from . import ToolContext, register

log = logging.getLogger("discord-llm-bot.tools.reminders")

SET_REMINDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "Schedule a one-off reminder that DMs the user at a specific "
            "future local date/time. Only call this once you know both a "
            "specific time and what to remind them about — ask the user "
            "first if either is missing or too vague (e.g. 'later')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "when_iso": {
                    "type": "string",
                    "description": (
                        "The target local date/time, ISO 8601 format WITHOUT "
                        "a timezone offset or 'Z' suffix, e.g. "
                        "'2026-08-27T22:00:00'. Resolve relative expressions "
                        "('tonight', 'in 20 minutes', 'next Friday at 3pm') "
                        "against the current local date/time given in the "
                        "system prompt. Do not guess a UTC offset — the "
                        "timezone is already known and applied separately."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Short description of what to remind about, e.g. 'Admin meeting'.",
                },
            },
            "required": ["when_iso", "text"],
        },
    },
}


SET_LOCATION_REMINDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_location_reminder",
        "description": (
            "Schedule a one-off reminder that DMs the user the next time "
            "they arrive at or leave home, instead of at a fixed time. "
            "Only call this when the user's phrasing is location-based "
            "('when I get home', 'next time I leave the house') rather than "
            "time-based — use set_reminder for actual times."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "trigger": {
                    "type": "string",
                    "enum": ["arrive", "leave"],
                    "description": "'arrive' fires on arriving home, 'leave' fires on leaving home.",
                },
                "text": {
                    "type": "string",
                    "description": "Short description of what to remind about, e.g. 'Take out the trash'.",
                },
            },
            "required": ["trigger", "text"],
        },
    },
}


def load_reminders() -> list[dict]:
    if not os.path.exists(config.REMINDERS_FILE):
        return []
    try:
        with open(config.REMINDERS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.exception("Failed to read %s, treating as empty", config.REMINDERS_FILE)
        return []


def save_reminders(reminders: list[dict]) -> None:
    jsonstore.write(config.REMINDERS_FILE, reminders)


def _add(reminder: dict) -> None:
    with jsonstore.lock(config.REMINDERS_FILE):
        reminders = load_reminders()
        reminders.append(reminder)
        save_reminders(reminders)


def _remove(reminder_id: str) -> None:
    with jsonstore.lock(config.REMINDERS_FILE):
        save_reminders([r for r in load_reminders() if r["id"] != reminder_id])


def parse_due_at(when_iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(when_iso)
    except ValueError:
        return None
    # Ignore any offset the model included (it can't reliably know DST
    # rules) — always trust our own tz database for the real offset.
    dt = dt.replace(tzinfo=config.LOCAL_TZ)
    return dt.astimezone(dt_timezone.utc)


def format_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(config.LOCAL_TZ).strftime("%a %b %d, %I:%M %p %Z")


def schedule_reminder(reminder: dict) -> None:
    """Set a one-shot timer that fires exactly at due_at, instead of
    polling. The JSON file is only there so reminders survive a restart
    (see reschedule_pending), not to be scanned periodically."""
    due_at = datetime.fromisoformat(reminder["due_at"])
    delay = max(0, (due_at - datetime.now(dt_timezone.utc)).total_seconds())
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
        log.exception("Failed to deliver reminder %s", reminder["id"])
    finally:
        _remove(reminder["id"])


def reschedule_pending() -> None:
    """Call once on startup to re-arm timers for reminders that were saved
    before a restart."""
    pending = load_reminders()
    timed = [r for r in pending if "due_at" in r]
    for reminder in timed:
        schedule_reminder(reminder)
    if timed:
        log.info("Rescheduled %d pending reminder(s)", len(timed))


def handle_set_reminder(arguments: dict, ctx: ToolContext) -> str:
    when_iso = arguments.get("when_iso")
    text = arguments.get("text")
    if not when_iso or not text:
        return "Error: both when_iso and text are required to set a reminder."

    due_at = parse_due_at(when_iso)
    if due_at is None:
        return f"Error: could not parse '{when_iso}' as a date/time. Ask the user to clarify."

    if due_at <= datetime.now(dt_timezone.utc):
        return (
            f"Error: {format_local(due_at)} is already in the past. "
            "Ask the user for a specific future time."
        )

    reminder = {
        "id": os.urandom(4).hex(),
        "author_id": ctx.message.author.id,
        "text": text,
        "due_at": due_at.isoformat(),
    }
    _add(reminder)
    schedule_reminder(reminder)

    return f"Reminder set for {format_local(due_at)}: {text}"


def handle_set_location_reminder(arguments: dict, ctx: ToolContext) -> str:
    trigger = arguments.get("trigger")
    text = arguments.get("text")
    if trigger not in ("arrive", "leave") or not text:
        return "Error: trigger (arrive/leave) and text are both required."

    reminder = {
        "id": os.urandom(4).hex(),
        "author_id": ctx.message.author.id,
        "text": text,
        "trigger": trigger,
    }
    _add(reminder)

    verb = "arrive home" if trigger == "arrive" else "leave home"
    return f"Reminder set for the next time you {verb}: {text}"


def pop_location_reminders(trigger: str) -> list[dict]:
    """Remove and return all pending reminders waiting on this geofence
    trigger ('arrive' or 'leave'), for delivery by geofence_server.py."""
    with jsonstore.lock(config.REMINDERS_FILE):
        reminders = load_reminders()
        matched = [r for r in reminders if r.get("trigger") == trigger]
        if matched:
            save_reminders([r for r in reminders if r.get("trigger") != trigger])
    return matched


register("set_reminder", SET_REMINDER_SCHEMA, handle_set_reminder)
register("set_location_reminder", SET_LOCATION_REMINDER_SCHEMA, handle_set_location_reminder)
