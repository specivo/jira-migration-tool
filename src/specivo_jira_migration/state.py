"""The state directory: what an export has read from Jira so far.

An export reads Jira first and writes the bundle last. Everything it reads is
kept in a state directory next to the output (``<out>.jira-state/``), written
file by file as it arrives, so a run that fails (a network error, a rate limit
it gave up on, Ctrl-C) continues where it stopped when it is started again
with the same options: projects whose issues are listed are not listed again,
issues already read are not read again.

The bundle is always written from the state directory alone, in a fixed
order, so a finished run is reproducible: the same state gives the same
bundle, byte for byte, including ``exported_at``, which is the time the first
attempt started.

The format is versioned (``STATE_VERSION``). A state written by another
version is refused, and has to be deleted: version 3 holds each issue's full
worklog list, which version 2 did not read; version 2 held Cloud issues as
read through REST v3 (text as ADF), where version 1 held them as read through
v2 (wiki markup).

Attachments are downloaded into ``files/``, each named by the SHA-256 of its
bytes (never by the name it has in Jira), so content two attachments share is
kept once. A download is written to a temporary name and renamed when it is
complete; ``attachments.json`` says, per attachment id, what became of it
(stored, with its hash, or skipped and why) and the media file id Jira Cloud
gave it. A stored file is hashed again before a continued run trusts it.
``watchers.json`` holds, per issue id, the watchers Jira listed (or that it
refused to), so a continued run asks only about issues it has not asked about.

The directory holds what Jira returned, email addresses included, so it is
created readable by its owner only (0700, files 0600). It is removed once the
bundle is written, unless ``--keep-state`` is given.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

STATE_VERSION = 3
STATE_FILE = "state.json"
META_FILE = "meta.json"
REFS_FILE = "refs.json"
ATTACHMENTS_FILE = "attachments.json"
WATCHERS_FILE = "watchers.json"
ISSUES_DIR = "issues"
FILES_DIR = "files"
ISSUE_ID = re.compile(r"[0-9]{1,20}")
SHA256 = re.compile(r"[0-9a-f]{64}")
_PARTIAL_PREFIX = ".part-"


class StateError(Exception):
    """The state directory cannot be used for this run."""


def state_path_for(out: Path) -> Path:
    """Where the state of an export to *out* lives: next to it, ``<name>.jira-state``."""
    return out.with_name(out.name + ".jira-state")


class StateDir:
    """Read and write one export's state. Every write replaces its file atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self, *, options: dict[str, Any], exported_at: str) -> bool:
        """Create the directory, or load it. Return True when an earlier run is continued.

        A state made with other options (another site, other projects, another
        ``--limit``) is refused: continuing it would mix two exports.
        """
        state_file = self.path / STATE_FILE
        if state_file.exists():
            try:
                self.data = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise StateError(f"{state_file} cannot be read ({exc}); remove it or pass --restart") from None
            if self.data.get("version") != STATE_VERSION:
                raise StateError(
                    f"{self.path} was written by another version of this tool (state format "
                    f"{self.data.get('version')!r}, this version reads {STATE_VERSION}) and cannot be continued; "
                    "delete it (or pass --restart, which deletes it) and run again"
                )
            if self.data.get("options") != options:
                raise StateError(
                    f"{self.path} holds an export made with other options "
                    f"({_differences(self.data.get('options') or {}, options)}); "
                    "run with the same options to continue it, or pass --restart to start again"
                )
            return True
        if self.path.exists() and any(self.path.iterdir()):
            raise StateError(f"{self.path} exists but is not an export's state; move it away or pass --restart")
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)
        self.data = {"version": STATE_VERSION, "options": options, "exported_at": exported_at, "projects": {}}
        self.save()
        return False

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def save(self) -> None:
        self.write_json(STATE_FILE, self.data)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def write_json(self, name: str, value: Any) -> None:
        target = self.path / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def read_json(self, name: str) -> Any:
        target = self.path / name
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    # Issues, one file each, named by Jira id.

    def issue_name(self, issue_id: str) -> str:
        # The id comes from the server and names a file: only Jira's numeric ids are accepted.
        if not ISSUE_ID.fullmatch(issue_id):
            raise StateError(f"not a Jira issue id: {issue_id[:40]!r}")
        return f"{ISSUES_DIR}/{issue_id}.json"

    def has_issue(self, issue_id: str) -> bool:
        return (self.path / self.issue_name(issue_id)).exists()

    def write_issue(self, issue_id: str, value: dict[str, Any]) -> None:
        self.write_json(self.issue_name(issue_id), value)

    def read_issue(self, issue_id: str) -> dict[str, Any]:
        value = self.read_json(self.issue_name(issue_id))
        if not isinstance(value, dict):
            raise StateError(f"{self.path / self.issue_name(issue_id)} is missing; pass --restart")
        return value

    # Attachment bytes, named by their SHA-256.

    def file_path(self, sha256: str) -> Path:
        if not SHA256.fullmatch(sha256):
            raise StateError(f"not a SHA-256: {sha256[:70]!r}")
        return self.path / FILES_DIR / sha256

    def partial_file(self) -> tuple[int, Path]:
        """A new, empty file for a download in progress (0600): its descriptor and path."""
        directory = self.path / FILES_DIR
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=_PARTIAL_PREFIX, dir=directory)
        os.chmod(name, 0o600)
        return handle, Path(name)

    def keep_file(self, partial: Path, sha256: str) -> Path:
        """Move a finished download to its name; if that content is stored already, drop the copy."""
        target = self.file_path(sha256)
        if holds(target, sha256):
            partial.unlink()
        else:
            os.replace(partial, target)
        return target

    def remove_partial_files(self) -> None:
        """Delete what downloads a run did not finish left behind."""
        directory = self.path / FILES_DIR
        if directory.is_dir():
            for entry in directory.glob(_PARTIAL_PREFIX + "*"):
                entry.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Per-project progress
    # ------------------------------------------------------------------

    def project(self, key: str) -> dict[str, Any]:
        projects: dict[str, Any] = self.data.setdefault("projects", {})
        entry: dict[str, Any] = projects.setdefault(key, {"ids": [], "cursor": None, "search_done": False})
        return entry


def holds(path: Path, sha256: str) -> bool:
    """Whether *path* is a regular file (not a link) whose bytes hash to *sha256*."""
    if path.is_symlink() or not path.is_file():
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest() == sha256


def _differences(old: dict[str, Any], new: dict[str, Any]) -> str:
    names = sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))
    return ", ".join(names) or "unknown"
