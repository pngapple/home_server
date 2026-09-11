"""
The webhook a separate voice-listener process (wake word + speech-to-text)
POSTs transcribed commands to. Two things matter here: the fast-path Kasa
matcher has to recognize the common phrasings without false-matching
ordinary conversation (which must fall through to the LLM), and the secret
check has to actually gate the endpoint.
"""

import asyncio

import pytest

from bot import config, voice_server


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("turn on desk lights", ("desk lights", "on")),
        ("turn desk lights off", ("desk lights", "off")),
        ("turn off the desk lights", ("desk lights", "off")),
        ("switch on Max's Desk Lights", ("Max's Desk Lights", "on")),
        ("please turn on the living room lamp", ("living room lamp", "on")),
        ("what's on my todo list", None),
        ("is the desk light on", None),
    ],
)
def test_match_plug_command(text, expected):
    assert voice_server.match_plug_command(text) == expected


@pytest.fixture
def voice_secret(monkeypatch):
    monkeypatch.setattr(config, "VOICE_SERVER_SECRET", "s3cret")
    return "s3cret"


@pytest.fixture(autouse=True)
def no_real_dm(monkeypatch):
    """Every request DMs the owner a record of what happened; none of these
    tests care about that side effect, and it would otherwise try to hit
    Discord."""

    async def fake_send_dm(user_id, text):
        return True

    monkeypatch.setattr(voice_server.notify, "send_dm", fake_send_dm)


@pytest.fixture(autouse=True)
def no_real_fetch_user(monkeypatch):
    import types

    async def fake_fetch_user(user_id):
        return types.SimpleNamespace(id=user_id, display_name="Owner")

    monkeypatch.setattr(voice_server.client, "fetch_user", fake_fetch_user)


def test_rejects_missing_secret_config():
    # config.VOICE_SERVER_SECRET is unset by default in the test env.
    resp = run(voice_server.handle_command(FakeRequest({"secret": "anything", "transcript": "turn on desk lights"})))
    assert resp.status == 503


def test_rejects_wrong_secret(voice_secret):
    resp = run(voice_server.handle_command(FakeRequest({"secret": "wrong", "transcript": "turn on desk lights"})))
    assert resp.status == 403


def test_rejects_missing_transcript(voice_secret):
    resp = run(voice_server.handle_command(FakeRequest({"secret": voice_secret, "transcript": ""})))
    assert resp.status == 400


def test_fast_path_dispatches_directly_without_the_llm(voice_secret, monkeypatch):
    calls = []

    def fake_dispatch_result(name, arguments, ctx):
        calls.append((name, arguments))
        from bot.tools import ToolResult

        return ToolResult("'Desk Lights' turned on.")

    def fake_ask_llm(message, user_text, roles=frozenset()):
        raise AssertionError("the fast path must not call the LLM")

    monkeypatch.setattr(voice_server, "dispatch_result", fake_dispatch_result)
    monkeypatch.setattr(voice_server, "ask_llm", fake_ask_llm)

    resp = run(
        voice_server.handle_command(
            FakeRequest({"secret": voice_secret, "transcript": "turn on desk lights"})
        )
    )

    assert resp.status == 200
    assert calls == [("set_plug_power", {"device": "desk lights", "state": "on"})]


def test_fast_path_dispatch_runs_off_the_event_loop(voice_secret, monkeypatch):
    """set_plug_power (bot/tools/kasa.py) does asyncio.run() internally,
    which raises "cannot be called from a running event loop" if invoked
    directly from this async handler instead of via asyncio.to_thread. This
    is a real bug that happened: a voice command failed with exactly that
    traceback because the fast path called dispatch_result() straight from
    the coroutine. Deliberately does NOT monkeypatch dispatch_result, so it
    exercises the real call path down into kasa.py's asyncio.run()."""
    from bot.tools import kasa

    monkeypatch.setattr(config, "KASA_USERNAME", "u")
    monkeypatch.setattr(config, "KASA_PASSWORD", "p")
    monkeypatch.setattr(kasa, "_CACHE", {})
    monkeypatch.setattr(kasa, "_CACHE_TS", 0.0)
    monkeypatch.setattr(kasa, "_UNREACHABLE", ())

    async def fake_discover(*args, **kwargs):
        return {}

    monkeypatch.setattr(kasa.Discover, "discover", fake_discover)

    resp = run(
        voice_server.handle_command(
            FakeRequest({"secret": voice_secret, "transcript": "turn off Max desk lights"})
        )
    )

    assert resp.status == 200
    body = run(resp_json(resp))
    assert "failed unexpectedly" not in body["reply"]
    assert "no plug named" in body["reply"]


def test_fallback_path_goes_through_ask_llm(voice_secret, monkeypatch):
    def fake_dispatch_result(name, arguments, ctx):
        raise AssertionError("no plug command was said, dispatch_result must not run")

    seen = {}

    def fake_ask_llm(message, user_text, roles=frozenset()):
        seen["message"] = message
        seen["user_text"] = user_text
        seen["roles"] = roles
        return "Added milk to your grocery list."

    monkeypatch.setattr(voice_server, "dispatch_result", fake_dispatch_result)
    monkeypatch.setattr(voice_server, "ask_llm", fake_ask_llm)

    resp = run(
        voice_server.handle_command(
            FakeRequest({"secret": voice_secret, "transcript": "add milk to the grocery list"})
        )
    )

    assert resp.status == 200
    assert seen["user_text"] == "add milk to the grocery list"
    assert seen["roles"] == frozenset({config.HOUSEHOLD_ROLE_NAME, config.ADMIN_ROLE_NAME})
    assert seen["message"].author.id == config.CLAUDE_CODE_OWNER_ID
    assert seen["message"].channel.id == voice_server._VOICE_CHANNEL_ID


def test_strips_card_relay_preamble(voice_secret, monkeypatch):
    from bot import cards

    def fake_ask_llm(message, user_text, roles=frozenset()):
        return cards.with_preamble("```\nsome card\n```", prefix="Added.")

    monkeypatch.setattr(voice_server, "ask_llm", fake_ask_llm)

    resp = run(
        voice_server.handle_command(
            FakeRequest({"secret": voice_secret, "transcript": "what's on my todo list"})
        )
    )

    body = run(resp_json(resp))
    assert cards.RELAY_PREAMBLE not in body["reply"]


async def resp_json(resp):
    import json

    return json.loads(resp.body.decode())
