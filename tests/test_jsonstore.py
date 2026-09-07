"""jsonstore — the atomic-write/locked-update layer under every JSON store."""

import json
import os

from bot import jsonstore


def test_read_returns_default_for_a_missing_file(tmp_path):
    assert jsonstore.read(str(tmp_path / "nope.json"), {"fallback": True}) == {"fallback": True}


def test_read_returns_default_for_corrupt_json(tmp_path):
    """A truncated file (a power cut mid-write on a Pi) must read as empty
    rather than taking the bot down."""
    path = tmp_path / "broken.json"
    path.write_text('{"half": ')
    assert jsonstore.read(str(path), {}) == {}


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / "data.json")
    jsonstore.write(path, {"a": 1})
    assert json.loads(open(path).read()) == {"a": 1}
    assert not os.path.exists(f"{path}.tmp")


def test_write_honors_the_requested_mode(tmp_path):
    """profiles.json and calendar_tokens.json hold secrets and are 0600."""
    path = str(tmp_path / "secret.json")
    jsonstore.write(path, {"token": "x"}, mode=0o600)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_update_writes_back_on_clean_exit(tmp_path):
    path = str(tmp_path / "data.json")
    with jsonstore.update(path, []) as data:
        data.append("one")
    assert jsonstore.read(path, None) == ["one"]


def test_update_skips_the_write_when_the_block_raises(tmp_path):
    """Raising inside the block leaves the file as it was, so a tool that
    fails halfway doesn't commit a partial change."""
    path = str(tmp_path / "data.json")
    jsonstore.write(path, ["original"])
    try:
        with jsonstore.update(path, []) as data:
            data.append("uncommitted")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert jsonstore.read(path, None) == ["original"]


def test_lock_is_per_path_and_stable(tmp_path):
    a, b = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    assert jsonstore.lock(a) is jsonstore.lock(a)
    assert jsonstore.lock(a) is not jsonstore.lock(b)
