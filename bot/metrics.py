"""
Metrics for LLM calls, fed by both the OpenRouter chat path (llm.py) and the
Claude Code bridge (claude_bridge.py). Consumed by llm_status_server.py for
the /llm/ dashboard.

The call history (and therefore total tokens/calls/avg tok-s, all derived
from it) and cumulative Claude Code spend persist to disk across restarts —
see _load()/_save() below. Uptime deliberately does NOT persist: it's meant
to show time since this process last started, which is useful signal in
itself (a suspiciously low uptime means the bot crashed/restarted
recently) — carrying it over would hide that.

Every recording path goes through record_many(), which takes the file lock
once and writes once: a single Claude Code turn can report several models
plus a cost, and that shouldn't mean several full rewrites of the file.
record_many() blocks on disk, so async callers should hand it to a thread.
"""

import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from . import config, jsonstore

log = logging.getLogger("discord-llm-bot.metrics")

_START_TIME = time.monotonic()
_MAX_RECENT = 200
# How many of _recent the dashboard shows; also the chart's bar count.
_SNAPSHOT_ROWS = 25


@dataclass
class Call:
    source: str  # "openrouter" or "claude-code"
    model: str
    input_tokens: int
    output_tokens: int
    duration_s: float
    context_window: int | None = None
    # Who triggered this call, snapshotted at record time (see
    # discord_client.display_name) rather than resolved fresh per dashboard
    # request — cheaper, and survives a user later leaving the server.
    # Both are None for calls recorded before this field existed (old
    # entries reloaded from disk) and get bucketed under "Unknown" in
    # snapshot()'s by_user grouping.
    user_id: int | None = None
    user_name: str | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def tokens_per_sec(self) -> float:
        return self.output_tokens / self.duration_s if self.duration_s > 0 else 0.0

    def as_dict(self) -> dict:
        """The dashboard-facing shape: stored fields plus the derived ones,
        rounded for display."""
        return {
            "source": self.source,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "duration_s": round(self.duration_s, 2),
            "tokens_per_sec": round(self.tokens_per_sec, 1),
            "context_window": self.context_window,
            "user_name": self.user_name,
            "timestamp": self.timestamp,
        }


_recent: deque[Call] = deque(maxlen=_MAX_RECENT)

# Claude Code doesn't expose a remaining-balance/quota endpoint the way
# OpenRouter does (see llm_status_server.py's live credits fetch) — this is
# just accumulated `total_cost_usd` from each headless invocation's JSON
# output, so "spend so far", not "spend vs. a known limit".
_claude_code_cost_usd = 0.0

# OpenRouter's own credits tile is account-wide (see llm_status_server.py's
# live key-info fetch), with no per-user breakdown — this is our own running
# total from each call's self-reported `usage.cost` (see llm.py's
# `usage: {"include": true}` request flag), the only way to get one.
_openrouter_cost_usd = 0.0

# Same two cumulative totals, broken out per Discord user id (as a string,
# since it round-trips through JSON object keys) — unlike the token/call
# counts in snapshot()'s by_user grouping, these aren't windowed to _recent,
# since a paid call should never quietly drop out of someone's running total
# just because 200 newer calls (from anyone) pushed it out of the deque.
_claude_code_cost_by_user: dict[str, float] = {}
_openrouter_cost_by_user: dict[str, float] = {}


def _load() -> None:
    global _claude_code_cost_usd, _openrouter_cost_usd
    data = jsonstore.read(config.LLM_METRICS_FILE, {})
    for entry in data.get("recent", []):
        # This module is imported at startup, so one malformed/outdated entry
        # must not take the whole bot down with it.
        try:
            _recent.append(Call(**entry))
        except TypeError:
            log.warning("Skipping unreadable metrics entry: %r", entry)
    _claude_code_cost_usd = data.get("claude_code_cost_usd", 0.0)
    _openrouter_cost_usd = data.get("openrouter_cost_usd", 0.0)
    _claude_code_cost_by_user.update(data.get("claude_code_cost_by_user", {}))
    _openrouter_cost_by_user.update(data.get("openrouter_cost_by_user", {}))


def _save() -> None:
    """Caller must hold the metrics file lock."""
    try:
        jsonstore.write(
            config.LLM_METRICS_FILE,
            {
                "recent": [asdict(c) for c in _recent],
                "claude_code_cost_usd": _claude_code_cost_usd,
                "openrouter_cost_usd": _openrouter_cost_usd,
                "claude_code_cost_by_user": _claude_code_cost_by_user,
                "openrouter_cost_by_user": _openrouter_cost_by_user,
            },
            indent=None,
        )
    except OSError:
        log.exception("Failed to write %s", config.LLM_METRICS_FILE)


