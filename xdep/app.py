"""XDep desktop: one project .env, explicit SFTP actions, background workers."""
from pathlib import Path
from threading import Event
import sys

from PySide6.QtCore import Qt, QThread, Signal, QStandardPaths
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QComboBox, QFileDialog, QMessageBox, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
    QPlainTextEdit, QListWidget, QSplitter, QAbstractItemView,
    QDialog, QDialogButtonBox, QFormLayout,
)
from .git import scan, safe_file, repository_root
from .repositories import Repository, RepositoryStore
from .credentials import load_connections, read_password
from .transfer import SFTPRemote, deploy
from .hostkeys import UnknownHostKey, trust_host_key


class Job(QThread):
    progress = Signal(object, object, str)
    row = Signal(object)

    def __init__(self, function):
        super().__init__()
        self.function = function
        self.value = None
        self.error = ""
        self.failure = None
        self.cancel = Event()

    def run(self):
        try:
            self.value = self.function(self)
        except Exception as exc:
            self.error = str(exc)
            self.failure = exc


class RepositoryDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Add repository")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. My website")
        self.directory = QLineEdit()
        self.directory.setPlaceholderText("Local Git repository directory")
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.directory, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        folder_row.addWidget(browse)
        form.addRow("Name", self.name)
        form.addRow("Directory", folder_row)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose Git repository")
        if folder:
            self.directory.setText(folder)
            if not self.name.text().strip():
                self.name.setText(Path(folder).name)

    def validate(self):
        try:
            Repository(self.name.text().strip(), str(Path(self.directory.text().strip()).expanduser())).validate()
            if not Path(self.directory.text().strip()).expanduser().is_dir():
                raise ValueError("Choose an existing directory")
        except ValueError as exc:
            QMessageBox.warning(self, "Repository", str(exc))
            return
        self.accept()


