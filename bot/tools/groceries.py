"""
Grocery-list tool: everyone gets one personal grocery list they add to; the
"shared" view aggregates everyone's open (and recently bought) items into
one card so the household can see it all at once. There's no separately
stored shared list to add to — "shared" is just a read-time merge over each
person's own list.

The list mechanics (item shape, retention, matching, pruning) are shared
with tools/todos.py via ../lists.py, and the card rendering via ../cards.py.
What's genuinely specific to groceries stays here: searching everyone's
lists rather than just the caller's, and DMing an item's owner when someone
else checks it off.

Items used to carry an "added_by" display-name snapshot, taken because
looking a name up per render was expensive. Now that ../users.py keeps
profiles, ownership is stored as the owner's id and the name is resolved at
render time — so the shared list shows what people are called *now* rather
than what they were called when they added the milk.

Checking off an item you don't own marks it done in its owner's list and DMs
them, so they find out even though they didn't do the checking off
themselves. Checked-off items are kept (done=true) rather than deleted, so
the card can show "recently bought" for a bit of context.
"""

import logging

from .. import cards, config, lists, notify, users
from . import ToolContext, tool

log = logging.getLogger("discord-llm-bot.tools.groceries")

# A household tool — see the `household` flag on @tool below.
GROCERIES = lists.ListStore(config.GROCERIES_FILE)

SCOPES = ("personal", "shared")
DEFAULT_SCOPE = "personal"


def _normalize_scope(raw: str | None) -> str:
    scope = (raw or DEFAULT_SCOPE).strip().lower()
    return scope if scope in SCOPES else DEFAULT_SCOPE


def _card(scope: str, open_owned: list[tuple[int, dict]], done_owned: list[tuple[int, dict]]) -> str:
    """`*_owned` are (owner_id, item) pairs. In the shared view each line is
    tagged with whose item it is, resolved from the profile store; in the
    personal view they're all the caller's, so the tag is left off."""
    title = "🛒  SHARED GROCERIES" if scope == "shared" else "🛒  MY GROCERIES"
    owner_of = {id(item): owner_id for owner_id, item in open_owned + done_owned}

    def tag(item: dict) -> str:
        if scope != "shared":
            return ""
        return f"  ({users.name(owner_of[id(item)])})"

    return cards.render(
        title,
        [item for _, item in open_owned],
        [item for _, item in done_owned],
        done_label="bought recently",
        empty_label="nothing here — nice",
        tag=tag,
    )


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
    household=True,
)
def handle_add_grocery_item(arguments: dict, ctx: ToolContext) -> str:
    text = arguments["text"].strip()
    if not text:
        return "Error: grocery item text can't be empty."

    _, open_count = GROCERIES.add(ctx.user_id, text)
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
    household=True,
)
def handle_list_groceries(arguments: dict, ctx: ToolContext) -> str:
    scope = _normalize_scope(arguments.get("scope"))

    if scope == "personal":
        open_items, done_items = GROCERIES.split_for(ctx.user_id)
        open_owned = [(ctx.user_id, item) for item in open_items]
        done_owned = [(ctx.user_id, item) for item in done_items]
    else:
        open_owned, done_owned = GROCERIES.merged_split()

    return cards.with_preamble(_card(scope, open_owned, done_owned))


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
    household=True,
)
def handle_check_off_grocery_item(arguments: dict, ctx: ToolContext) -> str:
    identifier = arguments["identifier"]
    notify_owner: tuple[int, str] | None = None

    with GROCERIES.update_all() as data:
        matches = GROCERIES.owners_of_open(identifier, ctx.user_id)
        if not matches:
            return f"Error: no open item matches '{identifier}'. Call list_groceries to check."
        if len(matches) > 1:
            texts = ", ".join(
                f"'{item['text']}'" + ("" if owner_id == ctx.user_id else f" ({users.name(owner_id)})")
                for owner_id, item in matches
            )
            return f"Error: '{identifier}' matches multiple items ({texts}). Ask the user to be more specific."

        # owners_of_open re-read the file, so mark the copy inside this
        # locked update rather than the detached one it handed back.
        owner_id, matched = matches[0]
        live = next(
            (i for i in data.get(str(owner_id), []) if i.get("id") == matched.get("id")),
            None,
        )
        if live is None:
            return f"Error: '{matched['text']}' was just changed by someone else. Call list_groceries and try again."

        GROCERIES.mark_done(live)
        item_text = live["text"]
        if owner_id != ctx.user_id:
            notify_owner = (owner_id, users.name(owner_id))

    if notify_owner is not None:
        owner_user_id, owner_name = notify_owner
        notify.dm(
            owner_user_id,
            f"🛒 {users.name(ctx.user_id)} checked off '{item_text}' from your grocery list.",
        )
        return f"Checked off '{item_text}' from {owner_name}'s list — they've been notified."

    return f"Checked off '{item_text}'."
