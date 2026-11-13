"""Build and read the term list for Part B.

The list holds words and file names that are likely to be specific to one
repo. Sweep proposes it; a person must read it, remove ordinary words, and
mark it reviewed before any search will use it.

"Ordinary" is decided by comparing against a background vocabulary built on
the spot from the text of Python's own standard library (a large body of
code and English that every machine running Sweep already has), plus a short
built-in list and the system dictionary where one exists.
"""
from __future__ import annotations

import builtins
import json
import keyword
import os
import re
import stat
import sysconfig
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import gitsrc
from .scan import (DEFAULT_MAX_SIZE, decode, is_cloud_only, is_link,
                   list_working_tree)

MIN_TERM_LENGTH = 4
DEFAULT_MAX_TERMS = 40
MAX_FILE_NAMES = 300

# Common project and English words that Python's library happens not to use.
EXTRA_COMMON = """
readme changelog license licence todo notes handoff plan roadmap draft docs
doc deploy deployment dashboard frontend backend website webapp homepage
landing login logout signup admin username password email phone address
kitchen office house home family friend photo photos picture pictures video
videos music movie movies game games player score level world story chapter
book novel poem recipe budget invoice receipt report reports summary review
meeting calendar schedule project projects folder folders backup backups
archive archives download downloads upload uploads desktop laptop tablet
printer scanner camera screen monitor keyboard mouse speaker router modem
wifi ethernet bluetooth firewall gateway subnet hostname localhost domain
subdomain server servers client clients database databases dataset datasets
pipeline workflow workflows script scripts plugin plugins widget widgets
component components layout theme themes style styles stylesheet template
templates fixture fixtures snapshot snapshots benchmark benchmarks metric
metrics analytics telemetry dashboard dashboards settings preferences
config configs configuration environment staging production development
docker container containers kubernetes cluster node nodes yaml toml json
html css javascript typescript python rust golang java kotlin swift ruby
github gitlab bitbucket commit commits branch branches merge rebase repo
repos repository repositories pull push clone fork issue issues release
releases version versions changelog upgrade downgrade install installer
uninstall setup wizard tutorial guide manual example examples sample
samples demo demos test tests testing tested tester runner runners
frontend middleware endpoint endpoints webhook webhooks payload payloads
token tokens secret secrets credential credentials session sessions cookie
cookies cache caches queue queues worker workers scheduler cron job jobs
task tasks event events message messages notification notifications alert
alerts email emails inbox outbox thread threads comment comments feedback
tunnel proxy vendor vendors device devices sensor sensors firmware driver
drivers hardware software network networks scan scans scanning mapper
diagram diagrams chart charts graph graphs table tables column columns
wireless topology evidence threat threats watchlist blocklist allowlist
whitelist blacklist checklist playlist wishlist shortlist gitignore
gitattributes gitkeep gitmodules dockerignore editorconfig prettierrc
eslintrc claude copilot chatgpt openai anthropic gemini codex cursor ollama
llama mistral assistant chatbot prompt prompts agent agents inventory
catalog catalogue registry tracker monitor monitoring detector analyzer
analyser enumerator classifier validator converter exporter importer
""".split()

_WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{3,39}(?![A-Za-z0-9_])")
_BG_WORD = re.compile(rb"[A-Za-z][A-Za-z0-9_]{2,}")
_SUBWORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_HEX = re.compile(r"[0-9a-f]{8,}")
_VOWEL = re.compile(r"[aeiouy]")

GENERIC_FILE_NAMES = {
    "readme.md", "readme.txt", "readme.rst", "changelog.md", "license",
    "license.txt", "license.md", "makefile", "dockerfile", "main.py",
    "index.js", "index.html", "setup.py", "setup.cfg", "pyproject.toml",
    "package.json", "package-lock.json", "requirements.txt", "config.json",
    "settings.json", "__init__.py", "__main__.py", ".gitignore",
    ".gitattributes", "handoff.md", "notes.md", "plan.md", "todo.md",
}


class TermError(Exception):
    pass


def split_subwords(token: str) -> list[str]:
    """FileServer_v2 -> ['File', 'Server', 'v', '2']"""
    return _SUBWORD.findall(token)


