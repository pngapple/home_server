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

log = logging.getLogger("discord-llm-bot.claude_bridge")

TRIGGER_PREFIX = "!code"

# channel_id -> claude session id (uuid4 string)
_sessions: dict[int, str] = {}


def _record_metrics(data: dict) -> None:
    """Pulls per-model token usage out of `claude -p --output-format json`
    output. modelUsage is keyed by the raw model id and has one entry per
    model actually used in the turn (usually one, but a fallback/retry can
    involve more than one) — duration_ms covers the whole call, so a
    multi-model turn gets an approximate per-model rate rather than an exact
    one."""
    model_usage = data.get("modelUsage") or {}
    duration_s = (data.get("duration_ms") or 0) / 1000
    metrics.add_claude_code_cost(data.get("total_cost_usd") or 0.0)
    if not model_usage:
        usage = data.get("usage") or {}
        metrics.record(
            source="claude-code",
            model="unknown",
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            duration_s=duration_s,
        )
        return
    for model_id, mu in model_usage.items():
        metrics.record(
            source="claude-code",
            model=mu.get("canonicalModel", model_id),
            input_tokens=mu.get("inputTokens", 0),
            output_tokens=mu.get("outputTokens", 0),
            duration_s=duration_s,
            context_window=mu.get("contextWindow"),
        )


# Restarting/stopping/killing this bot's own systemd unit (or rebooting the
# whole Pi) from inside a bridge session kills that session's own subprocess
# mid-run before it can report back — and, worse, kills every OTHER
# concurrently-running bridge session too, since systemd tears down the
# whole cgroup on stop. Blocking it outright forces an explicit, separate
# human action (a chat message asking the actual owner, or done directly by
# this session) instead of an agent unilaterally pulling the rug out.
_SELF_RESTART_DISALLOWED_TOOLS = [
    f"Bash({prefix}systemctl {action} discord-llm-bot*)"
    for prefix in ("", "sudo ")
    for action in ("restart", "stop", "kill")
] + [f"Bash({prefix}{cmd}*)" for prefix in ("", "sudo ") for cmd in ("reboot", "shutdown", "poweroff", "halt")]


def is_trigger(text: str) -> bool:
    return text.strip().lower().startswith(TRIGGER_PREFIX)


def strip_trigger(text: str) -> str:
    return text.strip()[len(TRIGGER_PREFIX) :].strip()


def is_authorized(discord_user_id: int) -> bool:
    return config.CLAUDE_CODE_OWNER_ID is not None and discord_user_id == config.CLAUDE_CODE_OWNER_ID


def has_session(channel_id: int) -> bool:
    """True once a channel/thread has an in-progress Claude Code session —
    used to let follow-up messages in a dedicated thread continue it without
    repeating the trigger prefix."""
    return channel_id in _sessions


async def run(channel_id: int, prompt: str) -> str:
    session_id = _sessions.get(channel_id)
    args = [
        config.CLAUDE_CODE_BINARY,
        "-p",
        prompt,
        "--permission-mode",
        "auto",
        "--output-format",
        "json",
        "--disallowedTools",
        *_SELF_RESTART_DISALLOWED_TOOLS,
    ]
    if session_id:
        args += ["--resume", session_id]
    else:
        session_id = str(uuid.uuid4())
        args += ["--session-id", session_id]

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=config.CLAUDE_CODE_WORKDIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
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

    # A real on-disk session now exists for this id even if this particular
    # run errored out below, so remember it — a retry should resume, not
    # silently start over.
    _sessions[channel_id] = session_id

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

    _record_metrics(data)
    return data.get("result") or "(no output)"
