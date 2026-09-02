"""
OpenRouter chat wrapper: per-user history plus a tool-calling loop.

Runs entirely in a worker thread (app.py hands ask_llm to
asyncio.to_thread), which is why this module uses blocking `requests`
rather than the shared aiohttp session the loop-side code uses.
"""

import json
import logging
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta

import requests

from . import config, metrics
from .discord_client import display_name
from .tools import ToolContext, dispatch, get_tool_schemas

log = logging.getLogger("discord-llm-bot.llm")

_API_BASE = "https://openrouter.ai/api/v1"

# Safety cap on tool-call round trips per user message, in case the model
# gets stuck calling tools instead of answering.
MAX_TOOL_ITERATIONS = 5

# Retry once on the transient failures OpenRouter actually produces (an
# upstream 5xx, a rate-limited 429, a dropped connection) before giving the
# user an error — those are common enough that one retry turns most of them
# into a normal reply.
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_S = 1.5

# Cap how many (channel, user) histories we keep, so a bot sitting in a busy
# server doesn't accumulate a deque per conversation forever.
# Least-recently-used conversations fall off the end.
_MAX_HISTORIES = 64

# (channel_id, user_id) -> deque of {"role": ..., "content": ...} dicts.
# Keyed per-user, not just per-channel, so two people talking to the bot in
# the same server channel each get their own conversation instead of the
# model seeing their messages interleaved as if from one person — that used
# to make it e.g. mix up whose room's lights were being asked about.
_history: OrderedDict[tuple[int, int], deque] = OrderedDict()

# One session, so repeated completions reuse the TCP/TLS connection instead
# of re-handshaking per message.
_session = requests.Session()

# model slug -> context length, lazily fetched from OpenRouter's public
# model catalog and cached for the life of the process (context windows
# don't change at runtime, so there's no need to ever refetch).
_context_windows: dict[str, int] = {}
_catalog_fetched_at: float | None = None
# Only retry a failed catalog fetch this often: a model missing from the
# catalog (e.g. metrics recorded under "unknown") must not re-download the
# whole thing on every single chat completion.
_CATALOG_RETRY_S = 300.0


def history_for(channel_id: int, user_id: int) -> deque:
    """This user's rolling history within this channel, creating it (and
    evicting the least-recently-used conversation) as needed."""
    key = (channel_id, user_id)
    existing = _history.get(key)
    if existing is None:
        existing = deque(maxlen=config.HISTORY_TURNS * 2)
        _history[key] = existing
        while len(_history) > _MAX_HISTORIES:
            _history.popitem(last=False)
    _history.move_to_end(key)
    return existing


def _context_window(model: str) -> int | None:
    global _catalog_fetched_at
    now = time.monotonic()
    if _catalog_fetched_at is None or (not _context_windows and now - _catalog_fetched_at > _CATALOG_RETRY_S):
        _catalog_fetched_at = now
        try:
            resp = _session.get(f"{_API_BASE}/models", timeout=10)
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
    user_id: int | None = None,
    user_name: str | None = None,
) -> dict:
    """Sends one chat completion request and returns the response message
    (a dict with "role", "content", and possibly "tool_calls"). `user_id`/
    `user_name` attribute the resulting metrics.record() call to whoever
    sent the message that triggered this — see llm_status_server.py's
    per-user table."""
    payload: dict = {
        "model": config.OPENROUTER_MODEL,
        "messages": messages,
        # OpenRouter omits per-call cost from `usage` unless asked — this is
        # the only way to get a per-user credit-usage figure, since the
        # account-wide key-info endpoint (llm_status_server.py's live
        # credits tile) has no per-user breakdown at all.
        "usage": {"include": True},
        # Some providers default an unset max_tokens to the model's full
        # context length instead of clamping to what's left after the
        # input, which 400s every request — see config.OPENROUTER_MAX_TOKENS.
        "max_tokens": config.OPENROUTER_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    provider: dict = {}
    if config.OPENROUTER_ZDR:
        provider["zdr"] = True
    if tool_choice == "required":
        # Some providers OpenRouter routes to silently ignore an unsupported
        # tool_choice and fall back to "auto" instead of erroring — which let
        # a forced first-turn call return plain text claiming an action
        # happened when no tool was ever invoked (see the kasa desk-lights
        # incident). require_parameters restricts routing to providers that
        # actually honor every parameter in the request, so a forced tool
        # call either really happens or the request fails loudly.
        provider["require_parameters"] = True
    if provider:
        payload["provider"] = provider

    start = time.monotonic()
    resp = _post_with_retry(payload, timeout)
    duration_s = time.monotonic() - start

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
        user_id=user_id,
        user_name=user_name,
        cost_usd=usage.get("cost") or 0.0,
    )
    return data["choices"][0]["message"]


