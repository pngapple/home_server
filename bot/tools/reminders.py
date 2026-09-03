"""
Reminder tool: lets the LLM schedule a one-off DM to fire either at a future
local date/time, or the next time a geofence event (arriving/leaving home)
comes in from bot/geofence_server.py — plus recurring location reminders
linked to a todo item, that keep firing every N minutes while a geofence
condition holds until the item is marked complete.

Stored as a flat JSON list on disk so they survive a restart. Deliberately
not a database: this is a handful of personal reminders, not a workload.

  [{"id": "...", "author_id": 123, "text": "Admin meeting",
    "due_at": "2026-08-27T22:00:00+00:00"},
   {"id": "...", "author_id": 123, "text": "Take out the trash",
    "trigger": "arrive"},
   {"id": "...", "author_id": 123, "text": "Dishes", "trigger": "arrive",
    "interval_minutes": 30, "linked_todo_id": "a1b2c3d4"}, ...]

Time-based reminders have a "due_at"; location-based ones have a "trigger"
("arrive" or "leave") instead. One-off location reminders sit untouched
until geofence_server.py calls pop_location_reminders() for a matching
event. Recurring ones (interval_minutes set) are started/paused by
sync_recurring_for_event() instead — see _recurring_loop.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime

from .. import config, jsonstore
from ..discord_client import client
from . import ToolContext, todos, tool

log = logging.getLogger("discord-llm-bot.tools.reminders")

# A household tool — gated to config.HOUSEHOLD_ROLE_NAME (see permissions.py)
# so randoms in the server/DMs can't touch it.
_HOUSEHOLD = config.HOUSEHOLD_ROLE_NAME

TRIGGERS = ("arrive", "leave")


def load_reminders() -> list[dict]:
    return jsonstore.read(config.REMINDERS_FILE, [])


def _add(reminder: dict) -> None:
    with jsonstore.update(config.REMINDERS_FILE, []) as reminders:
        reminders.append(reminder)


def _remove(reminder_id: str) -> None:
    with jsonstore.update(config.REMINDERS_FILE, []) as reminders:
        reminders[:] = [r for r in reminders if r.get("id") != reminder_id]


def _get(reminder_id: str) -> dict | None:
    return next((r for r in load_reminders() if r.get("id") == reminder_id), None)


async def _dm(user_id: int, text: str) -> None:
    try:
        user = await client.fetch_user(user_id)
        await user.send(text)
    except Exception:
        log.exception("Failed to DM user %s", user_id)


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
    await _dm(reminder["author_id"], f"⏰ Reminder: {reminder['text']}")
    _remove(reminder.get("id"))


def reschedule_pending() -> None:
    """Call once on startup to re-arm timers for reminders that were saved
    before a restart."""
    timed = [r for r in load_reminders() if "due_at" in r]
    for reminder in timed:
        schedule_reminder(reminder)
    if timed:
        log.info("Rescheduled %d pending reminder(s)", len(timed))

    # No webhook has landed yet this process — recover each resident's last
    # known arrive/leave state from disk so a recurring reminder that should
    # already be active (e.g. the server restarted while they were home)
    # doesn't just sit paused until the next geofence transition.
    for user_id_str, last_event in jsonstore.read(config.GEOFENCE_STATE_FILE, {}).items():
        sync_recurring_for_event(int(user_id_str), last_event)


def pop_location_reminders(user_id: int, trigger: str) -> list[dict]:
    """Remove and return this user's pending one-off reminders waiting on
    this geofence trigger ('arrive' or 'leave'), for delivery by
    geofence_server.py. Scoped to author_id so one resident's phone
    arriving/leaving never touches a housemate's reminders. Recurring
    reminders (interval_minutes set) are left alone here —
    sync_recurring_for_event handles those, since they persist across
    multiple arrive/leave cycles instead of firing once."""

    def is_match(r: dict) -> bool:
        return r.get("author_id") == user_id and r.get("trigger") == trigger and "interval_minutes" not in r

    with jsonstore.update(config.REMINDERS_FILE, []) as reminders:
        matched = [r for r in reminders if is_match(r)]
        reminders[:] = [r for r in reminders if not is_match(r)]
    return matched


# ---------------------------------------------------------------------------
# Recurring location reminders — repeat every interval_minutes while a
# geofence trigger holds, linked to a todo item so they stop on their own
# once it's checked off.
# ---------------------------------------------------------------------------

# reminder id -> the concurrent.futures.Future returned by
# run_coroutine_threadsafe for its _recurring_loop. In-memory only — an
# asyncio task can't survive a restart anyway, so this is rebuilt from
# geofence state (see reschedule_pending) rather than persisted.
_recurring_tasks: dict[str, object] = {}


def record_geofence_event(user_id: int, event: str) -> None:
    """Persist this user's most recent geofence event, so a restart
    (reschedule_pending, which runs before any webhook fires this process)
    knows whether their recurring reminder should already be active. Called
    by geofence_server.py on every webhook."""
    with jsonstore.update(config.GEOFENCE_STATE_FILE, {}) as state:
        state[str(user_id)] = event


def _last_geofence_event(user_id: int) -> str | None:
    return jsonstore.read(config.GEOFENCE_STATE_FILE, {}).get(str(user_id))


async def _recurring_loop(reminder_id: str) -> None:
    try:
        while True:
            reminder = _get(reminder_id)
            if reminder is None:
                return
            linked_id = reminder.get("linked_todo_id")
            if linked_id:
                todo = todos.get_todo(reminder["author_id"], linked_id)
                if todo is None or todo.get("done"):
                    _remove(reminder_id)
                    return
            await _dm(reminder["author_id"], f"⏰ Reminder: {reminder['text']}")
            await asyncio.sleep(reminder["interval_minutes"] * 60)
    finally:
        _recurring_tasks.pop(reminder_id, None)


def _start_recurring(reminder: dict) -> None:
    if reminder["id"] in _recurring_tasks:
        return
    # Called both from tool handlers (a worker thread — see app.py's
    # asyncio.to_thread(ask_llm, ...)) and from geofence_server's webhook
    # handler (already running on client.loop). run_coroutine_threadsafe is
    # safe from either context, so it's used uniformly rather than branching.
    _recurring_tasks[reminder["id"]] = asyncio.run_coroutine_threadsafe(_recurring_loop(reminder["id"]), client.loop)


def _stop_recurring(reminder_id: str) -> None:
    """Cancels the running loop without deleting the reminder, so it resumes
    next time its trigger condition holds again."""
    future = _recurring_tasks.get(reminder_id)
    if future is not None:
        future.cancel()


def sync_recurring_for_event(user_id: int, event: str) -> None:
    """Called on every geofence webhook (and once at startup, from
    reschedule_pending): starts this user's recurring reminders whose
    trigger just became active, and pauses the ones for the opposite
    trigger. Scoped to author_id, same reasoning as pop_location_reminders."""
    opposite = "leave" if event == "arrive" else "arrive"
    for reminder in load_reminders():
        if reminder.get("author_id") != user_id or "interval_minutes" not in reminder:
            continue
        if reminder.get("trigger") == event:
            _start_recurring(reminder)
        elif reminder.get("trigger") == opposite:
            _stop_recurring(reminder["id"])


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
    required_role=_HOUSEHOLD,
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
        "set_reminder for actual times, or set_recurring_location_reminder "
        "if they want it to repeat until a todo item is done."
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
    required_role=_HOUSEHOLD,
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


@tool(
    name="set_recurring_location_reminder",
    description=(
        "Schedule a repeating reminder linked to an existing todo item: DMs "
        "the user every interval_minutes while a geofence condition holds "
        "(e.g. every 30 minutes while home), and stops automatically once "
        "the linked todo is marked complete. The todo item must already "
        "exist — call add_todo first if the user hasn't added it yet. Only "
        "use this for recurring, location-gated reminders phrased like "
        "'remind me every 30 minutes when I'm home until I do the dishes' — "
        "use set_location_reminder for a single one-off location reminder, "
        "and set_reminder for a one-off time-based reminder."
    ),
    properties={
        "todo_identifier": {
            "type": "string",
            "description": "A distinctive snippet of the linked todo item's text, e.g. 'dishes'.",
        },
        "interval_minutes": {
            "type": "integer",
            "description": "How often to repeat the reminder, in minutes, while the trigger condition holds.",
        },
        "trigger": {
            "type": "string",
            "enum": list(TRIGGERS),
            "description": "'arrive' repeats every interval while home, 'leave' repeats every interval while away.",
        },
    },
    required=["todo_identifier", "interval_minutes", "trigger"],
    required_role=_HOUSEHOLD,
)
def handle_set_recurring_location_reminder(arguments: dict, ctx: ToolContext) -> str:
    trigger = arguments["trigger"]
    if trigger not in TRIGGERS:
        return f"Error: trigger must be one of {', '.join(TRIGGERS)}."

    try:
        interval_minutes = int(arguments["interval_minutes"])
    except (TypeError, ValueError):
        return "Error: interval_minutes must be a whole number of minutes."
    if interval_minutes <= 0:
        return "Error: interval_minutes must be positive."

    identifier = arguments["todo_identifier"]
    matches = todos.find_open(ctx.user_id, identifier)
    if not matches:
        return f"Error: no open todo matches '{identifier}'. Add it with add_todo first."
    if len(matches) > 1:
        texts = ", ".join(f"'{m['text']}'" for m in matches)
        return f"Error: '{identifier}' matches multiple items ({texts}). Ask the user to be more specific."

    todo = matches[0]
    reminder = {
        "id": os.urandom(4).hex(),
        "author_id": ctx.user_id,
        "text": todo["text"],
        "trigger": trigger,
        "interval_minutes": interval_minutes,
        "linked_todo_id": todo["id"],
    }
    _add(reminder)
    if trigger == _last_geofence_event(ctx.user_id):
        _start_recurring(reminder)

    verb = "home" if trigger == "arrive" else "away"
    return (
        f"Set: every {interval_minutes} minute(s) while {verb}, you'll be "
        f"reminded about '{todo['text']}' until it's marked complete."
    )
