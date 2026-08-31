"""
Direct bridge from Discord messages to a real headless Claude Code session
(`claude -p`) — not the small OpenRouter chat model used elsewhere in this
bot. This gets real shell/file access on the server, so it's gated to a
single owner Discord user id (config.CLAUDE_CODE_OWNER_ID) and triggered
only by an explicit prefix, never by ordinary conversation.

Each Discord channel/DM gets its own Claude Code session id, so follow-up
messages continue the same conversation (`claude --resume`) instead of
starting fresh every time. Only the id is kept here, in memory — the actual
conversation lives in Claude Code's own on-disk session storage, so a bot
restart just means the next message in a channel starts a new session
instead of resuming the old one, not that history is corrupted.
"""

import asyncio
import json
import logging
import uuid

from . import config, metrics
from .metrics import Call

log = logging.getLogger("discord-llm-bot.claude_bridge")

TRIGGER_PREFIX = "!code"

# channel_id -> claude session id (uuid4 string)
_sessions: dict[int, str] = {}

# channel_id -> lock. Two `claude --resume` processes on the same session id
# at once corrupt that session's transcript, and without this a follow-up
# sent while the first run is still going would also race on _sessions. Only
# ever touched from the event loop, so a plain dict needs no guard of its own.
_channel_locks: dict[int, asyncio.Lock] = {}

# How many `claude -p` subprocesses are currently running, across all
# channels — checked by deploy.py before restarting the bot, since a restart
# mid-run kills the subprocess the same way an in-session self-restart would.
_in_flight = 0


def in_flight_count() -> int:
    return _in_flight


def _channel_lock(channel_id: int) -> asyncio.Lock:
    return _channel_locks.setdefault(channel_id, asyncio.Lock())


def _calls_from(data: dict) -> list[Call]:
    """Per-model token usage out of `claude -p --output-format json` output.
    modelUsage is keyed by the raw model id and has one entry per model
    actually used in the turn (usually one, but a fallback/retry can involve
    more than one) — duration_ms covers the whole call, so a multi-model turn
    gets an approximate per-model rate rather than an exact one."""
    duration_s = (data.get("duration_ms") or 0) / 1000
    model_usage = data.get("modelUsage") or {}
    if not model_usage:
        usage = data.get("usage") or {}
        return [
            Call(
                source="claude-code",
                model="unknown",
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                duration_s=duration_s,
            )
        ]
    return [
        Call(
            source="claude-code",
            model=usage.get("canonicalModel", model_id),
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            duration_s=duration_s,
            context_window=usage.get("contextWindow"),
        )
        for model_id, usage in model_usage.items()
    ]


async def _record_metrics(data: dict) -> None:
    """One locked file write for the whole turn, off the event loop —
    metrics writes hit the disk, and this runs on the Discord client's loop."""
    await asyncio.to_thread(metrics.record_many, _calls_from(data), data.get("total_cost_usd") or 0.0)


# Restarting/stopping/killing this bot's own systemd unit (or rebooting the
# whole Pi) from inside a bridge session kills that session's own subprocess
# mid-run before it can report back — and, worse, kills every OTHER
# concurrently-running bridge session too, since systemd tears down the
# whole cgroup on stop. Denying these patterns pushes the agent toward an
# explicit, separate human action (`!deploy`, see deploy.py) instead of
# unilaterally pulling the rug out mid-task.
#
# This is a guardrail against accidents, NOT a security boundary: it's a
# pattern blocklist, so an equivalent command spelled differently (extra
# whitespace, `bash -c ...`, a script) still gets through. The real control
# is that only CLAUDE_CODE_OWNER_ID can reach this code path at all.
_SELF_RESTART_DISALLOWED_TOOLS = [
    f"Bash({prefix}systemctl {action} {config.SERVICE_NAME}*)"
    for prefix in ("", "sudo ")
    for action in ("restart", "stop", "kill")
] + [f"Bash({prefix}{cmd}*)" for prefix in ("", "sudo ") for cmd in ("reboot", "shutdown", "poweroff", "halt")]


def strip_trigger(text: str) -> str | None:
    """The prompt after the trigger word, or None if `text` isn't a trigger.
    Matches the whole first word only, so "!codebase questions" doesn't hand
    a mangled prompt to a shell-capable agent."""
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].lower() != TRIGGER_PREFIX:
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def is_trigger(text: str) -> bool:
    return strip_trigger(text) is not None


def is_authorized(discord_user_id: int) -> bool:
    return config.CLAUDE_CODE_OWNER_ID is not None and discord_user_id == config.CLAUDE_CODE_OWNER_ID


def has_session(channel_id: int) -> bool:
    """True once a channel/thread has an in-progress Claude Code session —
    used to let follow-up messages in a dedicated thread continue it without
    repeating the trigger prefix."""
    return channel_id in _sessions


async def run(channel_id: int, prompt: str) -> str:
    """Run one prompt in this channel's Claude Code session and return the
    final text. Serialized per channel: a second message arriving mid-run
    waits its turn rather than racing the same session id."""
    async with _channel_lock(channel_id):
        return await _run_locked(channel_id, prompt)


def _build_args(prompt: str, session_id: str, resume: bool) -> list[str]:
    return [
        config.CLAUDE_CODE_BINARY,
        "-p",
        prompt,
        "--permission-mode",
        "auto",
        "--output-format",
        "json",
        "--disallowedTools",
        *_SELF_RESTART_DISALLOWED_TOOLS,
        *(["--resume", session_id] if resume else ["--session-id", session_id]),
    ]


async def _run_locked(channel_id: int, prompt: str) -> str:
    global _in_flight

    session_id = _sessions.get(channel_id)
    resume = session_id is not None
    if session_id is None:
        session_id = str(uuid.uuid4())

    # Count this as in flight from before the spawn: deploy.py polls
    # in_flight_count() to decide whether a restart is safe, and a restart
    # landing between the check and the spawn would kill the process just the
    # same.
    _in_flight += 1
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *_build_args(prompt, session_id, resume),
                cwd=config.CLAUDE_CODE_WORKDIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            log.exception("Could not start %s", config.CLAUDE_CODE_BINARY)
            return (
                f"Error: couldn't start Claude Code (`{config.CLAUDE_CODE_BINARY}`). "
                f"Check that the CLI is installed at that path."
            )

        # Claude owns a session on disk under this id from here on, so
        # remember it even if the run below errors out or times out — a
        # retry should resume it, not silently start over. Recording it now
        # rather than after the run also makes has_session() true while the
        # run is in progress, which is what thread follow-ups check.
        _sessions[channel_id] = session_id

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=config.CLAUDE_CODE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                f"Error: Claude Code didn't finish within "
                f"{config.CLAUDE_CODE_TIMEOUT_SECONDS}s and was killed. Try a "
                f"narrower prompt, or continue it with another `{TRIGGER_PREFIX}` message."
            )
    finally:
        _in_flight -= 1

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        log.warning("claude -p exited %s: %s", proc.returncode, err[:2000])
        return f"Error: Claude Code exited with an error.\n{err[-1500:]}"

    raw = stdout.decode(errors="replace").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("claude -p --output-format json returned non-JSON stdout: %r", raw[:500])
        return raw or "(no output)"

    await _record_metrics(data)
    return data.get("result") or "(no output)"
