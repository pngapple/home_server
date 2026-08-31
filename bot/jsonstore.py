"""
Shared helpers for the flat JSON files this bot uses as storage (reminders,
cigarettes, calendar tokens, metrics).

Those files are read-modify-written from more than one thread: tool handlers
run in asyncio.to_thread worker threads (see app.py) while reminder timers,
the geofence webhook and the local dashboards run on the Discord event loop.
lock() gives each file its own mutex so a concurrent read-modify-write can't
lose an update; write() swaps the file in atomically so a reader — or a
power cut on a Pi — never sees a half-written file.

update() combines the two into the pattern every caller actually wants:

    with jsonstore.update(PATH, []) as reminders:
        reminders.append(new_one)

which takes the lock, reads, hands you the mutable value, and writes it back
on a clean exit.
"""

import json
import logging
import os
import threading
from contextlib import contextmanager

log = logging.getLogger("discord-llm-bot.jsonstore")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock(path: str) -> threading.Lock:
    """The mutex for `path`. Hold it across the whole load/modify/save."""
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def read(path: str, default):
    """Parse `path` as JSON, falling back to `default` if it's missing,
    truncated or otherwise unreadable. Pass a fresh `[]`/`{}` per call — the
    fallback is returned as-is, not copied."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        log.exception("Failed to read %s, treating as empty", path)
        return default


def write(path: str, data, indent: int | None = 2, mode: int = 0o644) -> None:
    """Serialize `data` to `path` as JSON, atomically."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


@contextmanager
def update(path: str, default, indent: int | None = 2, mode: int = 0o644):
    """Read-modify-write `path` under its lock. Yields the parsed value for
    in-place mutation and writes it back afterwards; to replace a list
    wholesale, assign into a slice (`data[:] = ...`) so the yielded object
    stays the one that gets written. Raising inside the block skips the
    write, leaving the file as it was."""
    with lock(path):
        data = read(path, default)
        yield data
        write(path, data, indent=indent, mode=mode)
