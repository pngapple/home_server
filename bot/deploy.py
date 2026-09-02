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

commit_and_push() runs first, so a restart always leaves the repo caught up
with whatever's actually about to run — CLAUDE_CODE_WORKDIR is where Claude
Code sessions (claude_bridge.py) edit files directly on disk, and a restart
is the point someone's decided those edits are good, so this is the natural
moment to persist them. Failures there are reported but never block the
restart itself: the new process runs whatever's on disk regardless of
whether git knows about it yet.
"""

import logging
import os
import subprocess

from . import config, jsonstore

log = logging.getLogger("discord-llm-bot.deploy")

TRIGGER_PREFIX = "!deploy"
FORCE_ARG = "force"

_NOTIFY_FILE = "deploy_notify.json"

_COMMIT_MESSAGE_SYSTEM_PROMPT = (
    "You write git commit messages for a personal Discord bot project. "
    "Given a `git diff --cached`, respond with ONLY the commit message: a "
    "short imperative-mood summary line (under 72 characters, no trailing "
    "period), optionally followed by a blank line and a couple of sentences "
    "of body explaining *why* the change was made if that's not obvious "
    "from the diff alone. No markdown, no code fences, no commentary "
    "outside the message itself."
)

# Diffs beyond this are truncated before being sent to the model — plenty
# for a commit summary, and keeps a huge auto-generated file change cheap.
_MAX_DIFF_CHARS = 12000

_GENERIC_COMMIT_MESSAGE = "Deploy: automatic commit of pending changes"


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


def _run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=config.CLAUDE_CODE_WORKDIR, capture_output=True, text=True
    )


def _commit_message(staged_diff: str, user_id: int | None, user_name: str | None) -> str:
    """Asks the OpenRouter chat model for a commit message summarizing
    `staged_diff`. Imported lazily (rather than at module load) because
    bot.llm pulls in bot.tools -> bot.permissions -> bot.discord_client,
    none of which deploy.py otherwise needs. Falls back to a generic message
    if the diff is empty or the call fails for any reason — a missing
    summary shouldn't block the commit itself."""
    if not staged_diff.strip():
        return _GENERIC_COMMIT_MESSAGE
    from .llm import call_openrouter

    try:
        reply = call_openrouter(
            [
                {"role": "system", "content": _COMMIT_MESSAGE_SYSTEM_PROMPT},
                {"role": "user", "content": staged_diff[:_MAX_DIFF_CHARS]},
            ],
            user_id=user_id,
            user_name=user_name,
        )
        message = (reply.get("content") or "").strip()
        return message or _GENERIC_COMMIT_MESSAGE
    except Exception:
        log.exception("Failed to generate a commit message; using a generic one")
        return _GENERIC_COMMIT_MESSAGE


def commit_and_push(user_id: int | None = None, user_name: str | None = None) -> str | None:
    """Commits and pushes any pending changes in CLAUDE_CODE_WORKDIR. Meant
    to run in a worker thread (blocking subprocess calls) right before
    restart(). Returns a short human-readable status for the Discord reply,
    or None if the working tree was already clean. `user_id`/`user_name`
    just attribute the commit-message LLM call for metrics (see
    llm.call_openrouter) — same as everywhere else usage is tracked."""
    status = _run_git("status", "--porcelain")
    if status.returncode != 0:
        log.error("git status failed: %s", status.stderr)
        return f"⚠️ Couldn't check for pending changes: {status.stderr.strip()[:300]}"
    if not status.stdout.strip():
        return None

    add = _run_git("add", "-A")
    if add.returncode != 0:
        log.error("git add failed: %s", add.stderr)
        return f"⚠️ Couldn't stage changes for commit: {add.stderr.strip()[:300]}"

    diff = _run_git("diff", "--cached")
    message = _commit_message(diff.stdout, user_id, user_name)

    commit = _run_git("commit", "-m", message)
    if commit.returncode != 0:
        log.error("git commit failed: %s", commit.stderr)
        return f"⚠️ Couldn't commit changes: {commit.stderr.strip()[:300]}"

    summary = message.splitlines()[0]
    push = _run_git("push")
    if push.returncode != 0:
        log.error("git push failed: %s", push.stderr)
        return f"✅ Committed ({summary!r}) but push failed: {push.stderr.strip()[:300]}"

    return f"✅ Committed and pushed: {summary!r}"


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
