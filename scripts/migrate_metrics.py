#!/usr/bin/env python3
"""
One-shot backfill of llm_metrics.json into the SQLite metrics database.

Run once, with the service stopped:

    sudo systemctl stop discord-llm-bot
    python3 scripts/migrate_metrics.py
    sudo systemctl start discord-llm-bot

Idempotent: re-running won't duplicate anything, because it refuses to
insert calls if the table already holds rows covering the same period, and
opening balances are keyed and replaced rather than added to.

The 200 entries in the JSON's "recent" list carry no per-call cost — the old
format only ever stored cost in aggregate — so they're inserted with
cost_usd = 0.0 and the historical totals go into opening_balance instead.
Seeding invented per-row costs would make the calls table lie about where
money went; this keeps the dashboard's totals correct without doing that.

Keep llm_metrics.json on disk afterwards until the dashboard has looked
right for a day or two. It costs nothing and it's the rollback.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config, db  # noqa: E402

_SOURCES = (("claude-code", "claude_code"), ("openrouter", "openrouter"))


def main() -> int:
    path = config.LLM_METRICS_FILE
    if not os.path.exists(path):
        print(f"{path} not found — nothing to migrate. The database will start empty.")
        db.connect()
        return 0

    with open(path) as f:
        data = json.load(f)

    calls = data.get("recent", [])
    conn = db.connect()

    existing = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    if existing:
        print(f"{config.METRICS_DB} already holds {existing} call(s) — skipping the call backfill.")
    elif calls:
        conn.executemany(
            "INSERT INTO calls (ts, source, model, input_tokens, output_tokens, "
            "duration_s, context_window, user_id, user_name, cost_usd) "
            "VALUES (:timestamp, :source, :model, :input_tokens, :output_tokens, "
            ":duration_s, :context_window, :user_id, :user_name, 0.0)",
            [
                {
                    "timestamp": c.get("timestamp", 0.0),
                    "source": c.get("source", "unknown"),
                    "model": c.get("model", "unknown"),
                    "input_tokens": c.get("input_tokens", 0),
                    "output_tokens": c.get("output_tokens", 0),
                    "duration_s": c.get("duration_s", 0.0),
                    "context_window": c.get("context_window"),
                    "user_id": c.get("user_id"),
                    "user_name": c.get("user_name"),
                }
                for c in calls
            ],
        )
        print(f"Inserted {len(calls)} call(s) from {path}.")

    # Opening balances: replaced wholesale so a re-run is idempotent.
    conn.execute("DELETE FROM opening_balance")
    rows = []
    for source, key in _SOURCES:
        total = data.get(f"{key}_cost_usd", 0.0)
        if total:
            rows.append((source, None, total))
        for uid, cost in (data.get(f"{key}_cost_by_user") or {}).items():
            if cost:
                rows.append((source, int(uid), cost))
    if rows:
        conn.executemany("INSERT INTO opening_balance (source, user_id, cost_usd) VALUES (?, ?, ?)", rows)
    conn.commit()

    print(f"Recorded {len(rows)} opening balance row(s).")
    for source, _ in _SOURCES:
        # Only the account-wide row (user_id IS NULL) is the total; the
        # per-user rows are a breakdown *of* it, so summing both would
        # double-count. This mirrors how metrics.snapshot() reads them.
        total = conn.execute(
            "SELECT COALESCE((SELECT SUM(cost_usd) FROM calls WHERE source = ?), 0) "
            "     + COALESCE((SELECT SUM(cost_usd) FROM opening_balance "
            "                 WHERE source = ? AND user_id IS NULL), 0)",
            (source, source),
        ).fetchone()[0]
        print(f"  {source}: ${total:.4f} total spend carried over")

    print(f"\nDone. Database: {config.METRICS_DB}")
    print(f"Keep {path} around until the dashboard looks right, then it can be deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
