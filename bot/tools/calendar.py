"""
Google Calendar tools: each Discord user links their own Google account
(see ../google_oauth.py), so "my calendar" always means the calendar of
whoever is talking to the bot.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .. import config, google_oauth
from ..discord_client import client
from . import ToolContext, register

log = logging.getLogger("discord-llm-bot.tools.calendar")

CONNECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "connect_google_calendar",
        "description": (
            "Send the user a link to connect their own Google Calendar. "
            "Call this if they ask to schedule an event but haven't linked "
            "their calendar yet (create_calendar_event will tell you if "
            "that's the case)."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

CREATE_EVENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_calendar_event",
        "description": (
            "Create an event on the user's own Google Calendar. Only call "
            "this once you know a specific title and start time — ask the "
            "user first if either is missing or too vague (e.g. 'later')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Short event title, e.g. 'Dentist appointment'.",
                },
                "start_iso": {
                    "type": "string",
                    "description": (
                        "Start local date/time, ISO 8601 WITHOUT a timezone "
                        "offset or 'Z' suffix, e.g. '2026-08-27T15:00:00'. "
                        "Resolve relative expressions ('tomorrow at 3pm', "
                        "'next Friday') against the current local date/time "
                        "given in the system prompt."
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": (
                        "End local date/time, same format as start_iso. "
                        "Optional — defaults to one hour after start_iso."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional longer note/details for the event.",
                },
                "location": {
                    "type": "string",
                    "description": "Optional location text.",
                },
            },
            "required": ["summary", "start_iso"],
        },
    },
}


async def _send_connect_dm(user: discord.abc.User, auth_url: str) -> None:
    try:
        await user.send(
            f"Connect your Google Calendar: {auth_url}\n\n"
            "This link is just for you — approving it links whatever "
            "Google account you sign in with to your Discord account here."
        )
    except Exception:
        log.exception("Failed to DM calendar connect link to %s", user.id)


LIST_EVENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_calendar_events",
        "description": (
            "List the user's Google Calendar events between two local "
            "date/times. Use this to answer scheduling/availability "
            "questions (e.g. 'am I free at 6pm Tuesday?', 'what's on my "
            "calendar tomorrow?') — for a specific-time question, pass the "
            "start and end of that whole day so you can see everything "
            "around the time in question and reason about overlaps "
            "yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_iso": {
                    "type": "string",
                    "description": (
                        "Range start, local date/time, ISO 8601 WITHOUT a "
                        "timezone offset or 'Z' suffix, e.g. "
                        "'2026-08-27T00:00:00'."
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": "Range end, same format as start_iso.",
                },
            },
            "required": ["start_iso", "end_iso"],
        },
    },
}


def handle_connect(arguments: dict, ctx: ToolContext) -> str:
    user_id = ctx.message.author.id
    if google_oauth.is_connected(user_id):
        return "The user's Google Calendar is already connected."

    auth_url = google_oauth.build_authorize_url(user_id)
    # This handler runs in a worker thread (see reminders.schedule_reminder),
    # so the DM has to be handed back to the Discord client's own loop.
    asyncio.run_coroutine_threadsafe(_send_connect_dm(ctx.message.author, auth_url), client.loop)
    return (
        "Sent the user a Google Calendar connect link via DM. Tell them to "
        "check their DMs, open it, and approve access."
    )


def _get_service(discord_user_id: int):
    creds = google_oauth.get_credentials(discord_user_id)
    if creds is None:
        return None
    return build("calendar", "v3", credentials=creds)


def _parse_local(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.replace(tzinfo=config.LOCAL_TZ)


def handle_create_event(arguments: dict, ctx: ToolContext) -> str:
    user_id = ctx.message.author.id
    summary = arguments.get("summary")
    start_iso = arguments.get("start_iso")
    if not summary or not start_iso:
        return "Error: both summary and start_iso are required."

    start_dt = _parse_local(start_iso)
    if start_dt is None:
        return f"Error: could not parse '{start_iso}' as a date/time. Ask the user to clarify."

    end_iso = arguments.get("end_iso")
    if end_iso:
        end_dt = _parse_local(end_iso)
        if end_dt is None:
            return f"Error: could not parse '{end_iso}' as a date/time. Ask the user to clarify."
    else:
        end_dt = start_dt + timedelta(hours=1)

    service = _get_service(user_id)
    if service is None:
        return (
            "Error: this user hasn't connected their Google Calendar yet. "
            "Call connect_google_calendar to send them a link, then ask "
            "them to try again once connected."
        )

    event = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE},
    }
    if arguments.get("description"):
        event["description"] = arguments["description"]
    if arguments.get("location"):
        event["location"] = arguments["location"]

    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
    except HttpError:
        log.exception("Google Calendar insert failed for user %s", user_id)
        return "Error: Google Calendar rejected the request. Check the server logs for details."

    return f"Event created: {created.get('htmlLink')}"


def handle_list_events(arguments: dict, ctx: ToolContext) -> str:
    user_id = ctx.message.author.id
    start_iso = arguments.get("start_iso")
    end_iso = arguments.get("end_iso")
    if not start_iso or not end_iso:
        return "Error: both start_iso and end_iso are required."

    start_dt = _parse_local(start_iso)
    end_dt = _parse_local(end_iso)
    if start_dt is None or end_dt is None:
        return "Error: could not parse start_iso/end_iso as date/times. Ask the user to clarify."

    service = _get_service(user_id)
    if service is None:
        return (
            "Error: this user hasn't connected their Google Calendar yet. "
            "Call connect_google_calendar to send them a link, then ask "
            "them to try again once connected."
        )

    try:
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError:
        log.exception("Google Calendar list failed for user %s", user_id)
        return "Error: Google Calendar rejected the request. Check the server logs for details."

    events = result.get("items", [])
    if not events:
        return f"No events between {start_iso} and {end_iso}."

    lines = []
    for ev in events:
        start = ev["start"].get("dateTime", ev["start"].get("date"))
        end = ev["end"].get("dateTime", ev["end"].get("date"))
        lines.append(f"- {ev.get('summary', '(no title)')}: {start} to {end}")
    return "\n".join(lines)


register("connect_google_calendar", CONNECT_SCHEMA, handle_connect)
register("create_calendar_event", CREATE_EVENT_SCHEMA, handle_create_event)
register("list_calendar_events", LIST_EVENTS_SCHEMA, handle_list_events)
