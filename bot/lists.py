"""
The checklist that todos and groceries both are.

Both tools had grown their own copy of the same thing: an item shaped
`{id, text, done, created_at, completed_at}`, a retention window for
checked-off items, a substring matcher over the open ones, and a pruner.
The copies had already drifted — todos' list handler showed *every* done
item while groceries' windowed them to the retention period, so a todo
checked off last week kept showing under "done recently" until some
unrelated write happened to prune it. Sharing the object fixes that class of
divergence by construction.

What stays with the individual tools is the genuinely different behaviour:
groceries searching everyone's list rather than just the caller's, and DMing
an item's owner when someone else checks it off. Those are real domain
differences, not incidental ones.

Items are kept after being checked off (done=true) rather than deleted, so
the card can show a little "recently done" context, and pruned for good once
they fall outside `retention`.
"""

import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from .store import key, user_store

log = logging.getLogger("discord-llm-bot.lists")

DEFAULT_RETENTION = timedelta(days=1)


def new_id() -> str:
    """A short random item id. Only needs to be unique within one user's
    list, which 4 bytes covers comfortably."""
    return os.urandom(4).hex()


class ListStore:
    """A per-user checklist persisted as `{user_id: [item, ...]}`."""

    def __init__(self, path: str, *, retention: timedelta = DEFAULT_RETENTION):
        self.retention = retention
        self._store = user_store(path, list)

    @property
    def path(self) -> str:
        return self._store.path

    # -- pure list operations -----------------------------------------
    # These take a plain list of items so callers already inside an update()
    # block can use them without re-reading the file.

    def build(self, text: str) -> dict:
        return {
            "id": new_id(),
            "text": text,
            "done": False,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def prune(self, items: list[dict]) -> None:
        """Drop checked-off items older than the retention window, in place."""
        cutoff = datetime.now(UTC) - self.retention
        items[:] = [item for item in items if not self._expired(item, cutoff)]

    def split(self, items: list[dict]) -> tuple[list[dict], list[dict]]:
        """Open items, plus checked-off ones still inside the retention
        window. Read-only, unlike prune(), so listing never has to write."""
        cutoff = datetime.now(UTC) - self.retention
        open_items = [item for item in items if not item.get("done")]
        done_items = [item for item in items if item.get("done") and not self._expired(item, cutoff)]
        return open_items, done_items

    def _expired(self, item: dict, cutoff: datetime) -> bool:
        if not item.get("done"):
            return False
        completed_at = item.get("completed_at")
        if not completed_at:
            # Checked off before completed_at was recorded, or hand-edited.
            # Treat it as expired rather than keeping it forever.
            return True
        try:
            return datetime.fromisoformat(completed_at) <= cutoff
        except ValueError:
            log.warning("Unparseable completed_at %r in %s, pruning", completed_at, self.path)
            return True

    @staticmethod
    def match(items: list[dict], identifier: str) -> list[dict]:
        """Open items whose text contains `identifier`, case-insensitively."""
        needle = identifier.strip().lower()
        if not needle:
            return []
        return [item for item in items if not item.get("done") and needle in item["text"].lower()]

    @staticmethod
    def mark_done(item: dict) -> None:
        item["done"] = True
        item["completed_at"] = datetime.now(UTC).isoformat()

    # -- store-backed convenience -------------------------------------

    def items(self, user_id: int) -> list[dict]:
        return self._store.get(user_id)

    def split_for(self, user_id: int) -> tuple[list[dict], list[dict]]:
        return self.split(self.items(user_id))

    def find_open(self, user_id: int, identifier: str) -> list[dict]:
        return self.match(self.items(user_id), identifier)

    def get(self, user_id: int, item_id: str) -> dict | None:
        return next((item for item in self.items(user_id) if item.get("id") == item_id), None)

    def add(self, user_id: int, text: str) -> tuple[dict, int]:
        """Append an item to this user's list, pruning expired ones on the
        way through. Returns the new item and the resulting open count."""
        item = self.build(text)
        with self._store.update_for(user_id) as items:
            self.prune(items)
            items.append(item)
            open_count = sum(1 for i in items if not i.get("done"))
        return item, open_count

    @contextmanager
    def update_for(self, user_id: int):
        """Read-modify-write one user's list, pruned on entry."""
        with self._store.update_for(user_id) as items:
            self.prune(items)
            yield items

    @contextmanager
    def update_all(self):
        """Read-modify-write every user's list at once, each pruned on
        entry. For the operations that genuinely span people — checking off
        an item from the shared grocery view, which may belong to anyone."""
        with self._store.update() as data:
            for items in data.values():
                self.prune(items)
            yield data

    def owners_of_open(self, identifier: str, prefer_user_id: int) -> list[tuple[int, dict]]:
        """Open items matching `identifier` across everyone's lists, as
        (owner_id, item). The caller's own list is searched first and only
        falls back to everyone else's if nothing of theirs matches, so
        "milk" prefers your own milk over a housemate's."""
        data = self._store.all()
        own_key = key(prefer_user_id)

        own = [(prefer_user_id, item) for item in self.match(data.get(own_key, []), identifier)]
        if own:
            return own

        return [
            (int(owner), item)
            for owner, items in data.items()
            if owner != own_key and owner.lstrip("-").isdigit()
            for item in self.match(items, identifier)
        ]

    def merged_split(self) -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
        """Everyone's open and recently-done items, as (owner_id, item),
        for the shared view."""
        open_all: list[tuple[int, dict]] = []
        done_all: list[tuple[int, dict]] = []
        for owner, items in self._store.all().items():
            if not owner.lstrip("-").isdigit():
                continue
            owner_id = int(owner)
            open_items, done_items = self.split(items)
            open_all += [(owner_id, item) for item in open_items]
            done_all += [(owner_id, item) for item in done_items]
        return open_all, done_all
