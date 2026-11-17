from pathlib import Path
import subprocess
import pytest
from xdep.git import scan


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    for name in ["edit.txt", "delete.txt", "rename me.txt", "reverted.txt"]:
        (tmp_path / name).write_text("original " + name + "\n" * 10)
    (tmp_path / ".gitignore").write_text("ignored*\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Initial")
    return tmp_path


def test_head_diff_and_nonignored_untracked(repo):
    (repo / "edit.txt").write_text("changed")
    (repo / "delete.txt").unlink()
    git(repo, "mv", "rename me.txt", "renamed\nfile.txt")
    (repo / "new space.txt").write_text("new")
    (repo / "ignored-secret").write_text("secret")
    root, changes = scan(repo)
    rows = {c.path: c for c in changes}
    assert root == repo
    assert rows["edit.txt"].uploadable
    assert rows["delete.txt"].status == "D"
    assert not rows["delete.txt"].uploadable
    assert rows["renamed\nfile.txt"].old_path == "rename me.txt"
    assert rows["new space.txt"].status == "?"
    assert "ignored-secret" not in rows


def test_staged_change_reverted_to_head_is_absent(repo):
    original = (repo / "reverted.txt").read_bytes()
    (repo / "reverted.txt").write_text("staged change")
    git(repo, "add", "reverted.txt")
    (repo / "reverted.txt").write_bytes(original)
    assert "reverted.txt" not in {c.path for c in scan(repo)[1]}


def test_symlink_and_directory_submodule_are_not_uploadable(repo):
    (repo / "link").symlink_to(repo / "edit.txt")
    change = next(c for c in scan(repo)[1] if c.path == "link")
    assert not change.uploadable
    assert "Symbolic" in change.reason


def test_unborn_repository(tmp_path):
    git(tmp_path, "init")
    (tmp_path / "staged").write_text("a")
    git(tmp_path, "add", "staged")
    (tmp_path / "new").write_text("b")
    assert {c.path for c in scan(tmp_path)[1]} == {"staged", "new"}


def test_invalid_repository(tmp_path):
    with pytest.raises(ValueError):
        scan(tmp_path)


def test_conflict_not_uploadable(repo):
    git(repo, "checkout", "-b", "other")
    (repo / "edit.txt").write_text("other\n")
    git(repo, "commit", "-am", "other")
    git(repo, "checkout", "-")
    (repo / "edit.txt").write_text("main\n")
    git(repo, "commit", "-am", "main")
    subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True)
    change = next(c for c in scan(repo)[1] if c.path == "edit.txt")
    assert not change.uploadable
    assert "conflict" in change.reason


def test_staged_deletion_recreated_file_is_uploadable(repo):
    git(repo, "rm", "edit.txt")
    (repo / "edit.txt").write_text("recreated with new contents")
    change = next(c for c in scan(repo)[1] if c.path == "edit.txt")
    assert change.uploadable
    assert change.status == "M"


def test_repository_folder_trailing_spaces(tmp_path):
    folder = tmp_path / "repo with trailing space "
    folder.mkdir()
    git(folder, "init")
    (folder / "new").write_text("new")
    assert scan(folder)[0] == folder
