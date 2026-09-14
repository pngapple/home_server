"""Chat and moderation must be able to point at different API endpoints.

The base URL used to be a hardcoded module constant duplicated in llm.py and
moderation.py, which meant "run the classifier on my own hardware" was not a
config change but an edit in two places. These lock in that each path reads
its own setting, and that pointing one at a local server does not hand it the
paid-API key.
"""

import types

import pytest

from bot import config, llm, moderation

REPO_SOURCES = ("bot/llm.py", "bot/moderation.py")


def _response(payload, status=200):
    return types.SimpleNamespace(
        status_code=status,
        json=lambda: payload,
        raise_for_status=lambda: None,
    )


@pytest.fixture
def captured(monkeypatch):
    """Swaps both modules' sessions for one recorder."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "json": json})
        return _response(
            {
                "choices": [{"message": {"role": "assistant", "content": '{"flagged": false}'}}],
                "usage": {},
            }
        )

    monkeypatch.setattr(llm._session, "post", fake_post)
    monkeypatch.setattr(moderation._session, "post", fake_post)
    return calls


# ---------------------------------------------------------------- defaults


def test_chat_defaults_to_openrouter():
    assert config.LLM_API_BASE == "https://openrouter.ai/api/v1"


def test_moderation_follows_chat_unless_told_otherwise():
    assert config.MODERATION_API_BASE == config.LLM_API_BASE
    assert config.MODERATION_API_KEY == config.OPENROUTER_API_KEY
    assert config.LLM_API_KEY == config.OPENROUTER_API_KEY


def test_bases_carry_no_trailing_slash():
    """A base ending in "/" would build ".../v1//chat/completions", which some
    servers 404 on."""
    assert not config.LLM_API_BASE.endswith("/")
    assert not config.MODERATION_API_BASE.endswith("/")


# ---------------------------------------------------------------- routing


def test_chat_requests_go_to_the_configured_base(monkeypatch, captured):
    monkeypatch.setattr(config, "LLM_API_BASE", "http://pc.tailnet:11434/v1")
    llm._post_with_retry({"model": "local"}, timeout=5)

    assert captured[0]["url"] == "http://pc.tailnet:11434/v1/chat/completions"


def test_moderation_can_move_without_moving_chat(monkeypatch, captured):
    """The whole point: classification on local hardware while the tool-calling
    chat path stays on a model that is reliably good at tool calls."""
    monkeypatch.setattr(config, "LLM_API_BASE", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(config, "MODERATION_API_BASE", "http://pc.tailnet:11434/v1")

    moderation._classify("hello")
    llm._post_with_retry({"model": "remote"}, timeout=5)

    assert captured[0]["url"] == "http://pc.tailnet:11434/v1/chat/completions"
    assert captured[1]["url"] == "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------- key leak


def test_moderation_sends_its_own_key_not_the_openrouter_one(monkeypatch, captured):
    monkeypatch.setattr(config, "MODERATION_API_BASE", "http://pc.tailnet:11434/v1")
    monkeypatch.setattr(config, "MODERATION_API_KEY", "local-placeholder")

    moderation._classify("hello")

    assert captured[0]["headers"]["Authorization"] == "Bearer local-placeholder"
    assert config.OPENROUTER_API_KEY not in captured[0]["headers"]["Authorization"]


def test_chat_sends_its_own_key_not_the_openrouter_one(monkeypatch, captured):
    monkeypatch.setattr(config, "LLM_API_BASE", "http://pc.tailnet:11434/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "local-placeholder")

    llm._post_with_retry({"model": "local"}, timeout=5)

    assert captured[0]["headers"]["Authorization"] == "Bearer local-placeholder"


# ---------------------------------------------------------------- regression


@pytest.mark.parametrize("path", REPO_SOURCES)
def test_no_module_hardcodes_the_completions_host(path):
    """The actual regression. Both modules used to define their own
    _API_BASE = "https://openrouter.ai/api/v1", so redirecting either one
    meant editing source."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / path).read_text()
    assert "openrouter.ai" not in source, f"{path} hardcodes the API host; read it from config instead"