def background_vocabulary() -> tuple[set[str], list[str]]:
    """Return (lowercase common words, where they came from)."""
    words: set[str] = set(EXTRA_COMMON)
    words.update(keyword.kwlist)
    words.update(name.lower() for name in dir(builtins))
    sources = ["built-in list"]
    raw: set[bytes] = set()
    stdlib = sysconfig.get_paths().get("stdlib", "")
    files = 0
    if stdlib and os.path.isdir(stdlib):
        for folder, dirs, names in os.walk(stdlib):
            dirs[:] = [d for d in dirs
                       if d not in ("site-packages", "__pycache__")]
            for name in names:
                if not name.endswith((".py", ".txt", ".html", ".rst")):
                    continue
                try:
                    with open(os.path.join(folder, name), "rb") as handle:
                        raw.update(_BG_WORD.findall(handle.read()))
                except OSError:
                    continue
                files += 1
    if files:
        sources.append(f"Python standard library text ({files} files)")
    for dictionary in ("/usr/share/dict/words",):
        try:
            with open(dictionary, "rb") as handle:
                raw.update(_BG_WORD.findall(handle.read()))
            sources.append(dictionary)
        except OSError:
            pass
    for token in raw:
        text = token.decode("ascii", errors="ignore")
        words.add(text.lower())
        for part in split_subwords(text):
            if len(part) >= 3:
                words.add(part.lower())
    return words, sources


def _is_junk(token: str) -> bool:
    lower = token.lower()
    if _HEX.fullmatch(lower):
        return True
    digits = sum(char.isdigit() for char in token)
    if digits / len(token) > 0.4:
        return True
    if len(token) >= 5 and not _VOWEL.search(lower):
        return True
    return False


_SUFFIXES = ("ers", "ing", "es", "ed", "ly", "er", "s")


def is_common(lower: str, vocabulary: set[str]) -> bool:
    """True for a common word, or a plain ending added to one
    ('probing' when 'probe' is common)."""
    if lower in vocabulary:
        return True
    for suffix in _SUFFIXES:
        if lower.endswith(suffix) and len(lower) - len(suffix) >= 3:
            stem = lower[: -len(suffix)]
            if stem in vocabulary or stem + "e" in vocabulary:
                return True
    return False


def _unknown_parts(token: str, vocabulary: set[str]) -> list[str]:
    return [part for part in split_subwords(token)
            if len(part) >= 3 and not part.isdigit()
            and not is_common(part.lower(), vocabulary)]


def classify(token: str, vocabulary: set[str]) -> str:
    """'unknown' (holds a word that is not common), 'compound' (common
    words joined into something specific), or '' (ordinary)."""
    if is_common(token.lower(), vocabulary) or _is_junk(token):
        return ""
    parts = [p for p in split_subwords(token) if not p.isdigit()]
    if _unknown_parts(token, vocabulary):
        return "unknown"
    if len(parts) >= 2:
        return "compound"
    return ""


@dataclass
class Candidate:
    term: str
    score: float
    files: int
    why: str


def _remote_names(repo: str) -> list[str]:
    names: list[str] = []
    for _name, url in gitsrc.remotes(repo):
        path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*/", "", url, flags=re.I)
        path = re.sub(r"^[^@/]+@[^:/]+:", "", path)
        path = path.rstrip("/").removesuffix(".git")
        pieces = [piece for piece in path.split("/") if piece]
        if len(pieces) >= 2:
            names.append("/".join(pieces[-2:]))
        if pieces:
            names.append(pieces[-1])
    return names


def _project_names(repo: str) -> list[str]:
    names: list[str] = []
    try:
        import tomllib
        with open(os.path.join(repo, "pyproject.toml"), "rb") as handle:
            name = tomllib.load(handle).get("project", {}).get("name")
        if isinstance(name, str):
            names.append(name)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(repo, "package.json"), encoding="utf-8") as handle:
            name = json.load(handle).get("name")
        if isinstance(name, str):
            names.append(name)
    except (OSError, ValueError, AttributeError):
        pass
    return names


