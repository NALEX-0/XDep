# XDep

A macOS Python + PySide6 desktop application for reviewing local Git changes and uploading selected files over password-authenticated SFTP.

## Start

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e '.[test]'
.venv/bin/python -m xdep
```

After setup, double-click `Launch XDep.command` in Finder. The working directory does not affect where XDep finds its configuration.

## One configuration file

XDep always reads **`.env` in this project's root**, beside `pyproject.toml`. It reads it on every startup. Click **Refresh connections** after editing it. There is no file chooser and no remembered alternative file. Legacy settings/profile files are left intact but are not used.

Each prefix becomes a connection name:

```dotenv
WATCHER_HOST='example.invalid'
WATCHER_USER='appuser'
WATCHER_PASSWORD='your-password'
WATCHER_DIRECTORY='/public_html/laravel_watcher'

# Optional; defaults to 22.
WATCHER_PORT=22

# Optional; omit this entry to disable backups.
WATCHER_BACKUP_DIRECTORY='/private-backups/watcher'

OTHER_HOST='another.invalid'
OTHER_USER='anotheruser'
OTHER_PASSWORD='another-password'
OTHER_DIRECTORY='/public_html/another'
```

Use `=` between keys and values, one entry per line, with no trailing backslashes. Every connection requires `_HOST`, `_USER`, `_PASSWORD` and `_DIRECTORY`. The dropdown displays `WATCHER`, `OTHER`, etc. Passwords are read literally, without shell execution, variable interpolation, or export into the process environment. Passwords are never copied to app settings or logs.

The file must be readable by the app. No owner or `chmod 600` restriction is imposed. Inside a Git working tree, `.env` must remain untracked and ignored by Git. Symbolic-link credential files are not supported. Incomplete or invalid configuration clears the connection list and reports the affected field without exposing password values.

## Connect and test

Loading or refreshing `.env` **never connects to a server**.

1. Select a connection. The app shows its host, user, deployment folder and backup setting.
2. Click **Connect** to authenticate over SFTP and list the server's root folders. This is read-only and does not upload files.
3. For an unknown server, compare the displayed SHA256 host-key fingerprint with your hosting provider's fingerprint. Trust it only after verification, then click **Connect** again. XDep saves approved public host keys in its application data folder. Existing trusted SSH host keys are also recognized. Changed keys are rejected, not silently replaced.
4. Browse remote directories by double-clicking folders or using **Up**. The deployment destination remains the `_DIRECTORY` value from `.env`.
5. In the **Repository** row, click **Add…**, enter a name, and choose the local Git directory. Saved repositories appear in the dropdown and remain available after restarting. Selecting one scans it automatically; **Refresh changes** rescans it. The selected repository is restored and scanned on startup. Select uploadable files and click **Preview selected**. This reads remote destination metadata and reports create/overwrite plans without remote writes or content comparison.
6. Click **Upload selected**. The confirmation names the server, target, file count, and whether backups are enabled. Only approving this confirmation starts file writes. A successful preview of the same selected files is required. Edits, connection changes, configuration refreshes, or changed selections require another preview.
7. **Cancel** stops cooperatively between chunks and safe steps. Completed files remain uploaded; unfinished temporary uploads are removed where possible. Each file has an individual result.

Each operation uses a short-lived SFTP session that closes afterward. A successful Connect verifies access for the selected configuration; subsequent actions reconnect using the current password from `.env`. Refreshing configuration or selecting another connection requires another Connect. There is no background polling or automatic deployment.

## Backups

**Backups are optional, as requested.**

- With `NAME_BACKUP_DIRECTORY` set, XDep must finish the old file's backup before replacing it. A backup failure blocks that file's upload.
- If the entry is missing or empty, existing files are overwritten **without backup**. The app shows this state in the connection details, preview results, and upload confirmation.
- A configured backup folder must be an absolute path separate from, and not nested within, the deployment folder. Choose a location outside publicly served website directories.
- Backup layout is `<backup-directory>/<connection-and-target-hash>/<unique-run-id>/<relative-file-path>`. Runs are unique and backups are never automatically deleted.

Uploads go to temporary sibling files and replace destinations with the SFTP server's POSIX rename extension. If the server lacks atomic replacement, uploads fail rather than deleting destinations first. Backup or cancelled-upload temporary files may remain if the server denies cleanup or the connection drops. Automated rollback and metadata-preserving restore are not included.

## Git behavior

Tracked changes are computed against HEAD using the current working tree, plus non-ignored untracked files. Staged changes reverted in the working tree are omitted. An initial repository with no commits is supported. Nothing is selected by default.

Deleted files are informational; XDep never deletes remote site files. Git-recognized renames show the old and new paths and upload only the new path. Old remote paths remain. Unresolved conflicts, symbolic links and submodules are not uploadable. Missing remote parent directories are created, preserving repository-relative paths.

## Local demo and tests

The normal app uses real SFTP. An explicitly launched disposable demo and isolated test adapter remain available for offline development:

```sh
.venv/bin/python -m xdep.demo
.venv/bin/python -m pytest -q
```

The demo uses a newly created temporary project and local filesystem server; it does not use your real `.env` or server. Click Connect in the demo to exercise its local adapter.

Tests block Python network connections and use only synthetic credentials, temporary Git repositories, local filesystem targets and fake SFTP clients. They cover root `.env` loading, refresh, explicit connection actions, host-key handling, password-only authentication, preview and confirmation, optional backups, failure preservation, cancellation and per-file results. No real server was contacted during implementation or testing. Real-server interoperability and permissions still need your manual test.

## Implementation

- `app.py`: native Qt widgets, one root `.env`, explicit Connect/preview/upload actions, and background workers.
- `credentials.py`: named connection discovery and literal password reads.
- `profiles.py`: non-secret profile model; legacy persistence remains available internally.
- `repositories.py`: named local repositories and last selection, saved atomically as `repositories.json` in the project root during development. The file is ignored by Git. A valid old application-data list is copied on startup only if no project file exists; the old file is retained and an existing project file takes precedence. Corrupt saved data is preserved for review.
- `hostkeys.py`: explicit host-key fingerprint review and atomic storage of approved public keys.
- `git.py`: NUL-delimited Git inspection and local file eligibility.
- `transfer.py`: password-only SFTP adapter, local test adapter, backups and file upload engine.
- `demo.py`: disposable local-only demonstration.

SSH key authentication and SSH agent discovery are disabled. Server identity host keys are still verified; these are distinct from user authentication keys. Application data, including approved server keys, normally lives at `~/Library/Application Support/XDep/XDep`.

Cancellation is cooperative and cannot undo completed uploads. Individual network operations have timeouts. Files are snapshotted before upload and checked against preview metadata. Do not run concurrent deployments to the same destination: SFTP provides no transaction spanning backup and replacement. Uploaded files receive the server's normal permissions for newly created temporary files; original permissions and other metadata are not preserved.

No remote file content comparison, cloud backend, CI/CD, remote site deletion, automatic backup cleanup or automatic rollback is included. This is a runnable Python app, not a signed/notarized macOS `.app` bundle.

References: [Paramiko SSH client](https://docs.paramiko.org/en/stable/api/client.html), [host keys](https://docs.paramiko.org/en/stable/api/hostkeys.html), [SFTP](https://docs.paramiko.org/en/stable/api/sftp.html), [Git diff](https://git-scm.com/docs/git-diff), [Qt threads](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html).
