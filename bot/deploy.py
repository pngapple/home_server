"""
Explicit, human-triggered remote restart of this bot's own systemd service
("!deploy" in Discord) — deliberately NOT routed through claude_bridge.py's
headless Claude Code sessions, which are blocked from doing this themselves
(see claude_bridge._SELF_RESTART_DISALLOWED_TOOLS) because an autonomous
session deciding to restart mid-task kills its own subprocess, and every
other session's, before anything can be reported back. This is the safe
side door: a human explicitly asks for exactly this one action. app.py
still checks claude_bridge.in_flight_count() before calling restart() below
(with a `!deploy force` override), since a restart at that moment would
kill any in-progress session just the same.

Restarting this process obviously kills the on_message coroutine that
triggered it, before it can send a "done" reply — so the flow is: app.py
notifies the channel *before* calling restart(), which stashes which
channel asked in _NOTIFY_FILE so the new process can announce itself on
startup (see consume_pending_notify(), called from app.py's on_ready).
"""

import logging
import os
import subprocess

from . import config, jsonstore

log = logging.getLogger("discord-llm-bot.deploy")

TRIGGER_PREFIX = "!deploy"
FORCE_ARG = "force"

_NOTIFY_FILE = "deploy_notify.json"


def _args(text: str) -> str | None:
    """The text after the trigger word, or None if `text` isn't a trigger.
    Matches the whole first word only, so "!deployment plans" doesn't restart
    the service."""
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].lower() != TRIGGER_PREFIX:
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def is_trigger(text: str) -> bool:
    return _args(text) is not None


def is_force(text: str) -> bool:
    return (_args(text) or "").lower() == FORCE_ARG


def restart(channel_id: int) -> None:
    """Unconditionally restarts the bot service — the caller (app.py) is
    responsible for deciding whether that's currently safe (see
    claude_bridge.in_flight_count()) and for saying so *before* calling
    this, since there's no chance to reply after: this process is about to
    die."""
    try:
        jsonstore.write(_NOTIFY_FILE, {"channel_id": channel_id})
    except OSError:
        # The caller has already promised a restart, so go through with it —
        # the only cost of losing this file is no "back online" message.
        log.exception("Failed to write %s", _NOTIFY_FILE)
    # Fire-and-forget: `sudo systemctl restart` tears down this whole
    # process (and this coroutine with it) almost immediately, so there's no
    # meaningful result to await here.
    subprocess.Popen(["sudo", "systemctl", "restart", config.SERVICE_NAME])


def consume_pending_notify() -> int | None:
    """Called once from on_ready. Returns the channel id to announce
    "back online" in, if this startup was the result of a !deploy restart."""
    data = jsonstore.read(_NOTIFY_FILE, {})
    try:
        os.remove(_NOTIFY_FILE)
    except OSError:
        pass
    return data.get("channel_id")
