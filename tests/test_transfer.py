from threading import Event
from pathlib import Path
import pytest
from xdep.git import Change
from xdep.profiles import Profile, ProfileStore
from xdep.transfer import LocalRemote, SFTPRemote, deploy, remote_path


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested/a.txt").write_bytes(b"new" * 100000)
    remote = LocalRemote(tmp_path / "remote")
    remote.mkdirs("/site/nested")
    remote.path("/site/nested/a.txt").write_bytes(b"old")
    return root, remote, Profile("Test"), [Change("nested/a.txt", "M")]


def run(setup, **kwargs):
    root, remote, profile, changes = setup
    return deploy(root, changes, profile, remote, kwargs.pop("cancel", Event()), **kwargs)


def test_dry_run_no_remote_writes(setup):
    _, remote, _, _ = setup
    before = {str(p): p.read_bytes() for p in remote.root.rglob("*") if p.is_file()}
    results = run(setup)
    assert results[0].state == "Planned"
    assert {str(p): p.read_bytes() for p in remote.root.rglob("*") if p.is_file()} == before
    assert not remote.path("/backups").exists()


def test_backup_before_overwrite_and_unique_runs(setup):
    root, remote, _, _ = setup
    first = run(setup, dry_run=False)[0]
    assert first.state == "Uploaded"
    assert remote.path(first.backup).read_bytes() == b"old"
    assert remote.path("/site/nested/a.txt").read_bytes() == (root / "nested/a.txt").read_bytes()
    second = run(setup, dry_run=False)[0]
    assert first.backup != second.backup
    assert remote.path(first.backup).read_bytes() == b"old"


def test_new_nested_file_and_empty_file(setup):
    root, remote, profile, _ = setup
    (root / "empty").touch()
    (root / "nested/new").write_text("hello")
    rows = deploy(root, [Change("empty", "?"), Change("nested/new", "?")], profile, remote, Event(), dry_run=False)
    assert all(r.state == "Uploaded" and not r.backup for r in rows)
    assert remote.path("/site/empty").read_bytes() == b""
    assert remote.path("/site/nested/new").read_text() == "hello"


def test_cancellation_during_upload_preserves_destination(setup):
    _, remote, _, _ = setup
    cancel = Event()
    rows = run(setup, dry_run=False, cancel=cancel, progress=lambda *args: cancel.set())
    assert rows[0].state == "Cancelled"
    assert remote.path("/site/nested/a.txt").read_bytes() == b"old"
    assert not list(remote.root.rglob("*.tmp"))
    assert remote.path(rows[0].backup).read_bytes() == b"old"


def test_cancelled_before_start_marks_all_pending(setup):
    cancel = Event()
    cancel.set()
    assert run(setup, cancel=cancel)[0].state == "Cancelled"


def test_backup_failure_blocks_overwrite(setup, monkeypatch):
    _, remote, _, _ = setup
    original = remote.open
    def failing(path, mode):
        if path.startswith("/backups"):
            raise PermissionError("backup denied")
        return original(path, mode)
    monkeypatch.setattr(remote, "open", failing)
    assert run(setup, dry_run=False)[0].state == "Failed"
    assert remote.path("/site/nested/a.txt").read_bytes() == b"old"


def test_replace_failure_keeps_original_and_cleans_temp(setup, monkeypatch):
    _, remote, _, _ = setup
    original = remote.replace
    def failing(src, dst):
        if dst.startswith("/site/"):
            raise OSError("atomic rename unsupported")
        original(src, dst)
    monkeypatch.setattr(remote, "replace", failing)
    assert run(setup, dry_run=False)[0].state == "Failed"
    assert remote.path("/site/nested/a.txt").read_bytes() == b"old"
    assert not list(remote.root.rglob("*.tmp"))


def test_deleted_skipped_and_other_files_continue(setup):
    root, remote, profile, _ = setup
    rows = deploy(root, [Change("gone", "D", uploadable=False, reason="Deleted"), Change("nested/a.txt", "M")], profile, remote, Event(), dry_run=False)
    assert [r.state for r in rows] == ["Skipped", "Uploaded"]


