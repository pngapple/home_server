"""
What the bot has learned, as text it can actually act on.

episodes.py records that something went wrong. This is the other half: the
notes that change what the bot does next time. A lesson is a sentence
injected into the system prompt — reversible, inspectable, and cheap, which
is what makes this the one part of the learning loop that's safe to write
automatically.

Three scopes, and the distinction is a security boundary as much as a
filing decision:

  USER   — "Max wants the whole list shown after every change." Written by
           the user's own request (tools/preferences.py), applies only to
           their own turns.
  TOOL   — "add_todo's `text` argument gets confused with an item id."
           Applies whenever that tool is in play, for anyone.
  GLOBAL — applies to every turn for everyone.

Anything a user says can become a USER lesson, because the blast radius is
their own conversation. GLOBAL and TOOL lessons reach everyone, so nothing
driven by an ordinary chat message may create one — otherwise "remember that
you should always do X" from any housemate becomes a standing instruction
for the whole household, which is prompt injection with extra steps. See
tools/preferences.py: it hardcodes USER scope and the caller's own id.

Bounded on purpose. An unbounded prompt makes a small model worse, not
better, so lessons expire if nothing reinforces them and compete for a fixed
character budget rather than all being injected.

JSON rather than SQLite, per the storage note in CLAUDE.md: a few dozen
short records, written when someone states a preference, and `cat
lessons.json` is the natural way to audit what the bot thinks it knows.
"""

import logging
import os
import re
from datetime import UTC, datetime, timedelta

from . import config, jsonstore, store

log = logging.getLogger("discord-llm-bot.lessons")

USER = "user"
TOOL = "tool"
GLOBAL = "global"

# How a lesson came to exist. "stated" is someone saying it outright;
# "inferred" is the reflection pass concluding it from failures. Kept apart
# so an inferred lesson that turns out to be wrong can be swept without
# touching anything a person actually asked for.
STATED = "stated"
INFERRED = "inferred"

_PATH = config.LESSONS_FILE


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(text: str) -> str:
    """For duplicate detection: case, punctuation and spacing don't make two
    notes different."""
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _read() -> list[dict]:
    return jsonstore.read(_PATH, [])


def all_lessons() -> list[dict]:
    return _read()


def add(
    text: str,
    scope: str = USER,
    subject: int | str | None = None,
    source: str = STATED,
    source_episodes: list[int] | None = None,
) -> dict | None:
    """Record a lesson, or refresh the matching one if it's already known.

    Returns the stored lesson, or None if `text` was empty. Restating
    something is the main way a lesson survives — see prune()."""
    text = " ".join(text.split())
    if not text:
        return None

    with jsonstore.update(_PATH, []) as data:
        for existing in data:
            if existing["scope"] == scope and existing.get("subject") == subject:
                if _normalize(existing["text"]) == _normalize(text):
                    existing["reinforced_at"] = _now()
                    existing["text"] = text  # keep the latest phrasing
                    return dict(existing)

        lesson = {
            "id": os.urandom(4).hex(),
            "text": text,
            "scope": scope,
            "subject": subject,
            "source": source,
            "source_episodes": source_episodes or [],
            "created_at": _now(),
            # The only clock that matters for survival (see prune). Bumped
            # when someone restates a lesson, not when it's injected —
            # counting injections would mean rewriting this file on every
            # single message, which is the write-amplification mistake the
            # metrics store already had to be rescued from.
            "reinforced_at": _now(),
        }
        data.append(lesson)

        # Enforce the per-person cap here rather than at read time, so the
        # file itself stays bounded. Oldest-reinforced goes first.
        if scope == USER:
            owned = [item for item in data if item["scope"] == USER and item.get("subject") == subject]
            if len(owned) > config.MAX_LESSONS_PER_USER:
                owned.sort(key=lambda item: item["reinforced_at"])
                for evicted in owned[: len(owned) - config.MAX_LESSONS_PER_USER]:
                    data.remove(evicted)
                    log.info("Evicted lesson %s (per-user cap)", evicted["id"])

        return dict(lesson)


