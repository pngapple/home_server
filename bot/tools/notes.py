"""
Notes tool: save something verbatim and get it back later by a text search.

The store itself — the item shape, id generation, the substring search —
lives in ../notes.py. What's left here is this tool's schema and wording.

Unlike preferences.py, a note is never fed into the system prompt — it's
inert data the user asked to have kept, not an instruction about how to
behave. Unlike todos.py/groceries.py, it has no done state; it just sits
there until forgotten.

Not role-gated, same reasoning as todos: each user's notes are keyed to
their own Discord id, so there's nothing shared to protect.
"""

import logging

from .. import cards, notes
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.notes")

_TITLE = "🗒️  NOTES"


def _card(items: list[dict]) -> str:
    return cards.render(
        _TITLE,
        items,
        [],
        done_label="",
        empty_label="nothing saved yet",
    )


@tool(
    name="add_note",
    description=(
        "Save a note for the user, verbatim, so they can ask for it back "
        "later. Use this for facts, snippets, or anything they explicitly "
        "ask you to 'note down' or 'remember' as information — a wifi "
        "password, a confirmation number, something someone told them. "
        "Do NOT use this for todo items (use add_todo), for standing rules "
        "about how you should behave (use remember_preference), or for "
        "anything that's really a task to do."
    ),
    properties={
        "text": {
            "type": "string",
            "description": "The note text, saved exactly as given.",
        },
    },
    required=["text"],
)
def handle_add_note(arguments: dict, ctx: ToolContext) -> str:
    text = arguments["text"].strip()
    if not text:
        return "Error: note text can't be empty."

    notes.add(ctx.user_id, text)
    return cards.with_preamble(_card(notes.all_for(ctx.user_id)), f"Noted: {text}")


@tool(
    name="list_notes",
    description=(
        "Retrieve the user's saved notes. Pass `query` to search for notes "
        "containing that text (e.g. 'wifi'); omit it to show everything. "
        "Returns a pre-formatted card for Discord — relay it back to the "
        "user exactly as given, inside its own code block, instead of "
        "rewriting or summarizing it."
    ),
    properties={
        "query": {
            "type": "string",
            "description": "Optional text to search for within saved notes.",
        },
    },
)
def handle_list_notes(arguments: dict, ctx: ToolContext) -> str:
    query = (arguments.get("query") or "").strip()
    if not query:
        return cards.with_preamble(_card(notes.all_for(ctx.user_id)))

    matches = notes.find(ctx.user_id, query)
    if not matches:
        return f"Error: no notes match '{query}'. Call list_notes with no query to see everything."
    return cards.with_preamble(_card(matches))


@tool(
    name="forget_note",
    description=(
        "Delete a saved note. Identify it by a distinctive snippet of its "
        "text — call list_notes first if you aren't sure which one they "
        "mean."
    ),
    properties={
        "identifier": {
            "type": "string",
            "description": "A distinctive snippet of the note's text.",
        },
    },
    required=["identifier"],
)
def handle_forget_note(arguments: dict, ctx: ToolContext) -> str:
    identifier = arguments["identifier"]
    matches = notes.find(ctx.user_id, identifier)

    if not matches:
        return f"Error: nothing saved matches '{identifier}'. Call list_notes to check."
    if len(matches) > 1:
        texts = ", ".join(f"'{m['text']}'" for m in matches)
        return f"Error: '{identifier}' matches more than one ({texts}). Ask the user which they mean."

    notes.remove(ctx.user_id, matches[0]["id"])
    return cards.with_preamble(
        _card(notes.all_for(ctx.user_id)), f"Forgotten: {matches[0]['text']}"
    )
