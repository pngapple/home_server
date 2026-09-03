"""
The `{discord_user_id: <their stuff>}` shape, which most of this bot's JSON
files are.

Every store used to spell the key conversion out at its own call sites —
`data.setdefault(str(ctx.user_id), [])` in todos/groceries, `str(user_id)` in
cigarettes/moderation/geofence state, and so on — so the id/key conversion
lived in six modules at once. UserKeyedStore owns it instead: callers pass a
real int user id and never see the string key.

Registering each store here also buys the thing that was previously
impossible: an inventory. `user_ids()` answers "who uses this server?" and
`forget_everywhere()` deletes a person from all of it at once, which
otherwise meant hand-editing seven files (see users.forget).
"""

import logging
import os
from contextlib import contextmanager

from . import jsonstore

log = logging.getLogger("discord-llm-bot.store")

# Every UserKeyedStore ever constructed, so a user can be enumerated or
# erased across all of them without each caller knowing the full list.
_REGISTRY: list["UserKeyedStore"] = []

# Stores that hold per-user data but aren't shaped `{user_id: value}` —
# reminders.json is a flat list of records with an author_id field. They
# register a callable here so forget_everywhere() still reaches them; a
# deletion that quietly skipped a store would be worse than no deletion at
# all. Each returns True if it actually removed something.
_ERASERS: list[tuple[str, object]] = []


def register_eraser(label: str, erase) -> None:
    """Register `erase(user_id) -> bool` for a store this module can't key
    into directly."""
    _ERASERS.append((label, erase))


# One store instance per file. Two modules legitimately want the same file —
# the cigarettes tool writes cigarettes.json and cigboard's leaderboard reads
# it — and registering it twice would double-count it in the inventory and
# report it twice in a forget().
_BY_PATH: dict[str, "UserKeyedStore"] = {}


def user_store(path: str, default_factory=list, mode: int = 0o644, label: str | None = None) -> "UserKeyedStore":
    """The store for `path`, created and registered on first request."""
    existing = _BY_PATH.get(path)
    if existing is not None:
        return existing
    created = UserKeyedStore(path, default_factory, mode, label)
    _BY_PATH[path] = created
    _REGISTRY.append(created)
    return created


def key(user_id: int | str) -> str:
    """The JSON object key for a user id. JSON object keys are always
    strings, so this is where int -> str happens, exactly once."""
    return str(user_id)


class UserKeyedStore:
    """A JSON file holding one value per Discord user.

    `default_factory` builds the empty value for a user with no entry yet
    (`list` for the item/timestamp stores, `dict` for the record ones). It's
    called per access rather than shared, so no two users can ever end up
    aliasing the same mutable default.
    """

    def __init__(self, path: str, default_factory=list, mode: int = 0o644, label: str | None = None):
        self.path = path
        self.mode = mode
        self.label = label or os.path.basename(path)
        self._default_factory = default_factory

    # -- reads ---------------------------------------------------------

    def all(self) -> dict:
        """The whole file, `{key: value}`. Read-only: mutating the result
        doesn't persist — use update()/update_for() for that."""
        return jsonstore.read(self.path, {})

    def get(self, user_id: int):
        """This user's value, or a fresh empty one if they have no entry."""
        return self.all().get(key(user_id), self._default_factory())

    def user_ids(self) -> set[int]:
        """Every user id with an entry in this store. Ids that aren't
        numeric are skipped rather than raising — a hand-edited file
        shouldn't take down whatever is taking an inventory."""
        ids = set()
        for raw in self.all():
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                log.warning("Ignoring non-numeric user key %r in %s", raw, self.path)
        return ids

    # -- writes --------------------------------------------------------

    @contextmanager
    def update(self):
        """Read-modify-write the whole file under its lock. For the handful
        of operations that genuinely span users (the shared grocery view's
        check-off, which may touch someone else's list)."""
        with jsonstore.update(self.path, {}, mode=self.mode) as data:
            yield data

    @contextmanager
    def update_for(self, user_id: int):
        """Read-modify-write just this user's value, under the file's lock.
        The entry is created from default_factory if absent, so callers can
        mutate what they're handed without checking first."""
        with jsonstore.update(self.path, {}, mode=self.mode) as data:
            yield data.setdefault(key(user_id), self._default_factory())

    def set(self, user_id: int, value) -> None:
        with jsonstore.update(self.path, {}, mode=self.mode) as data:
            data[key(user_id)] = value

    def forget(self, user_id: int) -> bool:
        """Drop this user's entry. True if there was one."""
        with jsonstore.update(self.path, {}, mode=self.mode) as data:
            return data.pop(key(user_id), None) is not None


def all_stores() -> list[UserKeyedStore]:
    return list(_REGISTRY)


def known_user_ids() -> set[int]:
    """Every user id appearing in any registered store."""
    ids: set[int] = set()
    for store in _REGISTRY:
        ids |= store.user_ids()
    return ids


def forget_everywhere(user_id: int) -> list[str]:
    """Erase this user from every registered store. Returns the labels of
    the stores that actually had something, for reporting back."""
    erased = []
    for store in _REGISTRY:
        try:
            if store.forget(user_id):
                erased.append(store.label)
        except Exception:
            log.exception("Failed to erase user %s from %s", user_id, store.path)
    for label, erase in _ERASERS:
        try:
            if erase(user_id):
                erased.append(label)
        except Exception:
            log.exception("Failed to erase user %s from %s", user_id, label)
    return erased
