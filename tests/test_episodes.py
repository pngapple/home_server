"""The per-turn outcome log."""

import time

import pytest

from bot import episodes


def _record(**kwargs):
    defaults = dict(user_text="hello", outcome=episodes.Outcome.OK, user_id=1, message_id=100)
    episodes.record(**{**defaults, **kwargs})


def test_record_and_read_back():
    _record(user_text="what's on my list", reply="nothing open")
    (row,) = episodes.recent()
    assert row["user_text"] == "what's on my list"
    assert row["reply"] == "nothing open"
    assert row["outcome"] == episodes.Outcome.OK


def test_tools_called_roundtrips_as_structured_data():
    _record(tools_called=[{"name": "add_todo", "ok": True}])
    (row,) = episodes.recent()
    assert row["tools_called"] == [{"name": "add_todo", "ok": True}]


def test_long_text_is_truncated_not_rejected():
    _record(user_text="x" * 10_000)
    (row,) = episodes.recent()
    assert len(row["user_text"]) < 10_000
    assert row["user_text"].endswith("[truncated]")


def test_recent_filters_by_outcome():
    _record(outcome=episodes.Outcome.OK)
    _record(outcome=episodes.Outcome.MAX_ITERATIONS)
    rows = episodes.recent(outcomes=frozenset({episodes.Outcome.MAX_ITERATIONS}))
    assert [r["outcome"] for r in rows] == [episodes.Outcome.MAX_ITERATIONS]


def test_recent_excludes_rows_outside_the_window():
    _record()
    episodes.record(user_text="ancient", outcome=episodes.Outcome.OK)
    with __import__("bot.db", fromlist=["db"]).write() as conn:
        conn.execute("UPDATE episodes SET ts = ? WHERE user_text = 'ancient'", (time.time() - 30 * 86400,))
    assert [r["user_text"] for r in episodes.recent(days=7)] == ["hello"]


def test_failures_collects_the_give_up_paths():
    _record(outcome=episodes.Outcome.OK)
    for outcome in sorted(episodes.FAILURES):
        _record(outcome=outcome)
    assert {r["outcome"] for r in episodes.failures()} == episodes.FAILURES


def test_moderated_turns_are_not_failures():
    """Refusing a flagged message is the system working, not failing."""
    _record(outcome=episodes.Outcome.MODERATED)
    assert episodes.failures() == []


def test_failures_includes_thumbed_down_successes():
    """A turn that "worked" but was wrong is invisible to the outcome column
    — the thumbs-down is the only signal there is."""
    _record(outcome=episodes.Outcome.OK, message_id=100, reply="wrong answer")
    episodes.attach_reply(100, 555)
    assert episodes.record_feedback(555, -1)
    assert [r["reply"] for r in episodes.failures()] == ["wrong answer"]


def test_attach_reply_links_a_turn_to_the_message_that_answered_it():
    _record(message_id=100)
    episodes.attach_reply(100, 555)
    assert episodes.recent()[0]["reply_id"] == 555


def test_attach_reply_does_not_overwrite_an_existing_link():
    """A second reply in the same turn shouldn't steal the reaction target."""
    _record(message_id=100)
    episodes.attach_reply(100, 555)
    episodes.attach_reply(100, 666)
    assert episodes.recent()[0]["reply_id"] == 555


def test_feedback_on_an_unknown_message_reports_no_match():
    """A reaction on some other bot message must be distinguishable from one
    that actually labelled an episode."""
    assert episodes.record_feedback(999, 1) is False


def test_summary_counts_outcomes_and_feedback():
    _record(outcome=episodes.Outcome.OK, message_id=1)
    _record(outcome=episodes.Outcome.OK, message_id=2)
    _record(outcome=episodes.Outcome.TOOL_ERROR, message_id=3)
    episodes.attach_reply(1, 11)
    episodes.record_feedback(11, 1)
    episodes.attach_reply(3, 33)
    episodes.record_feedback(33, -1)

    summary = episodes.summary()
    assert summary["total"] == 3
    assert summary["by_outcome"] == {episodes.Outcome.OK: 2, episodes.Outcome.TOOL_ERROR: 1}
    assert summary["failures"] == 1
    assert summary["thumbs_up"] == 1
    assert summary["thumbs_down"] == 1


def test_summary_of_an_empty_log():
    assert episodes.summary()["total"] == 0


def test_prune_drops_rows_outside_the_retention_window():
    _record()
    with __import__("bot.db", fromlist=["db"]).write() as conn:
        conn.execute("UPDATE episodes SET ts = ?", (time.time() - 400 * 86400,))
    assert episodes.prune(180) == 1
    assert episodes.recent(days=10_000) == []


def test_recording_never_raises(monkeypatch):
    """A telemetry problem must not turn a reply the user was about to get
    into an error."""
    from bot import db

    def broken():
        raise RuntimeError("database on fire")

    monkeypatch.setattr(db, "write", broken)
    _record()  # must not raise
    assert episodes.record_feedback(1, 1) is False
    assert episodes.prune(1) == 0


def test_reads_never_raise(monkeypatch):
    from bot import db

    def broken():
        raise RuntimeError("database on fire")

    monkeypatch.setattr(db, "read", broken)
    assert episodes.recent() == []
    assert episodes.failures() == []
    assert episodes.summary()["total"] == 0


class TestRecorder:
    def test_records_the_decision_trace(self):
        rec = episodes.Recorder(user_text="hi", user_id=1, message_id=100)
        rec.tool_call("add_todo")
        rec.tool_call("list_todos")
        rec.finish(episodes.Outcome.OK, reply="done")

        (row,) = episodes.recent()
        assert [t["name"] for t in row["tools_called"]] == ["add_todo", "list_todos"]
        assert all(t["ok"] for t in row["tools_called"])

    def test_raised_distinguishes_a_bug_from_a_refusal(self):
        """A permission gate firing is the system working; a handler blowing
        up is not, and only the second should pull a turn into the failure
        list."""
        rec = episodes.Recorder(user_text="hi")
        rec.tool_call("set_plug_power", "denied: owner_only")
        assert not rec.raised()
        rec.tool_call("set_plug_power", "raised: KasaException: unreachable")
        assert rec.raised()

    def test_missing_arguments_are_not_treated_as_a_bug(self):
        rec = episodes.Recorder(user_text="hi")
        rec.tool_call("add_todo", "missing_args: text")
        assert not rec.raised()

    def test_finish_records_duration(self):
        rec = episodes.Recorder(user_text="hi")
        rec.finish(episodes.Outcome.OK)
        assert episodes.recent()[0]["duration_s"] >= 0
