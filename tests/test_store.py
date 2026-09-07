"""UserKeyedStore and the cross-store erase that users.forget() depends on."""

import pytest

from bot import jsonstore, store


@pytest.fixture
def user_data(tmp_path):
    return store.user_store(str(tmp_path / "data.json"), list, label="data.json")


def test_get_returns_a_fresh_default_for_unknown_users(user_data):
    assert user_data.get(1) == []


def test_default_is_built_per_access_not_shared(user_data):
    """A shared mutable default would alias every user's list together."""
    first = user_data.get(1)
    first.append("leaked")
    assert user_data.get(2) == []


def test_set_and_get_roundtrip(user_data):
    user_data.set(1, ["a", "b"])
    assert user_data.get(1) == ["a", "b"]


def test_int_and_str_ids_address_the_same_entry(user_data):
    """JSON object keys are strings; the int/str conversion lives in the
    store so callers never see it."""
    user_data.set(1, ["x"])
    assert user_data.all() == {"1": ["x"]}


def test_update_for_creates_the_entry_and_persists_mutation(user_data):
    with user_data.update_for(7) as items:
        items.append("new")
    assert user_data.get(7) == ["new"]


def test_update_for_persists_without_reassignment(user_data):
    user_data.set(7, ["a"])
    with user_data.update_for(7) as items:
        items.append("b")
    assert user_data.get(7) == ["a", "b"]


def test_forget_reports_whether_anything_was_there(user_data):
    user_data.set(1, ["x"])
    assert user_data.forget(1) is True
    assert user_data.forget(1) is False
    assert user_data.get(1) == []


def test_user_ids_skips_non_numeric_keys(user_data, caplog):
    """A hand-edited file shouldn't take down an inventory."""
    jsonstore.write(user_data.path, {"1": [], "notanid": [], "2": []})
    assert user_data.user_ids() == {1, 2}


def test_forget_everywhere_reports_only_stores_that_had_data(tmp_path):
    a = store.user_store(str(tmp_path / "a.json"), list, label="a.json")
    b = store.user_store(str(tmp_path / "b.json"), list, label="b.json")
    a.set(5, ["something"])

    erased = store.forget_everywhere(5)
    assert "a.json" in erased
    assert "b.json" not in erased
    assert a.get(5) == []


def test_forget_everywhere_reaches_registered_erasers(tmp_path):
    """reminders.json is a flat list with an author_id field rather than a
    {user_id: value} map, so it registers a callable instead. A deletion
    that quietly skipped it would be worse than no deletion at all."""
    seen = []

    def erase(user_id: int) -> bool:
        seen.append(user_id)
        return True

    store.register_eraser("custom-store", erase)
    try:
        erased = store.forget_everywhere(5)
        assert seen == [5]
        assert "custom-store" in erased
    finally:
        store._ERASERS[:] = [e for e in store._ERASERS if e[0] != "custom-store"]


def test_forget_everywhere_continues_past_a_failing_store(tmp_path):
    """One broken store must not abandon the rest of someone's erasure."""
    good = store.user_store(str(tmp_path / "good.json"), list, label="good.json")
    good.set(5, ["x"])

    def boom(user_id: int) -> bool:
        raise OSError("disk gone")

    store.register_eraser("broken", boom)
    try:
        erased = store.forget_everywhere(5)
        assert "good.json" in erased
        assert "broken" not in erased
    finally:
        store._ERASERS[:] = [e for e in store._ERASERS if e[0] != "broken"]


def test_same_path_returns_the_same_store(tmp_path):
    """The cigarettes tool and cigboard's leaderboard both want
    cigarettes.json; registering it twice would double-count it."""
    path = str(tmp_path / "shared.json")
    assert store.user_store(path) is store.user_store(path)
