"""
Remembering how someone wants to be helped.

The write path into lessons.py. Everything here is deliberately fixed to
USER scope and `ctx.user_id` — a preference stated in chat only ever affects
the person who stated it. That's the whole safety story for letting ordinary
conversation write into the system prompt: a housemate cannot say "always do
X" and have it become a standing instruction for the household. Lessons that
reach everyone (GLOBAL/TOOL scope) have no chat-driven write path at all.

Like todos, these aren't role-gated — each person's notes are keyed to their
own Discord id, so there's nothing shared to protect.
"""

import logging

from .. import cards, lessons
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.preferences")

_TITLE = "🧠  WHAT I REMEMBER"


def _card(items: list[dict]) -> str:
    return cards.render(
        _TITLE,
        items,
        [],
        done_label="",
        empty_label="nothing yet — tell me how you like things",
    )


@tool(
    name="remember_preference",
    description=(
        "Save a durable preference about how this user wants you to behave, "
        "so you follow it in every future conversation. Use this when they "
        "say something is a standing rule — phrasings like 'always', 'from "
        "now on', 'stop doing X', 'I prefer', 'remember that I...'. "
        "Write the note as a short instruction to yourself in the third "
        "person, e.g. 'Show the full list after every change, not just the "
        "item that changed.' "
        "Do NOT use this for one-off requests ('add milk'), for facts that "
        "belong on a list, or for anything that only applies to the current "
        "conversation — those aren't preferences and will make you worse by "
        "cluttering what you remember."
    ),
    properties={
        "text": {
            "type": "string",
            "description": "The preference, phrased as a short instruction to yourself.",
        },
    },
    required=["text"],
)
def handle_remember_preference(arguments: dict, ctx: ToolContext) -> str:
    text = arguments["text"].strip()
    if not text:
        return "Error: the preference text can't be empty."

    lesson = lessons.add(text, scope=lessons.USER, subject=ctx.user_id, source=lessons.STATED)
    if lesson is None:
        return "Error: the preference text can't be empty."

    return cards.with_preamble(_card(lessons.for_user(ctx.user_id)), f"Got it — I'll remember: {lesson['text']}")


@tool(
    name="list_preferences",
    description=(
        "Show everything you've remembered about how this user wants you to "
        "behave. Use it when they ask what you know or remember about them. "
        "Returns a pre-formatted card for Discord — relay it back exactly as "
        "given, inside its own code block, instead of rewriting it."
    ),
)
def handle_list_preferences(arguments: dict, ctx: ToolContext) -> str:
    return cards.with_preamble(_card(lessons.for_user(ctx.user_id)))


@tool(
    name="forget_preference",
    description=(
        "Delete a preference you'd previously remembered about this user. "
        "Identify it by a distinctive snippet of its text — call "
        "list_preferences first if you aren't sure which one they mean."
    ),
    properties={
        "identifier": {
            "type": "string",
            "description": "A distinctive snippet of the preference's text, e.g. 'full list'.",
        },
    },
    required=["identifier"],
)
def handle_forget_preference(arguments: dict, ctx: ToolContext) -> str:
    identifier = arguments["identifier"]
    matches = lessons.find(ctx.user_id, identifier)

    if not matches:
        return f"Error: nothing remembered matches '{identifier}'. Call list_preferences to check."
    if len(matches) > 1:
        texts = ", ".join(f"'{m['text']}'" for m in matches)
        return f"Error: '{identifier}' matches more than one ({texts}). Ask the user which they mean."

    lessons.remove(matches[0]["id"])
    return cards.with_preamble(
        _card(lessons.for_user(ctx.user_id)), f"Forgotten: {matches[0]['text']}"
    )
