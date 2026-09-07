"""
Every exit path from ask_llm leaves a record.

This is the guarantee the whole reflection layer rests on: if a turn can end
without an episode row, the failures it represents are invisible again — which
is the state this bot was in before episodes.py existed. So each give-up path
in llm.py gets a test, driven through a stubbed OpenRouter.
"""

import pytest

from bot import episodes, llm


def tool_msg(name: str, arguments: str = "{}") -> dict:
    return {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": arguments}}]}


def text_msg(content: str) -> dict:
    return {"role": "assistant", "content": content}


@pytest.fixture
def openrouter(monkeypatch):
    """Scripts the model's responses in order. Anything past the end of the
    script is a plain text reply, which is how a normal turn wraps up."""

    def script(*responses):
        remaining = list(responses)

        def fake_call(messages, tools=None, tool_choice=None, **kwargs):
            return remaining.pop(0) if remaining else text_msg("all done")

        monkeypatch.setattr(llm, "call_openrouter", fake_call)

    return script


def only_episode() -> dict:
    rows = episodes.recent()
    assert len(rows) == 1, f"expected exactly one episode, got {len(rows)}"
    return rows[0]


def test_a_normal_turn_is_recorded_as_ok(message, openrouter):
    openrouter(tool_msg("no_action_needed"), text_msg("hello there"))

    assert llm.ask_llm(message, "hi") == "hello there"

    row = only_episode()
    assert row["outcome"] == episodes.Outcome.OK
    assert row["user_text"] == "hi"
    assert row["reply"] == "hello there"
    assert [t["name"] for t in row["tools_called"]] == ["no_action_needed"]
    assert row["message_id"] == message.id
    assert row["user_id"] == message.author.id


def test_a_raising_tool_makes_the_turn_a_failure(message, openrouter, registry):
    """The model will happily smooth over a crashed tool in prose. The turn
    still has to land in the failure list."""

    def boom(arguments, ctx):
        raise ValueError("kaboom")

    registry.register("t_boom", "d", boom)
    openrouter(tool_msg("t_boom"), text_msg("Sorry, I couldn't do that."))

    llm.ask_llm(message, "do the thing")

    row = only_episode()
    assert row["outcome"] == episodes.Outcome.TOOL_ERROR
    assert row["tools_called"][0]["error"].startswith("raised: ValueError: kaboom")
    assert episodes.failures()


def test_a_permission_denial_is_not_a_failure(message, openrouter, registry):
    """A gate doing its job shouldn't show up as something to go and fix."""
    registry.register("t_admin", "d", lambda a, c: "did it", owner_only=True)
    openrouter(tool_msg("t_admin"), text_msg("You're not allowed to do that."))

    llm.ask_llm(message, "restricted thing")

    row = only_episode()
    assert row["outcome"] == episodes.Outcome.OK
    assert row["tools_called"][0]["error"] == "denied: owner_only"
    assert episodes.failures() == []


def test_malformed_tool_arguments_are_recorded(message, openrouter, registry):
    """A model that can't emit valid JSON for a schema is usually telling
    you the schema needs rewording."""
    registry.register("t_args", "d", lambda a, c: "ok")
    openrouter(tool_msg("t_args", arguments="{not json"), text_msg("done"))

    llm.ask_llm(message, "go")

    assert only_episode()["tools_called"][0]["error"] == "bad_arguments_json"


def test_ignoring_a_forced_tool_call_twice_is_recorded(message, openrouter):
    """The desk-lights shape: plain text claiming an action that never
    happened. The bot refuses to relay it — and now says so on the record."""
    openrouter(text_msg("I turned the lights on!"), text_msg("I turned the lights on!"))

    reply = llm.ask_llm(message, "turn on the lights")

    assert "something went wrong confirming that" in reply
    row = only_episode()
    assert row["outcome"] == episodes.Outcome.FORCED_TOOL_MISS
    assert row["reply"] == reply


def test_running_out_of_tool_iterations_is_recorded(message, openrouter, registry):
    registry.register("t_loop", "d", lambda a, c: "again")
    openrouter(*[tool_msg("t_loop")] * llm.MAX_TOOL_ITERATIONS)

    reply = llm.ask_llm(message, "loop forever")

    assert "got stuck juggling tools" in reply
    row = only_episode()
    assert row["outcome"] == episodes.Outcome.MAX_ITERATIONS
    assert row["iterations"] == llm.MAX_TOOL_ITERATIONS


def test_an_llm_exception_is_recorded_and_still_raises(message, monkeypatch):
    """app.py needs the exception to reach it (that's what posts the error
    message), and the reflection pass needs the row."""

    def explode(*args, **kwargs):
        raise RuntimeError("openrouter is down")

    monkeypatch.setattr(llm, "call_openrouter", explode)

    with pytest.raises(RuntimeError):
        llm.ask_llm(message, "hi")

    row = only_episode()
    assert row["outcome"] == episodes.Outcome.LLM_ERROR
    assert row["detail"] == "RuntimeError: openrouter is down"


def test_the_model_used_is_recorded(message, openrouter):
    """Which model produced a failure is the first question the reflection
    pass asks — half of these are capability, not bugs."""
    openrouter(tool_msg("no_action_needed"), text_msg("hi"))
    llm.ask_llm(message, "hi")
    assert only_episode()["model"] == llm.config.OPENROUTER_MODEL


def test_a_recording_failure_does_not_break_the_reply(message, openrouter, monkeypatch):
    from bot import db

    openrouter(tool_msg("no_action_needed"), text_msg("hello there"))
    monkeypatch.setattr(db, "write", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    assert llm.ask_llm(message, "hi") == "hello there"
