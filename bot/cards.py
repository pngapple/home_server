"""
The boxed list card the bot renders into Discord for todos and groceries.

Discord doesn't align box-drawing characters outside a monospace code block,
so a card is always wrapped in ``` fences. The title sits *above* the frame
rather than inside a bordered line: emoji render at an inconsistent column
width across clients and fonts, which twice threw off a right border that
shared a line with one. Plain dashes never have that problem, since they're
always exactly CARD_WIDTH columns wide either way.

The "relay this verbatim" preamble lives here too. It was pasted at four
call sites, which is four chances for the wording to drift — and the model
follows it noticeably less reliably when it does.
"""

import textwrap

CARD_WIDTH = 26

RELAY_PREAMBLE = (
    "(Relay the card below to the user verbatim, unchanged, including "
    "the code block — don't rewrite, reformat, or summarize it.)"
)


def _wrapped(text: str, initial_indent: str, subsequent_indent: str) -> list[str]:
    """Wraps `text` to CARD_WIDTH so a long item (or its trailing tag) can't
    stick out past the frame, instead of overflowing it on one line."""
    return textwrap.wrap(
        text, width=CARD_WIDTH, initial_indent=initial_indent, subsequent_indent=subsequent_indent
    )


def render(
    title: str,
    open_items: list[dict],
    done_items: list[dict],
    *,
    done_label: str,
    empty_label: str,
    tag=None,
) -> str:
    """One card. `tag` optionally returns a suffix to append to an item's
    text (the shared grocery list uses it to label whose item it is);
    returning "" or None from it leaves the item bare."""
    border = "─" * CARD_WIDTH
    lines = [title, "┌" + border + "┐"]

    def label(item: dict) -> str:
        suffix = tag(item) if tag is not None else ""
        return f"{item['text']}{suffix or ''}"

    if open_items:
        for item in open_items:
            lines += _wrapped(label(item), "  • ", "    ")
    else:
        lines.append(f"  ({empty_label})")

    if done_items:
        lines.append("")
        lines.append(f"  ✓ {done_label}")
        for item in done_items:
            lines += _wrapped(label(item), "    ", "    ")

    lines.append("└" + border + "┘")
    return "```\n" + "\n".join(lines) + "\n```"


def with_preamble(card: str, prefix: str = "") -> str:
    """A card plus the instruction telling the model to pass it through
    untouched. `prefix` is any status line that should come first (e.g.
    "Added 'Milk' to your grocery list.")."""
    lead = f"{prefix}\n\n" if prefix else ""
    return f"{lead}{RELAY_PREAMBLE}\n\n{card}"
