"""ListStore — the shared checklist behind todos and groceries."""

from datetime import UTC, datetime, timedelta

import pytest

from bot import lists


@pytest.fixture
def todo_list(tmp_path):
    return lists.ListStore(str(tmp_path / "list.json"))


def _done_at(item: dict, when: datetime) -> dict:
    item["done"] = True
    item["completed_at"] = when.isoformat()
    return item


def test_add_returns_item_and_open_count(todo_list):
    item, open_count = todo_list.add(1, "buy milk")
    assert item["text"] == "buy milk"
    assert item["done"] is False
    assert open_count == 1

    _, open_count = todo_list.add(1, "walk dog")
    assert open_count == 2


def test_add_is_per_user(todo_list):
    todo_list.add(1, "mine")
    todo_list.add(2, "theirs")
    assert [i["text"] for i in todo_list.items(1)] == ["mine"]
    assert [i["text"] for i in todo_list.items(2)] == ["theirs"]


def test_match_is_case_insensitive_substring(todo_list):
    todo_list.add(1, "Buy Milk")
    assert [i["text"] for i in todo_list.find_open(1, "milk")] == ["Buy Milk"]
    assert [i["text"] for i in todo_list.find_open(1, "MILK")] == ["Buy Milk"]
    assert todo_list.find_open(1, "bread") == []


def test_match_ignores_done_items(todo_list):
    todo_list.add(1, "buy milk")
    with todo_list.update_for(1) as items:
        todo_list.mark_done(items[0])
    assert todo_list.find_open(1, "milk") == []


def test_match_on_blank_identifier_returns_nothing(todo_list):
    """An empty needle is a substring of everything — without the guard in
    match() this would silently check off the whole list."""
    todo_list.add(1, "buy milk")
    assert todo_list.match(todo_list.items(1), "   ") == []


def test_split_keeps_recently_done_but_hides_expired(todo_list):
    now = datetime.now(UTC)
    items = [
        todo_list.build("open one"),
        _done_at(todo_list.build("done just now"), now),
        _done_at(todo_list.build("done last week"), now - timedelta(days=7)),
    ]
    open_items, done_items = todo_list.split(items)
    assert [i["text"] for i in open_items] == ["open one"]
    assert [i["text"] for i in done_items] == ["done just now"]


def test_split_does_not_mutate(todo_list):
    """Listing must never write — split() is the read-only counterpart to
    prune() precisely so the list handlers don't have to take the lock."""
    items = [_done_at(todo_list.build("old"), datetime.now(UTC) - timedelta(days=7))]
    todo_list.split(items)
    assert len(items) == 1


def test_prune_drops_expired_done_items_in_place(todo_list):
    now = datetime.now(UTC)
    items = [
        todo_list.build("open"),
        _done_at(todo_list.build("recent"), now),
        _done_at(todo_list.build("stale"), now - timedelta(days=7)),
    ]
    todo_list.prune(items)
    assert [i["text"] for i in items] == ["open", "recent"]


def test_prune_drops_done_items_with_no_completed_at(todo_list):
    """Hand-edited or pre-completed_at items would otherwise live forever."""
    item = todo_list.build("no timestamp")
    item["done"] = True
    items = [item]
    todo_list.prune(items)
    assert items == []


def test_prune_drops_done_items_with_unparseable_completed_at(todo_list):
    item = todo_list.build("bad timestamp")
    item["done"] = True
    item["completed_at"] = "not a date"
    items = [item]
    todo_list.prune(items)
    assert items == []


def test_mark_done_sets_completion_timestamp(todo_list):
    item = todo_list.build("thing")
    todo_list.mark_done(item)
    assert item["done"] is True
    assert datetime.fromisoformat(item["completed_at"])


def test_get_finds_item_by_id(todo_list):
    item, _ = todo_list.add(1, "findable")
    assert todo_list.get(1, item["id"])["text"] == "findable"
    assert todo_list.get(1, "nope") is None
    assert todo_list.get(2, item["id"]) is None


def test_owners_of_open_prefers_the_callers_own_list(todo_list):
    """"milk" should mean your milk, not a housemate's."""
    todo_list.add(1, "milk")
    todo_list.add(2, "milk")
    owners = todo_list.owners_of_open("milk", prefer_user_id=1)
    assert [owner for owner, _ in owners] == [1]


def test_owners_of_open_falls_back_to_everyone_else(todo_list):
    todo_list.add(2, "milk")
    owners = todo_list.owners_of_open("milk", prefer_user_id=1)
    assert [owner for owner, _ in owners] == [2]


def test_merged_split_spans_users(todo_list):
    todo_list.add(1, "mine")
    todo_list.add(2, "theirs")
    open_all, _ = todo_list.merged_split()
    assert sorted(text for _, (text) in [(o, i["text"]) for o, i in open_all]) == ["mine", "theirs"]
    assert sorted(owner for owner, _ in open_all) == [1, 2]
