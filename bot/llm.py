"""
OpenRouter chat wrapper: per-channel history plus a tool-calling loop.
"""

import json
import logging
from collections import defaultdict, deque
from datetime import datetime

import requests

from . import config
from .tools import ToolContext, dispatch, get_tool_schemas

log = logging.getLogger("discord-llm-bot.llm")

# channel_id -> deque of {"role": ..., "content": ...} dicts
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=config.HISTORY_TURNS * 2))

# Safety cap on tool-call round trips per user message, in case the model
# gets stuck calling tools instead of answering.
MAX_TOOL_ITERATIONS = 5


def call_openrouter(messages: list[dict], tools: list[dict] | None = None, timeout: int = 60) -> dict:
    """Sends one chat completion request and returns the response message
    (a dict with "role", "content", and possibly "tool_calls")."""
    payload = {"model": config.OPENROUTER_MODEL, "messages": messages}
    if tools:
        payload["tools"] = tools
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
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def _system_prompt() -> str:
    now_local = datetime.now(config.LOCAL_TZ)
    return (
        f"{config.SYSTEM_PROMPT}\n\n"
        f"Current local date/time: {now_local:%A, %Y-%m-%d %H:%M} ({config.TIMEZONE})."
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

    for _ in range(MAX_TOOL_ITERATIONS):
        reply_message = call_openrouter(messages, tools=tool_schemas)
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
