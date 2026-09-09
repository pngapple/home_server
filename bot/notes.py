"""
Free-form notes: something jotted down verbatim and retrieved later by a
text search.

This is neither of the other two per-user stores it looks like. It isn't a
todos.py/groceries.py checklist — a note doesn't get checked off, so none of
lists.py's done/retention machinery applies. And it isn't a preferences.py
lesson — a note is data the user wants back, not an instruction that should
shape how the bot behaves, so it has no business anywhere near the system
prompt.

Stored as `{user_id: [note, ...]}` via store.user_store, so it's picked up
by users.forget()/forget_everywhere() for free, the same as todos and
groceries.
"""

import os
from datetime import UTC, datetime

from . import config, store

_STORE = store.user_store(config.NOTES_FILE, list)


def _new_id() -> str:
    """A short random note id. Only needs to be unique within one user's
    notes, which 4 bytes covers comfortably."""
    return os.urandom(4).hex()


def add(user_id: int, text: str) -> dict:
    note = {"id": _new_id(), "text": text, "created_at": datetime.now(UTC).isoformat()}
    with _STORE.update_for(user_id) as notes:
        notes.append(note)
    return note


def all_for(user_id: int) -> list[dict]:
    """This user's notes, newest first."""
    return sorted(_STORE.get(user_id), key=lambda n: n["created_at"], reverse=True)


def find(user_id: int, query: str) -> list[dict]:
    """This user's notes whose text contains `query`, case-insensitively,
    newest first."""
    needle = query.strip().lower()
    if not needle:
        return []
    return [n for n in all_for(user_id) if needle in n["text"].lower()]


def remove(user_id: int, note_id: str) -> bool:
    with _STORE.update_for(user_id) as notes:
        before = len(notes)
        notes[:] = [n for n in notes if n["id"] != note_id]
        return len(notes) < before
