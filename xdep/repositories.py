"""Named local repositories, stored atomically in the project root."""
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import tempfile


@dataclass(frozen=True)
class Repository:
    name: str
    directory: str

    def validate(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Enter a repository name")
        if not isinstance(self.directory, str) or not Path(self.directory).is_absolute():
            raise ValueError("Choose an absolute repository directory")


class RepositoryStore:
    def __init__(self, path):
        self.path = Path(path)

    def migrate_from(self, previous_path):
        """Copy a valid legacy list only when no project file exists.

        Retain the old file, and never replace an existing project list.
        """
        previous_path = Path(previous_path)
        if self.path.exists() or not previous_path.exists():
            return
        repos, selected = RepositoryStore(previous_path).load()
        self.save(repos, selected)

    def load(self):
        if not self.path.exists():
            return [], ""
        data = json.loads(self.path.read_text())
        if data.get("version") != 1:
            raise ValueError("Unsupported repository file version")
        repos = [Repository(**entry) for entry in data["repositories"]]
        selected = data.get("selected", "")
        self.validate(repos, selected)
        return repos, selected

    def validate(self, repos, selected):
        for repo in repos:
            repo.validate()
        if len({r.name.casefold() for r in repos}) != len(repos):
            raise ValueError("Use a unique repository name")
        if len({r.directory for r in repos}) != len(repos):
            raise ValueError("This repository is already saved")
        if selected and selected not in {r.name for r in repos}:
            raise ValueError("Selected repository is not saved")

    def save(self, repos, selected):
        self.validate(repos, selected)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".repositories-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"version": 1, "repositories": [asdict(r) for r in repos], "selected": selected}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
