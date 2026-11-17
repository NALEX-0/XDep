"""Non-secret profile persistence. Passwords are never serialized."""
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import os
import re
import tempfile


@dataclass(frozen=True)
class Profile:
    name: str
    host: str = ""
    port: int = 22
    username: str = ""
    remote_root: str = "/site"
    backup_root: str = "/backups"
    credential_file: str = ""
    password_variable: str = "XDEP_PASSWORD"

    def validate(self):
        from .transfer import remote_path
        if not self.name.strip():
            raise ValueError("Enter a profile name")
        if not 1 <= self.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        if self.credential_file and not Path(self.credential_file).expanduser().is_absolute():
            raise ValueError("Credential file path must be absolute")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.password_variable):
            raise ValueError("Enter a valid password variable name")
        remote_path(self.remote_root)
        site = remote_path(self.remote_root).rstrip("/")
        backup = remote_path(self.backup_root).rstrip("/") if self.backup_root else None
        if backup is not None and (site == backup or backup.startswith(site + "/") or site.startswith(backup + "/")):
            raise ValueError("Backup and deployment folders must be separate, non-nested paths")


class ProfileStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[Profile]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        if data.get("version") != 1:
            raise ValueError("Unsupported profile file version")
        profiles = [Profile(**row) for row in data["profiles"]]
        for profile in profiles:
            profile.validate()
        return profiles

    def save(self, profiles: list[Profile]):
        for profile in profiles:
            profile.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".profiles-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"version": 1, "profiles": [asdict(p) for p in profiles]}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
