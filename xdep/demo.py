"""Disposable local-only demo: python -m xdep.demo."""
from pathlib import Path
import subprocess
import tempfile
import sys
from PySide6.QtWidgets import QApplication
from .app import Window
from .transfer import LocalRemote
from .repositories import Repository, RepositoryStore


def create_demo(parent: Path):
    root = parent / "repository"
    root.mkdir()  # Refuse to overwrite an existing demo.
    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    git("init")
    (root / "assets").mkdir()
    (root / "assets/style.css").write_text("body { color: black; }\n")
    (root / "index.html").write_text("<h1>Original site</h1>\n")
    (root / "old-name.txt").write_text("This file will be renamed.\n" * 20)
    (root / "removed.txt").write_text("This file will be deleted locally.\n")
    (root / ".gitignore").write_text(".env\n")
    git("add", ".")
    git("-c", "user.name=XDep Demo", "-c", "user.email=demo@example.invalid", "commit", "-m", "Demo baseline")
    git("mv", "old-name.txt", "new-name.txt")
    (root / "removed.txt").unlink()
    (root / "assets/style.css").write_text("body { color: navy; }\n")
    (root / "index.html").write_text("<h1>Hello from XDep</h1>\n")
    (root / "new-file.txt").write_text("New, untracked file\n")
    (root / ".env").write_text("DEMO_ONLY=ignored\n")
    return root


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("XDep Demo")
    parent = Path(tempfile.mkdtemp(prefix="xdep-demo-"))
    root = create_demo(parent)
    env = parent / ".env"
    env.write_text("DEMO_HOST=demo.invalid\nDEMO_USER=demo\nDEMO_PASSWORD=mock-only\nDEMO_DIRECTORY=/site\nDEMO_BACKUP_DIRECTORY=/backups\n")
    remote = LocalRemote(parent / "remote")
    remote.mkdirs("/site/assets")
    remote.path("/site/assets/style.css").write_text("body { color: black; }\n")
    remote.path("/site/index.html").write_text("<h1>Original site</h1>\n")
    RepositoryStore(parent / "repositories.json").save([Repository("Demo website", str(root))], "Demo website")
    window = Window(parent / "appdata", project_root=parent, remote_factory=lambda profile: remote)
    window.setWindowTitle("XDep — Disposable local demo")
    window.show()
    print(f"Demo files: {parent}")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
