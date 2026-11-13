"""Read-only access to a git repository.

Every command here only reads. `--no-optional-locks` stops git from
refreshing its index file as a side effect, so the scanned repo is not
written to at all.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import Iterator

ZERO_SHA = "0" * 40
GITLINK_MODE = "160000"


class GitError(Exception):
    pass


# A repo's own settings can name programs for git to start. A scanned repo
# may not be trustworthy, so the settings that could start a program during
# the commands Sweep runs are switched off for every call.
_SAFE_SETTINGS = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
    "-c", "log.showSignature=false",
)


def _command(repo: str, args: tuple[str, ...]) -> list[str]:
    return ["git", "--no-optional-locks", "-c", "core.quotepath=off",
            *_SAFE_SETTINGS, "-C", repo, *args]


def git(repo: str, *args: str, ok_codes: tuple[int, ...] = (0,)) -> bytes:
    try:
        done = subprocess.run(_command(repo, args), capture_output=True,
                              stdin=subprocess.DEVNULL)
    except OSError as error:
        raise GitError(f"could not run git: {error}") from error
    if done.returncode not in ok_codes:
        message = done.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(message or f"git {' '.join(args)} failed")
    return done.stdout


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _split_z(data: bytes) -> list[str]:
    return [_text(part) for part in data.split(b"\0") if part]


def toplevel(path: str) -> str:
    """Return the repo's top folder, or raise GitError if it is not a repo."""
    return _text(git(path, "rev-parse", "--show-toplevel")).strip()


def inside_work_tree(path: str) -> bool:
    try:
        return _text(git(path, "rev-parse", "--is-inside-work-tree")).strip() == "true"
    except GitError:
        return False


def has_commits(repo: str) -> bool:
    return bool(git(repo, "rev-list", "-n", "1", "--all", "--reflog").strip())


def list_tracked(repo: str) -> list[str]:
    return _split_z(git(repo, "ls-files", "-z"))


def list_untracked(repo: str) -> list[str]:
    return _split_z(git(repo, "ls-files", "-z", "--others", "--exclude-standard"))


def list_ignored(repo: str) -> list[str]:
    """Ignored paths. A path ending in '/' is a wholly ignored folder."""
    return _split_z(git(repo, "ls-files", "-z", "--others", "--ignored",
                        "--exclude-standard", "--directory"))


def head_blobs(repo: str) -> set[str]:
    """Blob ids of every file in the current commit."""
    try:
        data = git(repo, "ls-tree", "-r", "-z", "HEAD")
    except GitError:
        return set()
    blobs: set[str] = set()
    for entry in _split_z(data):
        meta = entry.split("\t", 1)[0].split()
        if len(meta) == 3 and meta[1] == "blob":
            blobs.add(meta[2])
    return blobs


@dataclass
class BlobRef:
    """One version of one file, somewhere in history."""
    sha: str
    paths: set[str] = field(default_factory=set)
    commits: list[str] = field(default_factory=list)


def history_blobs(repo: str) -> tuple[int, dict[str, BlobRef]]:
    """Every file version in history, on all branches, tags, stashes and
    the reflog. Returns (number of commits, blob id -> BlobRef)."""
    data = git(repo, "log", "--all", "--reflog", "--raw", "--no-abbrev",
               "--no-renames", "-m", "--root", "-z", "--format=commit %H")
    tokens = data.split(b"\0")
    blobs: dict[str, BlobRef] = {}
    commits: set[str] = set()
    current = ""
    index = 0
    while index < len(tokens):
        token = _text(tokens[index]).lstrip("\n")
        index += 1
        if token.startswith("commit "):
            current = token[7:47]
            commits.add(current)
        elif token.startswith(":"):
            meta = token.split()
            path = _text(tokens[index]) if index < len(tokens) else ""
            index += 1
            if len(meta) < 5:
                continue
            new_mode, new_sha = meta[1], meta[3]
            if new_sha == ZERO_SHA or new_mode == GITLINK_MODE:
                continue
            ref = blobs.setdefault(new_sha, BlobRef(new_sha))
            ref.paths.add(path)
            if current and current not in ref.commits:
                ref.commits.append(current)
    return len(commits), blobs


def blob_sizes(repo: str, shas: list[str]) -> dict[str, int]:
    if not shas:
        return {}
    try:
        done = subprocess.run(
            _command(repo, ("cat-file", "--batch-check")),
            input=("\n".join(shas) + "\n").encode("ascii"),
            capture_output=True,
        )
    except OSError as error:
        raise GitError(f"could not run git: {error}") from error
    sizes: dict[str, int] = {}
    for line in _text(done.stdout).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            sizes[parts[0]] = int(parts[2])
    return sizes


def read_blobs(repo: str, shas: list[str]) -> Iterator[tuple[str, bytes]]:
    """Yield (blob id, content) for each id, using one git process."""
    if not shas:
        return
    process = subprocess.Popen(
        _command(repo, ("cat-file", "--batch")),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None

    def feed() -> None:
        try:
            process.stdin.write(("\n".join(shas) + "\n").encode("ascii"))
            process.stdin.close()
        except OSError:
            pass

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    try:
        for _ in shas:
            header = process.stdout.readline()
            if not header:
                break
            parts = _text(header).split()
            if len(parts) != 3:
                continue  # "<id> missing"
            size = int(parts[2])
            content = process.stdout.read(size)
            process.stdout.read(1)  # the newline after the content
            yield parts[0], content
    finally:
        process.stdout.close()
        process.wait()
        feeder.join(timeout=5)


@dataclass(frozen=True)
class CommitRecord:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    message: str


def commit_records(repo: str) -> list[CommitRecord]:
    data = git(repo, "log", "--all", "--reflog",
               "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e")
    records: list[CommitRecord] = []
    seen: set[str] = set()
    for chunk in _text(data).split("\x1e"):
        fields = chunk.lstrip("\n").split("\x1f")
        if len(fields) != 6 or fields[0] in seen:
            continue
        seen.add(fields[0])
        records.append(CommitRecord(*fields))
    return records


def object_format(repo: str) -> str:
    """'sha1' or 'sha256': how this repo names its objects."""
    try:
        name = _text(git(repo, "rev-parse", "--show-object-format")).strip()
    except GitError:
        return "sha1"
    return name if name in ("sha1", "sha256") else "sha1"


def anchor_commits(repo: str) -> list[str]:
    """Commits that identify this repo's history: its first commit(s) and
    the tips of its branches. Another folder holding any of them shares
    history with this repo."""
    found: list[str] = []
    for args in (("rev-list", "--max-parents=0", "--all"),
                 ("for-each-ref", "--format=%(objectname)", "refs/heads")):
        try:
            lines = _text(git(repo, *args)).split()
        except GitError:
            lines = []
        for line in lines:
            if line not in found:
                found.append(line)
    return found


def worktrees(repo: str) -> list[str]:
    """Folders of every working tree linked to this repo, including itself."""
    try:
        data = _text(git(repo, "worktree", "list", "--porcelain"))
    except GitError:
        return []
    return [line[len("worktree "):] for line in data.splitlines()
            if line.startswith("worktree ")]


def remotes(repo: str) -> list[tuple[str, str]]:
    data = git(repo, "config", "--get-regexp", r"^remote\..*\.url$",
               ok_codes=(0, 1))
    found: list[tuple[str, str]] = []
    for line in _text(data).splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote."):-len(".url")]
        found.append((name, url))
    return found
