"""Explicit host-key review; unknown keys are never silently trusted."""
from pathlib import Path
import base64
import hashlib
import os
import tempfile
import paramiko


class UnknownHostKey(paramiko.SSHException):
    def __init__(self, hostname, key):
        self.hostname = hostname
        self.key = key
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        self.fingerprint = "SHA256:" + digest
        super().__init__(f"Unrecognized server key for {hostname}: {self.fingerprint}")


class ReviewHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        # This occurs before Paramiko attempts password authentication.
        raise UnknownHostKey(hostname, key)


def trust_host_key(path: Path, hostname, key):
    """Save only an explicitly approved key; refuse to replace a changed key."""
    path = Path(path)
    keys = paramiko.HostKeys(str(path)) if path.exists() else paramiko.HostKeys()
    existing = keys.lookup(hostname)
    if existing is not None and any(saved.asbytes() != key.asbytes() for saved in existing.values()):
        raise ValueError("A different key is already saved for this server; review it manually")
    keys.add(hostname, key.get_name(), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".known-hosts-")
    os.close(fd)
    try:
        keys.save(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
