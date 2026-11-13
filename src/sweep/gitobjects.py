"""Check whether another folder's .git holds a given commit, by reading its
files directly.

Sweep does not run git inside folders it finds while searching. Git refuses
to run in a repo owned by someone else for a good reason: a repo's own
settings can make git start other programs. Reading the object store as
plain files avoids that completely.

Covers loose objects and version 2 pack index files. It does not follow
"alternates" files or read multi-pack indexes; a repo that stores a commit
only that way is reported as unrelated.
"""
from __future__ import annotations

import os
import struct

_IDX_MAGIC = b"\xfftOc"


def resolve_git_dir(dot_git: str) -> str | None:
    """Return the real git folder for a '.git' folder or pointer file."""
    try:
        if os.path.isdir(dot_git):
            found = dot_git
        else:
            with open(dot_git, "rb") as handle:
                first = handle.read(4096).decode("utf-8", errors="replace")
            if not first.startswith("gitdir:"):
                return None
            target = first[len("gitdir:"):].splitlines()[0].strip()
            found = os.path.normpath(
                os.path.join(os.path.dirname(dot_git), target))
            if not os.path.isdir(found):
                return None
        # A linked working tree keeps its objects in the main repo.
        common = os.path.join(found, "commondir")
        if os.path.isfile(common):
            with open(common, "rb") as handle:
                relative = handle.read(4096).decode("utf-8", errors="replace").strip()
            shared = os.path.normpath(os.path.join(found, relative))
            if os.path.isdir(shared):
                return shared
        return found
    except OSError:
        return None


def _index_has(path: str, raw: bytes) -> bool:
    with open(path, "rb") as handle:
        head = handle.read(8)
        if len(head) < 8 or head[:4] != _IDX_MAGIC:
            return False
        if struct.unpack(">I", head[4:])[0] != 2:
            return False
        table = handle.read(1024)
        if len(table) < 1024:
            return False
        fanout = struct.unpack(">256I", table)
        first = raw[0]
        low = fanout[first - 1] if first else 0
        high = fanout[first]
        width = len(raw)
        while low < high:
            middle = (low + high) // 2
            handle.seek(8 + 1024 + middle * width)
            current = handle.read(width)
            if current == raw:
                return True
            if current < raw:
                low = middle + 1
            else:
                high = middle
    return False


def has_object(git_dir: str, hex_id: str) -> bool:
    """True if the object is stored in this git folder."""
    try:
        raw = bytes.fromhex(hex_id)
    except ValueError:
        return False
    objects = os.path.join(git_dir, "objects")
    if os.path.isfile(os.path.join(objects, hex_id[:2], hex_id[2:])):
        return True
    packs = os.path.join(objects, "pack")
    try:
        names = os.listdir(packs)
    except OSError:
        return False
    for name in names:
        if name.endswith(".idx"):
            try:
                if _index_has(os.path.join(packs, name), raw):
                    return True
            except OSError:
                continue
    return False


def shares_history(dot_git: str, anchors: list[str]) -> bool:
    git_dir = resolve_git_dir(dot_git)
    if not git_dir:
        return False
    return any(has_object(git_dir, anchor) for anchor in anchors)
