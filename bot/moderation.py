"""
Auto-moderation gate for ordinary chat requests.

Runs a cheap, structured classification call through OpenRouter (reusing the
API this bot already talks to for chat completions — see llm.py) before a
message reaches the main model. A flagged message never gets answered — it's
refused outright, and repeat offenses within config.MODERATION_STRIKE_WINDOW_DAYS
escalate to a real Discord timeout via Member.timeout() (the bot needs the
"Timeout Members" permission in the guild; DMs have no such mechanism, so
those only ever get the refusal, never a timeout).

Administrators are exempt entirely — same trust boundary permissions.is_admin
already draws for !code/!deploy/owner-only tools.

Strike counts persist across restarts in config.MODERATION_STRIKES_FILE
(jsonstore, same read-modify-write pattern as reminders/cigarettes/todos).
"""

import asyncio
import json
import logging
import time
from datetime import timedelta

import discord

from . import completions, config, permissions
from .store import user_store

log = logging.getLogger("discord-llm-bot.moderation")

_STRIKES = user_store(config.MODERATION_STRIKES_FILE, list)

_TITLE = "home-server-discord-bot-moderation"

_CLASSIFIER_PROMPT = (
    "You are a content moderation classifier for a small private Discord "
    "server used by friends/family. Decide whether the user's message is a "
    "clearly inappropriate request: sexual content involving minors, "
    "credible threats or incitement of violence, hate speech/slurs targeting "
    "a protected group, or serious harassment. Ordinary rudeness, swearing, "
    "dark humor, and edgy jokes between friends are NOT flagged — only flag "
    "content that would be genuinely unacceptable in a normal group chat. "
    'Respond with strict JSON only, no other text: '
    '{"flagged": bool, "category": string, "reason": string}. '
    "category must be one of: sexual_minors, violence, hate, harassment, none."
)

# Strike count -> timeout in minutes. A first offense in the window is a
# refusal with no timeout; anything past the highest tier here reuses it.
_TIER_TIMEOUT_MINUTES = {
    2: config.MODERATION_TIMEOUT_MINUTES_TIER2,
    3: config.MODERATION_TIMEOUT_MINUTES_TIER3,
}


def _endpoints() -> tuple[completions.Endpoint, completions.Endpoint | None]:
    """Classification is the cheapest path to move onto local hardware: it runs
    on every ordinary message, needs no tool calling, and is where keeping
    household chat off a third party actually matters. Because it is also the
    safety net, a sleeping box must not silently switch moderation off — so
    when the local endpoint doesn't answer, this falls back to OpenRouter and
    keeps classifying rather than failing open."""
    primary = completions.Endpoint(
        config.MODERATION_API_BASE, config.MODERATION_API_KEY, config.MODERATION_MODEL, _TITLE
    )
    if primary.is_openrouter:
        return primary, None
    return primary, completions.openrouter_endpoint(config.OPENROUTER_MODEL, _TITLE)


def _classify(text: str) -> tuple[bool, str, str]:
    """Returns (flagged, category, reason). Fails open (not flagged) on any
    error — a moderation outage should never block ordinary chat. That is the
    last resort now rather than the first: see _endpoints()."""
    payload = {
        # No "model" — completions.post() fills it from whichever endpoint serves.
        "messages": [
            {"role": "system", "content": _CLASSIFIER_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": 150,
        "response_format": {"type": "json_object"},
    }
    primary, fallback = _endpoints()
    try:
        # attempts=1: this runs before every ordinary reply, so a retry loop
        # here would put its backoff on the critical path of each message.
        resp, _served = completions.post(payload, primary, timeout=15, fallback=fallback)
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return bool(data.get("flagged")), str(data.get("category") or "none"), str(data.get("reason") or "")
    except Exception:
        log.exception("Moderation classification failed for %r; failing open", text)
        return False, "none", ""


def _record_strike(user_id: int) -> int:
    """Appends a strike timestamp for `user_id`, prunes anything older than
    config.MODERATION_STRIKE_WINDOW_DAYS, and returns the count still in the
    window (including this one)."""
    cutoff = time.time() - config.MODERATION_STRIKE_WINDOW_DAYS * 86400
    with _STRIKES.update_for(user_id) as history:
        history[:] = [t for t in history if t > cutoff]
        history.append(time.time())
        return len(history)


async def enforce(message: discord.Message, roles: frozenset[str], text: str) -> str | None:
    """The moderation gate for one incoming chat message. Returns a reply to
    send instead of the normal LLM response if the message was flagged, or
    None if the message should proceed to ask_llm normally."""
    if not config.MODERATION_ENABLED or permissions.is_admin(message.author.id, roles):
        return None

    flagged, category, reason = await asyncio.to_thread(_classify, text)
    if not flagged:
        return None

    strikes = _record_strike(message.author.id)
    log.warning(
        "Flagged message from %s (category=%s, strike=%d): %r", message.author.id, category, strikes, text
    )

    timeout_minutes = _TIER_TIMEOUT_MINUTES.get(strikes) or (
        config.MODERATION_TIMEOUT_MINUTES_TIER3 if strikes > max(_TIER_TIMEOUT_MINUTES) else None
    )
    if timeout_minutes and isinstance(message.author, discord.Member):
        try:
            await message.author.timeout(timedelta(minutes=timeout_minutes), reason=f"Auto-moderation: {category}")
        except discord.Forbidden:
            log.warning("Missing permission to time out %s — check the bot's role has 'Timeout Members'", message.author.id)
        except discord.HTTPException:
            log.exception("Failed to time out %s", message.author.id)
        else:
            return (
                f"That request was flagged ({category}) and you've been timed out for "
                f"{timeout_minutes} minutes. This is strike {strikes}."
            )

    return f"That request was flagged ({category}) and won't be answered. This is strike {strikes}."
