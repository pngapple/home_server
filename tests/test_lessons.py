"""
The learned-notes store.

Two properties matter more than the rest and have the most tests: scope
isolation (a lesson must not leak between people, which is the security
boundary that makes chat-driven writes safe at all) and boundedness (an
unbounded prompt makes a small model worse, so lessons have to expire and
compete for a fixed budget rather than accumulating).
"""

from datetime import UTC, datetime, timedelta

import pytest

from bot import jsonstore, lessons, store


def backdate(lesson_id: str, days: float) -> None:
    when = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with jsonstore.update(lessons._PATH, []) as data:
        for item in data:
            if item["id"] == lesson_id:
                item["reinforced_at"] = when


class TestAdd:
    def test_stores_a_lesson_for_one_user(self):
        lesson = lessons.add("Show the full list after every change.", subject=1)
        assert lesson["scope"] == lessons.USER
        assert lesson["subject"] == 1
        assert [item["text"] for item in lessons.for_user(1)] == ["Show the full list after every change."]

    def test_whitespace_is_collapsed(self):
        assert lessons.add("  too    many   spaces  ", subject=1)["text"] == "too many spaces"

    def test_empty_text_is_refused(self):
        assert lessons.add("   ", subject=1) is None
        assert lessons.for_user(1) == []

    def test_restating_a_lesson_reinforces_rather_than_duplicates(self):
        first = lessons.add("Always show the full list.", subject=1)
        backdate(first["id"], days=30)
        again = lessons.add("always show the FULL list!", subject=1)

        assert again["id"] == first["id"]
        assert len(lessons.for_user(1)) == 1
        assert lessons.for_user(1)[0]["reinforced_at"] > first["reinforced_at"]

    def test_restating_keeps_the_newest_phrasing(self):
        lessons.add("show the full list", subject=1)
        lessons.add("Show the full list.", subject=1)
        assert lessons.for_user(1)[0]["text"] == "Show the full list."

    def test_the_same_text_for_two_people_is_two_lessons(self):
        lessons.add("Be brief.", subject=1)
        lessons.add("Be brief.", subject=2)
        assert len(lessons.for_user(1)) == 1
        assert len(lessons.for_user(2)) == 1

    def test_the_per_user_cap_evicts_the_stalest(self, monkeypatch):
        monkeypatch.setattr(lessons.config, "MAX_LESSONS_PER_USER", 3)
        oldest = lessons.add("lesson zero", subject=1)
        backdate(oldest["id"], days=10)
        for i in range(3):
            lessons.add(f"lesson {i + 1}", subject=1)

        texts = [item["text"] for item in lessons.for_user(1)]
        assert len(texts) == 3
        assert "lesson zero" not in texts

    def test_the_cap_is_per_person(self, monkeypatch):
        monkeypatch.setattr(lessons.config, "MAX_LESSONS_PER_USER", 2)
        for i in range(3):
            lessons.add(f"mine {i}", subject=1)
        lessons.add("theirs", subject=2)
        assert len(lessons.for_user(1)) == 2
        assert len(lessons.for_user(2)) == 1


class TestScope:
    def test_a_users_lesson_reaches_only_that_user(self):
        """The security boundary: this is what makes it safe for ordinary
        chat to write into the system prompt at all."""
        lessons.add("Call me Max.", subject=1)
        assert [item["text"] for item in lessons.applicable(1)] == ["Call me Max."]
        assert lessons.applicable(2) == []

    def test_a_global_lesson_reaches_everyone(self):
        lessons.add("The house wifi is called Skynet.", scope=lessons.GLOBAL)
        assert len(lessons.applicable(1)) == 1
        assert len(lessons.applicable(999)) == 1

    def test_a_tool_lesson_applies_only_when_that_tool_is_in_play(self):
        lessons.add("add_todo's text argument is the item, not an id.", scope=lessons.TOOL, subject="add_todo")
        assert lessons.applicable(1, frozenset({"add_todo"}))
        assert lessons.applicable(1, frozenset({"list_groceries"})) == []

    def test_a_tool_lesson_is_not_shown_when_the_user_lacks_the_tool(self):
        """get_tool_schemas already hides tools a user can't call; the notes
        about them shouldn't leak either."""
        lessons.add("Kasa plugs take a few seconds.", scope=lessons.TOOL, subject="set_plug_power")
        assert lessons.applicable(1, frozenset()) == []


