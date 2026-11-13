"""Part B: look outside the repo for traces of it. Reads only.

One pass over the chosen folders. For each file, at most one read, used for
both checks:
  - exact copy: same size and same content hash as a file version the repo
    holds (most files are ruled out by size alone and never opened)
  - terms: the reviewed term list, searched as plain text
plus two checks on the file name that need no read at all.

Never followed: symbolic links and junctions. Never opened: files that
exist only in the cloud, because opening one makes the sync client
download it.
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import __version__
from .fingerprint import Fingerprint, build as build_fingerprint
from .fingerprint import hash_bytes, hash_file, is_weak
from .gitobjects import shares_history
from .places import is_inside, norm, system_excludes
from .scan import DEFAULT_MAX_SIZE, SKIP_DIRS, is_cloud_only, is_link
from .terms import MIN_TERM_LENGTH, TermList, term_families

STRONG, MEDIUM, WEAK = "strong", "medium", "weak"
MAX_HITS = 50_000
MAX_EXAMPLES = 20

CANNOT_SEE = [
    "Inside archives and disk images (zip, 7z, tar, iso, vhd).",
    "Inside documents and databases for term search (docx, pdf, sqlite). "
    "Exact copies of such files are still found.",
    "Encrypted volumes that are not unlocked.",
    "Files that exist only in the cloud.",
    "Other machines, phones and online accounts.",
    "Network shares, unless one was given as a folder to search.",
    "Copies that were both renamed and edited, if none of the terms "
    "survived the edit.",
]


@dataclass
class TraceHit:
    path: str
    folder: str
    size: int
    strength: str
    exact_copy_of: list[str] = field(default_factory=list)
    weak_copy: bool = False
    name_match: bool = False
    name_terms: list[str] = field(default_factory=list)
    terms: dict[str, int] = field(default_factory=dict)
    families: int = 0
    line: int = 0
    snippet: str = ""
    # When the file was last changed, as seen during the search. A delete
    # is refused if this no longer matches.
    mtime_ns: int = 0


@dataclass
class TraceResult:
    tool: str
    version: str
    part: str
    repo: str
    started: str
    seconds: float = 0.0
    scopes: list[dict] = field(default_factory=list)
    terms_file: str = ""
    terms: list[str] = field(default_factory=list)
    file_names: int = 0
    fingerprint: dict = field(default_factory=dict)
    searched: dict = field(default_factory=dict)
    skipped: dict[str, dict] = field(default_factory=dict)
    cannot_see: list[str] = field(default_factory=list)
    copies: list[str] = field(default_factory=list)
    term_files: dict[str, int] = field(default_factory=dict)
    hits: list[TraceHit] = field(default_factory=list)
    hits_dropped: int = 0
    # True when the person pressed Stop: the search is incomplete.
    stopped: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class Stopped(Exception):
    """Raised inside the walk when the person asked to stop."""


HEAD_SIZE = 8192

# File types that are never plain text. They are not opened for the term
# search at all, because opening a file is the slow part of a first run.
# They are still checked for exact copies when their size matches.
BINARY_TYPES = frozenset("""
png jpg jpeg gif bmp ico webp tif tiff psd heic avif raw cr2 nef
mp3 wav flac ogg m4a aac wma mp4 mkv mov avi wmv webm m4v flv
zip 7z rar gz bz2 xz tar tgz zst cab iso img vhd vhdx vmdk dmg
exe dll sys msi msix appx so dylib bin o obj lib a pdb class jar war pyc pyd
ttf otf woff woff2 eot
pdf doc docx xls xlsx ppt pptx odt ods odp epub mobi
db sqlite sqlite3 mdb accdb pak unity3d assets bundle safetensors gguf onnx
pt pth ckpt npy npz parquet
""".split())


def _wide_text(sample: bytes) -> bool:
    """UTF-16 text without a byte order mark: every second byte is zero."""
    odd = sample[1::2]
    return len(sample) >= 8 and bool(odd) and odd.count(0) >= len(odd) * 0.9


def looks_binary(head: bytes) -> bool:
    """Judge from the first bytes of a file whether it is not text."""
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    sample = head[:HEAD_SIZE]
    return b"\0" in sample and not _wide_text(sample)


def searchable(data: bytes) -> bytes | None:
    """Return the data as UTF-8 text bytes, or None if it looks binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace").encode("utf-8")
    sample = data[:HEAD_SIZE]
    if b"\0" in sample:
        if _wide_text(sample):
            return data.decode("utf-16-le", errors="replace").encode("utf-8")
        return None
    return data


