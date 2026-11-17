"""Transfer engine shared by SFTP and the local test adapter."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event
import errno
import hashlib
import os
import stat
import tempfile
import uuid

CHUNK = 64 * 1024


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("Cancelled")


def remote_path(value: str) -> str:
    if not value.startswith("/") or "\x00" in value or ".." in value.split("/"):
        raise ValueError("Remote paths must be absolute and cannot contain '..' or NUL")
    return str(PurePosixPath(value))


def join(root: str, relative: str) -> str:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Unsafe relative path")
    return remote_path(root.rstrip("/") + "/" + str(path))


class LocalRemote:
    """A sandbox on this computer, never a network connection."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, path):
        relative = remote_path(path).lstrip("/")
        current = self.root
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Remote symbolic links are not supported")
        current.resolve().relative_to(self.root)
        return current

    def exists(self, path):
        target = self.path(path)
        if target.exists() and not target.is_file():
            raise ValueError("Destination is not a regular file")
        return target.exists()

    def mkdirs(self, path):
        self.path(path).mkdir(parents=True, exist_ok=True)

    def open(self, path, mode):
        return self.path(path).open(mode)

    def replace(self, source, destination):
        os.replace(self.path(source), self.path(destination))

    def remove(self, path):
        self.path(path).unlink(missing_ok=True)

    def listdirs(self, path):
        return sorted(p.name for p in self.path(path).iterdir() if p.is_dir() and not p.is_symlink())

    def close(self):
        pass


class SFTPRemote:
    """Password-only SFTP, invoked only by an explicit user connection action."""
    def __init__(self, profile, password: str, *, approved=False, known_hosts=None):
        if not approved:
            raise PermissionError("Approve the connection before contacting the server")
        profile.validate()
        if not profile.host or not profile.username:
            raise ValueError("Host and user are required")
        if not password:
            raise ValueError("A password is required")
        import paramiko
        self.client = paramiko.SSHClient()
        try:
            self.client.load_system_host_keys()
            if known_hosts is not None and Path(known_hosts).exists():
                self.client.load_host_keys(str(known_hosts))
            from .hostkeys import ReviewHostKeyPolicy
            self.client.set_missing_host_key_policy(ReviewHostKeyPolicy())
            self.client.connect(profile.host, port=profile.port, username=profile.username,
                                password=password, allow_agent=False, look_for_keys=False,
                                timeout=15, auth_timeout=15, banner_timeout=15,
                                channel_timeout=15)
            self.sftp = self.client.open_sftp()
            self.sftp.get_channel().settimeout(15)
        except Exception:
            self.client.close()
            raise

    def _check(self, path):
        path = remote_path(path)
        current = ""
        for part in PurePosixPath(path).parts[1:]:
            current += "/" + part
            try:
                mode = self.sftp.lstat(current).st_mode
                if stat.S_ISLNK(mode):
                    raise ValueError("Remote symbolic links are not supported")
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
        return path

    def exists(self, path):
        path = self._check(path)
        try:
            mode = self.sftp.lstat(path).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("Destination is not a regular file")
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return False
            raise

    def mkdirs(self, path):
        self._check(path)
        current = ""
        for part in PurePosixPath(path).parts[1:]:
            current += "/" + part
            try:
                self.sftp.mkdir(current)
            except OSError as exc:
                try:
                    mode = self.sftp.lstat(current).st_mode
                except OSError:
                    raise exc
                if not stat.S_ISDIR(mode):
                    raise ValueError("Remote parent is not a directory")

    def open(self, path, mode):
        return self.sftp.open(self._check(path), mode)

    def replace(self, source, destination):
        # Fail safely if the server lacks atomic POSIX rename. Never delete first.
        self.sftp.posix_rename(self._check(source), self._check(destination))

    def remove(self, path):
        try:
            self.sftp.remove(self._check(path))
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise

    def listdirs(self, path):
        return sorted(p.filename for p in self.sftp.listdir_attr(self._check(path)) if stat.S_ISDIR(p.st_mode))

    def close(self):
        try:
            self.sftp.close()
        finally:
            self.client.close()


