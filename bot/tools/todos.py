"""
TODO-list tool: lets the LLM add items to the user's personal todo list,
list them, and check items off.

Stored as a flat JSON dict keyed by Discord user id, same
deliberately-not-a-database pattern as tools/cigarettes.py:

  {"123456789": [{"id": "a1b2c3d4", "text": "Buy milk", "done": false,
                   "created_at": "2026-09-01T12:00:00+00:00"}, ...]}

Completed items are kept (done=true) rather than deleted, so list_todos can
still show "recently completed" for a bit of context — see _prune_done.

Unlike the other household tools in this package (calendar, reminders,
groceries), todos aren't role-gated — each user's list is keyed by their own
Discord id, so there's nothing shared to protect and no reason to keep
non-household members out.
"""

import logging
import os
import textwrap
from datetime import UTC, datetime, timedelta

from .. import config, jsonstore
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.todos")

# How long a completed item sticks around in "recently completed" before
# being pruned for good, so the file doesn't grow forever.
_DONE_RETENTION = timedelta(days=1)


def _load(user_id: int) -> list[dict]:
    return jsonstore.read(config.TODOS_FILE, {}).get(str(user_id), [])


def _prune_done(items: list[dict]) -> None:
    """Drop completed items older than _DONE_RETENTION, in place."""
    cutoff = datetime.now(UTC) - _DONE_RETENTION
    items[:] = [
        item
        for item in items
        if not item.get("done") or datetime.fromisoformat(item["completed_at"]) > cutoff
    ]


def _find(items: list[dict], identifier: str) -> list[dict]:
    """Match open items by a case-insensitive substring of their text."""
    open_items = [item for item in items if not item.get("done")]
    needle = identifier.strip().lower()
    return [item for item in open_items if needle in item["text"].lower()]


def find_open(user_id: int, identifier: str) -> list[dict]:
    """Public wrapper around _find, for other tools (recurring location
    reminders in tools/reminders.py) that need to resolve a todo by the same
    text-snippet matching complete_todo uses."""
    return _find(_load(user_id), identifier)


def get_todo(user_id: int, todo_id: str) -> dict | None:
    """Look up one todo by id, so tools/reminders.py's recurring location
    reminders can check whether the item they're linked to is still open."""
    return next((item for item in _load(user_id) if item["id"] == todo_id), None)


# Discord doesn't render box-drawing characters aligned outside a monospace
# code block, so the card is always wrapped in ``` fences. The title sits
# *above* the frame rather than inside a bordered line — emoji render at an
# inconsistent column width across clients/fonts, which twice threw off a
# right border that shared a line with one. Plain dashes never have that
# problem, since they're always exactly _CARD_WIDTH columns wide either way.
_CARD_WIDTH = 26


def _wrapped(text: str, initial_indent: str, subsequent_indent: str) -> list[str]:
    """Wraps `text` to _CARD_WIDTH so a long item can't stick out past the
    frame, instead of overflowing it on one line."""
    return textwrap.wrap(text, width=_CARD_WIDTH, initial_indent=initial_indent, subsequent_indent=subsequent_indent)


def _card(open_items: list[dict], done_items: list[dict]) -> str:
    border = "─" * _CARD_WIDTH
    lines = ["📝  TODO LIST", "┌" + border + "┐"]

    if open_items:
        for item in open_items:
            lines += _wrapped(item["text"], "  • ", "    ")
    else:
        lines.append("  (nothing open — nice)")

    if done_items:
        lines.append("")
        lines.append("  ✓ done recently")
        for item in done_items:
            lines += _wrapped(item["text"], "    ", "    ")

    lines.append("└" + border + "┘")
    return "```\n" + "\n".join(lines) + "\n```"


@tool(
    name="add_todo",
    description="Add an item to the user's personal todo list.",
    properties={
        "text": {
            "type": "string",
            "description": "The todo item text, e.g. 'Buy milk'.",
        },
    },
    required=["text"],
)
def handle_add_todo(arguments: dict, ctx: ToolContext) -> str:
    text = arguments["text"].strip()
    if not text:
        return "Error: todo text can't be empty."

    with jsonstore.update(config.TODOS_FILE, {}) as data:
        items = data.setdefault(str(ctx.user_id), [])
        _prune_done(items)
        items.append(
            {
                "id": os.urandom(4).hex(),
                "text": text,
                "done": False,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        open_count = sum(1 for item in items if not item.get("done"))
        card = _card([i for i in items if not i.get("done")], [i for i in items if i.get("done")])

    return (
        f"Added '{text}' to your todo list ({open_count} open item{'s' if open_count != 1 else ''}).\n\n"
        "(Relay the card below to the user verbatim, unchanged, including "
        "the code block — don't rewrite, reformat, or summarize it.)\n\n" + card
    )


@tool(
    name="list_todos",
    description=(
        "List the user's todo items. Shows open items first, plus any "
        "recently completed ones for context. Returns a pre-formatted card "
        "for Discord — relay it back to the user exactly as given, inside "
        "its own code block, instead of rewriting or summarizing it."
    ),
)
def handle_list_todos(arguments: dict, ctx: ToolContext) -> str:
    items = _load(ctx.user_id)
    open_items = [item for item in items if not item.get("done")]
    done_items = [item for item in items if item.get("done")]

    card = _card(open_items, done_items)
    return (
        "(Relay the card below to the user verbatim, unchanged, including "
        "the code block — don't rewrite, reformat, or summarize it.)\n\n" + card
    )


@tool(
    name="complete_todo",
    description=(
        "Mark a todo item as done, checking it off the user's list. Identify "
        "the item by a distinctive snippet of its text — call list_todos "
        "first if you aren't sure which item the user means."
    ),
    properties={
        "identifier": {
            "type": "string",
            "description": "A distinctive snippet of the item's text, e.g. 'milk'.",
        },
    },
    required=["identifier"],
)
def handle_complete_todo(arguments: dict, ctx: ToolContext) -> str:
    identifier = arguments["identifier"]

    with jsonstore.update(config.TODOS_FILE, {}) as data:
        items = data.setdefault(str(ctx.user_id), [])
        _prune_done(items)
        matches = _find(items, identifier)

        if not matches:
            return f"Error: no open todo matches '{identifier}'. Call list_todos to check."
        if len(matches) > 1:
            texts = ", ".join(f"'{m['text']}'" for m in matches)
            return f"Error: '{identifier}' matches multiple items ({texts}). Ask the user to be more specific."

        item = matches[0]
        item["done"] = True
        item["completed_at"] = datetime.now(UTC).isoformat()
        card = _card([i for i in items if not i.get("done")], [i for i in items if i.get("done")])

    return (
        f"Checked off '{item['text']}'.\n\n"
        "(Relay the card below to the user verbatim, unchanged, including "
        "the code block — don't rewrite, reformat, or summarize it.)\n\n" + card
    )