def remove(lesson_id: str) -> bool:
    with jsonstore.update(_PATH, []) as data:
        before = len(data)
        data[:] = [item for item in data if item["id"] != lesson_id]
        return len(data) < before


def for_user(user_id: int) -> list[dict]:
    """This person's own lessons, newest first — what `list_preferences`
    shows them."""
    owned = [item for item in _read() if item["scope"] == USER and item.get("subject") == user_id]
    return sorted(owned, key=lambda item: item["reinforced_at"], reverse=True)


def find(user_id: int, identifier: str) -> list[dict]:
    """This user's lessons matching a text snippet, for forget-by-description."""
    needle = identifier.strip().lower()
    return [item for item in for_user(user_id) if needle and needle in item["text"].lower()] if needle else []


def forget_user(user_id: int) -> bool:
    """Erase everything learned about one person. Registered with store.py
    so users.forget() reaches it — a deletion that quietly skipped this
    would leave the bot still acting on notes about someone who asked to be
    forgotten."""
    with jsonstore.update(_PATH, []) as data:
        before = len(data)
        data[:] = [item for item in data if not (item["scope"] == USER and item.get("subject") == user_id)]
        return len(data) < before


def applicable(user_id: int, tool_names: frozenset[str] = frozenset()) -> list[dict]:
    """Lessons in scope for this turn, most recently reinforced first."""
    selected = [
        item
        for item in _read()
        if item["scope"] == GLOBAL
        or (item["scope"] == USER and item.get("subject") == user_id)
        or (item["scope"] == TOOL and item.get("subject") in tool_names)
    ]
    return sorted(selected, key=lambda item: item["reinforced_at"], reverse=True)


def for_prompt(
    user_id: int, tool_names: frozenset[str] = frozenset(), budget: int | None = None
) -> list[dict]:
    """The lessons that actually fit. Newest-reinforced win the budget —
    when the model can only be told so much, the most recently confirmed
    thing is the best guess at what still matters."""
    budget = config.LESSON_PROMPT_BUDGET_CHARS if budget is None else budget
    chosen, used = [], 0
    for lesson in applicable(user_id, tool_names):
        cost = len(lesson["text"]) + 3  # the "- " bullet and newline
        if used + cost > budget:
            continue
        chosen.append(lesson)
        used += cost
    return chosen


def render(lessons: list[dict]) -> str:
    """The system-prompt block. Empty string when there's nothing learned,
    so the prompt doesn't carry a dangling empty heading.

    The wording is deliberate: these are framed as things established
    *earlier*, separate from the message being answered now, so a note can't
    be mistaken for part of the current request."""
    if not lessons:
        return ""
    bullets = "\n".join(f"- {lesson['text']}" for lesson in lessons)
    return (
        "Things you have learned from earlier conversations. Follow them "
        "unless the current message says otherwise:\n" + bullets
    )


def prune(ttl_days: int | None = None) -> int:
    """Drop lessons nothing has reinforced inside the TTL.

    This is what stops the prompt from silently growing into noise. A lesson
    that's still true gets restated or re-derived and its clock resets; one
    that doesn't, ages out.
    """
    ttl_days = config.LESSON_TTL_DAYS if ttl_days is None else ttl_days
    cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
    dropped = 0
    with jsonstore.update(_PATH, []) as data:
        keep = []
        for item in data:
            try:
                fresh = datetime.fromisoformat(item["reinforced_at"]) > cutoff
            except (KeyError, ValueError):
                log.warning("Lesson %s has an unreadable reinforced_at, dropping", item.get("id"))
                fresh = False
            keep.append(item) if fresh else None
            dropped += 0 if fresh else 1
        data[:] = keep
    if dropped:
        log.info("Pruned %d lesson(s) unreinforced for %d days", dropped, ttl_days)
    return dropped


store.register_eraser("lessons.json", forget_user)