class TestBudget:
    def test_lessons_beyond_the_budget_are_dropped(self):
        for i in range(20):
            lessons.add(f"lesson number {i} with some padding text", subject=1)
        chosen = lessons.for_prompt(1, budget=200)
        assert 0 < len(chosen) < 20
        assert sum(len(item["text"]) + 3 for item in chosen) <= 200

    def test_the_most_recently_reinforced_win_the_budget(self):
        stale = lessons.add("stale lesson", subject=1)
        fresh = lessons.add("fresh lesson", subject=1)
        backdate(stale["id"], days=60)

        chosen = lessons.for_prompt(1, budget=len("fresh lesson") + 3)
        assert [item["id"] for item in chosen] == [fresh["id"]]

    def test_a_zero_budget_yields_nothing(self):
        lessons.add("something", subject=1)
        assert lessons.for_prompt(1, budget=0) == []


class TestRender:
    def test_nothing_learned_renders_as_empty(self):
        """So the prompt doesn't carry a dangling empty heading."""
        assert lessons.render([]) == ""

    def test_lessons_render_as_a_labelled_block(self):
        text = lessons.render([{"text": "Be brief."}, {"text": "Call me Max."}])
        assert "- Be brief." in text
        assert "- Call me Max." in text
        assert "earlier conversations" in text

    def test_the_block_defers_to_the_current_message(self):
        """A standing note must not override what someone is asking for
        right now."""
        assert "unless the current message says otherwise" in lessons.render([{"text": "x"}])


class TestPrune:
    def test_unreinforced_lessons_expire(self):
        stale = lessons.add("forgotten thing", subject=1)
        backdate(stale["id"], days=200)
        assert lessons.prune(ttl_days=120) == 1
        assert lessons.for_user(1) == []

    def test_recently_reinforced_lessons_survive(self):
        lessons.add("current thing", subject=1)
        assert lessons.prune(ttl_days=120) == 0
        assert len(lessons.for_user(1)) == 1

    def test_restating_a_lesson_resets_its_clock(self):
        """The mechanism by which a still-true lesson survives."""
        lesson = lessons.add("still true", subject=1)
        backdate(lesson["id"], days=200)
        lessons.add("still true", subject=1)

        assert lessons.prune(ttl_days=120) == 0
        assert len(lessons.for_user(1)) == 1

    def test_an_unreadable_timestamp_is_dropped_not_raised(self):
        lesson = lessons.add("hand edited", subject=1)
        with jsonstore.update(lessons._PATH, []) as data:
            data[0]["reinforced_at"] = "not a date"
        assert lessons.prune(ttl_days=120) == 1


class TestRemoval:
    def test_remove_by_id(self):
        lesson = lessons.add("temporary", subject=1)
        assert lessons.remove(lesson["id"])
        assert lessons.for_user(1) == []
        assert not lessons.remove(lesson["id"])

    def test_find_matches_a_snippet_case_insensitively(self):
        lessons.add("Show the full list after every change.", subject=1)
        assert len(lessons.find(1, "FULL LIST")) == 1
        assert lessons.find(1, "groceries") == []

    def test_find_on_a_blank_identifier_matches_nothing(self):
        """An empty needle is a substring of everything."""
        lessons.add("Show the full list.", subject=1)
        assert lessons.find(1, "  ") == []

    def test_find_does_not_reach_another_users_lessons(self):
        lessons.add("Call me Max.", subject=1)
        assert lessons.find(2, "Max") == []

    def test_forget_everywhere_erases_what_the_bot_learned(self):
        """Still acting on notes about someone who asked to be forgotten
        would be the worst kind of deletion bug."""
        lessons.add("Call me Max.", subject=1)
        lessons.add("Be brief.", scope=lessons.GLOBAL)

        erased = store.forget_everywhere(1)
        assert "lessons.json" in erased
        assert lessons.for_user(1) == []
        # A global lesson isn't about them, so it stays.
        assert len(lessons.all_lessons()) == 1
