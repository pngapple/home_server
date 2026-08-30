"""
Cigarette counter tool: lets the LLM log a cigarette for the requesting
Discord user and report back counts.

Stored as a flat JSON dict keyed by Discord user id, one entry per smoke, so
it survives restarts/deploys the same way reminders.json does (see
tools/reminders.py) — deliberately not a database, just a personal tally.

  {"123456789": ["2026-08-27T22:00:00+00:00", "2026-08-28T09:15:00+00:00"]}
"""

import json
import logging
import os
from datetime import datetime, timezone as dt_timezone

from .. import config, jsonstore
from . import ToolContext, register

log = logging.getLogger("discord-llm-bot.tools.cigarettes")

LOG_CIGARETTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "log_cigarette",
        "description": (
            "Log that the user just smoked a cigarette, incrementing their "
            "personal counter. Call this whenever the user says something "
            "like 'smoked one' or 'log a cigarette', not for hypothetical "
            "or past-tense recollections unrelated to right now."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

GET_CIGARETTE_COUNT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_cigarette_count",
        "description": (
            "Get the user's cigarette counts: how many they've logged "
            "today and in total (all-time)."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

RESET_CIGARETTE_COUNT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "reset_cigarette_count",
        "description": (
            "Clear the user's entire cigarette log, e.g. if they logged one "
            "by mistake or want to start tracking over. Only call this if "
            "the user clearly asks to reset/clear their count."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


def _load() -> dict[str, list[str]]:
    if not os.path.exists(config.CIGARETTES_FILE):
        return {}
    try:
        with open(config.CIGARETTES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.exception("Failed to read %s, treating as empty", config.CIGARETTES_FILE)
        return {}


def _save(data: dict[str, list[str]]) -> None:
    jsonstore.write(config.CIGARETTES_FILE, data)


def _today_count(timestamps: list[str]) -> int:
    today = datetime.now(config.LOCAL_TZ).date()
    count = 0
    for ts in timestamps:
        local_date = datetime.fromisoformat(ts).astimezone(config.LOCAL_TZ).date()
        if local_date == today:
            count += 1
    return count


def handle_log_cigarette(arguments: dict, ctx: ToolContext) -> str:
    user_id = str(ctx.message.author.id)
    with jsonstore.lock(config.CIGARETTES_FILE):
        data = _load()
        timestamps = data.setdefault(user_id, [])
        timestamps.append(datetime.now(dt_timezone.utc).isoformat())
        _save(data)

    today = _today_count(timestamps)
    return f"Logged. {today} today, {len(timestamps)} total."


def handle_get_cigarette_count(arguments: dict, ctx: ToolContext) -> str:
    user_id = str(ctx.message.author.id)
    timestamps = _load().get(user_id, [])
    if not timestamps:
        return "No cigarettes logged yet."

    today = _today_count(timestamps)
    return f"{today} today, {len(timestamps)} total."


def handle_reset_cigarette_count(arguments: dict, ctx: ToolContext) -> str:
    user_id = str(ctx.message.author.id)
    with jsonstore.lock(config.CIGARETTES_FILE):
        data = _load()
        if user_id in data:
            del data[user_id]
            _save(data)
    return "Cigarette count reset to 0."


register("log_cigarette", LOG_CIGARETTE_SCHEMA, handle_log_cigarette)
register("get_cigarette_count", GET_CIGARETTE_COUNT_SCHEMA, handle_get_cigarette_count)
register("reset_cigarette_count", RESET_CIGARETTE_COUNT_SCHEMA, handle_reset_cigarette_count)
