"""The repo's fingerprint: a hash for every file version it holds.

Git already names each file version by a hash of its content, so history
costs nothing to fingerprint: the names are the hashes. Files in the working
tree are hashed the same way as they sit on disk, which also covers files
whose line endings git converted on checkout.

A file anywhere else with the same size and the same hash is an exact copy.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field

from . import gitsrc
from .scan import is_cloud_only, is_link, list_working_tree

# Smaller files (empty files, one-line stubs) are identical all over a disk
# by chance, so they are not fingerprinted.
MIN_HASH_SIZE = 32
# A match on one of these names, or on a very small file, is weak evidence:
# the same text sits in many unrelated projects.
SMALL_FILE = 128
WEAK_NAMES = {
    "license", "license.txt", "license.md", "licence", "licence.txt",
    "copying", "copying.txt", "notice", "notice.txt", "unlicense",
    ".gitignore", ".gitattributes", ".editorconfig", ".gitkeep",
    ".nojekyll", "__init__.py", "py.typed", "robots.txt", "manifest.in",
    "code_of_conduct.md", "contributing.md", "security.md",
}
_CHUNK = 1024 * 1024


def new_hasher(algo: str, size: int):
    """A hasher primed the way git names a file of `size` bytes."""
    hasher = hashlib.new(algo)
    hasher.update(b"blob %d\0" % size)
    return hasher


def hash_bytes(data: bytes, algo: str) -> str:
    hasher = new_hasher(algo, len(data))
    hasher.update(data)
    return hasher.hexdigest()


def hash_file(path: str, algo: str) -> tuple[str, int]:
    """Return (hash, size) for a file, reading it in pieces."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        hasher = new_hasher(algo, size)
        total = 0
        while True:
            piece = handle.read(_CHUNK)
            if not piece:
                break
            hasher.update(piece)
            total += len(piece)
    if total != size:
        raise OSError("file changed while it was being read")
    return hasher.hexdigest(), size


def is_weak(repo_paths: list[str], size: int) -> bool:
    if size < SMALL_FILE:
        return True
    names = {path.rsplit("/", 1)[-1].lower() for path in repo_paths}
    return bool(names) and names <= WEAK_NAMES


@dataclass
class Fingerprint:
    repo: str
    name: str
    algo: str
    # size -> hash -> repo paths that have had this content
    by_size: dict[int, dict[str, list[str]]] = field(default_factory=dict)
    paths: set[str] = field(default_factory=set)
    anchors: list[str] = field(default_factory=list)
    worktrees: list[str] = field(default_factory=list)
    versions: int = 0
    too_small: int = 0
    unreadable: int = 0

    def add(self, size: int, digest: str, path: str) -> None:
        self.paths.add(path)
        if size < MIN_HASH_SIZE:
            self.too_small += 1
            return
        bucket = self.by_size.setdefault(size, {})
        if digest not in bucket:
            bucket[digest] = []
            self.versions += 1
        if path not in bucket[digest] and len(bucket[digest]) < 5:
            bucket[digest].append(path)

    def lookup(self, size: int, digest: str) -> list[str] | None:
        paths = self.by_size.get(size, {}).get(digest)
        return sorted(paths) if paths else None


def build(repo: str) -> Fingerprint:
    repo = os.path.normpath(gitsrc.toplevel(repo))
    result = Fingerprint(
        repo=repo,
        name=os.path.basename(repo),
        algo=gitsrc.object_format(repo),
        anchors=gitsrc.anchor_commits(repo),
        worktrees=[os.path.normpath(p) for p in gitsrc.worktrees(repo)],
    )
    if gitsrc.has_commits(repo):
        _, blobs = gitsrc.history_blobs(repo)
        sizes = gitsrc.blob_sizes(repo, list(blobs))
        for sha, ref in blobs.items():
            size = sizes.get(sha)
            if size is None:
                result.unreadable += 1
                continue
            for path in sorted(ref.paths):
                result.add(size, sha, path)
    files, _ = list_working_tree(repo)
    for path, _status in files:
        full = os.path.join(repo, path)
        try:
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode) or is_link(info):
                continue
            if is_cloud_only(info):
                result.unreadable += 1
                continue
            if info.st_size < MIN_HASH_SIZE:
                result.paths.add(path)
                result.too_small += 1
                continue
            digest, size = hash_file(full, result.algo)
        except OSError:
            result.unreadable += 1
            continue
        result.add(size, digest, path)
    return result
