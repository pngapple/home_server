"""
Grocery-list tool: everyone gets one personal grocery list they add to; the
"shared" view aggregates everyone's open (and recently bought) items into
one card so the household can see it all at once. There's no separately
stored shared list to add to — "shared" is just a read-time merge over each
person's own list.

Stored as a flat JSON dict keyed by Discord user id, same
deliberately-not-a-database pattern as tools/todos.py:

  {"123456789": [{"id": "a1b2c3d4", "text": "Milk", "added_by": "Alex",
                   "done": false, "created_at": "..."}, ...]}

"added_by" is a display-name snapshot taken when the item was added, purely
for labeling items in the shared view — cheaper than fetching everyone's
current name on every list_groceries call, and good enough for that.

Checking off an item you don't own (identified while searching everyone's
lists — see _find_match) marks it done in its owner's list and DMs them, so
they find out even though they didn't do the checking off themselves.
Checked-off items are kept (done=true) rather than deleted, so list_groceries
can still show "recently bought" for a bit of context — see _split.
"""

import asyncio
import logging
import os
import textwrap
from datetime import UTC, datetime, timedelta

from .. import config, jsonstore
from ..discord_client import client
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.groceries")

# A household tool — gated to config.HOUSEHOLD_ROLE_NAME (see permissions.py)
# so randoms in the server/DMs can't touch it.
_HOUSEHOLD = config.HOUSEHOLD_ROLE_NAME

SCOPES = ("personal", "shared")
DEFAULT_SCOPE = "personal"

# How long a checked-off item sticks around in "recently bought" before
# being pruned for good, so the file doesn't grow forever.
_DONE_RETENTION = timedelta(days=1)


def _added_by(ctx: ToolContext) -> str:
    author = ctx.message.author
    return getattr(author, "display_name", None) or str(author)


def _prune_done(items: list[dict]) -> None:
    """Drop checked-off items older than _DONE_RETENTION, in place."""
    cutoff = datetime.now(UTC) - _DONE_RETENTION
    items[:] = [
        item
        for item in items
        if not item.get("done") or datetime.fromisoformat(item["completed_at"]) > cutoff
    ]


