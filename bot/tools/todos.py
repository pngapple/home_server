"""
TODO-list tool: lets the LLM add items to the user's personal todo list,
list them, and check items off.

The list itself — the item shape, the retention window for completed items,
the substring matching and the pruning — lives in ../lists.py, shared with
tools/groceries.py. The card rendering lives in ../cards.py. What's left
here is this tool's schema, its wording, and nothing else.

Unlike the other household tools in this package (calendar, reminders,
groceries), todos aren't role-gated — each user's list is keyed by their own
Discord id, so there's nothing shared to protect and no reason to keep
non-household members out.
"""

import logging

from .. import cards, config, lists
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.todos")

TODOS = lists.ListStore(config.TODOS_FILE)

_TITLE = "📝  TODO LIST"


def find_open(user_id: int, identifier: str) -> list[dict]:
    """Resolve a todo by the same text-snippet matching complete_todo uses,
    for other tools that need it (recurring location reminders in
    tools/reminders.py)."""
    return TODOS.find_open(user_id, identifier)


def get_todo(user_id: int, todo_id: str) -> dict | None:
    """Look up one todo by id, so tools/reminders.py's recurring location
    reminders can check whether the item they're linked to is still open."""
    return TODOS.get(user_id, todo_id)


def _card(open_items: list[dict], done_items: list[dict]) -> str:
    return cards.render(
        _TITLE,
        open_items,
        done_items,
        done_label="done recently",
        empty_label="nothing open — nice",
    )


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

    _, open_count = TODOS.add(ctx.user_id, text)
    open_items, done_items = TODOS.split_for(ctx.user_id)

    return cards.with_preamble(
        _card(open_items, done_items),
        f"Added '{text}' to your todo list ({open_count} open item{'s' if open_count != 1 else ''}).",
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
    open_items, done_items = TODOS.split_for(ctx.user_id)
    return cards.with_preamble(_card(open_items, done_items))


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

    with TODOS.update_for(ctx.user_id) as items:
        matches = TODOS.match(items, identifier)

        if not matches:
            return f"Error: no open todo matches '{identifier}'. Call list_todos to check."
        if len(matches) > 1:
            texts = ", ".join(f"'{m['text']}'" for m in matches)
            return f"Error: '{identifier}' matches multiple items ({texts}). Ask the user to be more specific."

        item = matches[0]
        TODOS.mark_done(item)
        open_items, done_items = TODOS.split(items)
        card = _card(open_items, done_items)

    return cards.with_preamble(card, f"Checked off '{item['text']}'.")