class _Tracer:
    def __init__(self, fingerprint: Fingerprint, term_list: TermList | None,
                 exclude: list[str], max_size: int,
                 include_dependency_folders: bool, progress,
                 should_stop=None, count_only: bool = False):
        self.fp = fingerprint
        self.max_size = max_size
        self.should_stop = should_stop or (lambda: False)
        # Count mode walks the same folders with the same rules but opens
        # nothing. It is used to estimate a search before running it.
        self.count_only = count_only
        self.would_open = 0
        self.include_deps = include_dependency_folders
        self.progress = progress
        terms = term_list.terms if term_list else []
        self.terms = terms
        self.term_bytes = [(t, t.lower().encode("utf-8")) for t in terms]
        self.terms_lower = [t.lower() for t in terms]
        self.families = term_families(terms)
        # A term that holds the repo's own name is stronger evidence than
        # any other single term. Hyphens and underscores count as the same.
        repo_name = fingerprint.name.lower().replace("-", "_")
        self.seeds = {
            t.lower() for t in terms
            if len(repo_name) >= MIN_TERM_LENGTH
            and repo_name in t.lower().replace("-", "_")
        }
        self.file_names = {
            name.lower() for name in (term_list.file_names if term_list else [])
        }
        self.term_file = norm(term_list.path) if term_list and term_list.path else ""
        self.excluded = {norm(p) for p in exclude if p}
        self.result_skipped: dict[str, dict] = {}
        self.copies: list[str] = []
        self.hits: list[TraceHit] = []
        self.hits_dropped = 0
        self.term_files: dict[str, int] = {t: 0 for t in terms}
        self.folders = 0
        self.files_seen = 0
        self.files_read = 0
        self.bytes_read = 0

    # --- bookkeeping ---

    def skip(self, reason: str, path: str) -> None:
        entry = self.result_skipped.setdefault(reason, {"count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < MAX_EXAMPLES:
            entry["examples"].append(path)

    def is_excluded(self, path: str) -> bool:
        return norm(path) in self.excluded

    # --- walking ---

    def walk(self, root: str) -> None:
        if os.path.isfile(root):
            try:
                info = os.stat(root)
            except OSError:
                self.skip("could not read file details", root)
                return
            self.files_seen += 1
            if is_cloud_only(info):
                self.skip("cloud-only file (reading it would download it)", root)
            else:
                self.check_file(root, os.path.basename(root), info.st_size,
                                info.st_mtime_ns)
            return
        try:
            if is_cloud_only(os.stat(root)):
                self.skip("cloud-only folder (listing it would fetch it)", root)
                return
        except OSError:
            self.skip("could not open folder", root)
            return
        stack = [root]
        while stack:
            if self.should_stop():
                raise Stopped()
            folder = stack.pop()
            try:
                with os.scandir(folder) as listing:
                    entries = list(listing)
            except OSError:
                self.skip("could not open folder", folder)
                continue
            self.folders += 1
            if self.folders % 200 == 0:
                self.progress(f"{self.files_seen:,} files seen, "
                              f"{len(self.hits):,} hits")
            dot_git = next((e for e in entries if e.name == ".git"), None)
            if (dot_git is not None and self.fp.anchors
                    and shares_history(dot_git.path, self.fp.anchors)):
                self.copies.append(folder)
                continue
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    self.skip("could not read file details", entry.path)
                    continue
                mode = info.st_mode
                if is_link(info):
                    self.skip("link or junction (not followed)", entry.path)
                elif stat.S_ISDIR(mode):
                    self.check_folder(entry, info, stack)
                elif stat.S_ISREG(mode):
                    self.files_seen += 1
                    if is_cloud_only(info):
                        self.skip("cloud-only file (reading it would "
                                  "download it)", entry.path)
                    else:
                        self.check_file(entry.path, entry.name, info.st_size,
                                        info.st_mtime_ns)

    def check_folder(self, entry, info, stack: list[str]) -> None:
        if entry.name == ".git":
            self.skip("git storage of another repo", entry.path)
        elif entry.name in SKIP_DIRS and not self.include_deps:
            self.skip("dependency or cache folder", entry.path)
        elif self.is_excluded(entry.path):
            self.skip("left out on purpose (the repo itself, a linked "
                      "working tree, the report folder, or a system folder)",
                      entry.path)
        elif is_cloud_only(info):
            self.skip("cloud-only folder (listing it would fetch it)",
                      entry.path)
        else:
            stack.append(entry.path)

    # --- one file ---

    def check_file(self, path: str, name: str, size: int,
                   mtime_ns: int = 0) -> None:
        if self.term_file and norm(path) == self.term_file:
            return
        lower = name.lower()
        name_match = lower in self.file_names
        name_terms = [t for t in self.terms_lower if t in lower]
        want_hash = size in self.fp.by_size
        want_terms = bool(self.term_bytes) and size >= MIN_TERM_LENGTH
        if want_terms and lower.rpartition(".")[2] in BINARY_TYPES:
            if not self.count_only:
                self.skip("binary file type (not searched for terms)", path)
            want_terms = False
        if self.count_only:
            if want_hash or (want_terms and size <= self.max_size):
                self.would_open += 1
            return
        copy_of: list[str] | None = None
        counts: dict[str, int] = {}
        line, snippet = 0, ""
        if size > self.max_size:
            if want_terms:
                self.skip("over the size limit (not searched for terms)", path)
            if want_hash:
                try:
                    digest, read = hash_file(path, self.fp.algo)
                except OSError:
                    self.skip("could not read file", path)
                    return
                self.files_read += 1
                self.bytes_read += read
                copy_of = self.fp.lookup(read, digest)
        elif want_hash or want_terms:
            try:
                with open(path, "rb") as handle:
                    # Read the start first. A binary file that is not a
                    # candidate for an exact copy needs nothing more.
                    data = handle.read(HEAD_SIZE)
                    binary = looks_binary(data)
                    if len(data) == HEAD_SIZE and (want_hash or not binary):
                        data += handle.read(self.max_size + 1 - HEAD_SIZE)
            except OSError:
                self.skip("could not read file", path)
                return
            self.files_read += 1
            self.bytes_read += len(data)
            if want_hash and len(data) in self.fp.by_size:
                copy_of = self.fp.lookup(len(data), hash_bytes(data, self.fp.algo))
            if want_terms and binary:
                self.skip("binary file (not searched for terms)", path)
            elif want_terms and len(data) <= self.max_size:
                counts, line, snippet = self.search(data, path)
        if not (copy_of or name_match or name_terms or counts):
            return
        hit = TraceHit(
            path=path, folder=os.path.dirname(path), size=size, strength=WEAK,
            exact_copy_of=copy_of or [],
            weak_copy=bool(copy_of) and is_weak(copy_of, size),
            name_match=name_match, name_terms=name_terms, terms=counts,
            families=len({self.families[t.lower()] for t in counts}),
            line=line, snippet=snippet, mtime_ns=mtime_ns,
        )
        hit.strength = self.strength(hit)
        for term in counts:
            self.term_files[term] += 1
        if len(self.hits) >= MAX_HITS and hit.strength == WEAK:
            self.hits_dropped += 1
            return
        self.hits.append(hit)

    def search(self, data: bytes, path: str) -> tuple[dict[str, int], int, str]:
        text = searchable(data)
        if text is None:
            self.skip("binary file (not searched for terms)", path)
            return {}, 0, ""
        lowered = text.lower()
        counts: dict[str, int] = {}
        first = -1
        for term, raw in self.term_bytes:
            position = lowered.find(raw)
            if position >= 0:
                counts[term] = lowered.count(raw)
                if first < 0 or position < first:
                    first = position
        if not counts:
            return {}, 0, ""
        start = text.rfind(b"\n", 0, first) + 1
        end = text.find(b"\n", first)
        if end < 0:
            end = len(text)
        left = max(start, first - 80)
        right = min(end, first + 120)
        snippet = text[left:right].decode("utf-8", errors="replace").strip()
        return counts, text.count(b"\n", 0, first) + 1, snippet

    def strength(self, hit: TraceHit) -> str:
        if hit.exact_copy_of and not hit.weak_copy:
            return STRONG
        found = {t.lower() for t in hit.terms} | set(hit.name_terms)
        if hit.name_match and found:
            return STRONG
        if hit.families >= 3:
            return STRONG
        if hit.name_match or hit.families == 2 or found & self.seeds:
            return MEDIUM
        return WEAK


def trace_repo(repo: str, term_list: TermList | None,
               scopes: list[tuple[str, str]], *, exclude: list[str] | None = None,
               max_size: int = DEFAULT_MAX_SIZE,
               include_dependency_folders: bool = False,
               progress=None, should_stop=None,
               count_only: bool = False,
               fingerprint: Fingerprint | None = None) -> TraceResult:
    """Search `scopes` (label, path) for traces of `repo`.

    should_stop: a function; when it returns True the search ends early and
    the result is marked stopped.
    count_only: walk with the same rules but open nothing; the result's
    `searched` holds the counts used for an estimate.
    """
    say = progress or (lambda message: None)
    started = time.monotonic()
    if fingerprint is None:
        say("Fingerprinting the repo")
        fingerprint = build_fingerprint(repo)
    result = TraceResult(
        tool="sweep", version=__version__, part="B", repo=fingerprint.repo,
        started=datetime.now().astimezone().isoformat(timespec="seconds"),
        cannot_see=list(CANNOT_SEE),
    )
    result.fingerprint = {
        "hash": fingerprint.algo,
        "file_versions": fingerprint.versions,
        "too_small_to_fingerprint": fingerprint.too_small,
        "unreadable": fingerprint.unreadable,
    }
    if term_list:
        result.terms_file = term_list.path
        result.terms = list(term_list.terms)
        result.file_names = len(term_list.file_names)

    never = [fingerprint.repo, *fingerprint.worktrees, *system_excludes(),
             *(exclude or [])]
    tracer = _Tracer(fingerprint, term_list, never, max_size,
                     include_dependency_folders, say, should_stop, count_only)

    walked: list[str] = []
    ordered = sorted(scopes, key=lambda item: len(os.path.realpath(item[1])))
    for label, given in ordered:
        entry = {"label": label, "path": given, "status": "searched"}
        result.scopes.append(entry)
        if result.stopped:
            entry["status"] = "not searched (stopped)"
            continue
        if not os.path.exists(given):
            entry["status"] = "does not exist"
            continue
        real = os.path.realpath(given)
        if real != os.path.abspath(given):
            entry["resolved_to"] = real
        blocked = next((n for n in never if n and is_inside(real, n)), None)
        if blocked:
            entry["status"] = f"left out on purpose (inside {blocked})"
            continue
        covering = next((w for w in walked if is_inside(real, w)), None)
        if covering:
            entry["status"] = f"already covered by {covering}"
            continue
        say(f"Searching {real}")
        try:
            tracer.walk(real)
        except Stopped:
            result.stopped = True
            entry["status"] = "stopped part way"
        walked.append(real)

    order = {STRONG: 0, MEDIUM: 1, WEAK: 2}
    tracer.hits.sort(key=lambda hit: (order[hit.strength], hit.path.lower()))
    result.hits = tracer.hits
    result.hits_dropped = tracer.hits_dropped
    result.copies = sorted(tracer.copies)
    result.term_files = tracer.term_files
    result.skipped = tracer.result_skipped
    result.seconds = round(time.monotonic() - started, 2)
    megabytes = tracer.bytes_read / (1024 * 1024)
    result.searched = {
        "folders": tracer.folders,
        "files_seen": tracer.files_seen,
        "files_read": tracer.files_read,
        "megabytes_read": round(megabytes, 1),
        "files_per_second": round(tracer.files_seen / result.seconds)
        if result.seconds else 0,
    }
    if count_only:
        result.searched["files_to_open"] = tracer.would_open
    return result
