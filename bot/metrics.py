"""
Metrics for LLM calls, fed by both the OpenRouter chat path (llm.py) and the
Claude Code bridge (claude_bridge.py). Consumed by llm_status_server.py for
the /llm/ dashboard.

Backed by SQLite (see db.py) — one INSERT per call rather than rewriting a
48 KB JSON file every time, and full history instead of a 200-entry deque.
The four module-level cost aggregates this used to maintain by hand
(cumulative and per-user, for each of the two sources) are gone: cost is a
column, and SUM() answers all four.

Uptime deliberately does NOT persist: it's meant to show time since this
process last started, which is useful signal in itself — a suspiciously low
uptime means the bot crashed or restarted recently — and carrying it over
would hide that.

record_many() blocks on disk, so async callers should hand it to a thread.
"""

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import config, db

log = logging.getLogger("discord-llm-bot.metrics")

_START_TIME = time.monotonic()

# How many rows the dashboard lists, and the chart's bar count.
_SNAPSHOT_ROWS = 25

# The window the headline tiles and the per-user call/token counts cover.
# Previously this was implicit — whatever happened to be in a 200-entry
# deque — which meant it drifted with traffic and silently discarded
# history. Now it's a stated period over data that's all still there.
WINDOW_DAYS = config.METRICS_WINDOW_DAYS
_WINDOW_S = WINDOW_DAYS * 86400

# Rows older than this are deleted at startup. Full history is only an
# improvement if it's bounded; this replaces the deque's cap with a policy.
RETENTION_DAYS = config.METRICS_RETENTION_DAYS


@dataclass
class Call:
    source: str  # "openrouter", "claude-code", or "groq"
    model: str
    input_tokens: int
    output_tokens: int
    duration_s: float
    context_window: int | None = None
    # Who triggered this call, snapshotted at record time. Unlike the live
    # views (the grocery list resolves names through users.py so it shows
    # what people are called *now*), a metrics row is a historical record —
    # "who was billed for this call, as they were known then" — so the
    # snapshot is deliberate here rather than a leftover.
    user_id: int | None = None
    user_name: str | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def tokens_per_sec(self) -> float:
        return self.output_tokens / self.duration_s if self.duration_s > 0 else 0.0


def _row_dict(row) -> dict:
    """The dashboard-facing shape: stored fields plus the derived ones,
    rounded for display."""
    duration_s = row["duration_s"] or 0.0
    output_tokens = row["output_tokens"] or 0
    return {
        "source": row["source"],
        "model": row["model"],
        "input_tokens": row["input_tokens"],
        "output_tokens": output_tokens,
        "total_tokens": row["input_tokens"] + output_tokens,
        "duration_s": round(duration_s, 2),
        "tokens_per_sec": round(output_tokens / duration_s, 1) if duration_s > 0 else 0.0,
        "context_window": row["context_window"],
        "user_name": row["user_name"],
        "timestamp": row["ts"],
    }


def prune(retention_days: int = RETENTION_DAYS) -> int:
    """Drop calls older than the retention window. Called once at startup."""
    cutoff = time.time() - retention_days * 86400
    with db.write() as conn:
        deleted = conn.execute("DELETE FROM calls WHERE ts < ?", (cutoff,)).rowcount
    if deleted:
        log.info("Pruned %d metrics row(s) older than %d days", deleted, retention_days)
    return deleted


