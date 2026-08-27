"""
Tool registry for LLM function calling.

To add a new tool:
  1. Create tools/your_tool.py.
  2. Define an OpenAI-style function schema (see tools/reminders.py for an
     example) and a handler(arguments: dict, ctx: ToolContext) -> str.
  3. Call register(name, schema, handler) at module import time.
  4. Import your module below, next to the reminders import, so it actually
     registers.

The handler's return value is fed back to the model as the tool result (a
plain string) — the model then turns that into a natural-language reply, so
it's fine for handlers to return short human-readable status/error strings
rather than structured data.
"""

import logging
from dataclasses import dataclass
from typing import Callable

import discord

log = logging.getLogger("discord-llm-bot.tools")


@dataclass
class ToolContext:
    """Discord-side context handed to a tool handler alongside its LLM-supplied arguments."""

    message: discord.Message


@dataclass
class Tool:
    schema: dict
    handler: Callable[[dict, ToolContext], str]


_REGISTRY: dict[str, Tool] = {}


def register(name: str, schema: dict, handler: Callable[[dict, ToolContext], str]) -> None:
    _REGISTRY[name] = Tool(schema=schema, handler=handler)


def get_tool_schemas() -> list[dict]:
    return [tool.schema for tool in _REGISTRY.values()]


def dispatch(name: str, arguments: dict, ctx: ToolContext) -> str:
    tool = _REGISTRY.get(name)
    if tool is None:
        return f"Error: no such tool '{name}'."
    try:
        return tool.handler(arguments, ctx)
    except Exception:
        log.exception("Tool '%s' raised while handling arguments=%r", name, arguments)
        return f"Error: tool '{name}' failed unexpectedly."


# Import tool modules so their register(...) calls run. Add new tools here.
from . import reminders  # noqa: E402,F401
