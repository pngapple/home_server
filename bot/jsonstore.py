"""
Shared helpers for the flat JSON files this bot uses as storage (reminders,
cigarettes, calendar tokens, metrics).

Those files are read-modify-written from more than one thread: tool handlers
run in asyncio.to_thread worker threads (see app.py) while reminder timers,
the geofence webhook and the local dashboards run on the Discord event loop.
lock() gives each file its own mutex so a concurrent read-modify-write can't
lose an update; write() swaps the file in atomically so a reader — or a
power cut on a Pi — never sees a half-written file.
"""

import json
import os
import threading

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock(path: str) -> threading.Lock:
    """The mutex for `path`. Hold it across the whole load/modify/save."""
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def write(path: str, data, indent: int | None = 2, mode: int = 0o644) -> None:
    """Serialize `data` to `path` as JSON, atomically."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)