_load()


def record_many(
    calls: Iterable[Call] = (), cost_usd: float = 0.0, user_id: int | None = None, source: str | None = None
) -> None:
    """Append calls and/or add to cumulative spend, in one write. `source`
    ("openrouter" or "claude-code") picks which cumulative total/by-user dict
    `cost_usd` adds to; both are no-ops if `cost_usd` is 0, so callers with
    nothing to attribute (or an unrecognized/omitted source) can leave it out."""
    global _claude_code_cost_usd, _openrouter_cost_usd
    with jsonstore.lock(config.LLM_METRICS_FILE):
        _recent.extend(calls)
        if cost_usd and source == "claude-code":
            _claude_code_cost_usd += cost_usd
            if user_id is not None:
                key = str(user_id)
                _claude_code_cost_by_user[key] = _claude_code_cost_by_user.get(key, 0.0) + cost_usd
        elif cost_usd and source == "openrouter":
            _openrouter_cost_usd += cost_usd
            if user_id is not None:
                key = str(user_id)
                _openrouter_cost_by_user[key] = _openrouter_cost_by_user.get(key, 0.0) + cost_usd
        _save()


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


def _by_user(
    calls: list[Call], claude_code_cost_by_user: dict[str, float], openrouter_cost_by_user: dict[str, float]
) -> list[dict]:
    """Per-user breakdown: call/token counts over the same _recent window as
    the rest of the dashboard, plus each user's all-time spend, broken out
    the same way the top-level tiles keep it — Claude Code spend and
    OpenRouter credit usage stay separate figures, not merged into one,
    since they're tracked (and shown) as distinct things everywhere else on
    this dashboard. Sorted busiest first by tokens."""
    buckets: dict[int | None, dict] = {}
    for c in calls:
        bucket = buckets.setdefault(c.user_id, {"user_name": c.user_name, "calls": 0, "total_tokens": 0})
        bucket["user_name"] = c.user_name or bucket["user_name"]  # keep the freshest known name
        bucket["calls"] += 1
        bucket["total_tokens"] += c.input_tokens + c.output_tokens

    rows = []
    for user_id, bucket in buckets.items():
        key = str(user_id) if user_id is not None else None
        rows.append(
            {
                "user_name": bucket["user_name"] or "Unknown",
                "calls": bucket["calls"],
                "total_tokens": bucket["total_tokens"],
                "claude_code_cost_usd": round(claude_code_cost_by_user.get(key, 0.0), 4) if key else 0.0,
                "openrouter_cost_usd": round(openrouter_cost_by_user.get(key, 0.0), 4) if key else 0.0,
            }
        )
    rows.sort(key=lambda r: r["total_tokens"], reverse=True)
    return rows


def snapshot() -> dict:
    """A plain-dict view of current stats, ready to serialize to JSON."""
    with jsonstore.lock(config.LLM_METRICS_FILE):
        calls = list(_recent)
        claude_code_cost = _claude_code_cost_usd
        openrouter_cost = _openrouter_cost_usd
        claude_code_cost_by_user = dict(_claude_code_cost_by_user)
        openrouter_cost_by_user = dict(_openrouter_cost_by_user)

    total_input = sum(c.input_tokens for c in calls)
    total_output = sum(c.output_tokens for c in calls)

    # Average throughput across recent calls that actually produced output —
    # a call with 0 output tokens (e.g. a tool-only round trip) would just
    # dilute the rate toward zero without saying anything about model speed.
    rated = [c for c in calls if c.duration_s > 0 and c.output_tokens > 0]
    avg_tps = sum(c.tokens_per_sec for c in rated) / len(rated) if rated else 0.0

    return {
        "uptime_s": round(time.monotonic() - _START_TIME, 1),
        "total_calls": len(calls),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "avg_tokens_per_sec": round(avg_tps, 1),
        "last": calls[-1].as_dict() if calls else None,
        "recent": [c.as_dict() for c in reversed(calls[-_SNAPSHOT_ROWS:])],
        "claude_code_cost_usd": round(claude_code_cost, 4),
        "openrouter_cost_usd": round(openrouter_cost, 4),
        "by_user": _by_user(calls, claude_code_cost_by_user, openrouter_cost_by_user),
    }
