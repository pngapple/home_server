"""
Feedback signals: the explicit 👍/👎 and the inferred re-ask.

The inferred one carries most of the weight in practice — almost nobody
reacts to a bot message — so its precision matters more than its recall. A
false positive here teaches the wrong lesson, which is worse than missing
one, and that's what the threshold tests below are pinning down.
"""

import time

import pytest

from bot import db, episodes
from bot.app import _feedback_value


def _episode(user_text="what's on my list", outcome=None, user_id=1, channel_id=5, message_id=100):
    episodes.record(
        user_text=user_text,
        outcome=outcome or episodes.Outcome.OK,
        user_id=user_id,
        channel_id=channel_id,
        message_id=message_id,
    )


class TestReactionParsing:
    @pytest.mark.parametrize("emoji", ["👍", "👍🏽", "👍️"])
    def test_thumbs_up_variants_all_count(self, emoji):
        assert _feedback_value(emoji) == 1

    @pytest.mark.parametrize("emoji", ["👎", "👎🏿"])
    def test_thumbs_down_variants_all_count(self, emoji):
        assert _feedback_value(emoji) == -1

    @pytest.mark.parametrize("emoji", ["🎉", "❤️", "🤔", "x"])
    def test_other_reactions_are_ignored(self, emoji):
        """People react to bot messages for all sorts of reasons."""
        assert _feedback_value(emoji) is None


class TestExplicitFeedback:
    def test_a_reaction_labels_the_turn_behind_the_reply(self):
        _episode(message_id=100)
        episodes.attach_reply(100, 555)
        assert episodes.record_feedback(555, -1)

        row = episodes.recent()[0]
        assert row["feedback"] == -1
        assert row["feedback_source"] == episodes.REACTION

    def test_removing_a_reaction_withdraws_the_label(self):
        """A misclick shouldn't permanently teach the wrong thing."""
        _episode(message_id=100)
        episodes.attach_reply(100, 555)
        episodes.record_feedback(555, 1)

        assert episodes.clear_feedback(555, 1)
        assert episodes.recent()[0]["feedback"] is None

    def test_removing_one_reaction_does_not_clear_a_different_one(self):
        """Switching 👍 to 👎 fires a remove for 👍 after the 👎 landed."""
        _episode(message_id=100)
        episodes.attach_reply(100, 555)
        episodes.record_feedback(555, 1)
        episodes.record_feedback(555, -1)

        assert not episodes.clear_feedback(555, 1)
        assert episodes.recent()[0]["feedback"] == -1


class TestRephraseDetection:
    @pytest.mark.parametrize(
        "before,after",
        [
            ("turn on the lights", "turn on the lights please"),
            ("what's on my todo list", "whats on my todo list"),
            ("remind me tomorrow at 3pm", "remind me tomorrow at 3"),
        ],
    )
    def test_repeats_are_recognized(self, before, after):
        assert episodes.looks_like_rephrase(before, after)

    @pytest.mark.parametrize(
        "before,after",
        [
            ("add milk to groceries", "add bread to groceries"),
            ("what's on my todo list", "turn off the desk lamp"),
            ("thanks", "thanks!"),
            ("ok", "no"),
        ],
    )
    def test_unrelated_and_too_short_messages_are_left_alone(self, before, after):
        """Two requests sharing a verb are not a complaint, and messages of
        one or two words score too erratically to trust."""
        assert not episodes.looks_like_rephrase(before, after)

    def test_a_quick_re_ask_marks_the_previous_turn(self):
        _episode(user_text="turn on the desk lights")
        assert episodes.note_possible_rephrase(1, 5, "turn on the desk lights please")

        row = episodes.recent()[0]
        assert row["feedback"] == -1
        assert row["feedback_source"] == episodes.REPHRASE

    def test_a_re_ask_much_later_is_a_new_request(self):
        _episode(user_text="turn on the desk lights")
        with db.write() as conn:
            conn.execute("UPDATE episodes SET ts = ?", (time.time() - episodes.REPHRASE_WINDOW_S - 10,))

        assert not episodes.note_possible_rephrase(1, 5, "turn on the desk lights please")
        assert episodes.recent()[0]["feedback"] is None

    def test_another_users_turn_is_not_touched(self):
        _episode(user_text="turn on the desk lights", user_id=1)
        assert not episodes.note_possible_rephrase(2, 5, "turn on the desk lights please")

    def test_a_different_channel_is_not_touched(self):
        _episode(user_text="turn on the desk lights", channel_id=5)
        assert not episodes.note_possible_rephrase(1, 99, "turn on the desk lights please")

    def test_an_already_failed_turn_is_not_relabelled(self):
        """It's accounted for; counting it twice would overstate the miss."""
        _episode(user_text="turn on the desk lights", outcome=episodes.Outcome.TOOL_ERROR)
        assert not episodes.note_possible_rephrase(1, 5, "turn on the desk lights please")

    def test_an_explicit_thumbs_up_is_not_overridden(self):
        """Someone who liked the reply and then rephrased has not changed
        their mind."""
        _episode(user_text="turn on the desk lights", message_id=100)
        episodes.attach_reply(100, 555)
        episodes.record_feedback(555, 1)

        assert not episodes.note_possible_rephrase(1, 5, "turn on the desk lights please")
        assert episodes.recent()[0]["feedback"] == 1

    def test_an_explicit_reaction_still_wins_after_an_inferred_miss(self):
        _episode(user_text="turn on the desk lights", message_id=100)
        episodes.attach_reply(100, 555)
        episodes.note_possible_rephrase(1, 5, "turn on the desk lights please")

        assert episodes.record_feedback(555, 1)
        row = episodes.recent()[0]
        assert row["feedback"] == 1
        assert row["feedback_source"] == episodes.REACTION

    def test_with_no_previous_turn_nothing_happens(self):
        assert not episodes.note_possible_rephrase(1, 5, "turn on the desk lights")


class TestSummary:
    def test_inferred_misses_are_counted_separately_from_thumbs_down(self):
        """They're much weaker evidence; conflating them would let the
        heuristic quietly dominate the failure list."""
        _episode(message_id=100)
        episodes.attach_reply(100, 555)
        episodes.record_feedback(555, -1)

        _episode(user_text="turn on the desk lights", message_id=200, channel_id=6)
        episodes.note_possible_rephrase(1, 6, "turn on the desk lights please")

        summary = episodes.summary()
        assert summary["thumbs_down"] == 1
        assert summary["inferred_misses"] == 1

    def test_both_kinds_reach_the_failure_list(self):
        _episode(user_text="turn on the desk lights")
        episodes.note_possible_rephrase(1, 5, "turn on the desk lights please")
        assert len(episodes.failures()) == 1
