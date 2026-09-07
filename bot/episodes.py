"""
What the bot was asked, what it decided to do, and whether that worked.

Every failure path in this bot used to end at a canned apology and a line in
journalctl: a tool raising (tools/__init__.py's dispatch), the model ignoring
a forced tool_choice twice, the tool loop hitting its iteration cap, the
whole OpenRouter call throwing. The user saw "sorry, something went wrong"
and nothing was kept, so there was no way to ask "what does this bot get
wrong, and how often?" — let alone fix it. This module is that missing
record.

It sits next to metrics.py in the same SQLite database, and the split
between them is deliberate: `calls` is one row per LLM request and answers
"what did this cost"; `episodes` is one row per *user turn* and answers "did
it work". A single turn can be several calls, and cost tells you nothing
about correctness.

Recording never raises. A telemetry problem must not turn a reply the user
was about to get into an error — every write here is wrapped, the same way
metrics.record_many is.

The forced first-turn tool call in llm.py means every turn already carries
an explicit decision (a real tool, or the no_action_needed no-op), so
`tools_called` is a genuine decision trace rather than a guess at intent.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field

from . import db

log = logging.getLogger("discord-llm-bot.episodes")

# Long messages and replies are truncated before storage. This is a record
# for diagnosis, not an archive — the reflection pass needs enough to
# recognize a failure, not the full text of a 40 KB card.
_MAX_TEXT = 4000


class Outcome:
    """How a turn ended. The failure values line up one-to-one with the
    give-up paths that previously returned a canned apology."""

    OK = "ok"
    # A tool handler raised (tools/__init__.py's dispatch caught it).
    TOOL_ERROR = "tool_error"
    # The model returned plain text on a forced tool_choice turn, twice —
    # the "desk-lights incident" shape, where it claimed an action happened
    # that never did. See llm.py.
    FORCED_TOOL_MISS = "forced_tool_miss"
    # Ran out of tool-call round trips without producing an answer.
    MAX_ITERATIONS = "max_iterations"
    # The OpenRouter call itself failed after retries.
    LLM_ERROR = "llm_error"
    # Refused by moderation.py before reaching the model.
    MODERATED = "moderated"


# The outcomes worth a human (or the reflection pass) looking at. MODERATED
# is deliberately not here: refusing a flagged message is the system working,
# not failing.
FAILURES = frozenset(
    {Outcome.TOOL_ERROR, Outcome.FORCED_TOOL_MISS, Outcome.MAX_ITERATIONS, Outcome.LLM_ERROR}
)


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…[truncated]"


@dataclass
class Recorder:
    """Accumulates one turn's trace, then writes a single row.

    Built at the top of llm.ask_llm and finished on every exit path from it,
    so a turn cannot end without being accounted for — which is the whole
    point. Mutable and single-threaded by construction: one Recorder belongs
    to one ask_llm call, which runs start to finish in one worker thread.
    """

    user_text: str
    message_id: int | None = None
    channel_id: int | None = None
    user_id: int | None = None
    user_name: str | None = None
    model: str | None = None
    iterations: int = 0
    tools: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    def tool_call(self, name: str, error: str | None = None) -> None:
        self.tools.append({"name": name, "ok": error is None, **({"error": error} if error else {})})

    def raised(self) -> bool:
        """True if any tool handler blew up this turn — the difference
        between a turn that merely refused something (a permission gate
        doing its job) and one that hit a bug."""
        return any(t.get("error", "").startswith("raised:") for t in self.tools)

    def finish(self, outcome: str, reply: str | None = None, detail: str | None = None) -> None:
        record(
            user_text=self.user_text,
            outcome=outcome,
            message_id=self.message_id,
            channel_id=self.channel_id,
            user_id=self.user_id,
            user_name=self.user_name,
            model=self.model,
            iterations=self.iterations,
            duration_s=time.monotonic() - self.started_at,
            tools_called=self.tools,
            reply=reply,
            detail=detail,
        )


def record(
    user_text: str,
    outcome: str,
    message_id: int | None = None,
    channel_id: int | None = None,
    user_id: int | None = None,
    user_name: str | None = None,
    model: str | None = None,
    iterations: int = 0,
    duration_s: float = 0.0,
    tools_called: list[dict] | None = None,
    reply: str | None = None,
    detail: str | None = None,
) -> None:
    """Append one turn. Swallows its own failures — see the module docstring."""
    try:
        with db.write() as conn:
            conn.execute(
                "INSERT INTO episodes (ts, message_id, channel_id, user_id, user_name, user_text, "
                "outcome, detail, model, iterations, duration_s, tools_called, reply) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    message_id,
                    channel_id,
                    user_id,
                    user_name,
                    _clip(user_text),
                    outcome,
                    _clip(detail),
                    model,
                    iterations,
                    duration_s,
                    json.dumps(tools_called or []),
                    _clip(reply),
                ),
            )
    except Exception:
        log.exception("Failed to record episode (outcome=%s)", outcome)


def attach_reply(message_id: int, reply_id: int) -> None:
    """Link the turn started by `message_id` to the bot message that
    answered it, so a reaction on that reply can be resolved back to the
    episode it is feedback about."""
    try:
        with db.write() as conn:
            conn.execute(
                "UPDATE episodes SET reply_id = ? WHERE message_id = ? AND reply_id IS NULL",
                (reply_id, message_id),
            )
    except Exception:
        log.exception("Failed to attach reply %s to message %s", reply_id, message_id)


# Where a feedback label came from. A deliberate 👎 is evidence; an inferred
# rephrase is a hint, and the reflection pass should weight them differently.
REACTION = "reaction"
REPHRASE = "rephrase"


def record_feedback(reply_id: int, value: int, source: str = REACTION) -> bool:
    """Label the episode whose reply is `reply_id` (+1/-1). Returns whether
    a matching episode was found, so a reaction on some other bot message is
    distinguishable from one that landed.

    A deliberate reaction overrides an inferred one, never the other way
    round — someone who thumbs-upped a reply and then happened to rephrase
    their question has not changed their mind."""
    try:
        with db.write() as conn:
            query = "UPDATE episodes SET feedback = ?, feedback_source = ? WHERE reply_id = ?"
            params: list = [value, source, reply_id]
            if source != REACTION:
                query += " AND feedback IS NULL"
            changed = conn.execute(query, params).rowcount
        return bool(changed)
    except Exception:
        log.exception("Failed to record feedback %s for reply %s", value, reply_id)
        return False


def clear_feedback(reply_id: int, value: int) -> bool:
    """Undo a reaction that was taken back. Only clears a label that still
    matches `value`, so removing a 👍 doesn't wipe a 👎 added afterwards."""
    try:
        with db.write() as conn:
            changed = conn.execute(
                "UPDATE episodes SET feedback = NULL, feedback_source = NULL "
                "WHERE reply_id = ? AND feedback = ? AND feedback_source = ?",
                (reply_id, value, REACTION),
            ).rowcount
        return bool(changed)
    except Exception:
        log.exception("Failed to clear feedback for reply %s", reply_id)
        return False


