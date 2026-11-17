import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from xdep.app import Window
from xdep.transfer import LocalRemote
from test_git import git
from test_connections import group


@pytest.fixture(scope="module", autouse=True)
def application_lifetime():
    app = QApplication.instance() or QApplication([])
    yield app
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait(window):
    app = QApplication.instance()
    end = time.monotonic() + 10
    while window.job is not None and time.monotonic() < end:
        app.processEvents()
        time.sleep(.01)
    assert window.job is None


def setup_window(tmp_path, monkeypatch, text=None):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(text or group(directory="/site"))
    remote = LocalRemote(tmp_path / "remote")
    remote.mkdirs("/site")
    calls = []
    def adapter(profile, password, **kwargs):
        calls.append((profile, password, kwargs))
        return remote
    monkeypatch.setattr("xdep.app.SFTPRemote", adapter)
    window = Window(tmp_path / "appdata", project_root=project)
    warnings = []
    window.warning = warnings.append
    wait(window)
    return window, remote, calls, warnings


def prepare_files(window, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    (repo / "hello.txt").write_text("hello")
    from xdep.repositories import Repository
    window.save_repository(Repository("Test repository", str(repo)))
    wait(window)
    window.select_all(True)
    return repo


def test_root_env_autoload_refresh_and_real_adapter_routing(tmp_path, monkeypatch):
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    assert window.current_profile().name == "WATCHER"
    assert not calls  # Startup reads config; it never contacts a server.
    assert not hasattr(window, "env_button")
    assert not hasattr(window, "choose_env")
    window.refresh_button.click()
    wait(window)
    assert not calls
    window.connect_button.click()
    wait(window)
    assert window.connected_profile == window.current_profile()
    assert len(calls) == 1
    assert calls[0][1] == "literal-${HOME}-secret"
    assert calls[0][2]["approved"] is True
    assert window.dirs.item(0).text() == "site"
    window.close()
    restored = Window(tmp_path / "appdata", project_root=window.project_root)
    wait(restored)
    assert restored.current_profile().name == "WATCHER"
    assert len(calls) == 1  # Restart does not connect either.
    restored.close()


def test_connection_changes_add_update_remove(tmp_path, monkeypatch):
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    env = Path(window.source_file)
    env.write_text(group(directory="/updated") + group("SECOND", "/second"))
    window.refresh_button.click()
    wait(window)
    assert window.profile_box.count() == 2
    assert window.current_profile().name == "WATCHER"
    assert window.current_profile().remote_root == "/updated"
    env.write_text(group("SECOND", "/second"))
    window.refresh_button.click()
    wait(window)
    assert window.profile_box.count() == 1
    assert window.current_profile().name == "SECOND"
    assert not calls
    assert not (window.data_dir / "settings.ini").exists()
    assert not (window.data_dir / "profiles.json").exists()
    window.close()


from pathlib import Path


def test_legacy_file_preference_is_ignored(tmp_path, monkeypatch):
    data = tmp_path / "appdata"
    data.mkdir()
    (data / "settings.ini").write_text("[connections]\nenv_file=/some/old/file.env\n")
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    assert window.source_file == str(tmp_path / "project" / ".env")
    assert window.current_profile().name == "WATCHER"
    window.close()


def test_missing_root_env_can_be_created_and_refreshed_without_picker(tmp_path, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: pytest.fail("No env picker allowed"))
    window = Window(tmp_path / "data", project_root=tmp_path)
    wait(window)
    assert window.current_profile() is None
    (tmp_path / ".env").write_text(group())
    window.refresh_button.click()
    wait(window)
    assert window.current_profile().name == "WATCHER"
    (tmp_path / ".env").write_text("# empty\n")
    window.refresh_button.click()
    wait(window)
    assert window.current_profile() is None
    assert "literal" not in window.log.toPlainText()
    window.close()


def test_scan_connect_preview_upload_and_browse(tmp_path, monkeypatch):
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    prepare_files(window, tmp_path)
    window.transfer(True)
    assert not calls and warnings  # Explicit connection action required.
    window.connect_profile()
    wait(window)
    window.transfer(False)
    assert len(calls) == 1  # Upload requires a successful preview.
    window.transfer(True)
    wait(window)
    assert "Planned: hello.txt" in window.log.toPlainText()
    assert not remote.path("/site/hello.txt").exists()
    confirmations = []
    window.confirm_upload = lambda profile, count: confirmations.append((profile, count)) or True
    window.transfer(False)
    wait(window)
    assert confirmations[0][0].backup_root == ""
    assert confirmations[0][1] == 1
    assert remote.path("/site/hello.txt").read_text() == "hello"
    assert "Uploaded: hello.txt" in window.log.toPlainText()
    assert "literal" not in window.log.toPlainText()
    window.current_dir = "/site"
    window.browse()
    wait(window)
    assert window.remote_label.text() == "/site"
    window.close()


def test_edits_invalidate_preview_and_rejected_confirmation_does_not_write(tmp_path, monkeypatch):
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    repo = prepare_files(window, tmp_path)
    window.connect_profile()
    wait(window)
    window.transfer(True)
    wait(window)
    window.confirm_upload = lambda *a: False
    window.transfer(False)
    assert not remote.path("/site/hello.txt").exists()
    assert len(calls) == 2
    (repo / "hello.txt").write_text("edited after preview")
    window.confirm_upload = lambda *a: pytest.fail("Stale preview cannot reach confirmation")
    window.transfer(False)
    assert warnings and "Preview" in warnings[-1]
    assert len(calls) == 2
    window.refresh_connections()
    wait(window)
    assert window.connected_profile is None
    assert window.preview_signature is None
    window.close()


def test_upload_confirmation_explains_backup_state(tmp_path, monkeypatch):
    from dataclasses import replace
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    messages = []
    def question(parent, title, message, *args):
        messages.append(message)
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", question)
    assert not window.confirm_upload(window.current_profile(), 2)
    assert "DISABLED" in messages[-1]
    assert not window.confirm_upload(replace(window.current_profile(), backup_root="/archive"), 2)
    assert "Back up existing files to: /archive" in messages[-1]
    window.close()


def test_unknown_host_key_requires_trust_and_a_new_connect_click(tmp_path, monkeypatch):
    import paramiko
    from xdep.hostkeys import UnknownHostKey
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    key = paramiko.RSAKey.generate(1024)
    def unknown(*a, **k):
        raise UnknownHostKey("example.invalid", key)
    monkeypatch.setattr("xdep.app.SFTPRemote", unknown)
    messages = []
    def question(parent, title, message, *args):
        messages.append(message)
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)
    window.connect_profile()
    wait(window)
    assert window.connected_profile is None
    assert "SHA256:" in messages[-1]
    assert (window.data_dir / "known_hosts").exists()
    assert "Click Connect again" in window.log.toPlainText()
    window.close()


