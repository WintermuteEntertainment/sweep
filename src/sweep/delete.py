"""Deleting, as an offer the person confirms. Never called by the scan or
the search themselves.

Every file is checked again on the server just before anything happens to
it, whatever the page sent. A file is left alone, and the reason reported,
when it:
  - no longer exists, or is now a link, junction or cloud-only file
  - changed size or modified time since the search
  - sits inside a git repo other than the one being scanned (never
    deletable from Sweep: the person is told how to remove it by hand)
  - is a tracked file of the scanned repo and the DANGER warning for it
    was not acknowledged
  - is a report outside Sweep's report folder, or not named like one

Two actions: move to the Recycle Bin (Windows), or delete permanently,
which needs the number of files typed as confirmation.
"""
from __future__ import annotations

import os
import platform
import re
import stat
from dataclasses import dataclass, field

from .places import is_inside, norm
from .scan import is_cloud_only, is_link

LEFTOVER = "leftover"
REPO_UNTRACKED = "repo-untracked"
REPO_IGNORED = "repo-ignored"
REPO_TRACKED = "repo-tracked"
REPORT = "report"
KINDS = {LEFTOVER, REPO_UNTRACKED, REPO_IGNORED, REPO_TRACKED, REPORT}

RECYCLE = "recycle"
PERMANENT = "permanent"

REPORT_NAME = re.compile(
    r".+\.(sweep-report\.(json|txt)|sweep-trace\.(json|txt)|sweep-terms\.txt)$",
    re.IGNORECASE)


class DeleteError(Exception):
    pass


@dataclass
class Target:
    path: str
    size: int
    mtime_ns: int
    kind: str


@dataclass
class Outcome:
    path: str
    done: bool
    action: str
    message: str
    steps: list[str] = field(default_factory=list)


def enclosing_repo(path: str) -> str | None:
    """The top folder of the git repo holding `path`, found by looking for a
    '.git' entry in each parent folder. Reads folder entries only; git is
    not run."""
    folder = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.lexists(os.path.join(folder, ".git")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def manual_steps(path: str, repo: str) -> list[str]:
    """How to remove a file from another repo by hand. Shown, never run."""
    relative = os.path.relpath(path, repo).replace("\\", "/")
    return [
        f'This file belongs to another git repo: {repo}',
        "Sweep does not delete files in other repos. To remove it yourself:",
        f'  If git tracks it:   git -C "{repo}" rm "{relative}"',
        f'                      git -C "{repo}" commit -m "Remove {relative}"',
        "  If git does not track it: delete it in File Explorer.",
        "  Either way, anything already committed stays in that repo's "
        "history until the history is rewritten (for example with "
        "git-filter-repo), and copies others already pulled keep it.",
    ]


def tracked_warning(path: str, repo: str) -> list[str]:
    relative = os.path.relpath(path, repo).replace("\\", "/")
    return [
        "DANGER: git tracks this file.",
        "Deleting it only removes the copy in the folder. Git still has it:",
        f'  - the deletion must be committed: git -C "{repo}" commit -a',
        "  - every earlier version stays in history until history is "
        "rewritten, and copies others already pulled keep it.",
        f"File: {relative}",
    ]


def refusal(target: Target, scanned_repo: str, *, danger_ok: bool = False,
            report_dir: str = "") -> tuple[str, list[str]]:
    """Why this target must be left alone, or ('', []) if it may go."""
    if target.kind not in KINDS:
        return "unknown kind of file", []
    try:
        info = os.lstat(target.path)
    except FileNotFoundError:
        return "no longer there", []
    except OSError as error:
        return f"could not check it: {error.strerror}", []
    if is_link(info):
        return "it is a link or junction now", []
    if not stat.S_ISREG(info.st_mode):
        return "it is not a plain file", []
    if is_cloud_only(info):
        return "it is a cloud-only file", []
    if info.st_size != target.size or info.st_mtime_ns != target.mtime_ns:
        return "it changed since the search; search again first", []

    owner = enclosing_repo(target.path)
    scanned = norm(scanned_repo) if scanned_repo else ""
    if target.kind == REPORT:
        if not report_dir or not is_inside(target.path, report_dir):
            return "it is not in Sweep's report folder", []
        if not REPORT_NAME.match(os.path.basename(target.path)):
            return "it is not named like a Sweep report", []
        if owner:
            return "the report folder is inside a git repo", []
        return "", []
    if owner and norm(owner) != scanned:
        return "it is inside another git repo", manual_steps(target.path, owner)
    if target.kind == LEFTOVER:
        if owner:
            return "it is inside the scanned repo; use section 1", []
        return "", []
    # A file of the scanned repo.
    if not owner or norm(owner) != scanned:
        return "it is not inside the scanned repo", []
    if target.kind == REPO_TRACKED and not danger_ok:
        return ("git tracks it and the DANGER warning was not acknowledged",
                tracked_warning(target.path, owner))
    return "", []


# --- the two actions --------------------------------------------------------

def recycle_supported() -> bool:
    return platform.system() == "Windows"


def _recycle_windows(path: str) -> None:
    """Send one file to the Recycle Bin with the Windows shell.

    FOF_WANTNUKEWARNING makes Windows ask on screen before it would delete
    a file permanently, which it does when a file cannot go to the Recycle
    Bin (too large for it, or on a drive without one). Without that flag
    the file would be destroyed silently.
    """
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    fo_delete = 3
    flags = 0x0004 | 0x0010 | 0x0040 | 0x0400 | 0x4000
    # SILENT | NOCONFIRMATION | ALLOWUNDO | NOERRORUI | WANTNUKEWARNING
    source = ctypes.create_unicode_buffer(os.path.abspath(path) + "\0")
    operation = SHFILEOPSTRUCTW(None, fo_delete, ctypes.cast(source, wintypes.LPCWSTR),
                                None, flags, False, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if operation.fAnyOperationsAborted:
        raise OSError("cancelled on screen")
    if code != 0:
        raise OSError(f"Windows shell error {code:#x}")
    if os.path.lexists(path):
        raise OSError("the file is still there after the shell call")


# Tests replace this to avoid filling the real Recycle Bin.
send_to_recycle_bin = _recycle_windows


def perform(targets: list[Target], scanned_repo: str, mode: str, *,
            confirm_count: int | None = None, danger_ok: set[str] | None = None,
            report_dir: str = "") -> list[Outcome]:
    if mode not in (RECYCLE, PERMANENT):
        raise DeleteError("unknown action")
    if not targets:
        raise DeleteError("no files were chosen")
    if mode == RECYCLE and not recycle_supported():
        raise DeleteError("the Recycle Bin is only supported on Windows")
    if mode == PERMANENT and confirm_count != len(targets):
        raise DeleteError(
            f"permanent delete needs the number of files typed: {len(targets)}")
    danger_ok = danger_ok or set()
    outcomes: list[Outcome] = []
    for target in targets:
        reason, steps = refusal(target, scanned_repo,
                                danger_ok=norm(target.path) in danger_ok,
                                report_dir=report_dir)
        if reason:
            outcomes.append(Outcome(target.path, False, "left alone", reason, steps))
            continue
        try:
            if mode == RECYCLE:
                send_to_recycle_bin(target.path)
                outcomes.append(Outcome(target.path, True, "moved to Recycle Bin", ""))
            else:
                os.remove(target.path)
                outcomes.append(Outcome(target.path, True, "deleted permanently", ""))
        except OSError as error:
            outcomes.append(Outcome(target.path, False, "failed",
                                    str(error.strerror or error)))
    return outcomes
