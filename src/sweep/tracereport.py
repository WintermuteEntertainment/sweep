"""Readable text report for Part B."""
from __future__ import annotations

from collections import defaultdict

from .report import MAX_LOCATIONS, _heading, _pair, _plural, _row, _rule
from .trace import MEDIUM, STRONG, WEAK, TraceHit, TraceResult

FOLDER_LIMIT = 30
FILES_PER_FOLDER = 6
NOISY_TERM = 100

_MEANING = {
    STRONG: "an exact copy, a matching file name with a term, or three or "
            "more separate terms",
    MEDIUM: "a matching file name, two separate terms, or the repo's own name",
    WEAK: "one term, or a copy of a file that many projects share",
}


def summarize_trace(result: TraceResult) -> dict[str, tuple[int, int]]:
    """strength -> (files, folders)."""
    files: dict[str, int] = defaultdict(int)
    folders: dict[str, set] = defaultdict(set)
    for hit in result.hits:
        files[hit.strength] += 1
        folders[hit.strength].add(hit.folder)
    return {s: (files[s], len(folders[s])) for s in (STRONG, MEDIUM, WEAK)}


def _hit_lines(hit: TraceHit) -> list[str]:
    name = hit.path[len(hit.folder):].lstrip("\\/")
    lines = [f"        {name}   ({hit.size:,} bytes)"]
    if hit.exact_copy_of:
        note = "  (weak: many projects hold this file)" if hit.weak_copy else ""
        lines.append("            exact copy of: "
                     + ", ".join(hit.exact_copy_of[:3]) + note)
    if hit.name_match:
        lines.append("            same file name as one in the repo")
    if hit.name_terms:
        lines.append("            file name contains: " + ", ".join(hit.name_terms))
    if hit.terms:
        found = ", ".join(f"{term} ({count})" for term, count in
                          sorted(hit.terms.items(), key=lambda kv: -kv[1]))
        lines.append(f"            terms: {found}")
        if hit.snippet:
            lines.append(f"            line {hit.line}: {hit.snippet}")
    return lines


def render_trace_text(result: TraceResult) -> str:
    out: list[str] = [
        _rule(),
        _row("SWEEP REPORT    Part B: outside the repo"),
        _rule(),
        _pair("Repo", result.repo),
        _pair("Scanned", f"{result.started}  (took {result.seconds} s)"),
        _pair("Version", f"sweep {result.version}"),
        _rule("-"),
        _row("Nothing was changed. This report lists where traces of the"),
        _row("repo were found. It may quote personal data: keep it private."),
        _rule(),
    ]

    out += _heading("SUMMARY")
    summary = summarize_trace(result)
    for strength in (STRONG, MEDIUM, WEAK):
        files, folders = summary[strength]
        out.append(f"  {strength:<7}| {_plural(files, 'file'):<12}| "
                   f"in {_plural(folders, 'folder'):<12}| {_MEANING[strength]}")
    out.append(f"  copies | {_plural(len(result.copies), 'other folder')} "
               "sharing this repo's git history")
    if result.hits_dropped:
        out.append(f"  NOTE: {result.hits_dropped} further weak hits were not "
                   "recorded (limit reached). Remove noisy terms and run again.")
    if not result.hits and not result.copies:
        out.append("  Nothing was found in the places searched. That is not")
        out.append("  proof that nothing is there: see what was not searched.")

    out += _heading("WHERE IT LOOKED")
    for scope in result.scopes:
        out.append(f"  {scope['label']:<20}| {scope['path']}")
        if scope.get("resolved_to"):
            out.append(f"  {'':<20}|   which is really {scope['resolved_to']}")
        if scope["status"] != "searched":
            out.append(f"  {'':<20}|   NOT SEARCHED: {scope['status']}")
    searched = result.searched
    out.append(f"  Totals              | {searched['folders']:,} folders, "
               f"{searched['files_seen']:,} files seen, "
               f"{searched['files_read']:,} opened "
               f"({searched['megabytes_read']:,} MB read)")
    out.append(f"  Speed               | {searched['files_per_second']:,} files "
               "per second")

    out += _heading("WHAT WAS LOOKED FOR")
    fingerprint = result.fingerprint
    out.append(f"  Exact copies        | of {fingerprint['file_versions']:,} file "
               "versions from the repo's files and history")
    out.append(f"  Too small to match  | {fingerprint['too_small_to_fingerprint']:,} "
               "tiny files were left out of the fingerprint")
    if result.terms_file:
        out.append(f"  Term list           | {result.terms_file}")
        out.append(f"                      | {_plural(len(result.terms), 'term')}, "
                   f"{_plural(result.file_names, 'file name')}")
    else:
        out.append("  Term list           | none: exact copies only")

    out += _heading("WHAT WAS NOT SEARCHED")
    for note in result.cannot_see:
        out.append(f"  - {note}")
    for reason, entry in sorted(result.skipped.items()):
        out.append(f"  - Skipped, {reason}: {entry['count']:,}")
        for path in entry["examples"][:MAX_LOCATIONS]:
            out.append(f"      {path}")
        if entry["count"] > MAX_LOCATIONS:
            out.append(f"      ... and {entry['count'] - MAX_LOCATIONS:,} more")

    out += _heading("OTHER COPIES OF THIS REPO (same git history)")
    if not result.copies:
        out.append("  none found")
    for path in result.copies:
        out.append(f"  {path}")
    if result.copies:
        out.append("  These were not searched file by file. Run 'sweep repo' "
                   "on each one.")

    if result.terms:
        out += _heading("TERMS: HOW MANY FILES EACH ONE HIT")
        width = max(len(term) for term in result.terms) + 2
        for term, count in sorted(result.term_files.items(),
                                  key=lambda kv: (-kv[1], kv[0].lower())):
            flag = ("   <- hits a lot of files; consider removing it"
                    if count >= NOISY_TERM else "")
            out.append(f"  {term:<{width}}| {count:,}{flag}")

    out += _heading("HITS BY FOLDER")
    if not result.hits:
        out.append("  none")
    number = 0
    for strength in (STRONG, MEDIUM, WEAK):
        folders: dict[str, list[TraceHit]] = defaultdict(list)
        for hit in result.hits:
            if hit.strength == strength:
                folders[hit.folder].append(hit)
        if not folders:
            continue
        out.append("")
        out.append(f"  == {strength} " + "=" * (72 - len(strength)))
        ranked = sorted(folders.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
        for folder, hits in ranked[:FOLDER_LIMIT]:
            number += 1
            out.append("")
            out.append(f"  [{number}] {folder}   ({_plural(len(hits), 'file')})")
            for hit in hits[:FILES_PER_FOLDER]:
                out += _hit_lines(hit)
            if len(hits) > FILES_PER_FOLDER:
                out.append(f"        ... and {len(hits) - FILES_PER_FOLDER} "
                           "more files here (all are in the JSON report)")
        if len(ranked) > FOLDER_LIMIT:
            out.append("")
            out.append(f"  ... and {len(ranked) - FOLDER_LIMIT} more {strength} "
                       "folders (all are in the JSON report)")
    out.append("")
    return "\n".join(out)
