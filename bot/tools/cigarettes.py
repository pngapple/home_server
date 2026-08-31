"""
Cigarette counter tool: lets the LLM log a cigarette for the requesting
Discord user and report back counts.

Stored as a flat JSON dict keyed by Discord user id, one entry per smoke, so
it survives restarts/deploys the same way reminders.json does (see
tools/reminders.py) — deliberately not a database, just a personal tally.

  {"123456789": ["2026-08-27T22:00:00+00:00", "2026-08-28T09:15:00+00:00"]}

cigboard/ reads the same file for the leaderboard.
"""

import logging
from datetime import UTC, datetime

from .. import config, jsonstore
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.cigarettes")


def _load() -> dict[str, list[str]]:
    return jsonstore.read(config.CIGARETTES_FILE, {})


def _today_count(timestamps: list[str]) -> int:
    today = datetime.now(config.LOCAL_TZ).date()
    return sum(1 for ts in timestamps if datetime.fromisoformat(ts).astimezone(config.LOCAL_TZ).date() == today)


def _summary(timestamps: list[str]) -> str:
    return f"{_today_count(timestamps)} today, {len(timestamps)} total."


@tool(
    name="log_cigarette",
    description=(
        "Log that the user just smoked a cigarette, incrementing their "
        "personal counter. Call this whenever the user says something like "
        "'smoked one' or 'log a cigarette', not for hypothetical or "
        "past-tense recollections unrelated to right now."
    ),
)
def handle_log_cigarette(arguments: dict, ctx: ToolContext) -> str:
    user_id = str(ctx.user_id)
    with jsonstore.update(config.CIGARETTES_FILE, {}) as data:
        timestamps = data.setdefault(user_id, [])
        timestamps.append(datetime.now(UTC).isoformat())
        summary = _summary(timestamps)
    return f"Logged. {summary}"


@tool(
    name="get_cigarette_count",
    description="Get the user's cigarette counts: how many they've logged today and in total (all-time).",
)
def handle_get_cigarette_count(arguments: dict, ctx: ToolContext) -> str:
    timestamps = _load().get(str(ctx.user_id), [])
    if not timestamps:
        return "No cigarettes logged yet."
    return _summary(timestamps)


@tool(
    name="reset_cigarette_count",
    description=(
        "Clear the user's entire cigarette log, e.g. if they logged one by "
        "mistake or want to start tracking over. Only call this if the user "
        "clearly asks to reset/clear their count."
    ),
)
def handle_reset_cigarette_count(arguments: dict, ctx: ToolContext) -> str:
    with jsonstore.update(config.CIGARETTES_FILE, {}) as data:
        data.pop(str(ctx.user_id), None)
    return "Cigarette count reset to 0."