def build_candidates(repo: str, *, max_terms: int = DEFAULT_MAX_TERMS,
                     max_size: int = DEFAULT_MAX_SIZE,
                     vocabulary: set[str] | None = None,
                     ) -> tuple[list[Candidate], list[str], list[str]]:
    """Return (terms, file names, notes) proposed for this repo."""
    repo = os.path.normpath(gitsrc.toplevel(repo))
    notes: list[str] = []
    if vocabulary is None:
        vocabulary, sources = background_vocabulary()
        notes.append("Common words came from: " + "; ".join(sources) + ".")

    # Seeds: names the repo goes by. Always proposed, whatever they score.
    seeds: dict[str, str] = {}
    spelling: dict[str, str] = {}
    named = ([(os.path.basename(repo), "the repo folder name")]
             + [(n, "from the remote address") for n in _remote_names(repo)]
             + [(n, "the project name") for n in _project_names(repo)])
    for name, why in named:
        for variant in sorted({name, name.replace("-", "_")}):
            if len(variant) >= MIN_TERM_LENGTH:
                seeds.setdefault(variant.lower(), why)
                spelling.setdefault(variant.lower(), variant)
            else:
                notes.append(f"'{variant}' ({why}) is shorter than "
                             f"{MIN_TERM_LENGTH} characters and was left out.")

    files, _skipped = list_working_tree(repo)
    in_files: Counter[str] = Counter()
    in_names: set[str] = set()
    all_names: set[str] = set()
    for path, status in files:
        if status == "ignored":
            continue
        all_names.add(path.rsplit("/", 1)[-1])
        for component in path.split("/"):
            if component.startswith("."):
                continue  # .gitignore, .github, .vscode: tool names, not ours
            for token in _WORD.findall(component.replace(".", " ").replace("-", " ")):
                in_names.add(token.lower())
                spelling.setdefault(token.lower(), token)
        full = os.path.join(repo, path)
        try:
            info = os.lstat(full)
            if (not stat.S_ISREG(info.st_mode) or is_link(info)
                    or is_cloud_only(info) or info.st_size > max_size):
                continue
            with open(full, "rb") as handle:
                text = decode(handle.read())
        except OSError:
            continue
        if text is None:
            continue
        seen = set()
        for token in _WORD.findall(text):
            lower = token.lower()
            if lower not in seen:
                seen.add(lower)
                spelling.setdefault(lower, token)
        in_files.update(seen)

    scored: list[Candidate] = []
    for lower, why in seeds.items():
        scored.append(Candidate(spelling.get(lower, lower), 100.0,
                                in_files.get(lower, 0), why))
    for lower in set(in_files) | in_names:
        if lower in seeds:
            continue
        token = spelling[lower]
        kind = classify(token, vocabulary)
        if not kind:
            continue
        count = in_files.get(lower, 0)
        named = lower in in_names
        if count < 2 and not named:
            continue
        score = 3.0 if kind == "unknown" else 1.5
        reasons = ["not a common word" if kind == "unknown"
                   else "common words joined together"]
        if named:
            score += 4.0
            reasons.append("in a file or folder name")
        if "_" in token or any(c.isdigit() for c in token) or (
                token != token.lower() and token != token.upper()
                and not token.istitle()):
            score += 1.0
        score += min(count, 8) * 0.4 + min(len(token), 14) * 0.1
        scored.append(Candidate(token, round(score, 2), count,
                                ", ".join(reasons)))
    scored.sort(key=lambda c: (-c.score, c.term.lower()))
    if len(scored) > max_terms:
        notes.append(f"{len(scored) - max_terms} lower-scoring candidate "
                     f"terms were left out (limit {max_terms}).")
    terms = scored[:max_terms]

    # File names: exact names worth looking for elsewhere.
    history_names: set[str] = set()
    if gitsrc.has_commits(repo):
        _, blobs = gitsrc.history_blobs(repo)
        for ref in blobs.values():
            history_names.update(p.rsplit("/", 1)[-1] for p in ref.paths)
    ignored_names = {p.rsplit("/", 1)[-1] for p, s in files if s == "ignored"}
    names: list[tuple[int, str]] = []
    for name in sorted(all_names | history_names | ignored_names, key=str.lower):
        rank = distinctive_file_name(name, vocabulary)
        if rank:
            names.append((rank, name))
    names.sort(key=lambda item: (-item[0], item[1].lower()))
    if len(names) > MAX_FILE_NAMES:
        notes.append(f"{len(names) - MAX_FILE_NAMES} file names were left "
                     f"out (limit {MAX_FILE_NAMES}).")
    return terms, [name for _, name in names[:MAX_FILE_NAMES]], notes


