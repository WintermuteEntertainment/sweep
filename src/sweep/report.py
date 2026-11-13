"""Write a scan result as a JSON file and a readable text file.

Reports hold the personal data that was found, so they are never written
inside a git repository, and an existing file is never overwritten.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import gitsrc
from .scan import Finding, ScanResult

WIDTH = 78
SEVERITIES = ("high", "medium", "low")
MAX_LOCATIONS = 5


class ReportError(Exception):
    pass


def default_report_dir() -> Path:
    return Path.home() / "sweep-reports"


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            break
        current = current.parent
    return current


def check_report_dir(directory: Path) -> None:
    """Refuse a report folder that sits inside a git repository."""
    existing = _nearest_existing(directory.resolve())
    if gitsrc.inside_work_tree(str(existing)):
        raise ReportError(
            f"{directory} is inside a git repository. Reports contain the "
            "personal data that was found, so they are not written there. "
            "Choose another folder with --out.")


def _free_stem(directory: Path, stem: str, endings: tuple[str, ...]) -> str:
    candidate, number = stem, 1
    while any((directory / f"{candidate}.{ending}").exists()
              for ending in endings):
        number += 1
        candidate = f"{stem}-{number}"
    return candidate


def write_files(directory: Path, repo: str,
                contents: dict[str, str]) -> dict[str, Path]:
    """Write one new file per entry of `contents` (file ending -> text),
    all sharing one name. Refuses a folder inside a git repository and
    never overwrites: an existing name gets a number added."""
    check_report_dir(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = Path(repo).name or "repo"
    stem = _free_stem(directory, f"{name}-{stamp}", tuple(contents))
    written: dict[str, Path] = {}
    for ending, text in contents.items():
        path = directory / f"{stem}.{ending}"
        # "x" mode fails if the file exists, so nothing is ever overwritten.
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        written[ending] = path
    return written


def _as_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_reports(result: ScanResult, directory: Path) -> tuple[Path, Path]:
    written = write_files(directory, result.repo, {
        "sweep-report.json": _as_json(result.to_dict()),
        "sweep-report.txt": render_text(result),
    })
    return written["sweep-report.json"], written["sweep-report.txt"]


def write_trace_reports(result, directory: Path) -> tuple[Path, Path]:
    from .tracereport import render_trace_text
    written = write_files(directory, result.repo, {
        "sweep-trace.json": _as_json(result.to_dict()),
        "sweep-trace.txt": render_trace_text(result),
    })
    return written["sweep-trace.json"], written["sweep-trace.txt"]


def write_term_file(repo: str, text: str, directory: Path) -> Path:
    return write_files(directory, repo, {"sweep-terms.txt": text})["sweep-terms.txt"]


# --- text layout ------------------------------------------------------------

def _rule(char: str = "=") -> str:
    return "+" + char * WIDTH + "+"


def _row(text: str) -> str:
    return "| " + text[: WIDTH - 2].ljust(WIDTH - 2) + " |"


def _pair(label: str, value: str) -> str:
    return _row(f"{label:<10}| {value}")


def _heading(title: str) -> list[str]:
    return ["", f"[ {title} ]"]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def summarize(result: ScanResult) -> dict[str, tuple[int, int]]:
    """severity -> (number of findings, number of distinct values)."""
    counts: Counter[str] = Counter()
    values: dict[str, set] = defaultdict(set)
    for finding in result.findings:
        if finding.area == "history" and finding.in_head:
            continue  # the same text is counted once, in the working tree
        counts[finding.severity] += 1
        values[finding.severity].add((finding.detector, finding.value.lower()))
    return {sev: (counts[sev], len(values[sev])) for sev in SEVERITIES}


def _location(finding: Finding) -> str:
    if finding.area == "file-name":
        where = finding.path
        if finding.status == "history":
            where += f"  (history, commit {finding.commits[0][:10]})" if finding.commits else "  (history)"
        else:
            where += f"  ({finding.status})"
        return where
    if finding.area == "history":
        commit = finding.commits[0][:10] if finding.commits else "unknown"
        return f"{finding.path}:{finding.line}  (commit {commit})"
    if finding.area == "working-tree":
        return f"{finding.path}:{finding.line}  ({finding.status})"
    return finding.path


def _group_lines(number: int, group: list[Finding]) -> list[str]:
    first = group[0]
    lines = [f"  [{number}] {first.detector}: {first.value}"]
    if first.note:
        lines.append(f"      why: {first.note}")
    by_area: dict[str, list[Finding]] = defaultdict(list)
    for finding in group:
        by_area[finding.area].append(finding)

    def section(title: str, items: list[Finding]) -> None:
        if not items:
            return
        files = len({item.path for item in items})
        lines.append(f"      {title}: {_plural(len(items), 'occurrence')} "
                     f"in {_plural(files, 'place')}")
        # A value that appears twice on one line is listed once here.
        shown: list[Finding] = []
        seen_lines: set[tuple[str, int, str]] = set()
        for item in items:
            key = (item.path, item.line, item.blob)
            if key not in seen_lines:
                seen_lines.add(key)
                shown.append(item)
        for item in shown[:MAX_LOCATIONS]:
            lines.append(f"        {_location(item)}")
            if item.snippet:
                lines.append(f"            {item.snippet}")
        if len(shown) > MAX_LOCATIONS:
            lines.append(f"        ... and {len(shown) - MAX_LOCATIONS} more lines "
                         "(all are in the JSON report)")

    section("working tree", by_area["working-tree"])
    history = by_area["history"]
    gone = [item for item in history if not item.in_head]
    if history:
        lines.append(f"      history: found in {_plural(len({i.blob for i in history}), 'file version')}, "
                     f"{len({i.blob for i in gone})} of them no longer in the current commit")
    section("history only (stays in the repo until history is rewritten)", gone)
    section("file names", by_area["file-name"])
    section("commit messages", by_area["commit-message"])
    section("remotes", by_area["remote"])
    return lines


def render_text(result: ScanResult) -> str:
    out: list[str] = [
        _rule(),
        _row("SWEEP REPORT    Part A: inside the repo"),
        _rule(),
        _pair("Repo", result.repo),
        _pair("Scanned", f"{result.started}  (took {result.seconds} s)"),
        _pair("Version", f"sweep {result.version}"),
        _rule("-"),
        _row("This file contains the personal data that was found."),
        _row("Keep it out of git repositories and shared folders."),
        _rule(),
    ]

    out += _heading("SUMMARY")
    summary = summarize(result)
    for severity in SEVERITIES:
        count, distinct = summary[severity]
        out.append(f"  {severity:<7}| {_plural(count, 'finding'):<14}| "
                   f"{_plural(distinct, 'distinct value')}")
    if not result.findings:
        out.append("  Nothing was found in the places searched. That is not")
        out.append("  proof that nothing is there: see what was not searched.")

    out += _heading("WHAT WAS SEARCHED")
    tree = result.searched.get("working_tree", {})
    out.append(f"  Working tree     | tracked {tree.get('tracked', 0)}, "
               f"untracked {tree.get('untracked', 0)}, "
               f"ignored {tree.get('ignored', 0)} files")
    if "history" in result.searched:
        history = result.searched["history"]
        out.append(f"  History          | {_plural(history['commits'], 'commit')}, "
                   f"{_plural(history['file_versions'], 'file version')} "
                   f"({history['file_versions_read']} read as text)")
        out.append(f"  Commit messages  | {result.searched.get('commit_messages', 0)}")
    out.append(f"  Remotes          | {result.searched.get('remotes', 0)}")
    if result.terms_file:
        out.append(f"  Private terms    | {result.terms_count} from {result.terms_file}")
    else:
        out.append("  Private terms    | none given")

    out += _heading("WHAT WAS NOT SEARCHED")
    for note in result.not_checked:
        out.append(f"  - {note}")
    reasons: dict[str, list[str]] = defaultdict(list)
    for skipped in result.skipped:
        reasons[skipped.reason].append(f"{skipped.path} ({skipped.area})")
    for reason, paths in sorted(reasons.items()):
        out.append(f"  - Skipped, {reason}: {len(paths)}")
        for path in paths[:MAX_LOCATIONS]:
            out.append(f"      {path}")
        if len(paths) > MAX_LOCATIONS:
            out.append(f"      ... and {len(paths) - MAX_LOCATIONS} more "
                       "(all are in the JSON report)")

    out += _heading("THIS MACHINE'S IDENTITY (searched for as exact values)")
    for value in result.identity:
        out.append(f"  {value['kind']:<17}| {value['value']}")
    for note in result.identity_notes:
        out.append(f"  note: {note}")

    out += _heading("COMMIT IDENTITIES (published with the repo when it is pushed)")
    if not result.commit_identities:
        out.append("  none")
    for person in result.commit_identities:
        flag = "  [matches this machine]" if person["matches_machine"] else ""
        out.append(f"  {person['name']} <{person['email']}>{flag}")
        out.append(f"      author of {person['authored']}, "
                   f"committer of {person['committed']}")

    out += _heading("REMOTES")
    if not result.remotes:
        out.append("  none")
    for remote in result.remotes:
        out.append(f"  {remote['name']:<17}| {remote['url']}")

    out += _heading("FINDINGS BY VALUE")
    groups: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in result.findings:
        groups[(finding.detector, finding.value.lower())].append(finding)
    if not groups:
        out.append("  none")
    number = 0
    for severity in SEVERITIES:
        chosen = [group for group in groups.values() if group[0].severity == severity]
        if not chosen:
            continue
        out.append("")
        out.append(f"  == {severity} " + "=" * (WIDTH - 6 - len(severity)))
        chosen.sort(key=lambda group: (-len(group), group[0].detector,
                                       group[0].value.lower()))
        for group in chosen:
            number += 1
            out.append("")
            out += _group_lines(number, group)
    out.append("")
    return "\n".join(out)
