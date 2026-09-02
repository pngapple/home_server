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
import requests

from . import config, jsonstore, permissions

log = logging.getLogger("discord-llm-bot.moderation")

_API_BASE = "https://openrouter.ai/api/v1"
_session = requests.Session()

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


def _classify(text: str) -> tuple[bool, str, str]:
    """Returns (flagged, category, reason). Fails open (not flagged) on any
    error — a moderation outage should never block ordinary chat."""
    payload = {
        "model": config.MODERATION_MODEL,
        "messages": [
            {"role": "system", "content": _CLASSIFIER_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": 150,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "home-server-discord-bot-moderation",
    }
    try:
        resp = _session.post(f"{_API_BASE}/chat/completions", headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
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
    with jsonstore.update(config.MODERATION_STRIKES_FILE, {}) as data:
        key = str(user_id)
        history = [t for t in data.get(key, []) if t > cutoff]
        history.append(time.time())
        data[key] = history
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