def test_add_repository_form_validates_git_and_normalizes_directory(tmp_path, monkeypatch):
    from xdep.app import RepositoryDialog
    from PySide6.QtWidgets import QDialog
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    repo = tmp_path / "my-site"
    repo.mkdir()
    git(repo, "init")
    (repo / "subfolder").mkdir()
    (repo / "new.txt").write_text("new")
    def dialog(parent):
        form = RepositoryDialog(parent)
        form.name.setText("My website")
        form.directory.setText(str(repo / "subfolder"))
        form.exec = lambda: QDialog.DialogCode.Accepted
        return form
    monkeypatch.setattr("xdep.app.RepositoryDialog", dialog)
    window.add_repo_button.click()
    wait(window)
    assert window.repo_box.currentText() == "My website"
    assert window.current_repository().directory == str(repo)
    assert window.root == repo
    assert window.table.item(0, 2).text() == "new.txt"
    assert not calls
    before = (window.project_root / "repositories.json").read_bytes()
    window.add_repo_button.click()
    assert warnings[-1] == "Use a unique repository name"
    assert (window.project_root / "repositories.json").read_bytes() == before
    window.close()


def test_repository_switch_and_restart_restore_selection(tmp_path, monkeypatch):
    from xdep.repositories import Repository
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    for name in ["one", "two"]:
        directory = tmp_path / name
        directory.mkdir()
        git(directory, "init")
        (directory / (name + ".txt")).write_text(name)
        window.save_repository(Repository(name.title(), str(directory)))
        wait(window)
    assert window.current_repository().name == "Two"
    window.select_all(True)
    window.preview_signature = "stale preview"
    window.repo_box.setCurrentIndex(0)
    wait(window)
    assert window.root == tmp_path / "one"
    assert window.table.item(0, 2).text() == "one.txt"
    assert window.table.item(0, 0).checkState() == Qt.CheckState.Unchecked
    assert window.preview_signature is None
    window.close()
    restored = Window(window.data_dir, project_root=window.project_root)
    wait(restored)
    assert restored.repo_box.count() == 2
    assert restored.current_repository().name == "One"
    assert restored.root == tmp_path / "one"
    assert not calls
    restored.close()


def test_invalid_git_directory_does_not_add_a_repository(tmp_path, monkeypatch):
    from xdep.app import RepositoryDialog
    from PySide6.QtWidgets import QDialog
    window, remote, calls, warnings = setup_window(tmp_path, monkeypatch)
    invalid = tmp_path / "not-git"
    invalid.mkdir()
    def dialog(parent):
        form = RepositoryDialog(parent)
        form.name.setText("Not Git")
        form.directory.setText(str(invalid))
        form.exec = lambda: QDialog.DialogCode.Accepted
        return form
    monkeypatch.setattr("xdep.app.RepositoryDialog", dialog)
    window.add_repository()
    wait(window)
    assert window.repo_box.count() == 0
    assert not (window.project_root / "repositories.json").exists()
    assert "Error:" in window.log.toPlainText()
    assert not calls
    window.close()