@pytest.mark.parametrize("path", ["relative", "/site/../etc", "/bad\x00path"])
def test_reject_remote_traversal(path):
    with pytest.raises(ValueError):
        remote_path(path)


def test_local_and_remote_symlinks_rejected(setup):
    root, remote, _, _ = setup
    (root / "link").symlink_to(root / "nested/a.txt")
    assert deploy(root, [Change("link", "?")], Profile("Test"), remote, Event())[0].state == "Failed"
    remote.path("/site/link").symlink_to(root)
    with pytest.raises(ValueError):
        remote.path("/site/link/nested/a.txt")


def test_profiles_nonsecret_atomic_persistence(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.save([Profile("Test", "example.invalid", 22, "user")])
    assert store.load()[0].host == "example.invalid"
    import json
    assert "password" not in json.loads(store.path.read_text())["profiles"][0]
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        store.save([Profile("Bad", backup_root="/site/backups")])
    assert store.load()[0].name == "Test"


def test_live_connections_fail_before_network():
    with pytest.raises(PermissionError):
        SFTPRemote(Profile("Test", "example.invalid"), "not-a-real-password")


def test_missing_file_is_reported_without_losing_other_results(setup):
    root, remote, profile, _ = setup
    rows = deploy(root, [Change("missing", "M"), Change("nested/a.txt", "M")], profile, remote, Event(), dry_run=False)
    assert [row.state for row in rows] == ["Failed", "Uploaded"]


def test_cancel_during_backup_leaves_original_and_no_partial(setup, monkeypatch):
    _, remote, _, _ = setup
    cancel = Event()
    original = remote.open
    def opening(path, mode):
        stream = original(path, mode)
        if path.startswith("/backups"):
            cancel.set()
        return stream
    monkeypatch.setattr(remote, "open", opening)
    row = run(setup, cancel=cancel, dry_run=False)[0]
    assert row.state == "Cancelled"
    assert row.backup == ""
    assert remote.path("/site/nested/a.txt").read_bytes() == b"old"
    assert not list(remote.root.rglob("*.partial-*"))


def test_sftp_authentication_disables_keys_and_rejects_unknown_hosts(monkeypatch):
    # The SSH client is a pure fake; no network calls are possible.
    import paramiko
    from types import SimpleNamespace
    import xdep.transfer as transfer
    calls = {}
    class FakeClient:
        def load_system_host_keys(self):
            calls["known_hosts"] = True
        def set_missing_host_key_policy(self, policy):
            calls["policy"] = policy
        def connect(self, hostname, **kwargs):
            calls["hostname"] = hostname
            calls["connect"] = kwargs
        def open_sftp(self):
            return SimpleNamespace(get_channel=lambda: SimpleNamespace(settimeout=lambda n: None), close=lambda: None)
        def close(self):
            pass
    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    remote = transfer.SFTPRemote(Profile("Test", "fake.invalid", username="user"), "fake-only", approved=True)
    assert calls["known_hosts"]
    from xdep.hostkeys import ReviewHostKeyPolicy
    assert isinstance(calls["policy"], ReviewHostKeyPolicy)
    assert calls["connect"]["look_for_keys"] is False
    assert calls["connect"]["allow_agent"] is False
    assert calls["connect"]["password"] == "fake-only"
    assert "key_filename" not in calls["connect"]
    remote.close()


def test_optional_backups_overwrite_without_creating_backup_folder(setup):
    from dataclasses import replace
    root, remote, profile, changes = setup
    profile = replace(profile, backup_root="")
    plan = deploy(root, changes, profile, remote, Event(), dry_run=True)
    assert plan[0].detail == "Overwrite without backup"
    rows = deploy(root, changes, profile, remote, Event(), dry_run=False)
    assert rows[0].state == "Uploaded"
    assert rows[0].backup == ""
    assert "disabled" in rows[0].detail.lower()
    assert remote.path("/site/nested/a.txt").read_bytes() == (root / "nested/a.txt").read_bytes()
    assert not remote.path("/backups").exists()
