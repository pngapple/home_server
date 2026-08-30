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
"""

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field

from . import config, jsonstore

log = logging.getLogger("discord-llm-bot.metrics")

_START_TIME = time.monotonic()
_MAX_RECENT = 200


@dataclass
class Call:
    source: str  # "openrouter" or "claude-code"
    model: str
    input_tokens: int
    output_tokens: int
    duration_s: float
    context_window: int | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def tokens_per_sec(self) -> float:
        return self.output_tokens / self.duration_s if self.duration_s > 0 else 0.0


_recent: deque[Call] = deque(maxlen=_MAX_RECENT)

# Claude Code doesn't expose a remaining-balance/quota endpoint the way
# OpenRouter does (see llm_status_server.py's live credits fetch) — this is
# just accumulated `total_cost_usd` from each headless invocation's JSON
# output, so "spend so far", not "spend vs. a known limit".
_claude_code_cost_usd = 0.0


def _load() -> None:
    global _claude_code_cost_usd
    try:
        with open(config.LLM_METRICS_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (json.JSONDecodeError, OSError):
        log.exception("Failed to read %s, starting with empty metrics", config.LLM_METRICS_FILE)
        return
    for c in data.get("recent", []):
        # This module is imported at startup, so one malformed/outdated entry
        # must not take the whole bot down with it.
        try:
            _recent.append(Call(**c))
        except TypeError:
            log.warning("Skipping unreadable metrics entry: %r", c)
    _claude_code_cost_usd = data.get("claude_code_cost_usd", 0.0)


def _save() -> None:
    data = {
        "recent": [asdict(c) for c in _recent],
        "claude_code_cost_usd": _claude_code_cost_usd,
    }
    try:
        jsonstore.write(config.LLM_METRICS_FILE, data, indent=None)
    except OSError:
        log.exception("Failed to write %s", config.LLM_METRICS_FILE)


_load()


def add_claude_code_cost(cost_usd: float) -> None:
    global _claude_code_cost_usd
    with jsonstore.lock(config.LLM_METRICS_FILE):
        _claude_code_cost_usd += cost_usd
        _save()


def record(
    source: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_s: float,
    context_window: int | None = None,
) -> None:
    with jsonstore.lock(config.LLM_METRICS_FILE):
        _recent.append(Call(source, model, input_tokens, output_tokens, duration_s, context_window))
        _save()


def snapshot() -> dict:
    """A plain-dict view of current stats, ready to serialize to JSON."""
    with jsonstore.lock(config.LLM_METRICS_FILE):
        calls = list(_recent)
        claude_code_cost = _claude_code_cost_usd
    total_input = sum(c.input_tokens for c in calls)
    total_output = sum(c.output_tokens for c in calls)

    # Average throughput across recent calls that actually produced output —
    # a call with 0 output tokens (e.g. a tool-only round trip) would just
    # dilute the rate toward zero without saying anything about model speed.
    rated = [c for c in calls if c.duration_s > 0 and c.output_tokens > 0]
    avg_tps = sum(c.tokens_per_sec for c in rated) / len(rated) if rated else 0.0

    last = calls[-1] if calls else None

    def call_dict(c: Call) -> dict:
        return {
            "source": c.source,
            "model": c.model,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
            "total_tokens": c.input_tokens + c.output_tokens,
            "duration_s": round(c.duration_s, 2),
            "tokens_per_sec": round(c.tokens_per_sec, 1),
            "context_window": c.context_window,
            "timestamp": c.timestamp,
        }

    return {
        "uptime_s": round(time.monotonic() - _START_TIME, 1),
        "total_calls": len(calls),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "avg_tokens_per_sec": round(avg_tps, 1),
        "last": call_dict(last) if last else None,
        "recent": [call_dict(c) for c in reversed(calls[-25:])],
        "claude_code_cost_usd": round(claude_code_cost, 4),
    }
