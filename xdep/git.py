"""Git inspection using NUL-delimited output; no shell interpolation."""
from dataclasses import dataclass
from pathlib import Path
import os
import stat
import subprocess


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    old_path: str = ""
    uploadable: bool = True
    reason: str = ""


def git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if proc.returncode:
        raise ValueError(proc.stderr.decode(errors="replace").removesuffix("\n"))
    return proc.stdout


def repository_root(folder: Path) -> Path:
    return Path(os.fsdecode(git(folder, "rev-parse", "--show-toplevel")).removesuffix("\n"))


def safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in ("..", ".git") for p in path.parts):
        raise ValueError("Unsafe repository path")
    current = root.resolve()
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symbolic links cannot be uploaded")
    if not stat.S_ISREG(current.stat().st_mode):
        raise ValueError("Only regular files can be uploaded")
    current.resolve().relative_to(root.resolve())
    return current


def scan(folder: Path) -> tuple[Path, list[Change]]:
    root = repository_root(folder)
    # Diff HEAD directly: staged changes reverted in the working tree disappear.
    has_head = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True).returncode == 0
    args = ("diff", "--name-status", "-z", "--find-renames", "HEAD", "--") if has_head else ("diff", "--cached", "--name-status", "-z", "--")
    tokens = git(root, *args).split(b"\0")
    rows = []
    i = 0
    while i < len(tokens) and tokens[i]:
        code = tokens[i].decode("ascii")
        path = os.fsdecode(tokens[i + 1])
        i += 2
        old = ""
        if code[0] in "RC":
            old, path = path, os.fsdecode(tokens[i])
            i += 1
        rows.append((path, code, old))
    if not has_head:
        # Include modifications/deletions of staged files in an unborn repository.
        rows = [(p, "A" if (root / p).exists() else "D", old) for p, _, old in rows]
    tracked_unmerged = set(os.fsdecode(p) for p in git(root, "diff", "--name-only", "--diff-filter=U", "-z").split(b"\0") if p)
    seen = {r[0] for r in rows}
    rows += [(os.fsdecode(p), "?", "") for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p and os.fsdecode(p) not in seen]
    changes = []
    for path, code, old in rows:
        # A staged deletion may have been recreated as an untracked file.
        # Show the current uploadable file instead of hiding it as a deletion.
        if code[0] == "D" and os.path.lexists(root / path):
            code = "M"
        reason = ""
        if code[0] == "D":
            reason = "Deleted locally; remote file will be left untouched"
        elif path in tracked_unmerged or code[0] == "U":
            reason = "Resolve merge conflict before uploading"
        else:
            try:
                safe_file(root, path)
            except (ValueError, OSError) as exc:
                reason = str(exc)
        changes.append(Change(path, code, old, not reason, reason))
    return root, sorted(changes, key=lambda c: c.path)
