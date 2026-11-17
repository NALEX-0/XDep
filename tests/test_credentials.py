from dataclasses import replace
import os
import subprocess
import pytest
from xdep.credentials import read_password
from xdep.profiles import Profile, ProfileStore


@pytest.fixture
def credential(tmp_path):
    path = tmp_path / "credentials.env"
    path.write_text("XDEP_PASSWORD='literal-${HOME}-#-$(never-executed)'\n")
    path.chmod(0o600)
    return path, Profile("Test", credential_file=str(path))


def test_reads_literal_password_without_exporting(credential):
    path, profile = credential
    before = dict(os.environ)
    assert read_password(profile) == "literal-${HOME}-#-$(never-executed)"
    assert dict(os.environ) == before


def test_accepts_shared_permissions(credential):
    path, profile = credential
    path.chmod(0o644)
    assert read_password(profile) == "literal-${HOME}-#-$(never-executed)"


def test_rejects_symlink(credential):
    path, profile = credential
    link = path.parent / "link.env"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symbolic link"):
        read_password(replace(profile, credential_file=str(link)))


def test_accepts_ignored_untracked_credentials_in_repository(credential):
    path, profile = credential
    subprocess.run(["git", "-C", str(path.parent), "init"], check=True, capture_output=True)
    (path.parent / ".gitignore").write_text("*.env\n")
    assert read_password(profile) == "literal-${HOME}-#-$(never-executed)"


def test_missing_variable_no_secret_in_error(credential):
    path, profile = credential
    with pytest.raises(ValueError, match="missing or empty") as error:
        read_password(replace(profile, password_variable="OTHER_PASSWORD"))
    assert "literal" not in str(error.value)


def test_profile_saves_only_reference(credential):
    path, profile = credential
    store = ProfileStore(path.parent / "profiles.json")
    store.save([profile])
    assert "literal" not in store.path.read_text()
    assert store.load()[0].credential_file == str(path)


def test_no_file_selected():
    with pytest.raises(ValueError, match="Choose a"):
        read_password(Profile("Test"))


def test_rejects_unignored_credentials_in_repository(credential):
    path, profile = credential
    subprocess.run(["git", "-C", str(path.parent), "init"], check=True, capture_output=True)
    with pytest.raises(ValueError, match="untracked and ignored"):
        read_password(profile)


def test_rejects_tracked_credentials_even_with_ignore_rule(credential):
    path, profile = credential
    subprocess.run(["git", "-C", str(path.parent), "init"], check=True, capture_output=True)
    (path.parent / ".gitignore").write_text("*.env\n")
    subprocess.run(["git", "-C", str(path.parent), "add", "-f", "--", path.name], check=True, capture_output=True)
    with pytest.raises(ValueError, match="untracked and ignored"):
        read_password(profile)


def test_does_not_require_current_account_ownership(credential, monkeypatch):
    path, profile = credential
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    assert read_password(profile) == "literal-${HOME}-#-$(never-executed)"
