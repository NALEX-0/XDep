from dataclasses import asdict
import json
import pytest
from xdep.credentials import load_connections, read_password


def group(name="WATCHER", directory="/public_html/laravel_watcher", extra=""):
    return f"{name}_HOST='example.invalid'\n{name}_USER='appuser'\n{name}_PASSWORD='literal-${{HOME}}-secret'\n{name}_DIRECTORY='{directory}'\n{extra}"


def test_loads_named_connections_without_storing_passwords(tmp_path):
    path = tmp_path / ".env"
    path.write_text(group() + group("OTHER", "/other", "OTHER_PORT=2222\nOTHER_BACKUP_DIRECTORY=/archive\n"))
    profiles = load_connections(path)
    assert [p.name for p in profiles] == ["OTHER", "WATCHER"]
    other, watcher = profiles
    assert watcher.host == "example.invalid"
    assert watcher.username == "appuser"
    assert watcher.remote_root == "/public_html/laravel_watcher"
    assert watcher.port == 22
    assert watcher.backup_root == ""
    assert other.port == 2222 and other.backup_root == "/archive"
    assert watcher.password_variable == "WATCHER_PASSWORD"
    assert read_password(watcher) == "literal-${HOME}-secret"
    assert "literal" not in json.dumps([asdict(p) for p in profiles])


@pytest.mark.parametrize("suffix", ["HOST", "USER", "PASSWORD", "DIRECTORY"])
def test_requires_complete_groups(tmp_path, suffix):
    path = tmp_path / ".env"
    path.write_text("\n".join(line for line in group().splitlines() if not line.startswith("WATCHER_" + suffix + "=")))
    with pytest.raises(ValueError, match="WATCHER_" + suffix):
        load_connections(path)


@pytest.mark.parametrize("extra", ["WATCHER_PORT=abc\n", "WATCHER_PORT=70000\n", "WATCHER_BACKUP_DIRECTORY=/public_html/laravel_watcher/backups\n"])
def test_invalid_settings_do_not_expose_values(tmp_path, extra):
    path = tmp_path / ".env"
    path.write_text(group(extra=extra))
    with pytest.raises(ValueError) as error:
        load_connections(path)
    assert "literal" not in str(error.value)


def test_missing_equals_sign_has_actionable_error(tmp_path):
    path = tmp_path / ".env"
    path.write_text(group().replace("WATCHER_DIRECTORY=", "WATCHER_DIRECTORY-"))
    with pytest.raises(ValueError, match="WATCHER_DIRECTORY.*KEY=value"):
        load_connections(path)


def test_no_connections(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# empty\n")
    with pytest.raises(ValueError, match="No connections"):
        load_connections(path)
