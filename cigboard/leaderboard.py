"""
Stats computation over cigarettes.json — the same flat
{user_id: [iso timestamp, ...]} file bot/tools/cigarettes.py writes to (one
entry per logged cigarette). Pure data-in, data-out: no Discord/HTTP here.
"""

import logging
from datetime import datetime, timedelta

from bot import config, jsonstore

log = logging.getLogger("discord-llm-bot.cigboard.leaderboard")

# How many trailing days of daily counts to hand back per user, for the
# sparkline on each leaderboard card.
SPARKLINE_DAYS = 14


def _load() -> dict[str, list[str]]:
    return jsonstore.read(config.CIGARETTES_FILE, {})


def _local_dates(timestamps: list[str]):
    for ts in timestamps:
        yield datetime.fromisoformat(ts).astimezone(config.LOCAL_TZ)


def _stats_for(user_id: str, timestamps: list[str], now) -> dict:
    local_dt = sorted(_local_dates(timestamps))
    today = now.date()
    week_ago = today - timedelta(days=7)

    total = len(local_dt)
    today_count = sum(1 for dt in local_dt if dt.date() == today)
    week_count = sum(1 for dt in local_dt if dt.date() > week_ago)

    first_date = local_dt[0].date() if local_dt else today
    span_days = max(1, (today - first_date).days + 1)
    avg_per_day = total / span_days

    last_dt = local_dt[-1] if local_dt else None
    last_smoked_ago_s = (now - last_dt).total_seconds() if last_dt else None

    counts_by_date: dict = {}
    for dt in local_dt:
        counts_by_date[dt.date()] = counts_by_date.get(dt.date(), 0) + 1
    sparkline = [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": counts_by_date.get(today - timedelta(days=offset), 0),
        }
        for offset in range(SPARKLINE_DAYS - 1, -1, -1)
    ]

    return {
        "id": user_id,
        "total": total,
        "today": today_count,
        "week": week_count,
        "avg_per_day": round(avg_per_day, 2),
        "last_smoked": last_dt.isoformat() if last_dt else None,
        "last_smoked_ago_s": last_smoked_ago_s,
        "sparkline": sparkline,
    }


def compute() -> list[dict]:
    """Returns per-user stats sorted by all-time total, descending."""
    data = _load()
    now = datetime.now(config.LOCAL_TZ)
    rows = [_stats_for(uid, ts, now) for uid, ts in data.items() if ts]
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows
