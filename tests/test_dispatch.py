"""The tool registry's gates: permissions, argument validation, and the
promise that a raising handler never escapes into the chat loop."""

import pytest

from bot import tools
from bot.tools import ToolContext


def _ctx(message, roles=()):
    return ToolContext(message=message, roles=frozenset(roles))


def test_unknown_tool_is_reported_not_raised(message):
    assert "no such tool" in tools.dispatch("nope", {}, _ctx(message))


def test_missing_required_argument_is_caught_before_the_handler(registry, message):
    """The model routinely omits arguments it declared as required."""
    called = []
    registry.register("t_req", "d", lambda a, c: called.append(a) or "ok", required=["text"])

    result = registry.dispatch("t_req", {}, _ctx(message))
    assert "missing required argument(s) for t_req: text" in result
    assert called == []


def test_empty_string_counts_as_a_missing_argument(registry, message):
    registry.register("t_blank", "d", lambda a, c: "ok", required=["text"])
    assert "missing required argument" in registry.dispatch("t_blank", {"text": ""}, _ctx(message))


def test_handler_exception_becomes_an_error_string(registry, message):
    """A raising tool must not take down the whole chat turn."""

    def boom(arguments, ctx):
        raise ValueError("kaboom")

    registry.register("t_boom", "d", boom)
    assert registry.dispatch("t_boom", {}, _ctx(message)) == "Error: tool 't_boom' failed unexpectedly."


def test_owner_only_blocks_non_admins(registry, message):
    registry.register("t_owner", "d", lambda a, c: "did it", owner_only=True)
    assert "restricted to an administrator" in registry.dispatch("t_owner", {}, _ctx(message))


def test_owner_only_allows_the_admin_role(registry, message):
    registry.register("t_owner2", "d", lambda a, c: "did it", owner_only=True)
    assert registry.dispatch("t_owner2", {}, _ctx(message, ["Administrator"])) == "did it"


def test_required_role_blocks_users_without_it(registry, message):
    registry.register("t_house", "d", lambda a, c: "did it", household=True)
    assert "requires the 'Home Resident' role" in registry.dispatch("t_house", {}, _ctx(message))


def test_required_role_allows_users_with_it(registry, message):
    registry.register("t_house2", "d", lambda a, c: "did it", household=True)
    assert registry.dispatch("t_house2", {}, _ctx(message, ["Home Resident"])) == "did it"


def test_household_shorthand_sets_the_configured_role(registry):
    registry.register("t_house3", "d", lambda a, c: "x", household=True)
    assert registry._REGISTRY["t_house3"].required_role == "Home Resident"


def test_duplicate_registration_is_refused(registry):
    registry.register("t_dupe", "d", lambda a, c: "x")
    with pytest.raises(ValueError, match="Duplicate tool name"):
        registry.register("t_dupe", "d", lambda a, c: "x")


def test_schema_is_built_in_openai_function_shape(registry):
    registry.register(
        "t_schema",
        "does a thing",
        lambda a, c: "x",
        properties={"text": {"type": "string"}},
        required=["text"],
    )
    schema = registry._REGISTRY["t_schema"].schema
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "t_schema"
    assert schema["function"]["description"] == "does a thing"
    assert schema["function"]["parameters"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }


def test_get_tool_schemas_hides_tools_the_user_cannot_call(registry):
    """So "what can you do?" reflects real access rather than the full
    registry."""
    registry.register("t_open", "d", lambda a, c: "x")
    registry.register("t_admin", "d", lambda a, c: "x", owner_only=True)
    registry.register("t_resident", "d", lambda a, c: "x", household=True)

    names = {s["function"]["name"] for s in registry.get_tool_schemas(user_id=2, roles=frozenset())}
    assert "t_open" in names
    assert "t_admin" not in names
    assert "t_resident" not in names

    names = {
        s["function"]["name"]
        for s in registry.get_tool_schemas(user_id=2, roles=frozenset({"Administrator", "Home Resident"}))
    }
    assert {"t_open", "t_admin", "t_resident"} <= names


def test_no_action_needed_is_always_available():
    """llm.py forces a tool call on the first turn of every message; this is
    the escape hatch that keeps ordinary conversation working."""
    names = {s["function"]["name"] for s in tools.get_tool_schemas(user_id=2, roles=frozenset())}
    assert "no_action_needed" in names
