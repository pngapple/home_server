"""
Tool registry for LLM function calling.

To add a new tool:
  1. Create tools/your_tool.py.
  2. Write `handler(arguments: dict, ctx: ToolContext) -> str` and decorate
     it with @tool(...), which builds the OpenAI function schema and
     registers it in one step (see tools/reminders.py for an example).
  3. Add your module to _TOOL_MODULES below so the import actually runs.

The handler's return value is fed back to the model as the tool result (a
plain string) — the model then turns that into a natural-language reply, so
it's fine for handlers to return short human-readable status/error strings
rather than structured data.

dispatch() enforces the `required` argument list before calling a handler,
enforces `owner_only` for tools that shouldn't be drivable by anyone who can
@mention the bot (see permissions.is_admin), and enforces `required_role`
for tools gated to a specific Discord role (see permissions.py) — e.g. the
household tools (todos, groceries, reminders, calendar) require the "Home
Resident" role so randoms in the server/DMs can't touch them.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module

import discord

from .. import permissions

log = logging.getLogger("discord-llm-bot.tools")


@dataclass(frozen=True)
class ToolContext:
    """Discord-side context handed to a tool handler alongside its LLM-supplied arguments."""

    message: discord.Message
    # The author's Discord role names, resolved once per incoming message —
    # see permissions.resolve_roles(). Empty for tests/callers that don't
    # need role-gated tools.
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def user_id(self) -> int:
        return self.message.author.id

    @property
    def channel_id(self) -> int:
        return self.message.channel.id


Handler = Callable[[dict, ToolContext], str]


@dataclass(frozen=True)
class Tool:
    name: str
    schema: dict
    handler: Handler
    required: tuple[str, ...] = ()
    owner_only: bool = False
    required_role: str | None = None


_REGISTRY: dict[str, Tool] = {}


def register(
    name: str,
    description: str,
    handler: Handler,
    properties: dict | None = None,
    required: list[str] | None = None,
    owner_only: bool = False,
    required_role: str | None = None,
) -> None:
    """Add one tool to the registry, building its OpenAI function schema."""
    if name in _REGISTRY:
        raise ValueError(f"Duplicate tool name registered: {name!r}")
    parameters: dict = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = list(required)
    _REGISTRY[name] = Tool(
        name=name,
        schema={"type": "function", "function": {"name": name, "description": description, "parameters": parameters}},
        handler=handler,
        required=tuple(required or ()),
        owner_only=owner_only,
        required_role=required_role,
    )


def tool(
    name: str,
    description: str,
    properties: dict | None = None,
    required: list[str] | None = None,
    owner_only: bool = False,
    required_role: str | None = None,
):
    """Decorator form of register(), so a tool is one self-contained function."""

    def decorate(handler: Handler) -> Handler:
        register(name, description, handler, properties, required, owner_only, required_role)
        return handler

    return decorate


def get_tool_schemas(user_id: int, roles: frozenset[str]) -> list[dict]:
    """Schemas for only the tools this user is actually allowed to call, so
    the model doesn't describe or attempt functionality dispatch() would
    just reject — e.g. "what can you do?" reflects real access rather than
    the full registry."""
    return [
        t.schema
        for t in _REGISTRY.values()
        if (not t.owner_only or permissions.is_admin(user_id, roles))
        and (not t.required_role or t.required_role in roles)
    ]


def dispatch(name: str, arguments: dict, ctx: ToolContext) -> str:
    entry = _REGISTRY.get(name)
    if entry is None:
        return f"Error: no such tool '{name}'."

    if entry.owner_only and not permissions.is_admin(ctx.user_id, ctx.roles):
        log.warning("Blocked owner-only tool '%s' for user %s", name, ctx.user_id)
        return f"Error: '{name}' is restricted to an administrator. Tell the user they aren't authorized to do that."

    if entry.required_role and entry.required_role not in ctx.roles:
        log.warning("Blocked '%s' for user %s missing role %r", name, ctx.user_id, entry.required_role)
        return f"Error: '{name}' requires the '{entry.required_role}' role. Tell the user they aren't authorized to do that."

    # The model routinely omits arguments it declared as required, so check
    # here once rather than in every handler. Empty strings count as missing.
    missing = [key for key in entry.required if not arguments.get(key)]
    if missing:
        return f"Error: missing required argument(s) for {name}: {', '.join(missing)}."

    try:
        return entry.handler(arguments, ctx)
    except Exception:
        log.exception("Tool '%s' raised while handling arguments=%r", name, arguments)
        return f"Error: tool '{name}' failed unexpectedly."


# A no-op the model can pick when nothing else applies. llm.py forces
# tool_choice="required" on the first turn of every message so the model
# can't just skip tool-calling and free-text a claimed result (it did
# exactly that for a set_reminder request once, silently) — this is the
# escape hatch for when no real tool actually applies, so ordinary
# conversation still works, and every "no action" decision is logged
# instead of being invisible.
@tool(
    name="no_action_needed",
    description=(
        "Call this when the message is just conversation, a question, or "
        "anything else that doesn't require any of the other tools. You'll "
        "still give your normal natural-language reply right after."
    ),
)
def _handle_no_action(arguments: dict, ctx: ToolContext) -> str:
    return "No tool action needed for this message."


# Tool modules, imported for their @tool registrations. Add new tools here.
_TOOL_MODULES = ("reminders", "calendar", "kasa", "cigarettes", "todos", "groceries")

for _module in _TOOL_MODULES:
    import_module(f"{__name__}.{_module}")