def _post_with_retry(payload: dict, timeout: int) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # Optional but recommended by OpenRouter for attribution/rate-limit purposes:
        "X-Title": "home-server-discord-bot",
    }
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        last = attempt == _RETRY_ATTEMPTS
        try:
            resp = _session.post(f"{_API_BASE}/chat/completions", headers=headers, json=payload, timeout=timeout)
        except requests.RequestException:
            if last:
                raise
            log.warning("OpenRouter request failed (attempt %d), retrying", attempt, exc_info=True)
        else:
            if last or resp.status_code not in _RETRY_STATUSES:
                resp.raise_for_status()
                return resp
            log.warning("OpenRouter returned HTTP %s (attempt %d), retrying", resp.status_code, attempt)
        time.sleep(_RETRY_BACKOFF_S * attempt)
    raise AssertionError("unreachable")  # the final attempt always returns or raises


def _system_prompt() -> str:
    now_local = datetime.now(config.LOCAL_TZ)
    # Small/fast models are unreliable at mental date arithmetic (e.g.
    # miscounting "next Monday" across a month boundary). Handing over a
    # precomputed lookup table turns that into a lookup instead of a
    # calculation, which is much more reliable.
    upcoming_dates = "\n".join(
        f"{(now_local + timedelta(days=i)):%A, %Y-%m-%d}" + (" (today)" if i == 0 else "") for i in range(14)
    )
    return (
        f"{config.SYSTEM_PROMPT}\n\n"
        f"Current local date/time: {now_local:%A, %Y-%m-%d %H:%M} ({config.TIMEZONE}).\n\n"
        f"Upcoming dates for reference (use these directly instead of "
        f"calculating weekdays yourself):\n{upcoming_dates}"
    )


def _run_tool_calls(tool_calls: list[dict], ctx: ToolContext) -> list[dict]:
    results = []
    for call in tool_calls:
        name = call["function"]["name"]
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            log.warning("Bad tool arguments JSON from model for %s: %r", name, call["function"]["arguments"])
            arguments = {}
        if not isinstance(arguments, dict):
            log.warning("Non-object tool arguments from model for %s: %r", name, arguments)
            arguments = {}
        log.info("Tool call: %s(%r)", name, arguments)
        results.append(
            {"role": "tool", "tool_call_id": call["id"], "content": dispatch(name, arguments, ctx)}
        )
    return results


def ask_llm(message, user_text: str, roles: frozenset[str] = frozenset()) -> str:
    """Runs the chat + tool-calling loop for one user message and returns
    the final natural-language reply. `message` is the discord.Message that
    triggered this, passed through to tool handlers as context. `roles` is
    the author's Discord role names (see permissions.resolve_roles),
    checked by any role-gated tool (see tools/__init__.py's required_role)."""
    ctx = ToolContext(message=message, roles=roles)
    user_id, user_name = ctx.user_id, display_name(message.author)
    conversation_history = history_for(ctx.channel_id, user_id)
    tool_schemas = get_tool_schemas(ctx.user_id, ctx.roles)

    messages = [{"role": "system", "content": _system_prompt()}]
    messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_text})

    for i in range(MAX_TOOL_ITERATIONS):
        # Force the first decision on a fresh message through actual
        # tool-calling (real tool or the no_action_needed no-op) instead of
        # letting the model silently free-text a claimed result. Once that
        # decision's been made, later turns just need to wrap up in plain
        # text, so let those be unconstrained.
        forced = i == 0
        reply_message = call_openrouter(
            messages,
            tools=tool_schemas,
            tool_choice="required" if forced else "auto",
            user_id=user_id,
            user_name=user_name,
        )
        tool_calls = reply_message.get("tool_calls")

        if forced and not tool_calls:
            # require_parameters (see call_openrouter) should keep OpenRouter
            # from routing this to a provider that ignores tool_choice, but
            # that's been observed to fail anyway — a forced first turn came
            # back with plain text confidently claiming an action happened
            # when no tool was ever called (the desk-lights incident). Retry
            # once before trusting it; if it happens twice in a row, refuse
            # to relay what would be an unverified claim.
            log.warning("Forced tool_choice returned no tool_calls; retrying once")
            reply_message = call_openrouter(
                messages, tools=tool_schemas, tool_choice="required", user_id=user_id, user_name=user_name
            )
            tool_calls = reply_message.get("tool_calls")
            if not tool_calls:
                log.error("Model ignored forced tool_choice twice for channel %s", ctx.channel_id)
                return "Sorry, something went wrong confirming that — can you try again?"

        if not tool_calls:
            # An empty content field would otherwise make the bot silently
            # not reply at all (app.chunk("") yields nothing).
            reply = reply_message.get("content") or "(no reply)"
            conversation_history.append({"role": "user", "content": user_text})
            conversation_history.append({"role": "assistant", "content": reply})
            return reply

        messages.append(reply_message)
        messages.extend(_run_tool_calls(tool_calls, ctx))

    log.warning("Hit max tool iterations (%d) for channel %s", MAX_TOOL_ITERATIONS, ctx.channel_id)
    return "Sorry, I got stuck juggling tools on that one — try rephrasing?"