def record_many(
    calls: Iterable[Call] = (), cost_usd: float = 0.0, user_id: int | None = None, source: str | None = None
) -> None:
    """Append calls and/or attribute spend, in one transaction. `source`
    ("openrouter" or "claude-code") says which one `cost_usd` belongs to.

    A Claude Code turn reports several models plus one total cost, so the
    cost is attached to the turn's first row rather than split across them
    or stored as a phantom row — SUM(cost_usd) is the same either way, and
    it stays attributable to a real call.
    """
    rows = [
        (
            c.timestamp,
            c.source,
            c.model,
            c.input_tokens,
            c.output_tokens,
            c.duration_s,
            c.context_window,
            c.user_id,
            c.user_name,
            0.0,
        )
        for c in calls
    ]
    if cost_usd and rows:
        rows[0] = rows[0][:-1] + (cost_usd,)

    try:
        with db.write() as conn:
            if rows:
                conn.executemany(
                    "INSERT INTO calls (ts, source, model, input_tokens, output_tokens, "
                    "duration_s, context_window, user_id, user_name, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            elif cost_usd and source:
                # Spend with no call rows to hang it on (shouldn't normally
                # happen, but don't silently drop money).
                conn.execute(
                    "INSERT INTO opening_balance (source, user_id, cost_usd) VALUES (?, ?, ?) "
                    "ON CONFLICT(source, user_id) DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd",
                    (source, user_id, cost_usd),
                )
    except Exception:
        log.exception("Failed to record metrics (%d call(s), cost=%s)", len(rows), cost_usd)


def record(
    source: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_s: float,
    context_window: int | None = None,
    user_id: int | None = None,
    user_name: str | None = None,
    cost_usd: float = 0.0,
) -> None:
    record_many(
        [
            Call(
                source=source,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_s=duration_s,
                context_window=context_window,
                user_id=user_id,
                user_name=user_name,
            )
        ],
        cost_usd=cost_usd,
        user_id=user_id,
        source=source,
    )


def _opening_balances(conn) -> dict[tuple[str, int | None], float]:
    """Pre-migration spend, summed per (source, user).

    Summed rather than read row-for-row because the account-wide rows have
    user_id NULL, and SQLite treats NULLs in a PRIMARY KEY as distinct — so
    the upsert in record_many can't dedupe them and duplicates are possible.
    Aggregating here makes that harmless instead of silently losing money.
    """
    return {
        (row["source"], row["user_id"]): row["cost_usd"]
        for row in conn.execute(
            "SELECT source, user_id, SUM(cost_usd) AS cost_usd FROM opening_balance GROUP BY source, user_id"
        )
    }


def _by_user(conn, since: float, balances: dict) -> list[dict]:
    """Per-user breakdown: call/token counts over the same window as the
    rest of the dashboard, plus each user's all-time spend. Claude Code
    spend and OpenRouter credit usage stay separate figures, not merged into
    one, since they're tracked and shown as distinct things everywhere else.
    Sorted busiest first by tokens.

    This replaces about thirty lines of hand-rolled bucketing with a GROUP BY.
    """
    rows = conn.execute(
        """
        SELECT user_id,
               MAX(user_name)                                          AS user_name,
               COUNT(*)       FILTER (WHERE ts > :since)                AS calls,
               COALESCE(SUM(input_tokens + output_tokens)
                            FILTER (WHERE ts > :since), 0)              AS total_tokens,
               COALESCE(SUM(cost_usd) FILTER (WHERE source = 'claude-code'), 0) AS cc_cost,
               COALESCE(SUM(cost_usd) FILTER (WHERE source = 'openrouter'), 0)  AS or_cost,
               COALESCE(SUM(cost_usd) FILTER (WHERE source = 'groq'), 0)        AS groq_cost
        FROM calls
        GROUP BY user_id
        """,
        {"since": since},
    ).fetchall()

    out = []
    for row in rows:
        user_id = row["user_id"]
        cc = row["cc_cost"] + balances.get(("claude-code", user_id), 0.0)
        orc = row["or_cost"] + balances.get(("openrouter", user_id), 0.0)
        groq = row["groq_cost"] + balances.get(("groq", user_id), 0.0)
        # A user with no activity in the window and no spend at all has
        # nothing to show; keep them out rather than listing an empty row.
        if not row["calls"] and not cc and not orc and not groq:
            continue
        out.append(
            {
                "user_name": row["user_name"] or "Unknown",
                "calls": row["calls"],
                "total_tokens": row["total_tokens"],
                "claude_code_cost_usd": round(cc, 4),
                "openrouter_cost_usd": round(orc, 4),
                "groq_cost_usd": round(groq, 4),
            }
        )
    out.sort(key=lambda r: r["total_tokens"], reverse=True)
    return out


def snapshot() -> dict:
    """A plain-dict view of current stats, ready to serialize to JSON. Same
    shape it has always had, so llm_status_server.py needs no changes."""
    since = time.time() - _WINDOW_S
    try:
        with db.read() as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*)                          AS total_calls,
                       COALESCE(SUM(input_tokens),  0)   AS total_input_tokens,
                       COALESCE(SUM(output_tokens), 0)   AS total_output_tokens,
                       AVG(CASE WHEN duration_s > 0 AND output_tokens > 0
                                THEN output_tokens / duration_s END)  AS avg_tps
                FROM calls WHERE ts > ?
                """,
                (since,),
            ).fetchone()

            recent = conn.execute(
                "SELECT * FROM calls ORDER BY ts DESC LIMIT ?", (_SNAPSHOT_ROWS,)
            ).fetchall()

            balances = _opening_balances(conn)
            spend = {
                row["source"]: row["total"]
                for row in conn.execute("SELECT source, SUM(cost_usd) AS total FROM calls GROUP BY source")
            }
            by_user = _by_user(conn, since, balances)
    except Exception:
        log.exception("Failed to read metrics")
        return _empty_snapshot()

    claude_code_cost = (spend.get("claude-code") or 0.0) + balances.get(("claude-code", None), 0.0)
    openrouter_cost = (spend.get("openrouter") or 0.0) + balances.get(("openrouter", None), 0.0)
    groq_cost = (spend.get("groq") or 0.0) + balances.get(("groq", None), 0.0)
    total_input = totals["total_input_tokens"]
    total_output = totals["total_output_tokens"]

    return {
        "uptime_s": round(time.monotonic() - _START_TIME, 1),
        "total_calls": totals["total_calls"],
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "avg_tokens_per_sec": round(totals["avg_tps"] or 0.0, 1),
        # recent is newest-first out of SQL, which is the order the
        # dashboard wants; `last` is simply the newest of them.
        "last": _row_dict(recent[0]) if recent else None,
        "recent": [_row_dict(r) for r in recent],
        "claude_code_cost_usd": round(claude_code_cost, 4),
        "openrouter_cost_usd": round(openrouter_cost, 4),
        "groq_cost_usd": round(groq_cost, 4),
        "by_user": by_user,
    }


def _empty_snapshot() -> dict:
    """Served when the database can't be read, so one bad query renders an
    empty dashboard instead of failing the whole poll."""
    return {
        "uptime_s": round(time.monotonic() - _START_TIME, 1),
        "total_calls": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "avg_tokens_per_sec": 0.0,
        "last": None,
        "recent": [],
        "claude_code_cost_usd": 0.0,
        "openrouter_cost_usd": 0.0,
        "groq_cost_usd": 0.0,
        "by_user": [],
    }