# --- the implicit signal --------------------------------------------------
#
# Most bad answers are never thumbed down; the user just asks again. That
# re-ask is the most common negative signal there is, and it's free to
# detect. Conservative on purpose — a false positive here teaches the wrong
# lesson, which is worse than missing one.

# How soon a repeat has to arrive to count as dissatisfaction rather than a
# new request that happens to share words.
REPHRASE_WINDOW_S = 90.0
# Jaccard overlap of the two messages' word sets. 0.6 rather than 0.5 for
# one concrete reason: "add milk to groceries" and "add bread to groceries"
# score exactly 0.5, so at that threshold adding two items in a row would
# file the first one as a failure. The words that differ there are the
# content ("milk"/"bread"), which is the user asking for a *different*
# thing; in a real repeat what differs is filler ("please") or spelling.
REPHRASE_OVERLAP = 0.6
# Below this many distinct words the overlap score is too unstable to trust
# ("ok" vs "no" would score 0.0, "thanks" vs "thanks!" 1.0).
_REPHRASE_MIN_WORDS = 2


def _words(text: str) -> set[str]:
    """Content words, lowercased. Short tokens are dropped so filler ("on",
    "my", "the") can't carry the similarity on its own, and apostrophes are
    stripped so "what's" and "whats" are the same word — a re-ask is very
    often the same sentence typed slightly differently."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower().replace("'", "")) if len(w) > 2}


def looks_like_rephrase(previous: str, current: str) -> bool:
    """Deliberately tuned for precision over recall. It catches a re-typed
    question, not a semantic restatement ("what's on my todo list" ->
    "show me my todos" scores zero) — word overlap can't see those, and
    guessing would file good turns as failures."""
    a, b = _words(previous), _words(current)
    if len(a) < _REPHRASE_MIN_WORDS or len(b) < _REPHRASE_MIN_WORDS:
        return False
    return len(a & b) / len(a | b) >= REPHRASE_OVERLAP


def note_possible_rephrase(user_id: int, channel_id: int, text: str) -> bool:
    """Called with each new message, before it's answered. If it reads as
    the user repeating themselves right after a reply, mark that earlier
    turn as unsatisfying.

    Only ever labels turns that recorded as `ok` — a turn that already
    failed is accounted for, and re-labelling it would double-count it."""
    try:
        with db.read() as conn:
            row = conn.execute(
                "SELECT id, ts, user_text FROM episodes WHERE user_id = ? AND channel_id = ? "
                "AND outcome = ? AND feedback IS NULL ORDER BY ts DESC LIMIT 1",
                (user_id, channel_id, Outcome.OK),
            ).fetchone()
    except Exception:
        log.exception("Failed to look up the previous turn for user %s", user_id)
        return False

    if row is None or time.time() - row["ts"] > REPHRASE_WINDOW_S:
        return False
    if not looks_like_rephrase(row["user_text"] or "", text):
        return False

    try:
        with db.write() as conn:
            conn.execute(
                "UPDATE episodes SET feedback = -1, feedback_source = ? WHERE id = ? AND feedback IS NULL",
                (REPHRASE, row["id"]),
            )
    except Exception:
        log.exception("Failed to mark episode %s as rephrased", row["id"])
        return False

    log.info("Marked episode %s as a likely miss (user re-asked within %.0fs)", row["id"], REPHRASE_WINDOW_S)
    return True


def _row(row) -> dict:
    data = dict(row)
    try:
        data["tools_called"] = json.loads(data["tools_called"] or "[]")
    except json.JSONDecodeError:
        data["tools_called"] = []
    return data


def recent(days: int = 7, outcomes: frozenset[str] | None = None, limit: int = 200) -> list[dict]:
    """Turns from the last `days`, newest first, optionally filtered to
    specific outcomes. The read side of the reflection pass."""
    since = time.time() - days * 86400
    query = "SELECT * FROM episodes WHERE ts >= ?"
    params: list = [since]
    if outcomes:
        query += f" AND outcome IN ({','.join('?' * len(outcomes))})"
        params += sorted(outcomes)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    try:
        with db.read() as conn:
            return [_row(r) for r in conn.execute(query, params)]
    except Exception:
        log.exception("Failed to read recent episodes")
        return []


def failures(days: int = 7, limit: int = 200) -> list[dict]:
    """Turns that went wrong — plus anything a human explicitly thumbed
    down, which is a failure the outcome column can't see."""
    since = time.time() - days * 86400
    try:
        with db.read() as conn:
            return [
                _row(r)
                for r in conn.execute(
                    f"SELECT * FROM episodes WHERE ts >= ? AND (outcome IN "
                    f"({','.join('?' * len(FAILURES))}) OR feedback < 0) "
                    f"ORDER BY ts DESC LIMIT ?",
                    [since, *sorted(FAILURES), limit],
                )
            ]
    except Exception:
        log.exception("Failed to read recent failures")
        return []


