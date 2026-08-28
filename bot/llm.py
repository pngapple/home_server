"""
OpenRouter chat wrapper: per-channel history plus a tool-calling loop.
"""

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

import requests

from . import config, metrics
from .tools import ToolContext, dispatch, get_tool_schemas

log = logging.getLogger("discord-llm-bot.llm")

# channel_id -> deque of {"role": ..., "content": ...} dicts
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=config.HISTORY_TURNS * 2))

# Safety cap on tool-call round trips per user message, in case the model
# gets stuck calling tools instead of answering.
MAX_TOOL_ITERATIONS = 5

# model slug -> context length, lazily fetched from OpenRouter's public
# model catalog and cached for the life of the process (context windows
# don't change at runtime, so there's no need to ever refetch).
_context_windows: dict[str, int] = {}


def _context_window(model: str) -> int | None:
    if model in _context_windows:
        return _context_windows[model]
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        resp.raise_for_status()
        for entry in resp.json().get("data", []):
            length = entry.get("context_length")
            if entry.get("id") and length:
                _context_windows[entry["id"]] = length
    except Exception:
        log.warning("Failed to fetch OpenRouter model catalog for context-window lookup", exc_info=True)
        return None
    return _context_windows.get(model)


def call_openrouter(
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    timeout: int = 60,
) -> dict:
    """Sends one chat completion request and returns the response message
    (a dict with "role", "content", and possibly "tool_calls")."""
    payload = {"model": config.OPENROUTER_MODEL, "messages": messages}
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    start = time.monotonic()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Optional but recommended by OpenRouter for attribution/rate-limit purposes:
            "X-Title": "home-server-discord-bot",
        },
        json=payload,
        timeout=timeout,
    )
    duration_s = time.monotonic() - start
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage") or {}
    model = data.get("model", config.OPENROUTER_MODEL)
    metrics.record(
        source="openrouter",
        model=model,
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        duration_s=duration_s,
        context_window=_context_window(model),
    )
    return data["choices"][0]["message"]


def _system_prompt() -> str:
    now_local = datetime.now(config.LOCAL_TZ)
    # Small/fast models are unreliable at mental date arithmetic (e.g.
    # miscounting "next Monday" across a month boundary). Handing over a
    # precomputed lookup table turns that into a lookup instead of a
    # calculation, which is much more reliable.
    upcoming_dates = "\n".join(
        f"{(now_local + timedelta(days=i)):%A, %Y-%m-%d}" + (" (today)" if i == 0 else "")
        for i in range(14)
    )
    return (
        f"{config.SYSTEM_PROMPT}\n\n"
        f"Current local date/time: {now_local:%A, %Y-%m-%d %H:%M} ({config.TIMEZONE}).\n\n"
        f"Upcoming dates for reference (use these directly instead of "
        f"calculating weekdays yourself):\n{upcoming_dates}"
    )


def ask_llm(message, user_text: str) -> str:
    """Runs the chat + tool-calling loop for one user message and returns
    the final natural-language reply. `message` is the discord.Message that
    triggered this, passed through to tool handlers as context."""
    channel_id = message.channel.id
    ctx = ToolContext(message=message)
    tool_schemas = get_tool_schemas()

    messages = [{"role": "system", "content": _system_prompt()}]
    messages.extend(history[channel_id])
    messages.append({"role": "user", "content": user_text})

    for i in range(MAX_TOOL_ITERATIONS):
        # Force the first decision on a fresh message through actual
        # tool-calling (real tool or the no_action_needed no-op) instead of
        # letting the model silently free-text a claimed result. Once that
        # decision's been made, later turns just need to wrap up in plain
        # text, so let those be unconstrained.
        tool_choice = "required" if i == 0 else "auto"
        reply_message = call_openrouter(messages, tools=tool_schemas, tool_choice=tool_choice)
        tool_calls = reply_message.get("tool_calls")

        if not tool_calls:
            reply = reply_message.get("content") or ""
            history[channel_id].append({"role": "user", "content": user_text})
            history[channel_id].append({"role": "assistant", "content": reply})
            return reply

        messages.append(reply_message)
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                log.warning("Bad tool arguments JSON from model for %s: %r", name, call["function"]["arguments"])
                arguments = {}
            log.info("Tool call: %s(%r)", name, arguments)
            result = dispatch(name, arguments, ctx)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    log.warning("Hit max tool iterations (%d) for channel %s", MAX_TOOL_ITERATIONS, channel_id)
    return "Sorry, I got stuck juggling tools on that one — try rephrasing?"
