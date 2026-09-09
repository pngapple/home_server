"""
The notes tools: save something verbatim, retrieve it by search, forget it.

Notes are inert data, not instructions — unlike preferences.py's lessons,
nothing here ever reaches the system prompt, so there's no scope boundary to
assert. What matters is that notes are per-user (no leaking into someone
else's list) and that search/forget behave like the todo/preference tools
they're patterned after.
"""

from bot import notes
from bot.tools import dispatch, dispatch_result


def _add(ctx, text):
    return dispatch("add_note", {"text": text}, ctx)


class TestAddNote:
    def test_stores_a_note_for_the_caller(self, ctx):
        _add(ctx, "wifi password is hunter2")
        assert [n["text"] for n in notes.all_for(ctx.user_id)] == ["wifi password is hunter2"]

    def test_the_reply_shows_the_updated_list(self, ctx):
        reply = _add(ctx, "gate code 4471")
        assert "gate code 4471" in reply
        assert "NOTES" in reply

    def test_empty_text_is_refused(self, ctx):
        assert "can't be empty" in dispatch("add_note", {"text": "   "}, ctx)

    def test_a_missing_argument_is_caught_by_the_registry(self, ctx):
        assert "missing required argument" in dispatch("add_note", {}, ctx)


class TestListNotes:
    def test_lists_only_the_callers_own(self, ctx):
        from conftest import make_message

        from bot.tools import ToolContext

        _add(ctx, "Call me Max.")
        other = ToolContext(message=make_message(user_id=999), roles=frozenset())

        assert "Call me Max." in dispatch("list_notes", {}, ctx)
        assert "Call me Max." not in dispatch("list_notes", {}, other)

    def test_an_empty_list_reads_as_empty(self, ctx):
        assert "nothing saved" in dispatch("list_notes", {}, ctx)

    def test_query_filters_to_matching_notes(self, ctx):
        _add(ctx, "wifi password is hunter2")
        _add(ctx, "gate code 4471")

        reply = dispatch("list_notes", {"query": "wifi"}, ctx)
        assert "wifi password" in reply
        assert "gate code" not in reply

    def test_query_with_no_match_is_reported(self, ctx):
        _add(ctx, "wifi password is hunter2")
        assert "no notes match" in dispatch("list_notes", {"query": "nonexistent"}, ctx)


class TestForgetNote:
    def test_removes_a_matching_note(self, ctx):
        _add(ctx, "gate code 4471")
        reply = dispatch("forget_note", {"identifier": "gate code"}, ctx)
        assert "Forgotten" in reply
        assert notes.all_for(ctx.user_id) == []

    def test_no_match_is_reported(self, ctx):
        assert "nothing saved matches" in dispatch("forget_note", {"identifier": "nonexistent"}, ctx)

    def test_an_ambiguous_snippet_asks_rather_than_guessing(self, ctx):
        _add(ctx, "gate code for the front door")
        _add(ctx, "gate code for the back door")
        reply = dispatch("forget_note", {"identifier": "gate code"}, ctx)
        assert "matches more than one" in reply
        assert len(notes.all_for(ctx.user_id)) == 2

    def test_it_cannot_delete_another_users_note(self, ctx):
        notes.add(999, "someone else's note")
        assert "nothing saved matches" in dispatch("forget_note", {"identifier": "someone else's"}, ctx)
        assert len(notes.all_for(999)) == 1

    def test_no_tool_here_reports_a_defect_on_normal_use(self, ctx):
        """These paths return refusals, not crashes — a refusal must not be
        logged as a bug to go and fix (see episodes.Recorder.raised)."""
        result = dispatch_result("forget_note", {"identifier": "nope"}, ctx)
        assert result.ok
