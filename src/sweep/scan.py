"""Part A: scan one git repository for personal data. Reads only."""
from __future__ import annotations

import bisect
import os
import stat
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import __version__, gitsrc
from .detectors import HIGH, Matchers, sensitive_filename
from .identity import IdentityValue, gather

DEFAULT_MAX_SIZE = 5 * 1024 * 1024

# Folders of downloaded dependencies and caches. When git ignores them they
# are not searched: they are other people's code, and very large.
SKIP_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "site-packages", ".git",
}

# Windows marks files that exist only in the cloud with these attributes.
# Reading one makes the sync client download it. The same bits on a folder
# mean listing it would fetch its contents.
_CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000
_REPARSE_POINT = 0x400
# Cloud sync placeholders (OneDrive and others) carry a reparse tag of the
# form 0x9000_x01A. They are real files or folders, not links, so they are
# handled by the cloud-only check instead.
_CLOUD_TAG_MASK, _CLOUD_TAG = 0xFFFF0FFF, 0x9000001A


def is_link(info: os.stat_result) -> bool:
    """True for anything that points somewhere else: symbolic links,
    junctions, and every other Windows reparse point except cloud
    placeholders. Python 3.11 does not count junctions as links, so the
    reparse attribute is checked directly. Never follow these."""
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        tag = getattr(info, "st_reparse_tag", 0)
        return (tag & _CLOUD_TAG_MASK) != _CLOUD_TAG
    return False


@dataclass
class Finding:
    detector: str
    severity: str
    value: str
    reserved: bool
    note: str
    area: str            # working-tree, history, file-name, commit-message, remote
    path: str = ""
    line: int = 0
    column: int = 0
    snippet: str = ""
    status: str = ""     # working tree: tracked, untracked, ignored
    blob: str = ""       # history: blob id
    commits: list[str] = field(default_factory=list)
    in_head: bool = False


@dataclass
class Skipped:
    path: str
    reason: str
    area: str


