"""Read an explicitly selected .env file without exporting secrets."""
from pathlib import Path
import io
import os
import stat
import subprocess


def read_values(filename) -> dict:
    if not filename:
        raise ValueError("Choose a .env credential file in the profile")
    path = Path(filename).expanduser()
    if not path.is_absolute():
        raise ValueError("Credential file path must be absolute")
    if path.is_symlink():
        raise ValueError("Credential file cannot be a symbolic link")
    # Project-local credentials are allowed only when untracked and ignored.
    try:
        probe = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"], capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise ValueError("Could not check the credential file Git status") from None
    if probe.returncode == 0 and probe.stdout.strip() == b"true":
        try:
            ignored = subprocess.run(
                ["git", "-C", str(path.parent), "check-ignore", "--quiet", "--", path.name],
                capture_output=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("Could not check the credential file Git status") from None
        # Git check-ignore excludes already tracked files, even if an ignore
        # rule would otherwise match them. Never use --no-index here.
        if ignored.returncode != 0:
            raise ValueError("Credential files inside a Git repository must be untracked and ignored by Git")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Credential file must be a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Credential file is too large")
    finally:
        if fd != -1:
            os.close(fd)
    from dotenv import dotenv_values
    # No interpolation, shell execution, or mutation of os.environ.
    values = dotenv_values(stream=io.StringIO(content), interpolate=False)
    return values


def read_password(profile) -> str:
    values = read_values(profile.credential_file)
    password = values.get(profile.password_variable)
    if not password:
        raise ValueError("The selected password variable is missing or empty")
    return password


def load_connections(filename) -> list:
    """Build non-secret profiles from PREFIX_HOST/USER/PASSWORD/DIRECTORY."""
    import re
    from .profiles import Profile
    values = read_values(filename)
    suffixes = ("_HOST", "_USER", "_PASSWORD", "_DIRECTORY", "_PORT", "_BACKUP_DIRECTORY")
    names = set()
    for key in values:
        for suffix in sorted(suffixes, key=len, reverse=True):
            if key.endswith(suffix):
                name = key[:-len(suffix)]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    names.add(name)
                break
    if not names:
        raise ValueError("No connections found. Use NAME_HOST, NAME_USER, NAME_PASSWORD and NAME_DIRECTORY with KEY=value syntax.")
    profiles = []
    for name in sorted(names):
        for suffix in ("HOST", "USER", "PASSWORD", "DIRECTORY"):
            key = f"{name}_{suffix}"
            if not values.get(key):
                raise ValueError(f"Missing or empty {key}. Use KEY=value syntax.")
        try:
            port = int(values.get(f"{name}_PORT", "22"))
        except (TypeError, ValueError):
            raise ValueError(f"{name}_PORT must be a number") from None
        profile = Profile(name, values[f"{name}_HOST"], port, values[f"{name}_USER"],
                          values[f"{name}_DIRECTORY"], values.get(f"{name}_BACKUP_DIRECTORY") or "",
                          str(Path(filename).expanduser().resolve()), f"{name}_PASSWORD")
        try:
            profile.validate()
        except ValueError:
            raise ValueError(f"Invalid settings for {name}: check the port and separate absolute deployment/backup directories") from None
        profiles.append(profile)
    return profiles
