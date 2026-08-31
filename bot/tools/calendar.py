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
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.calendar")

_NOT_CONNECTED = (
    "Error: this user hasn't connected their Google Calendar yet. Call "
    "connect_google_calendar to send them a link, then ask them to try "
    "again once connected."
)

_LOCAL_ISO_FORMAT_NOTE = (
    "local date/time, ISO 8601 WITHOUT a timezone offset or 'Z' suffix, "
    "e.g. '2026-08-27T15:00:00'"
)


async def _send_connect_dm(user: discord.abc.User, auth_url: str) -> None:
    try:
        await user.send(
            f"Connect your Google Calendar: {auth_url}\n\n"
            "This link is just for you — approving it links whatever "
            "Google account you sign in with to your Discord account here."
        )
    except Exception:
        log.exception("Failed to DM calendar connect link to %s", user.id)


def _get_service(discord_user_id: int):
    """A Calendar API client for this user, or None if they aren't linked
    (or their stored grant has been revoked — google_oauth drops it then, so
    the user just needs to connect again)."""
    creds = google_oauth.get_credentials(discord_user_id)
    if creds is None:
        return None
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_local(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso).replace(tzinfo=config.LOCAL_TZ)
    except ValueError:
        return None


@tool(
    name="connect_google_calendar",
    description=(
        "Send the user a link to connect their own Google Calendar. Call "
        "this if they ask to schedule an event but haven't linked their "
        "calendar yet (create_calendar_event will tell you if that's the case)."
    ),
)
def handle_connect(arguments: dict, ctx: ToolContext) -> str:
    if google_oauth.is_connected(ctx.user_id):
        return "The user's Google Calendar is already connected."

    auth_url = google_oauth.build_authorize_url(ctx.user_id)
    # This handler runs in a worker thread (see reminders.schedule_reminder),
    # so the DM has to be handed back to the Discord client's own loop.
    asyncio.run_coroutine_threadsafe(_send_connect_dm(ctx.message.author, auth_url), client.loop)
    return (
        "Sent the user a Google Calendar connect link via DM. Tell them to "
        "check their DMs, open it, and approve access."
    )


@tool(
    name="create_calendar_event",
    description=(
        "Create an event on the user's own Google Calendar. Only call this "
        "once you know a specific title and start time — ask the user first "
        "if either is missing or too vague (e.g. 'later')."
    ),
    properties={
        "summary": {"type": "string", "description": "Short event title, e.g. 'Dentist appointment'."},
        "start_iso": {
            "type": "string",
            "description": (
                f"Start {_LOCAL_ISO_FORMAT_NOTE}. Resolve relative "
                "expressions ('tomorrow at 3pm', 'next Friday') against the "
                "current local date/time given in the system prompt."
            ),
        },
        "end_iso": {
            "type": "string",
            "description": f"End {_LOCAL_ISO_FORMAT_NOTE}. Optional — defaults to one hour after start_iso.",
        },
        "description": {"type": "string", "description": "Optional longer note/details for the event."},
        "location": {"type": "string", "description": "Optional location text."},
    },
    required=["summary", "start_iso"],
)
def handle_create_event(arguments: dict, ctx: ToolContext) -> str:
    start_iso = arguments["start_iso"]
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

    service = _get_service(ctx.user_id)
    if service is None:
        return _NOT_CONNECTED

    event = {
        "summary": arguments["summary"],
        "start": {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE},
    }
    for optional in ("description", "location"):
        if arguments.get(optional):
            event[optional] = arguments[optional]

    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
    except HttpError:
        log.exception("Google Calendar insert failed for user %s", ctx.user_id)
        return "Error: Google Calendar rejected the request. Check the server logs for details."

    return f"Event created: {created.get('htmlLink')}"


@tool(
    name="list_calendar_events",
    description=(
        "List the user's Google Calendar events between two local "
        "date/times. Use this to answer scheduling/availability questions "
        "(e.g. 'am I free at 6pm Tuesday?', 'what's on my calendar "
        "tomorrow?') — for a specific-time question, pass the start and end "
        "of that whole day so you can see everything around the time in "
        "question and reason about overlaps yourself."
    ),
    properties={
        "start_iso": {"type": "string", "description": f"Range start, {_LOCAL_ISO_FORMAT_NOTE}."},
        "end_iso": {"type": "string", "description": f"Range end, {_LOCAL_ISO_FORMAT_NOTE}."},
    },
    required=["start_iso", "end_iso"],
)
def handle_list_events(arguments: dict, ctx: ToolContext) -> str:
    start_dt = _parse_local(arguments["start_iso"])
    end_dt = _parse_local(arguments["end_iso"])
    if start_dt is None or end_dt is None:
        return "Error: could not parse start_iso/end_iso as date/times. Ask the user to clarify."

    service = _get_service(ctx.user_id)
    if service is None:
        return _NOT_CONNECTED

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
        log.exception("Google Calendar list failed for user %s", ctx.user_id)
        return "Error: Google Calendar rejected the request. Check the server logs for details."

    events = result.get("items", [])
    if not events:
        return f"No events between {arguments['start_iso']} and {arguments['end_iso']}."

    return "\n".join(
        f"- {ev.get('summary', '(no title)')}: "
        f"{ev['start'].get('dateTime', ev['start'].get('date'))} to "
        f"{ev['end'].get('dateTime', ev['end'].get('date'))}"
        for ev in events
    )
