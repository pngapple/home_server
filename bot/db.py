"""
The SQLite connection behind metrics.py.

Metrics are the one thing in this repo that outgrew a JSON file. The lists,
reminders and cigarette logs are a couple of kilobytes written when someone
asks for something — jsonstore's atomic write and per-file lock is the right
tool, and being able to `cat todos.json` over SSH is worth keeping. Metrics
were different on three counts: the file was rewritten in full (48 KB) on
every single LLM call, a 200-entry cap meant older history was gone rather
than merely unshown, and per-user spend had to be tracked in parallel
dictionaries outside that window because it couldn't be windowed correctly.

One connection for the process, guarded by one lock — the same discipline
jsonstore already applies per file, so nothing about the threading model
changes: metrics are written from worker threads (llm.py) and from the
event loop via asyncio.to_thread (claude_bridge.py), and read by the
dashboard handler.

The pragmas matter on a Pi. WAL lets the dashboard's 4-second poll read
while a call is being written instead of blocking on it, and under WAL
synchronous=NORMAL trades only the very last commit on a power cut for a
large reduction in SD-card writes.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager

from . import config

log = logging.getLogger("discord-llm-bot.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY,
    ts             REAL    NOT NULL,
    source         TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    duration_s     REAL    NOT NULL,
    context_window INTEGER,
    user_id        INTEGER,
    user_name      TEXT,
    cost_usd       REAL    NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS calls_ts   ON calls (ts DESC);
CREATE INDEX IF NOT EXISTS calls_user ON calls (user_id, ts DESC);

-- Spend from before the migration, which the JSON file only ever kept in
-- aggregate. Backfilled rows carry no per-call cost, so seeding invented
-- per-row figures would make the calls table lie; this keeps the totals
-- correct without doing that. user_id NULL is the account-wide total.
CREATE TABLE IF NOT EXISTS opening_balance (
    source   TEXT NOT NULL,
    user_id  INTEGER,
    cost_usd REAL NOT NULL,
    PRIMARY KEY (source, user_id)
);

-- One row per user turn: what was asked, what the bot decided to do about
-- it, and how that turned out. The `calls` table above answers "what did
-- this cost"; this one answers "did it work", which is what the reflection
-- pass reads. See episodes.py.
CREATE TABLE IF NOT EXISTS episodes (
    id           INTEGER PRIMARY KEY,
    ts           REAL    NOT NULL,
    -- The Discord ids for the user's message and our reply to it. reply_id
    -- is filled in after the fact (app.py only learns it once the reply is
    -- actually sent) and is what a 👍/👎 reaction is resolved through.
    message_id   INTEGER,
    reply_id     INTEGER,
    channel_id   INTEGER,
    user_id      INTEGER,
    user_name    TEXT,
    user_text    TEXT    NOT NULL,
    outcome      TEXT    NOT NULL,
    detail       TEXT,
    model        TEXT,
    iterations   INTEGER NOT NULL DEFAULT 0,
    duration_s   REAL    NOT NULL DEFAULT 0.0,
    -- JSON array of {name, ok, error} — the decision trace for the turn.
    tools_called TEXT    NOT NULL DEFAULT '[]',
    reply        TEXT,
    -- -1/+1 once someone reacts, NULL while unlabelled. feedback_source
    -- says where the label came from ("reaction" or "rephrase") — an
    -- inferred signal is much weaker than a deliberate 👎 and the two must
    -- stay tellable apart, both for weighting and for judging whether the
    -- rephrase heuristic earns its keep.
    feedback        INTEGER,
    feedback_source TEXT
);
CREATE INDEX IF NOT EXISTS episodes_ts      ON episodes (ts DESC);
CREATE INDEX IF NOT EXISTS episodes_outcome ON episodes (outcome, ts DESC);
CREATE INDEX IF NOT EXISTS episodes_reply   ON episodes (reply_id);
CREATE INDEX IF NOT EXISTS episodes_message ON episodes (message_id);
"""

# Columns added after a database was first created. CREATE TABLE IF NOT
# EXISTS covers a new table but silently does nothing for an existing one
# with an older shape, so anything added to a live table has to be listed
# here too. Keep both in step: the schema above is what a fresh database
# gets, this is what an old one is brought up to.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "episodes": {"feedback_source": "TEXT"},
}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def connect(path: str | None = None) -> sqlite3.Connection:
    """The process-wide connection, opened and migrated on first use."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(path or config.METRICS_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current column set. Idempotent,
    and cheap enough to run on every open — PRAGMA table_info is a read of
    already-parsed schema, not a table scan."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table isn't there yet; _SCHEMA just created it in full
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("Added column %s.%s", table, name)


@contextmanager
def write():
    """A transaction. Commits on clean exit, rolls back on an exception, so
    a half-recorded turn never lands."""
    conn = connect()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def read():
    """A read, serialized against writers the same way jsonstore.lock() is."""
    conn = connect()
    with _lock:
        yield conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