def _split(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Open items, plus done ones still inside the retention window —
    read-only, unlike _prune_done, so listing never has to write the file."""
    cutoff = datetime.now(UTC) - _DONE_RETENTION
    open_items = [item for item in items if not item.get("done")]
    done_items = [
        item for item in items if item.get("done") and datetime.fromisoformat(item["completed_at"]) > cutoff
    ]
    return open_items, done_items


def _find_match(data: dict, ctx: ToolContext, identifier: str) -> list[tuple[str, dict]]:
    """Match open items by a case-insensitive substring of their text,
    searching the caller's own list first and only falling back to
    everyone else's lists if nothing of theirs matches — so "milk" prefers
    your own milk over someone else's."""
    needle = identifier.strip().lower()
    own_id = str(ctx.user_id)

    own_matches = [(own_id, item) for item in data.get(own_id, []) if not item.get("done") and needle in item["text"].lower()]
    if own_matches:
        return own_matches

    return [
        (user_id, item)
        for user_id, items in data.items()
        if user_id != own_id
        for item in items
        if not item.get("done") and needle in item["text"].lower()
    ]


def _notify_checked_off(owner_id: int, item_text: str, checked_by: str) -> None:
    # Tool handlers run in a worker thread (app.py hands ask_llm to
    # asyncio.to_thread), so there's no running loop here to create_task on —
    # hand the coroutine to the Discord client's loop explicitly, same as
    # tools/reminders.py's schedule_reminder.
    asyncio.run_coroutine_threadsafe(_send_checkoff_dm(owner_id, item_text, checked_by), client.loop)


async def _send_checkoff_dm(owner_id: int, item_text: str, checked_by: str) -> None:
    try:
        user = await client.fetch_user(owner_id)
        await user.send(f"🛒 {checked_by} checked off '{item_text}' from your grocery list.")
    except Exception:
        log.exception("Failed to notify user %s about checked-off grocery item", owner_id)


def _normalize_scope(raw: str | None) -> str:
    scope = (raw or DEFAULT_SCOPE).strip().lower()
    return scope if scope in SCOPES else DEFAULT_SCOPE


# Discord doesn't render box-drawing characters aligned outside a monospace
# code block, so the card is always wrapped in ``` fences. The title sits
# *above* the frame rather than inside a bordered line — emoji render at an
# inconsistent column width across clients/fonts, which is what threw off
# the todo list's border when a title shared a line with one (see
# tools/todos.py). Plain dashes never have that problem, since they're
# always exactly _CARD_WIDTH columns wide either way.
_CARD_WIDTH = 26


def _wrapped(text: str, initial_indent: str, subsequent_indent: str) -> list[str]:
    """Wraps `text` to _CARD_WIDTH so a long item (or its "(added by)" tag)
    can't stick out past the frame, instead of overflowing it on one line."""
    return textwrap.wrap(text, width=_CARD_WIDTH, initial_indent=initial_indent, subsequent_indent=subsequent_indent)


def _card(scope: str, open_items: list[dict], done_items: list[dict]) -> str:
    label = "SHARED GROCERIES" if scope == "shared" else "MY GROCERIES"
    border = "─" * _CARD_WIDTH
    lines = [f"🛒  {label}", "┌" + border + "┐"]

    if open_items:
        for item in open_items:
            suffix = f"  ({item['added_by']})" if scope == "shared" else ""
            lines += _wrapped(f"{item['text']}{suffix}", "  • ", "    ")
    else:
        lines.append("  (nothing here — nice)")

    if done_items:
        lines.append("")
        lines.append("  ✓ bought recently")
        for item in done_items:
            suffix = f"  ({item['added_by']})" if scope == "shared" else ""
            lines += _wrapped(f"{item['text']}{suffix}", "    ", "    ")

    lines.append("└" + border + "┘")
    return "```\n" + "\n".join(lines) + "\n```"


@tool(
    name="add_grocery_item",
    description="Add an item to the user's own personal grocery list.",
    properties={
        "text": {
            "type": "string",
            "description": "The grocery item, e.g. 'Milk'.",
        },
    },
    required=["text"],
    required_role=_HOUSEHOLD,
)
def handle_add_grocery_item(arguments: dict, ctx: ToolContext) -> str:
    text = arguments["text"].strip()
    if not text:
        return "Error: grocery item text can't be empty."

    with jsonstore.update(config.GROCERIES_FILE, {}) as data:
        items = data.setdefault(str(ctx.user_id), [])
        _prune_done(items)
        items.append(
            {
                "id": os.urandom(4).hex(),
                "text": text,
                "added_by": _added_by(ctx),
                "done": False,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        open_count = sum(1 for item in items if not item.get("done"))

    return f"Added '{text}' to your grocery list ({open_count} open item{'s' if open_count != 1 else ''})."


@tool(
    name="list_groceries",
    description=(
        "List grocery items. 'personal' (default) shows just the user's own "
        "list; 'shared' merges everyone's open items into one list so the "
        "household can see it all together. Returns a pre-formatted card "
        "for Discord — relay it back to the user exactly as given, inside "
        "its own code block, instead of rewriting or summarizing it."
    ),
    properties={
        "scope": {
            "type": "string",
            "enum": list(SCOPES),
            "description": (
                "'personal' for just this user's own list (default), or "
                "'shared' to see everyone's items together — use 'shared' "
                "when the user asks for the household/family/everyone's list."
            ),
        },
    },
    required_role=_HOUSEHOLD,
)
def handle_list_groceries(arguments: dict, ctx: ToolContext) -> str:
    scope = _normalize_scope(arguments.get("scope"))
    data = jsonstore.read(config.GROCERIES_FILE, {})

    if scope == "personal":
        open_items, done_items = _split(data.get(str(ctx.user_id), []))
    else:
        open_items, done_items = [], []
        for items in data.values():
            o, d = _split(items)
            open_items += o
            done_items += d

    card = _card(scope, open_items, done_items)
    return (
        "(Relay the card below to the user verbatim, unchanged, including "
        "the code block — don't rewrite, reformat, or summarize it.)\n\n" + card
    )


@tool(
    name="check_off_grocery_item",
    description=(
        "Mark a grocery item as bought, checking it off. Identify the item "
        "by a distinctive snippet of its text — call list_groceries first "
        "if you aren't sure which item the user means. This searches the "
        "user's own list first, then everyone else's, so it also works for "
        "checking off something someone else added (e.g. after seeing the "
        "shared list) — the owner gets DM'd when that happens."
    ),
    properties={
        "identifier": {
            "type": "string",
            "description": "A distinctive snippet of the item's text, e.g. 'milk'.",
        },
    },
    required=["identifier"],
    required_role=_HOUSEHOLD,
)
def handle_check_off_grocery_item(arguments: dict, ctx: ToolContext) -> str:
    identifier = arguments["identifier"]
    own_id = str(ctx.user_id)
    notify: tuple[int, str] | None = None

    with jsonstore.update(config.GROCERIES_FILE, {}) as data:
        for items in data.values():
            _prune_done(items)

        matches = _find_match(data, ctx, identifier)
        if not matches:
            return f"Error: no open item matches '{identifier}'. Call list_groceries to check."
        if len(matches) > 1:
            texts = ", ".join(
                f"'{item['text']}'" + ("" if owner_id == own_id else f" ({item['added_by']})") for owner_id, item in matches
            )
            return f"Error: '{identifier}' matches multiple items ({texts}). Ask the user to be more specific."

        owner_id, item = matches[0]
        item["done"] = True
        item["completed_at"] = datetime.now(UTC).isoformat()
        item_text = item["text"]
        if owner_id != own_id:
            notify = (int(owner_id), item["added_by"])

    if notify is not None:
        owner_user_id, owner_name = notify
        _notify_checked_off(owner_user_id, item_text, _added_by(ctx))
        return f"Checked off '{item_text}' from {owner_name}'s list — they've been notified."

    return f"Checked off '{item_text}'."