@dataclass(frozen=True)
class Result:
    path: str
    state: str
    detail: str = ""
    backup: str = ""


def copy_stream(source, target, cancel, progress=lambda count: None):
    while True:
        check_cancel(cancel)
        block = source.read(CHUNK)
        if not block:
            return
        target.write(block)
        progress(len(block))


def deploy(root, changes, profile, remote, cancel: Event, *, dry_run=True,
           progress=lambda done, total, path: None, result=lambda row: None,
           expected_stamps=None):
    """No remote deletions; configured backups must complete before overwrite.

    Cancellation is cooperative. A completed file remains deployed; later files
    are cancelled. Temporary uploads never replace the destination on failure.
    """
    from .git import safe_file
    profile.validate()
    run_id = uuid.uuid4().hex
    namespace = hashlib.sha256(f"{profile.host}:{profile.port}:{profile.username}:{profile.remote_root}".encode()).hexdigest()[:16]
    backup_base = join(profile.backup_root, namespace + "/" + run_id) if profile.backup_root else ""
    results = []
    total = 0
    for change in changes:
        if change.uploadable:
            try:
                total += safe_file(root, change.path).stat().st_size
            except (ValueError, OSError):
                pass  # Report unavailable files individually in the main loop.
    done = 0
    for change in changes:
        temporary = ""
        backup = ""
        try:
            check_cancel(cancel)
            if not change.uploadable:
                row = Result(change.path, "Skipped", change.reason)
            else:
                source = safe_file(root, change.path)
                destination = join(profile.remote_root, change.path)
                exists = remote.exists(destination)
                if dry_run:
                    done += source.stat().st_size
                    progress(done, total, change.path)
                    row = Result(change.path, "Planned", ("Back up and overwrite" if backup_base else "Overwrite without backup") if exists else "Create new file")
                else:
                    # Snapshot before any remote writes; avoids mid-upload edits.
                    with tempfile.TemporaryFile() as snapshot:
                        with source.open("rb") as stream:
                            before = os.fstat(stream.fileno())
                            if expected_stamps is not None and expected_stamps.get(change.path) != (before.st_size, before.st_mtime_ns):
                                raise ValueError("Local file changed since preview; preview again")
                            copy_stream(stream, snapshot, cancel)
                            after = os.fstat(stream.fileno())
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError("Local file changed while preparing upload; scan again")
                        snapshot.seek(0)
                        if exists and backup_base:
                            candidate = join(backup_base, change.path)
                            remote.mkdirs(str(PurePosixPath(candidate).parent))
                            partial_backup = candidate + ".partial-" + uuid.uuid4().hex
                            try:
                                with remote.open(destination, "rb") as old, remote.open(partial_backup, "wb") as saved:
                                    copy_stream(old, saved, cancel)
                                check_cancel(cancel)
                                remote.replace(partial_backup, candidate)
                                backup = candidate
                            finally:
                                remote.remove(partial_backup)
                        remote.mkdirs(str(PurePosixPath(destination).parent))
                        temporary = destination + ".xdep-" + uuid.uuid4().hex + ".tmp"
                        def advance(count):
                            nonlocal done
                            done += count
                            progress(done, max(total, done), change.path)
                        with remote.open(temporary, "wb") as target:
                            copy_stream(snapshot, target, cancel, advance)
                        check_cancel(cancel)
                        remote.replace(temporary, destination)
                        temporary = ""
                    row = Result(change.path, "Uploaded", "" if backup_base else "Backups disabled", backup)
        except Cancelled:
            row = Result(change.path, "Cancelled", "Existing destination preserved", backup)
        except Exception as exc:
            row = Result(change.path, "Failed", str(exc), backup)
        finally:
            if temporary:
                try:
                    remote.remove(temporary)
                except Exception:
                    pass
        results.append(row)
        result(row)
    return results
