from dataclasses import replace
from types import SimpleNamespace
from threading import Event
import errno
import paramiko
import pytest
from xdep.transfer import SFTPRemote, LocalRemote, deploy
from xdep.hostkeys import UnknownHostKey, ReviewHostKeyPolicy, trust_host_key
from xdep.profiles import Profile
from xdep.git import Change


@pytest.fixture
def wire(tmp_path, monkeypatch):
    disk = LocalRemote(tmp_path / "server")
    disk.mkdirs("/site/nested")
    disk.path("/site/nested/a.txt").write_text("old version")
    calls = {}
    class SFTP:
        def get_channel(self):
            return SimpleNamespace(settimeout=lambda n: calls.update(timeout=n))
        def lstat(self, path):
            return disk.path(path).lstat()
        def mkdir(self, path):
            disk.path(path).mkdir()
        def open(self, path, mode):
            return disk.open(path, mode)
        def posix_rename(self, source, target):
            disk.replace(source, target)
        def remove(self, path):
            disk.path(path).unlink()  # Real ENOENT behavior for absent temp files.
        def listdir_attr(self, path):
            return [SimpleNamespace(filename=p.name, st_mode=p.lstat().st_mode) for p in disk.path(path).iterdir()]
        def close(self):
            calls["sftp_closed"] = True
    class Client:
        def load_system_host_keys(self):
            calls["system_keys"] = True
        def load_host_keys(self, path):
            calls["app_keys"] = path
        def set_missing_host_key_policy(self, policy):
            calls["policy"] = policy
        def connect(self, host, **kwargs):
            calls["host"] = host
            calls["auth"] = kwargs
        def open_sftp(self):
            return SFTP()
        def close(self):
            calls["client_closed"] = True
    monkeypatch.setattr(paramiko, "SSHClient", Client)
    profile = Profile("Test", "example.invalid", username="user")
    remote = SFTPRemote(profile, "fake-password", approved=True)
    return disk, remote, profile, calls


def test_sftp_adapter_preview_backup_and_atomic_replacement(wire, tmp_path):
    disk, remote, profile, calls = wire
    local = tmp_path / "repo"
    (local / "nested").mkdir(parents=True)
    (local / "nested/a.txt").write_text("new version")
    (local / "nested/new.txt").write_text("new file")
    changes = [Change("nested/a.txt", "M"), Change("nested/new.txt", "?")]
    plan = deploy(local, changes, profile, remote, Event())
    assert all(r.state == "Planned" for r in plan)
    assert disk.path("/site/nested/a.txt").read_text() == "old version"
    assert not disk.path("/backups").exists()
    rows = deploy(local, changes, profile, remote, Event(), dry_run=False)
    assert [r.state for r in rows] == ["Uploaded", "Uploaded"]
    assert disk.path(rows[0].backup).read_text() == "old version"
    assert disk.path("/site/nested/a.txt").read_text() == "new version"
    assert disk.path("/site/nested/new.txt").read_text() == "new file"
    assert remote.listdirs("/site") == ["nested"]
    assert calls["auth"]["look_for_keys"] is False
    assert calls["auth"]["allow_agent"] is False
    remote.close()
    assert calls["client_closed"] and calls["sftp_closed"]


def test_sftp_without_backups_creates_missing_parents(wire, tmp_path):
    disk, remote, profile, calls = wire
    local = tmp_path / "repo"
    (local / "new/deep").mkdir(parents=True)
    (local / "new/deep/a.txt").write_text("new")
    profile = replace(profile, remote_root="/another-site", backup_root="")
    rows = deploy(local, [Change("new/deep/a.txt", "?")], profile, remote, Event(), dry_run=False)
    assert rows[0].state == "Uploaded"
    assert disk.path("/another-site/new/deep/a.txt").read_text() == "new"
    assert not disk.path("/backups").exists()
    remote.close()


def test_unknown_host_key_is_not_accepted_automatically(tmp_path):
    key = paramiko.RSAKey.generate(1024)
    with pytest.raises(UnknownHostKey) as error:
        ReviewHostKeyPolicy().missing_host_key(None, "[example.invalid]:2222", key)
    assert error.value.fingerprint.startswith("SHA256:")
    assert not list(tmp_path.iterdir())


def test_trusted_key_persists_and_changed_keys_are_rejected(tmp_path):
    path = tmp_path / "known_hosts"
    key = paramiko.RSAKey.generate(1024)
    trust_host_key(path, "[example.invalid]:2222", key)
    assert paramiko.HostKeys(str(path)).check("[example.invalid]:2222", key)
    replacement = paramiko.RSAKey.generate(1024)
    with pytest.raises(ValueError, match="different key"):
        trust_host_key(path, "[example.invalid]:2222", replacement)
    assert paramiko.HostKeys(str(path)).check("[example.invalid]:2222", key)


def test_local_changes_after_preview_fail_before_remote_writes(wire, tmp_path):
    disk, remote, profile, calls = wire
    local = tmp_path / "repo"
    local.mkdir()
    (local / "a.txt").write_text("updated")
    row = deploy(local, [Change("a.txt", "M")], profile, remote, Event(), dry_run=False,
                 expected_stamps={"a.txt": (1, 1)})[0]
    assert row.state == "Failed"
    assert "since preview" in row.detail
    assert not disk.path("/site/a.txt").exists()
    assert not disk.path("/backups").exists()
    remote.close()


def test_network_failure_closes_client_before_raising(monkeypatch):
    calls = {}
    class Client:
        def load_system_host_keys(self):
            pass
        def set_missing_host_key_policy(self, policy):
            pass
        def connect(self, *a, **k):
            raise paramiko.AuthenticationException("Authentication failed")
        def close(self):
            calls["closed"] = True
    monkeypatch.setattr(paramiko, "SSHClient", Client)
    with pytest.raises(paramiko.AuthenticationException):
        SFTPRemote(Profile("Test", "example.invalid", username="user"), "fake-password", approved=True)
    assert calls["closed"]
