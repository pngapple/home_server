"""
In-memory metrics for LLM calls, fed by both the OpenRouter chat path
(llm.py) and the Claude Code bridge (claude_bridge.py). Consumed by
llm_status_server.py for the /llm/ dashboard.

Lost on restart — same tradeoff as the chat history in llm.py, and fine for
the same reason: this is an at-a-glance dashboard, not an audit log.
"""

import time
from collections import deque
from dataclasses import dataclass, field

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


def record(
    source: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_s: float,
    context_window: int | None = None,
) -> None:
    _recent.append(Call(source, model, input_tokens, output_tokens, duration_s, context_window))


def snapshot() -> dict:
    """A plain-dict view of current stats, ready to serialize to JSON."""
    calls = list(_recent)
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
    }
