"""!deploy's patch notes must wait for the restart to finish.

Before this fix, restart() only stashed channel_id, so the commit summary
had to be broadcast *before* calling it — while the service was still up but
about to disappear for a restart. If someone reads that message right as the
restart lands, the bot looks dead mid-announcement. The fix rides the patch
note along in the same notify file so it goes out only once the new process
calls consume_pending_notify() from on_ready, pairing it with "back online".
"""

from bot import deploy


def test_restart_stashes_patch_notes_and_header_for_after_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "_NOTIFY_FILE", str(tmp_path / "deploy_notify.json"))
    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *a, **k: None)

    deploy.restart(12345, patch_notes="✅ Committed and pushed: 'do the thing'", header="**Deploy by Someone**")

    pending = deploy.consume_pending_notify()
    assert pending == {
        "channel_id": 12345,
        "patch_notes": "✅ Committed and pushed: 'do the thing'",
        "header": "**Deploy by Someone**",
    }


def test_consume_pending_notify_is_none_without_a_pending_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "_NOTIFY_FILE", str(tmp_path / "deploy_notify.json"))

    assert deploy.consume_pending_notify() is None


def test_consume_pending_notify_removes_the_file_so_it_fires_once(monkeypatch, tmp_path):
    notify_file = tmp_path / "deploy_notify.json"
    monkeypatch.setattr(deploy, "_NOTIFY_FILE", str(notify_file))
    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *a, **k: None)

    deploy.restart(1, patch_notes=None, header=None)
    assert notify_file.exists()
    deploy.consume_pending_notify()
    assert not notify_file.exists()
    # A second read (e.g. a stray extra on_ready fire) must not replay it.
    assert deploy.consume_pending_notify() is None
