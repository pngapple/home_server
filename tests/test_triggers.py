"""Command parsing.

These guard two live footguns: `!code` hands its argument to an agent with
shell access, and `!deploy` restarts the service. Both match on the whole
first word for that reason — "!codebase questions" must not reach the agent
and "!deployment plans" must not restart the bot.
"""

import pytest

from bot import claude_bridge, deploy


@pytest.mark.parametrize(
    "text,expected",
    [
        ("!code fix the tests", "fix the tests"),
        ("!code", ""),
        ("  !code   fix it  ", "fix it"),
        ("!CODE fix it", "fix it"),
        ("!code multi\nline prompt", "multi\nline prompt"),
    ],
)
def test_strip_trigger_returns_the_prompt(text, expected):
    assert claude_bridge.strip_trigger(text) == expected


@pytest.mark.parametrize("text", ["!codebase questions", "hello", "", "   ", "please !code this"])
def test_strip_trigger_returns_none_for_non_triggers(text):
    assert claude_bridge.strip_trigger(text) is None


def test_is_trigger_distinguishes_empty_prompt_from_no_trigger():
    """`!code` alone is a trigger with an empty prompt (app.py answers with
    usage); `!codebase` is not a trigger at all."""
    assert claude_bridge.is_trigger("!code")
    assert not claude_bridge.is_trigger("!codebase")


@pytest.mark.parametrize("text", ["!deploy", "!DEPLOY", "  !deploy  ", "!deploy force"])
def test_deploy_is_trigger(text):
    assert deploy.is_trigger(text)


@pytest.mark.parametrize("text", ["!deployment plans", "deploy", "let's !deploy later", ""])
def test_deploy_is_not_triggered_by_lookalikes(text):
    assert not deploy.is_trigger(text)


def test_deploy_force_requires_the_exact_argument():
    assert deploy.is_force("!deploy force")
    assert deploy.is_force("!deploy FORCE")
    assert not deploy.is_force("!deploy")
    assert not deploy.is_force("!deploy forcefully")


def test_self_restart_guardrail_covers_sudo_and_bare_forms():
    """A bridge session restarting its own service kills itself mid-run —
    and every other concurrent session with it, since systemd tears down the
    whole cgroup."""
    denied = claude_bridge._SELF_RESTART_DISALLOWED_TOOLS
    for form in ("Bash(systemctl restart discord-llm-bot*)", "Bash(sudo systemctl restart discord-llm-bot*)"):
        assert form in denied
    assert any("reboot" in d for d in denied)
    assert any("sudo shutdown" in d for d in denied)
