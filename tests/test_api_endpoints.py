"""Chat and moderation each pick their own endpoint, and fall back when it's down.

Either path can be pointed at an OpenAI-compatible server on the tailnet. That
box sleeps on idle, so "the endpoint didn't answer" is an ordinary condition
here rather than an outage, and it must not mean a dropped reply or — for the
classifier, which is the safety net — moderation silently switching off.

The base URL used to be a hardcoded constant duplicated in llm.py and
moderation.py, so redirecting either was a source edit in two places.
"""

import types

import pytest
import requests

from bot import completions, config, llm, moderation

LOCAL = "http://pc.tailnet:11434/v1"
OPENROUTER = completions.OPENROUTER_BASE


def _response(payload=None, status=200):
    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"HTTP {status}", response=types.SimpleNamespace(status_code=status))

    return types.SimpleNamespace(
        status_code=status,
        json=lambda: payload or {"choices": [{"message": {"content": '{"flagged": false}'}}], "usage": {}},
        raise_for_status=raise_for_status,
    )


@pytest.fixture
def http(monkeypatch):
    """Records every request and lets a test script per-host behaviour."""
    calls = []
    behaviour = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        for host, outcome in behaviour.items():
            if url.startswith(host):
                if isinstance(outcome, Exception):
                    raise outcome
                return _response(status=outcome)
        return _response()

    monkeypatch.setattr(completions._session, "post", fake_post)
    return types.SimpleNamespace(calls=calls, behaviour=behaviour)


def _endpoint(base, model="m", key="k"):
    return completions.Endpoint(base, key, model, "test")


# ---------------------------------------------------------------- defaults


def test_chat_defaults_to_openrouter():
    assert config.LLM_API_BASE == OPENROUTER
    assert config.LLM_MODEL == config.OPENROUTER_MODEL


def test_moderation_follows_chat_unless_told_otherwise():
    assert config.MODERATION_API_BASE == config.LLM_API_BASE
    assert config.MODERATION_API_KEY == config.OPENROUTER_API_KEY
    assert config.LLM_API_KEY == config.OPENROUTER_API_KEY


def test_bases_carry_no_trailing_slash():
    """A base ending in "/" builds ".../v1//chat/completions", which some
    servers 404 on."""
    assert not config.LLM_API_BASE.endswith("/")
    assert not config.MODERATION_API_BASE.endswith("/")


def test_an_unconfigured_install_has_nothing_to_fall_back_to():
    """Default config must issue exactly one request per turn, as before."""
    assert llm._endpoints()[1] is None
    assert moderation._endpoints()[1] is None


# ---------------------------------------------------------------- routing


def test_requests_go_to_the_configured_base(http):
    completions.post({"messages": []}, _endpoint(LOCAL), timeout=5)
    assert http.calls[0]["url"] == f"{LOCAL}/chat/completions"


def test_the_model_travels_with_the_endpoint_not_the_payload(http):
    completions.post({"messages": []}, _endpoint(LOCAL, model="qwen2.5:7b"), timeout=5)
    assert http.calls[0]["json"]["model"] == "qwen2.5:7b"


def test_moderation_can_move_without_moving_chat(monkeypatch, http):
    """The point of the split: classification on local hardware while the
    tool-calling chat path stays on a model reliably good at tool calls."""
    monkeypatch.setattr(config, "MODERATION_API_BASE", LOCAL)

    moderation._classify("hello")
    llm_primary, _ = llm._endpoints()

    assert http.calls[0]["url"].startswith(LOCAL)
    assert llm_primary.base == OPENROUTER


# ---------------------------------------------------------------- fallback


def test_an_unreachable_endpoint_falls_back_to_openrouter(http):
    http.behaviour[LOCAL] = requests.ConnectionError("connection refused")

    resp, served = completions.post(
        {"messages": []},
        _endpoint(LOCAL, model="qwen2.5:7b"),
        timeout=5,
        fallback=_endpoint(OPENROUTER, model="anthropic/claude-haiku-4.5"),
    )

    assert [c["url"] for c in http.calls] == [
        f"{LOCAL}/chat/completions",
        f"{OPENROUTER}/chat/completions",
    ]
    assert served.base == OPENROUTER
    # The local slug would be rejected upstream, so the fallback must swap it.
    assert http.calls[1]["json"]["model"] == "anthropic/claude-haiku-4.5"


def test_a_sleeping_box_does_not_switch_moderation_off(monkeypatch, http):
    """Falling open was the old behaviour for any error. Now it's the last
    resort: an asleep PC costs a few cents, not the safety net."""
    monkeypatch.setattr(config, "MODERATION_API_BASE", LOCAL)
    http.behaviour[LOCAL] = requests.ConnectionError("asleep")

    moderation._classify("hello")

    assert [c["url"] for c in http.calls] == [
        f"{LOCAL}/chat/completions",
        f"{OPENROUTER}/chat/completions",
    ]


def test_a_bad_request_is_not_retried_against_a_paid_api(http):
    """A 400 is the request's fault and would fail identically upstream."""
    http.behaviour[LOCAL] = 400

    with pytest.raises(requests.HTTPError):
        completions.post(
            {"messages": []}, _endpoint(LOCAL), timeout=5, fallback=_endpoint(OPENROUTER)
        )

    assert len(http.calls) == 1


def test_a_5xx_does_fall_back(http):
    http.behaviour[LOCAL] = 503
    completions.post({"messages": []}, _endpoint(LOCAL), timeout=5, fallback=_endpoint(OPENROUTER))
    assert len(http.calls) == 2


def test_openrouter_failing_raises_rather_than_calling_itself_twice(http):
    http.behaviour[OPENROUTER] = requests.ConnectionError("down")

    with pytest.raises(requests.ConnectionError):
        completions.post(
            {"messages": []}, _endpoint(OPENROUTER), timeout=5, fallback=_endpoint(OPENROUTER)
        )

    assert len(http.calls) == 1


# ---------------------------------------------------------------- key leak


def test_each_endpoint_sends_its_own_key(http):
    completions.post({"messages": []}, _endpoint(LOCAL, key="local-placeholder"), timeout=5)
    assert http.calls[0]["headers"]["Authorization"] == "Bearer local-placeholder"


def test_moderation_does_not_hand_the_paid_key_to_a_local_server(monkeypatch, http):
    monkeypatch.setattr(config, "MODERATION_API_BASE", LOCAL)
    monkeypatch.setattr(config, "MODERATION_API_KEY", "local-placeholder")

    moderation._classify("hello")

    assert http.calls[0]["headers"]["Authorization"] == "Bearer local-placeholder"
    assert config.OPENROUTER_API_KEY not in http.calls[0]["headers"]["Authorization"]


# ---------------------------------------------------------------- metrics


def test_a_local_call_is_not_billed_as_openrouter_spend():
    """metrics.record(source=...) drives the /llm/ dashboard's cost figures.
    A local call costs nothing; filing it as OpenRouter inflates them."""
    assert _endpoint(LOCAL).metrics_source == "local"
    assert _endpoint(OPENROUTER).metrics_source == "openrouter"


# ---------------------------------------------------------------- regression


@pytest.mark.parametrize("path", ["bot/llm.py", "bot/moderation.py"])
def test_no_call_site_hardcodes_the_api_host(path):
    """Both modules used to define their own
    _API_BASE = "https://openrouter.ai/api/v1". completions.py is the single
    place allowed to name the host, because the fallback target has to be
    written down somewhere."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / path).read_text()
    assert "openrouter.ai" not in source, f"{path} hardcodes the API host; read it from config instead"