@dataclass
class ScanResult:
    tool: str
    version: str
    part: str
    repo: str
    started: str
    seconds: float = 0.0
    identity: list[dict] = field(default_factory=list)
    identity_notes: list[str] = field(default_factory=list)
    terms_file: str = ""
    terms_count: int = 0
    searched: dict = field(default_factory=dict)
    not_checked: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    commit_identities: list[dict] = field(default_factory=list)
    remotes: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def decode(data: bytes) -> str | None:
    """Return text, or None if the data looks binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    sample = data[:8192]
    if b"\0" in sample:
        # UTF-16 without a byte order mark: every second byte is zero.
        odd = sample[1::2]
        if len(sample) >= 8 and odd and odd.count(0) >= len(odd) * 0.9:
            return data.decode("utf-16-le", errors="replace")
        return None
    return data.decode("utf-8", errors="replace")


def _snippet(text: str, start: int, end: int, limit: int = 160) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end].rstrip("\r")
    if len(line) <= limit:
        return line.strip()
    middle = start - line_start
    left = max(0, middle - limit // 2)
    right = min(len(line), left + limit)
    piece = line[left:right].strip()
    return ("..." if left > 0 else "") + piece + ("..." if right < len(line) else "")


def scan_text(text: str, matchers: Matchers, **where) -> list[Finding]:
    matches = matchers.scan(text)
    if not matches:
        return []
    newlines = [i for i, char in enumerate(text) if char == "\n"]
    findings: list[Finding] = []
    for match in matches:
        line_index = bisect.bisect_left(newlines, match.start)
        line_start = newlines[line_index - 1] + 1 if line_index else 0
        findings.append(Finding(
            detector=match.detector, severity=match.severity,
            value=match.value, reserved=match.reserved, note=match.note,
            line=line_index + 1, column=match.start - line_start + 1,
            snippet=_snippet(text, match.start, match.end), **where,
        ))
    return findings


def _is_cloud_only(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _CLOUD_ONLY)


def _walk_ignored_dir(root: str, relative: str, result: ScanResult) -> list[str]:
    """List files under a wholly ignored folder. Leaves out SKIP_DIRS and
    nested repos, and never follows links or junctions or lists a folder
    that exists only in the cloud."""
    files: list[str] = []
    stack = [os.path.join(root, relative)]
    while stack:
        folder = stack.pop()
        rel_folder = os.path.relpath(folder, root).replace("\\", "/")
        try:
            with os.scandir(folder) as listing:
                entries = list(listing)
        except OSError as error:
            result.skipped.append(Skipped(rel_folder + "/",
                                          f"could not open folder: {error.strerror}",
                                          "working-tree"))
            continue
        if any(entry.name == ".git" for entry in entries):
            result.skipped.append(Skipped(rel_folder + "/",
                                          "nested git repository", "working-tree"))
            continue
        for entry in entries:
            relative_path = f"{rel_folder}/{entry.name}"
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                result.skipped.append(Skipped(relative_path, "could not read file details",
                                              "working-tree"))
                continue
            if is_link(info):
                result.skipped.append(Skipped(relative_path,
                                              "link or junction (not followed)",
                                              "working-tree"))
            elif stat.S_ISDIR(info.st_mode):
                if entry.name in SKIP_DIRS:
                    result.skipped.append(Skipped(relative_path + "/",
                                                  "dependency or cache folder",
                                                  "working-tree"))
                elif _is_cloud_only(info):
                    result.skipped.append(Skipped(
                        relative_path + "/",
                        "cloud-only folder (listing it would fetch it)",
                        "working-tree"))
                else:
                    stack.append(entry.path)
            else:
                files.append(relative_path)
    return files


def _working_tree_files(repo: str, result: ScanResult) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for path in gitsrc.list_tracked(repo):
        files.append((path, "tracked"))
    for path in gitsrc.list_untracked(repo):
        if path.endswith("/"):
            result.skipped.append(Skipped(path, "nested git repository",
                                          "working-tree"))
        else:
            files.append((path, "untracked"))
    for path in gitsrc.list_ignored(repo):
        if not path.endswith("/"):
            if any(part in SKIP_DIRS for part in path.split("/")[:-1]):
                continue
            files.append((path, "ignored"))
            continue
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name in SKIP_DIRS:
            result.skipped.append(Skipped(path, "dependency or cache folder",
                                          "working-tree"))
            continue
        for inner in _walk_ignored_dir(repo, path.rstrip("/"), result):
            files.append((inner, "ignored"))
    return files


def list_working_tree(repo: str) -> tuple[list[tuple[str, str]], list[Skipped]]:
    """Every file in the working tree as (path, status), plus what was
    left out. Status is tracked, untracked or ignored."""
    holder = ScanResult(tool="sweep", version=__version__, part="", repo=repo,
                        started="")
    return _working_tree_files(repo, holder), holder.skipped


def is_cloud_only(info: os.stat_result) -> bool:
    return _is_cloud_only(info)


def _filename_finding(path: str, **where) -> Finding | None:
    reason = sensitive_filename(path)
    if not reason:
        return None
    return Finding(detector="sensitive-file", severity=HIGH,
                   value=path.rsplit("/", 1)[-1], reserved=False, note=reason,
                   path=path, **where)


def _scan_working_tree(repo: str, matchers: Matchers, max_size: int,
                       result: ScanResult) -> None:
    counts: Counter[str] = Counter()
    for path, status in _working_tree_files(repo, result):
        full = os.path.join(repo, path)
        try:
            info = os.lstat(full)
        except OSError as error:
            result.skipped.append(Skipped(path, f"could not read: {error.strerror}",
                                          "working-tree"))
            continue
        if is_link(info):
            result.skipped.append(Skipped(path, "link or junction (not followed)",
                                          "working-tree"))
            continue
        if stat.S_ISDIR(info.st_mode):
            result.skipped.append(Skipped(path + "/", "submodule or nested git repository",
                                          "working-tree"))
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        counts[status] += 1
        named = _filename_finding(path, area="file-name", status=status)
        if named:
            result.findings.append(named)
        if _is_cloud_only(info):
            result.skipped.append(Skipped(path, "cloud-only file (reading it would download it)",
                                          "working-tree"))
            continue
        if info.st_size > max_size:
            result.skipped.append(Skipped(path, "over the size limit", "working-tree"))
            continue
        try:
            with open(full, "rb") as handle:
                data = handle.read()
        except OSError as error:
            result.skipped.append(Skipped(path, f"could not read: {error.strerror}",
                                          "working-tree"))
            continue
        text = decode(data)
        if text is None:
            result.skipped.append(Skipped(path, "binary file", "working-tree"))
            continue
        result.findings.extend(scan_text(text, matchers, area="working-tree",
                                         path=path, status=status))
    result.searched["working_tree"] = {
        "tracked": counts["tracked"],
        "untracked": counts["untracked"],
        "ignored": counts["ignored"],
    }


def _scan_history(repo: str, matchers: Matchers, max_size: int,
                  result: ScanResult) -> None:
    commit_count, blobs = gitsrc.history_blobs(repo)
    in_head = gitsrc.head_blobs(repo)
    sizes = gitsrc.blob_sizes(repo, list(blobs))
    readable: list[str] = []
    for sha, ref in blobs.items():
        path = sorted(ref.paths)[0]
        named = _filename_finding(path, area="file-name", status="history",
                                  blob=sha, commits=ref.commits[:5],
                                  in_head=sha in in_head)
        if named:
            result.findings.append(named)
        size = sizes.get(sha)
        if size is None:
            result.skipped.append(Skipped(path, "object missing from the repository",
                                          "history"))
        elif size > max_size:
            result.skipped.append(Skipped(path, "over the size limit", "history"))
        else:
            readable.append(sha)
    scanned = 0
    for sha, data in gitsrc.read_blobs(repo, readable):
        ref = blobs[sha]
        path = sorted(ref.paths)[0]
        text = decode(data)
        if text is None:
            result.skipped.append(Skipped(path, "binary file", "history"))
            continue
        scanned += 1
        result.findings.extend(scan_text(
            text, matchers, area="history", path=path, blob=sha,
            commits=ref.commits[:5], in_head=sha in in_head))
    result.searched["history"] = {
        "commits": commit_count,
        "file_versions": len(blobs),
        "file_versions_read": scanned,
    }


def _scan_commits(repo: str, matchers: Matchers, identity: list[IdentityValue],
                  result: ScanResult) -> None:
    records = gitsrc.commit_records(repo)
    machine = {value.value.lower() for value in identity}
    authored: Counter[tuple[str, str]] = Counter()
    committed: Counter[tuple[str, str]] = Counter()
    for record in records:
        authored[(record.author_name, record.author_email)] += 1
        committed[(record.committer_name, record.committer_email)] += 1
        result.findings.extend(scan_text(
            record.message, matchers, area="commit-message",
            path=f"commit {record.sha[:10]}", commits=[record.sha]))
    for name, email in sorted(set(authored) | set(committed)):
        result.commit_identities.append({
            "name": name,
            "email": email,
            "authored": authored[(name, email)],
            "committed": committed[(name, email)],
            "matches_machine": name.lower() in machine or email.lower() in machine,
        })
    result.searched["commit_messages"] = len(records)


def _scan_remotes(repo: str, matchers: Matchers, result: ScanResult) -> None:
    for name, url in gitsrc.remotes(repo):
        result.remotes.append({"name": name, "url": url})
        result.findings.extend(scan_text(url, matchers, area="remote",
                                         path=f"remote '{name}'"))
    result.searched["remotes"] = len(result.remotes)


def scan_repo(path: str, *, terms: list[str] | None = None, terms_file: str = "",
              history: bool = True, max_size: int = DEFAULT_MAX_SIZE,
              identity: list[IdentityValue] | None = None,
              progress=None) -> ScanResult:
    """Scan one repository. Raises gitsrc.GitError if `path` is not a repo."""
    say = progress or (lambda message: None)
    started = time.monotonic()
    repo = os.path.normpath(gitsrc.toplevel(path))
    result = ScanResult(tool="sweep", version=__version__, part="A", repo=repo,
                        started=datetime.now().astimezone().isoformat(timespec="seconds"))
    if identity is None:
        identity, result.identity_notes = gather(repo)
    result.identity = [asdict(value) for value in identity]
    result.terms_file = terms_file
    result.terms_count = len(terms or [])
    matchers = Matchers(identity, terms or [])

    result.not_checked.append(
        "Secrets (API keys, tokens, passwords in code): not checked. "
        "This version does not scan for them. Use a secret scanner such as "
        "gitleaks as well.")
    result.not_checked.append(
        "Inside binary files, archives and documents (zip, docx, pdf, "
        "images, databases): not checked.")
    result.not_checked.append("IPv6 addresses: not checked.")

    say("Scanning working tree")
    _scan_working_tree(repo, matchers, max_size, result)
    if history and gitsrc.has_commits(repo):
        say("Scanning history")
        _scan_history(repo, matchers, max_size, result)
        say("Scanning commit messages")
        _scan_commits(repo, matchers, identity, result)
    elif history:
        result.searched["history"] = {"commits": 0, "file_versions": 0,
                                      "file_versions_read": 0}
        result.searched["commit_messages"] = 0
    else:
        result.not_checked.append("Git history and commit messages: "
                                  "not checked (turned off for this run).")
    _scan_remotes(repo, matchers, result)
    result.seconds = round(time.monotonic() - started, 2)
    return result