class Window(QMainWindow):
    def __init__(self, data_dir=None, *, project_root=None, remote_factory=None):
        super().__init__()
        self.setWindowTitle("XDep")
        self.resize(1080, 800)
        self.data_dir = Path(data_dir or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
        self.project_root = Path(project_root) if project_root is not None else (
            Path(__file__).resolve().parent.parent if data_dir is None else self.data_dir / "project"
        )
        self.source_file = str(self.project_root / ".env")
        self.remote_factory = remote_factory
        self.demo_mode = remote_factory is not None
        self.repository_store = RepositoryStore(self.project_root / "repositories.json")
        self.repository_error = ""
        try:
            self.repository_store.migrate_from(self.data_dir / "repositories.json")
            self.repositories, self.selected_repository = self.repository_store.load()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.repositories, self.selected_repository = [], ""
            self.repository_error = str(exc)
        self.initial_repo_scan = bool(self.repositories)
        self.profiles = []
        self.connected_profile = None
        self.preview_signature = None
        self.root = None
        self.changes = []
        self.job = None
        self.current_dir = "/"
        self.build_ui()
        self.reload_profiles()
        self.reload_repositories(self.selected_repository)
        if self.repository_error:
            self.log.appendPlainText("Saved repositories need review; the file will not be overwritten: " + self.repository_error)
        self.refresh_connections()

    def button(self, text, callback, layout):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def build_ui(self):
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Connection"))
        self.profile_box = QComboBox()
        self.profile_box.currentIndexChanged.connect(self.profile_changed)
        profile_row.addWidget(self.profile_box, 1)
        self.connect_button = self.button("Connect", self.connect_profile, profile_row)
        self.refresh_button = self.button("Refresh connections", self.refresh_connections, profile_row)
        layout.addLayout(profile_row)
        self.source_label = QLabel("Connections from " + self.source_file)
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.connection_detail = QLabel()
        self.connection_detail.setWordWrap(True)
        layout.addWidget(self.connection_detail)
        repo_row = QHBoxLayout()
        repo_row.addWidget(QLabel("Repository"))
        self.repo_box = QComboBox()
        self.repo_box.currentIndexChanged.connect(self.repository_changed)
        repo_row.addWidget(self.repo_box, 1)
        self.add_repo_button = self.button("Add…", self.add_repository, repo_row)
        self.scan_button = self.button("Refresh changes", self.scan_repo, repo_row)
        layout.addLayout(repo_row)
        self.repo_label = QLabel("Add a local Git repository")
        self.repo_label.setWordWrap(True)
        layout.addWidget(self.repo_label)
        split = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Local changes relative to HEAD"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Upload", "Status", "Relative path"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        left_layout.addWidget(self.table)
        select_row = QHBoxLayout()
        self.all_button = self.button("Select uploadable", lambda: self.select_all(True), select_row)
        self.none_button = self.button("Clear selection", lambda: self.select_all(False), select_row)
        left_layout.addLayout(select_row)
        left_layout.addWidget(QLabel("Deleted files stay on the server. Renames upload the new path only."))
        split.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("Remote folders"))
        self.remote_label = QLabel("/")
        self.remote_label.setWordWrap(True)
        right_layout.addWidget(self.remote_label)
        self.dirs = QListWidget()
        self.dirs.itemDoubleClicked.connect(self.enter_dir)
        right_layout.addWidget(self.dirs)
        browse_row = QHBoxLayout()
        self.browse_button = self.button("Browse / refresh", self.browse, browse_row)
        self.up_button = self.button("Up", self.up_dir, browse_row)
        right_layout.addLayout(browse_row)
        split.addWidget(right)
        split.setSizes([780, 260])
        layout.addWidget(split, 1)
        actions = QHBoxLayout()
        self.preview_button = self.button("Preview selected", lambda: self.transfer(True), actions)
        self.upload_button = self.button("Upload selected", lambda: self.transfer(False), actions)
        actions.addStretch()
        self.cancel_button = self.button("Cancel", self.cancel, actions)
        self.cancel_button.setEnabled(False)
        layout.addLayout(actions)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.log.setPlaceholderText("Plans and per-file results appear here. Backup behavior follows .env settings.")
        layout.addWidget(self.log)
        self.statusBar().showMessage("Ready")
        self.busy_controls = [self.profile_box, self.connect_button, self.refresh_button,
            self.repo_box, self.add_repo_button, self.scan_button, self.table, self.all_button, self.none_button,
            self.browse_button, self.up_button, self.dirs,
            self.preview_button, self.upload_button]

    def refresh_connections(self):
        if self.job:
            return
        selected = self.current_profile()
        self.selected_connection = selected.name if selected else ""
        self.connected_profile = None
        self.preview_signature = None
        filename = self.source_file
        self.start_job(lambda job: load_connections(filename), "connections")

    def current_profile(self):
        index = self.profile_box.currentIndex()
        return self.profiles[index] if 0 <= index < len(self.profiles) else None

    def reload_profiles(self, index=0):
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItems([p.name for p in self.profiles])
        if self.profiles:
            self.profile_box.setCurrentIndex(min(index, len(self.profiles) - 1))
        self.profile_box.blockSignals(False)
        self.profile_changed()

    def profile_changed(self):
        self.connected_profile = None
        self.preview_signature = None
        self.current_dir = "/"
        self.remote_label.setText("/")
        self.dirs.clear()
        p = self.current_profile()
        if p:
            backup = p.backup_root or "Disabled"
            self.connection_detail.setText(f"{p.username}@{p.host}:{p.port} · Target: {p.remote_root} · Backups: {backup}")
            self.statusBar().showMessage("Click Connect to test SFTP and browse")
        else:
            self.connection_detail.setText("Configure connections in the project .env and click Refresh connections")
            self.statusBar().showMessage("No connections loaded")

    def current_repository(self):
        index = self.repo_box.currentIndex()
        return self.repositories[index] if 0 <= index < len(self.repositories) else None

    def reload_repositories(self, selected=""):
        self.repo_box.blockSignals(True)
        self.repo_box.clear()
        for repo in self.repositories:
            self.repo_box.addItem(repo.name)
            self.repo_box.setItemData(self.repo_box.count() - 1, repo.directory, Qt.ItemDataRole.ToolTipRole)
        index = next((i for i, r in enumerate(self.repositories) if r.name == selected), 0)
        if self.repositories:
            self.repo_box.setCurrentIndex(index)
        self.repo_box.blockSignals(False)
        repo = self.current_repository()
        self.repo_label.setText(repo.directory if repo else "Add a local Git repository")

    def repository_changed(self):
        self.preview_signature = None
        self.root, self.changes = None, []
        self.populate()
        repo = self.current_repository()
        self.repo_label.setText(repo.directory if repo else "Add a local Git repository")
        if repo:
            try:
                if self.repository_error:
                    raise ValueError("Review the existing repositories file before saving changes")
                self.repository_store.save(self.repositories, repo.name)
            except (OSError, ValueError) as exc:
                self.warning(str(exc))
            self.scan_repo()

    def save_repository(self, repo):
        if self.repository_error:
            raise ValueError("Review the existing repositories file before adding repositories")
        repos = self.repositories + [repo]
        self.repository_store.save(repos, repo.name)
        self.repositories = repos
        self.reload_repositories(repo.name)
        self.repository_changed()

    def add_repository(self):
        if self.job:
            return
        dialog = RepositoryDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name.text().strip()
        directory = Path(dialog.directory.text().strip()).expanduser()
        if any(r.name.casefold() == name.casefold() for r in self.repositories):
            self.warning("Use a unique repository name")
            return
        # Validate Git and resolve subdirectory selections off the UI thread.
        self.start_job(lambda job: Repository(name, str(repository_root(directory).resolve())), "add_repository")

    def scan_repo(self):
        if self.job:
            return
        repo = self.current_repository()
        if not repo:
            self.warning("Add a repository first")
            return
        self.preview_signature = None
        self.root, self.changes = None, []
        self.populate()
        self.start_job(lambda job: scan(Path(repo.directory)), "scan")

    def populate(self):
        self.table.setRowCount(len(self.changes))
        for row, change in enumerate(self.changes):
            item = QTableWidgetItem()
            if change.uploadable:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                # Require deliberate selection; untracked files can contain secrets.
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            detail = change.reason or (f"From {change.old_path}; old remote path retained" if change.old_path else "")
            for column, value in enumerate([item, QTableWidgetItem(change.status), QTableWidgetItem(change.path)]):
                value.setToolTip(detail or change.path)
                self.table.setItem(row, column, value)

    def select_all(self, checked):
        for i, change in enumerate(self.changes):
            if change.uploadable:
                self.table.item(i, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def open_remote(self, profile):
        # Only called inside a worker after an explicit Connect/browse/transfer.
        if self.remote_factory is not None:
            return self.remote_factory(profile)
        return SFTPRemote(profile, read_password(profile), approved=True,
                          known_hosts=self.data_dir / "known_hosts")

    def connect_profile(self):
        profile = self.current_profile()
        if not profile or self.job:
            return
        self.connected_profile = None
        self.preview_signature = None
        self.current_dir = "/"
        self.pending_profile = profile
        def check(job):
            remote = self.open_remote(profile)
            try:
                return remote.listdirs("/")
            finally:
                remote.close()
        self.start_job(check, "connect")

    def require_connection(self, profile):
        if self.connected_profile != profile:
            self.warning("Click Connect for this connection first")
            return False
        return True

    def browse(self):
        profile = self.current_profile()
        if not profile or not self.require_connection(profile):
            return
        path = self.current_dir
        def listing(job):
            remote = self.open_remote(profile)
            try:
                return remote.listdirs(path)
            finally:
                remote.close()
        self.start_job(listing, "browse")

    def enter_dir(self, item):
        self.current_dir = self.current_dir.rstrip("/") + "/" + item.text()
        self.browse()

    def up_dir(self):
        self.current_dir = str(Path(self.current_dir).parent)
        self.browse()

    def signature(self, profile, root, changes):
        stamps = []
        for change in changes:
            info = safe_file(root, change.path).stat()
            stamps.append((change.path, info.st_size, info.st_mtime_ns))
        return profile, str(root), tuple(stamps)

    def confirm_upload(self, profile, count):
        backup = f"Back up existing files to: {profile.backup_root}" if profile.backup_root else "Backups are DISABLED for this connection. Existing files will be overwritten without a backup."
        answer = QMessageBox.question(
            self, "Upload selected files?",
            f"Upload {count} files to {profile.username}@{profile.host}:{profile.port}\n"
            f"Target: {profile.remote_root}\n\n{backup}\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def transfer(self, dry_run):
        profile = self.current_profile()
        selected = [c for i, c in enumerate(self.changes) if c.uploadable and self.table.item(i, 0).checkState() == Qt.CheckState.Checked]
        if not profile or not self.root or not selected:
            self.warning("Choose a connection, scan a repository, and select files")
            return
        if not self.require_connection(profile):
            return
        root = self.root
        try:
            signature = self.signature(profile, root, selected)
        except (OSError, ValueError):
            self.warning("Local files changed; refresh the repository and preview again")
            return
        if not dry_run:
            if self.preview_signature != signature:
                self.warning("Preview these selected files before uploading. Preview again after changing files, selection, or connection.")
                return
            if not self.confirm_upload(profile, len(selected)):
                return
        self.pending_signature = signature
        self.pending_dry_run = dry_run
        self.preview_signature = None
        self.log.appendPlainText("\nPREVIEW (read-only)" if dry_run else "\nUPLOAD")
        def perform(job):
            _, fresh = scan(root)
            available = {c.path for c in fresh if c.uploadable}
            if any(c.path not in available for c in selected) or self.signature(profile, root, selected) != signature:
                raise ValueError("Repository files changed; refresh and preview again")
            remote = self.open_remote(profile)
            try:
                return deploy(root, selected, profile, remote, job.cancel, dry_run=dry_run,
                              progress=job.progress.emit, result=job.row.emit,
                              expected_stamps={path: (size, modified) for path, size, modified in signature[2]} if not dry_run else None)
            finally:
                remote.close()
        self.start_job(perform, "transfer")

    def start_job(self, function, kind):
        if self.job:
            return
        self.job_kind = kind
        self.job = Job(function)
        self.job.progress.connect(self.update_progress)
        self.job.row.connect(self.show_result)
        self.job.finished.connect(self.finish_job)
        for control in self.busy_controls:
            control.setEnabled(False)
        self.cancel_button.setEnabled(kind == "transfer")
        self.progress.setRange(0, 0)
        self.statusBar().showMessage("Working…")
        self.job.start()

    def update_progress(self, done, total, path):
        self.progress.setRange(0, 1000)
        self.progress.setValue(int(done * 1000 / total) if total else 1000)
        self.statusBar().showMessage(path)

    def show_result(self, result):
        detail = result.detail + (f" · Backup: {result.backup}" if result.backup else "")
        self.log.appendPlainText(f"{result.state}: {result.path}" + (f" — {detail}" if detail else ""))

    def review_host_key(self, failure):
        answer = QMessageBox.question(
            self, "Verify server identity",
            f"First connection to {failure.hostname}\n"
            f"Key type: {failure.key.get_name()}\n{failure.fingerprint}\n\n"
            "Verify this fingerprint with your server provider. Trust and save this server key?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                trust_host_key(self.data_dir / "known_hosts", failure.hostname, failure.key)
                self.log.appendPlainText("Server key saved. Click Connect again to authenticate.")
            except (OSError, ValueError) as exc:
                self.warning(str(exc))

    def finish_job(self):
        job = self.job
        kind = self.job_kind
        self.job = None
        for control in self.busy_controls:
            control.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1000)
        if job.error:
            self.progress.setValue(0)
            self.log.appendPlainText("Error: " + job.error)
            if kind == "connections":
                self.profiles = []
                self.reload_profiles()
            if kind == "scan":
                self.root, self.changes = None, []
                self.populate()
            if kind in ("connect", "browse", "transfer"):
                self.connected_profile = None
                self.preview_signature = None
            self.statusBar().showMessage("Operation failed — see results")
            if isinstance(job.failure, UnknownHostKey):
                self.review_host_key(job.failure)
        else:
            if kind == "connections":
                self.profiles = job.value
                index = next((i for i, p in enumerate(self.profiles) if p.name == self.selected_connection), 0)
                self.reload_profiles(index)
                message = f"Loaded {len(self.profiles)} connections from project .env"
            elif kind == "add_repository":
                try:
                    self.save_repository(job.value)
                    message = f"Added repository: {job.value.name}"
                except (OSError, ValueError) as exc:
                    self.warning(str(exc))
                    message = "Repository was not saved"
            elif kind == "scan":
                self.root, self.changes = job.value
                self.repo_label.setText(str(self.root))
                self.populate()
                message = f"{len(self.changes)} local changes — select files to upload"
            elif kind in ("connect", "browse"):
                if kind == "connect":
                    self.connected_profile = self.pending_profile
                self.dirs.clear()
                self.dirs.addItems(job.value)
                self.remote_label.setText(self.current_dir)
                message = "SFTP connection verified; remote folders loaded" if kind == "connect" else "Remote folders loaded"
            else:
                counts = {}
                for row in job.value:
                    counts[row.state] = counts.get(row.state, 0) + 1
                if self.pending_dry_run and all(row.state == "Planned" for row in job.value):
                    self.preview_signature = self.pending_signature
                message = ", ".join(f"{v} {k.lower()}" for k, v in counts.items())
            self.progress.setValue(1000)
            self.statusBar().showMessage(message)
        if self.initial_repo_scan and kind == "connections":
            self.initial_repo_scan = False
            self.scan_repo()
        job.failure = None  # Drop exception tracebacks, which may hold credentials.
        job.deleteLater()

    def cancel(self):
        if self.job:
            self.job.cancel.set()
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("Cancelling after the current safe step…")

    def closeEvent(self, event):
        if self.job:
            self.cancel()
            self.statusBar().showMessage("Waiting for background work to stop; close again when finished")
            event.ignore()
        else:
            event.accept()

    def warning(self, message):
        QMessageBox.warning(self, "XDep", message)


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("XDep")
    app.setApplicationName("XDep")
    window = Window()
    window.show()
    return app.exec()
