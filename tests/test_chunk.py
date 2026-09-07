"""app.chunk — splitting a reply across Discord's 2000-character cap."""

from bot.app import chunk


def test_short_text_is_one_chunk():
    assert list(chunk("hello", limit=100)) == ["hello"]


def test_empty_text_yields_nothing():
    """An empty reply must not send an empty message (Discord rejects it)."""
    assert list(chunk("", limit=100)) == []


def test_prefers_line_boundaries():
    """Code blocks and lists shouldn't get cut mid-line."""
    text = "aaaa\nbbbb\ncccc"
    assert list(chunk(text, limit=10)) == ["aaaa\nbbbb", "cccc"]


def test_hard_cuts_a_single_line_longer_than_the_limit():
    assert list(chunk("x" * 25, limit=10)) == ["x" * 10, "x" * 10, "x" * 5]


def test_every_chunk_fits_the_limit():
    text = "\n".join("line %d %s" % (i, "y" * i) for i in range(60))
    assert all(len(part) <= 40 for part in chunk(text, limit=40))


def test_content_is_preserved_across_line_splits():
    text = "\n".join(f"line {i}" for i in range(50))
    assert "\n".join(chunk(text, limit=30)) == text