def summary(days: int = 7) -> dict:
    """Counts per outcome over the window, plus the feedback tally. Cheap
    enough for a dashboard tile or a periodic report."""
    since = time.time() - days * 86400
    try:
        with db.read() as conn:
            by_outcome = {
                r["outcome"]: r["n"]
                for r in conn.execute(
                    "SELECT outcome, COUNT(*) AS n FROM episodes WHERE ts >= ? GROUP BY outcome", (since,)
                )
            }
            row = conn.execute(
                "SELECT "
                "  COALESCE(SUM(feedback > 0), 0) AS up, "
                "  COALESCE(SUM(feedback < 0 AND feedback_source = ?), 0) AS down, "
                "  COALESCE(SUM(feedback < 0 AND feedback_source = ?), 0) AS inferred "
                "FROM episodes WHERE ts >= ?",
                (REACTION, REPHRASE, since),
            ).fetchone()
    except Exception:
        log.exception("Failed to summarize episodes")
        return {
            "total": 0,
            "by_outcome": {},
            "failures": 0,
            "thumbs_up": 0,
            "thumbs_down": 0,
            "inferred_misses": 0,
        }

    total = sum(by_outcome.values())
    return {
        "total": total,
        "by_outcome": by_outcome,
        "failures": sum(n for outcome, n in by_outcome.items() if outcome in FAILURES),
        "thumbs_up": row["up"],
        "thumbs_down": row["down"],
        # Turns nobody complained about, but that the user immediately
        # re-asked. Reported separately because it's a weaker signal.
        "inferred_misses": row["inferred"],
    }


def prune(retention_days: int) -> int:
    """Drop episodes older than the window, bounding the table the same way
    metrics.prune bounds `calls`. Called once at startup."""
    cutoff = time.time() - retention_days * 86400
    try:
        with db.write() as conn:
            deleted = conn.execute("DELETE FROM episodes WHERE ts < ?", (cutoff,)).rowcount
    except Exception:
        log.exception("Failed to prune old episodes")
        return 0
    if deleted:
        log.info("Pruned %d episode(s) older than %d days", deleted, retention_days)
    return deleted