def distinctive_file_name(name: str, vocabulary: set[str]) -> int:
    """0 if the name is ordinary; otherwise 2 (holds an uncommon word) or
    1 (three or more common words joined)."""
    lower = name.lower()
    if lower in GENERIC_FILE_NAMES:
        return 0
    stem = name.split(".")[0] if not name.startswith(".") else name[1:].split(".")[0]
    if len(stem) < 6 or _HEX.fullmatch(stem.lower()):
        return 0
    parts = [p for p in split_subwords(stem) if not p.isdigit()]
    if not parts:
        return 0
    if _unknown_parts(stem, vocabulary):
        return 2
    if len(parts) >= 3:
        return 1
    return 0


# --- the term file ---------------------------------------------------------

_HEADER = """\
# Sweep term list
# repo: {repo}
# made: {made}
#
# reviewed: no
#
# READ THIS FILE BEFORE USING IT.
# Sweep will search other folders for every term and file name below.
#   1. Delete any line that is an ordinary word or name. An ordinary word
#      matches thousands of unrelated files and buries the real results.
#   2. Add lines for anything specific to this project that is missing.
#   3. Change the line above to "# reviewed: yes".
# Sweep refuses to search until step 3 is done.
#
# Terms match anywhere, even inside a longer word, and ignore upper and
# lower case. A term must be at least {min_length} characters long.
# Text after two spaces and a '#' is a note and is ignored.
#
# This machine's own identity (login name, computer name and so on) is left
# out on purpose: it appears all over the machine, so it cannot tell this
# repo's traces apart from anything else.
"""


@dataclass
class TermList:
    path: str = ""
    reviewed: bool = False
    terms: list[str] = field(default_factory=list)
    file_names: list[str] = field(default_factory=list)


def render_term_file(repo: str, terms: list[Candidate], file_names: list[str],
                     notes: list[str]) -> str:
    lines = [_HEADER.format(
        repo=repo, made=datetime.now().astimezone().isoformat(timespec="seconds"),
        min_length=MIN_TERM_LENGTH)]
    for note in notes:
        lines.append(f"# note: {note}")
    lines += ["", "[terms]"]
    width = max((len(c.term) for c in terms), default=0)
    for candidate in terms:
        lines.append(f"{candidate.term:<{width}}  # in {candidate.files} files; "
                     f"{candidate.why}")
    lines += ["", "[file-names]"]
    lines += file_names
    lines.append("")
    return "\n".join(lines)


def load_term_file(path: str) -> TermList:
    result = TermList(path=os.path.abspath(path))
    section = ""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            raw_lines = handle.read().splitlines()
    except OSError as error:
        raise TermError(f"could not read the term file: {error}") from error
    for raw in raw_lines:
        line = raw.strip()
        if line.startswith("#"):
            match = re.fullmatch(r"#\s*reviewed\s*:\s*(\w+)", line, re.I)
            if match:
                result.reviewed = match.group(1).lower() == "yes"
            continue
        line = re.split(r"\s{2,}#", line, maxsplit=1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "terms":
            result.terms.append(line)
        elif section == "file-names":
            result.file_names.append(line)
        else:
            raise TermError(f"'{line}' is outside the [terms] and "
                            "[file-names] sections")
    short = [term for term in result.terms if len(term) < MIN_TERM_LENGTH]
    if short:
        raise TermError(
            "these terms are shorter than "
            f"{MIN_TERM_LENGTH} characters and would match too much: "
            + ", ".join(short) + ". Remove or lengthen them.")
    return result


def require_reviewed(term_list: TermList) -> None:
    if not term_list.reviewed:
        raise TermError(
            f"{term_list.path} has not been marked as reviewed. Open it, "
            "remove ordinary words, then change '# reviewed: no' to "
            "'# reviewed: yes'. Sweep does not search with a term list "
            "nobody has read.")


def term_families(terms: list[str]) -> dict[str, str]:
    """Map each term (lowercase) to the shortest term contained in it, so
    'filesrv' and 'filesrv_core' found together count as one piece of
    evidence."""
    lowered = sorted({term.lower() for term in terms}, key=len)
    roots: dict[str, str] = {}
    for term in lowered:
        roots[term] = next((other for other in lowered
                            if other in term), term)
    return roots
