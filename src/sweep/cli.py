"""Command line for Sweep."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, gitsrc
from .detectors import load_terms
from .places import fixed_drives, known_places
from .report import (ReportError, SEVERITIES, check_report_dir,
                     default_report_dir, summarize, write_reports,
                     write_term_file, write_trace_reports)
from .scan import DEFAULT_MAX_SIZE, scan_repo
from .terms import (DEFAULT_MAX_TERMS, TermError, build_candidates,
                    load_term_file, render_term_file, require_reviewed)
from .trace import MEDIUM, STRONG, WEAK, trace_repo
from .tracereport import summarize_trace

_MB = 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sweep",
        description="Finds personal data in a git repo, and traces of the "
                    "repo elsewhere. Reports only: it never deletes, moves "
                    "or changes anything.")
    parser.add_argument("--version", action="version",
                        version=f"sweep {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("path", help="a folder inside the repository")
        command.add_argument("--out", metavar="DIR",
                             help="where to write the output "
                                  "(default: sweep-reports in your home folder)")

    repo = commands.add_parser(
        "repo", help="Part A: scan one git repository and write a report")
    common(repo)
    repo.add_argument("--terms", metavar="FILE",
                      help="private term file: one term per line, kept "
                           "outside every repository")
    repo.add_argument("--no-history", action="store_true",
                      help="skip git history and commit messages")
    repo.add_argument("--max-size-mb", type=float,
                      default=DEFAULT_MAX_SIZE / _MB, metavar="N",
                      help="skip files larger than this (default: 5)")

    terms = commands.add_parser(
        "terms", help="Part B, step 1: propose a term list for you to review")
    common(terms)
    terms.add_argument("--max-terms", type=int, default=DEFAULT_MAX_TERMS,
                       metavar="N", help="how many terms to propose "
                                         f"(default: {DEFAULT_MAX_TERMS})")

    trace = commands.add_parser(
        "trace", help="Part B, step 2: search other folders for traces of "
                      "the repo")
    common(trace)
    trace.add_argument("--terms", metavar="FILE",
                       help="the term list from 'sweep terms', after you "
                            "have reviewed it")
    trace.add_argument("--no-terms", action="store_true",
                       help="look for exact copies only, with no term list")
    trace.add_argument("--scope", metavar="FOLDER", action="append", default=[],
                       help="a folder to search (may be given more than once)")
    trace.add_argument("--drive", metavar="LETTER", action="append", default=[],
                       help="a whole drive to search, for example D")
    trace.add_argument("--machine", action="store_true",
                       help="search every local fixed drive")
    trace.add_argument("--known-places", action="store_true",
                       help="search the places leftovers usually collect: "
                            "temp, Downloads, Desktop, Recycle Bin, editor "
                            "history, AI assistant folders")
    trace.add_argument("--include-dependency-folders", action="store_true",
                       help="also search node_modules, .venv and similar")
    trace.add_argument("--max-size-mb", type=float,
                       default=DEFAULT_MAX_SIZE / _MB, metavar="N",
                       help="do not search files larger than this for terms "
                            "(default: 5). Exact copies are found at any size.")
    web = commands.add_parser(
        "web", help="open the Sweep page in your browser (this machine only, "
                    "unless --lan)")
    web.add_argument("--port", type=int, default=8765,
                     help="port to listen on (default: 8765)")
    web.add_argument("--lan", action="store_true",
                     help="also accept connections from the local network; "
                          "asks for a password")
    web.add_argument("--no-browser", action="store_true",
                     help="do not open a browser window")
    return parser


def _cmd_web(args: argparse.Namespace) -> int:
    import getpass
    import webbrowser

    from .web import make_server
    password = None
    if args.lan:
        password = getpass.getpass("Password for LAN access (8+ characters): ")
        if getpass.getpass("Same password again: ") != password:
            print("sweep: the passwords did not match", file=sys.stderr)
            return 2
    try:
        server, _token, urls = make_server(args.port, lan=args.lan, password=password)
    except (ValueError, OSError) as error:
        print(f"sweep: {error}", file=sys.stderr)
        return 2
    print("Sweep is running. Open:")
    for url in urls:
        print(f"  {url}")
    if args.lan:
        print("LAN mode: anyone on your network who has the password can use")
        print("it. Traffic is not encrypted, so use it only on a network you trust.")
    print("Press Ctrl+C here to stop it.")
    if not args.no_browser:
        webbrowser.open(urls[0])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def _say(message: str) -> None:
    print(f"  {message} ...", file=sys.stderr)


def _box(title: str, rows: list[str]) -> None:
    print("+" + "=" * 60 + "+")
    print(f"| {title:<58} |")
    print("+" + "=" * 60 + "+")
    for row in rows:
        print(f"| {row:<58} |")
    print("+" + "-" * 60 + "+")


def _cmd_repo(args: argparse.Namespace, out_dir: Path) -> int:
    terms = load_terms(args.terms) if args.terms else []
    result = scan_repo(
        args.path, terms=terms, terms_file=args.terms or "",
        history=not args.no_history,
        max_size=int(args.max_size_mb * _MB), progress=_say)
    json_path, text_path = write_reports(result, out_dir)
    summary = summarize(result)
    _box("SWEEP  Part A: " + Path(result.repo).name, [
        f"{severity:<7}| {summary[severity][0]} findings, "
        f"{summary[severity][1]} distinct values"
        for severity in SEVERITIES])
    print(f"Took {result.seconds} s. Nothing was changed.")
    print(f"Read this:  {text_path}")
    print(f"Full data:  {json_path}")
    return 0


def _cmd_terms(args: argparse.Namespace, out_dir: Path) -> int:
    repo = os.path.normpath(gitsrc.toplevel(args.path))
    _say("Reading the repo and building the list of common words")
    candidates, file_names, notes = build_candidates(repo, max_terms=args.max_terms)
    path = write_term_file(
        repo, render_term_file(repo, candidates, file_names, notes), out_dir)
    _box("SWEEP  Part B, step 1: " + Path(repo).name, [
        f"{len(candidates)} terms proposed",
        f"{len(file_names)} file names proposed",
    ])
    print(f"Term list:  {path}")
    print("Next: open that file, delete ordinary words, add anything missing,")
    print("change '# reviewed: no' to '# reviewed: yes', then run:")
    print(f'  sweep trace "{repo}" --terms "{path}" --known-places')
    return 0


def _drive_root(text: str) -> str:
    letter = text.strip().rstrip("\\/").rstrip(":")
    if len(letter) == 1 and letter.isalpha():
        return f"{letter.upper()}:\\"
    return text


def _cmd_trace(args: argparse.Namespace, out_dir: Path) -> int:
    if args.terms and args.no_terms:
        raise TermError("give --terms FILE or --no-terms, not both")
    if not args.terms and not args.no_terms:
        raise TermError(
            "give --terms FILE (the reviewed list from 'sweep terms'), or "
            "--no-terms to look for exact copies only")
    scopes: list[tuple[str, str]] = [("folder", p) for p in args.scope]
    scopes += [("drive", _drive_root(d)) for d in args.drive]
    if args.machine:
        scopes += [("drive", d) for d in fixed_drives()]
    if args.known_places:
        scopes += known_places()
    if not scopes:
        raise TermError(
            "nothing is searched by default. Say where to look with --scope "
            "FOLDER, --drive LETTER, --known-places or --machine")
    term_list = None
    if args.terms:
        term_list = load_term_file(args.terms)
        require_reviewed(term_list)
    result = trace_repo(
        args.path, term_list, scopes, exclude=[str(out_dir)],
        max_size=int(args.max_size_mb * _MB),
        include_dependency_folders=args.include_dependency_folders,
        progress=_say)
    json_path, text_path = write_trace_reports(result, out_dir)
    summary = summarize_trace(result)
    rows = [f"{strength:<7}| {summary[strength][0]} files in "
            f"{summary[strength][1]} folders"
            for strength in (STRONG, MEDIUM, WEAK)]
    rows.append(f"copies | {len(result.copies)} other folders share its history")
    _box("SWEEP  Part B: " + Path(result.repo).name, rows)
    searched = result.searched
    print(f"Took {result.seconds} s: {searched['files_seen']:,} files seen, "
          f"{searched['files_read']:,} opened. Nothing was changed.")
    print(f"Read this:  {text_path}")
    print(f"Full data:  {json_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = _parser().parse_args(argv)
    if args.command == "web":
        return _cmd_web(args)
    out_dir = Path(args.out) if args.out else default_report_dir()
    handlers = {"repo": _cmd_repo, "terms": _cmd_terms, "trace": _cmd_trace}
    try:
        # Checked first, so a long scan is not wasted on a refused folder.
        check_report_dir(out_dir)
        return handlers[args.command](args, out_dir)
    except (gitsrc.GitError, ReportError, TermError, OSError) as error:
        print(f"sweep: {error}", file=sys.stderr)
        return 2
