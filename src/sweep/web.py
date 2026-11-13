"""The local web interface: one page, served by Python's own HTTP server.

Listens on 127.0.0.1 only, unless started with --lan, which needs a
password. The page talks to a small JSON API. Long jobs (scan, proposing
search words, counting, searching) run one at a time in a background
thread; the page asks for their progress while one is running.

Protections, because this server can read any folder and delete files:
  - The Host header must name this server, which blocks DNS rebinding.
  - Every API call must carry a random token that only the served page
    knows, so another web site open in the browser cannot call the API.
  - In LAN mode, every request needs a session cookie from logging in.
  - The page never sends paths to delete: it sends the ids of files the
    server listed itself, and the server checks each file again.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import __version__, delete, gitsrc
from .places import fixed_drives, known_places, norm
from .report import (default_report_dir, render_text, write_reports,
                     write_trace_reports)
from .scan import decode, is_cloud_only, is_link, scan_repo
from .terms import MIN_TERM_LENGTH, TermList, build_candidates
from .trace import trace_repo
from .tracereport import render_trace_text

STATIC = Path(__file__).with_name("static")
# Measured on the build machine, 2026-11-12: about 30 ms to open a file the
# first time. Repeat runs are much faster. Used only for a rough estimate.
FIRST_OPEN_SECONDS = 0.030
# The viewer shows at most this much of a file.
VIEW_LIMIT = 512 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Job:
    def __init__(self, kind: str):
        self.kind = kind
        self.state = "running"      # running, done, failed, stopped
        self.message = "starting"
        self.error = ""
        self.started = time.monotonic()
        self.stop = threading.Event()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "state": self.state, "message": self.message,
                "error": self.error,
                "seconds": round(time.monotonic() - self.started, 1)}


class Session:
    """Everything the page has done so far. Kept in memory only."""

    def __init__(self, report_dir: Path):
        self.lock = threading.Lock()
        self.report_dir = report_dir
        self.job: Job | None = None
        self.log: list[dict] = []
        self.reset("")

    def reset(self, repo: str) -> None:
        self.repo = repo
        self.scan = None
        self.scan_files: dict[str, dict] = {}
        self.candidates: list[dict] = []
        self.file_names: list[str] = []
        self.word_notes: list[str] = []
        self.term_list: TermList | None = None
        self.estimate: dict | None = None
        self.trace = None
        self.hit_files: dict[str, dict] = {}


# --- helpers ---------------------------------------------------------------

def _target(entry: dict) -> delete.Target:
    return delete.Target(entry["path"], entry["size"], entry["mtime_ns"], entry["kind"])


def _snapshot(path: str) -> tuple[int, int] | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def browse(path: str) -> dict:
    """Subfolders of `path` (or the drives), for the folder picker. Links,
    junctions and cloud-only folders are shown but cannot be opened."""
    if not path:
        return {"path": "", "parent": "", "folders": [
            {"name": d, "path": d, "openable": True} for d in fixed_drives()]}
    path = os.path.abspath(path)
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError as error:
        raise ApiError(f"cannot open {path}: {error.strerror}")
    folders = []
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not entry.is_dir(follow_symlinks=False) and not is_link(info):
            continue
        blocked = ("link or junction" if is_link(info)
                   else "cloud only" if is_cloud_only(info) else "")
        folders.append({"name": entry.name, "path": entry.path,
                        "openable": not blocked, "why": blocked,
                        "is_repo": os.path.lexists(os.path.join(entry.path, ".git"))})
    parent = os.path.dirname(path)
    return {"path": path, "parent": "" if parent == path else parent,
            "is_repo": os.path.lexists(os.path.join(path, ".git")),
            "folders": folders}


def history_list(report_dir: Path) -> list[dict]:
    groups: dict[str, dict] = {}
    if not report_dir.is_dir():
        return []
    for entry in os.scandir(report_dir):
        if not entry.is_file(follow_symlinks=False):
            continue
        if not delete.REPORT_NAME.match(entry.name):
            continue
        stem = entry.name.split(".sweep-")[0]
        info = entry.stat(follow_symlinks=False)
        group = groups.setdefault(stem, {"name": stem, "files": [], "bytes": 0,
                                         "modified": 0})
        group["files"].append({"path": entry.path, "name": entry.name,
                               "size": info.st_size, "mtime_ns": info.st_mtime_ns})
        group["bytes"] += info.st_size
        group["modified"] = max(group["modified"], int(info.st_mtime))
    return sorted(groups.values(), key=lambda g: -g["modified"])


# --- the API ---------------------------------------------------------------

class Api:
    def __init__(self, session: Session):
        self.s = session

    # jobs

    def _start(self, kind: str, work) -> dict:
        with self.s.lock:
            if self.s.job and self.s.job.state == "running":
                raise ApiError("another job is still running", 409)
            job = Job(kind)
            self.s.job = job

        def run() -> None:
            try:
                work(job)
                job.state = "stopped" if job.stop.is_set() else "done"
                job.message = "stopped" if job.stop.is_set() else "done"
            except Exception as error:  # reported to the page, not raised
                job.state = "failed"
                job.error = str(error) or error.__class__.__name__
        threading.Thread(target=run, daemon=True).start()
        return job.as_dict()

    def _need_repo(self) -> str:
        if not self.s.repo:
            raise ApiError("choose a repo first")
        return self.s.repo

    def state(self, _body: dict) -> dict:
        s = self.s
        return {
            "version": __version__,
            "repo": s.repo,
            "job": s.job.as_dict() if s.job else None,
            "scanned": s.scan is not None,
            "words_proposed": bool(s.candidates),
            "reviewed": s.term_list is not None,
            "searched": s.trace is not None,
            "recycle_supported": delete.recycle_supported(),
            "report_dir": str(s.report_dir),
            "log": s.log[-200:],
        }

    def stop(self, _body: dict) -> dict:
        if self.s.job and self.s.job.state == "running":
            self.s.job.stop.set()
            self.s.job.message = "stopping"
        return {"ok": True}

    def browse(self, body: dict) -> dict:
        return browse(str(body.get("path") or ""))

    def set_repo(self, body: dict) -> dict:
        path = str(body.get("path") or "").strip()
        if not path:
            raise ApiError("give a folder")
        try:
            top = os.path.normpath(gitsrc.toplevel(path))
        except gitsrc.GitError:
            raise ApiError(f"{path} is not inside a git repo")
        with self.s.lock:
            if self.s.job and self.s.job.state == "running":
                raise ApiError("wait for the running job to finish", 409)
            self.s.reset(top)
        commits = 0
        if gitsrc.has_commits(top):
            commits = int(gitsrc.git(top, "rev-list", "--count", "--all").strip() or 0)
        return {"repo": top, "commits": commits,
                "remotes": [{"name": n, "url": u} for n, u in gitsrc.remotes(top)]}

    # section 1

    def scan(self, _body: dict) -> dict:
        repo = self._need_repo()

        def work(job: Job) -> None:
            job.message = "scanning the repo"
            result = scan_repo(repo, progress=lambda m: setattr(job, "message", m))
            files: dict[str, dict] = {}
            for finding in result.findings:
                if finding.area not in ("working-tree", "file-name"):
                    continue
                if finding.status not in ("tracked", "untracked", "ignored"):
                    continue
                full = os.path.normpath(os.path.join(repo, finding.path))
                if full in {f["path"] for f in files.values()}:
                    continue
                snap = _snapshot(full)
                if not snap:
                    continue
                file_id = f"s{len(files)}"
                files[file_id] = {"id": file_id, "path": full, "relative": finding.path,
                                  "size": snap[0], "mtime_ns": snap[1],
                                  "kind": f"repo-{finding.status}",
                                  "status": finding.status}
            self.s.scan, self.s.scan_files = result, files
        return self._start("scan", work)

    def scan_result(self, _body: dict) -> dict:
        if not self.s.scan:
            raise ApiError("run section 1 first")
        data = self.s.scan.to_dict()
        by_path = {f["relative"]: f["id"] for f in self.s.scan_files.values()}
        for finding in data["findings"]:
            if finding["area"] in ("working-tree", "file-name"):
                finding["file_id"] = by_path.get(finding["path"], "")
        data["files"] = list(self.s.scan_files.values())
        return data

    # section 2

    def propose_words(self, _body: dict) -> dict:
        repo = self._need_repo()

        def work(job: Job) -> None:
            job.message = "reading the repo and building the list of common words"
            terms, names, notes = build_candidates(repo)
            self.s.candidates = [asdict(t) for t in terms]
            self.s.file_names, self.s.word_notes = names, notes
            self.s.term_list = None
        return self._start("words", work)

    def words(self, _body: dict) -> dict:
        return {"words": self.s.candidates, "file_names": self.s.file_names,
                "notes": self.s.word_notes,
                "reviewed": self.s.term_list is not None,
                "kept": self.s.term_list.terms if self.s.term_list else [],
                "kept_names": self.s.term_list.file_names if self.s.term_list else []}

    def review_words(self, body: dict) -> dict:
        self._need_repo()
        words = [str(w).strip() for w in body.get("words", []) if str(w).strip()]
        names = [str(n).strip() for n in body.get("file_names", []) if str(n).strip()]
        short = [w for w in words if len(w) < MIN_TERM_LENGTH]
        if short:
            raise ApiError(f"search words must be at least {MIN_TERM_LENGTH} "
                           "characters: " + ", ".join(short))
        if not words and not names:
            raise ApiError("keep at least one search word or file name")
        self.s.term_list = TermList(path="", reviewed=True,
                                    terms=list(dict.fromkeys(words)),
                                    file_names=list(dict.fromkeys(names)))
        self.s.trace, self.s.hit_files = None, {}
        return {"reviewed": True, "words": len(self.s.term_list.terms),
                "file_names": len(self.s.term_list.file_names)}

    # section 3

    def places(self, _body: dict) -> dict:
        found = [{"label": label, "path": path,
                  "checked": label != "Recycle Bin",
                  "recycle_bin": label == "Recycle Bin"}
                 for label, path in known_places()]
        return {"places": found, "drives": fixed_drives()}

    def _scopes(self, body: dict) -> list[tuple[str, str]]:
        scopes = [(str(s.get("label") or "folder"), str(s.get("path")))
                  for s in body.get("scopes", []) if s.get("path")]
        if not scopes:
            raise ApiError("choose at least one place to look")
        return scopes

    def _require_review(self) -> TermList:
        if self.s.term_list is None:
            raise ApiError("review the search words first (section 2)")
        return self.s.term_list

    def estimate(self, body: dict) -> dict:
        repo, terms = self._need_repo(), self._require_review()
        scopes = self._scopes(body)

        def work(job: Job) -> None:
            job.message = "counting files (nothing is opened)"
            result = trace_repo(repo, terms, scopes, count_only=True,
                                exclude=[str(self.s.report_dir)],
                                should_stop=job.stop.is_set,
                                progress=lambda m: setattr(job, "message", m))
            to_open = result.searched["files_to_open"]
            self.s.estimate = {
                "files": result.searched["files_seen"],
                "folders": result.searched["folders"],
                "files_to_open": to_open,
                "first_run_minutes": round(to_open * FIRST_OPEN_SECONDS / 60, 1),
                "count_seconds": result.seconds,
                "scopes": result.scopes, "stopped": result.stopped,
            }
        return self._start("estimate", work)

    def estimate_result(self, _body: dict) -> dict:
        return {"estimate": self.s.estimate}

    def search(self, body: dict) -> dict:
        repo, terms = self._need_repo(), self._require_review()
        scopes = self._scopes(body)

        def work(job: Job) -> None:
            result = trace_repo(repo, terms, scopes,
                                exclude=[str(self.s.report_dir)],
                                should_stop=job.stop.is_set,
                                progress=lambda m: setattr(job, "message", m))
            files: dict[str, dict] = {}
            owners: dict[str, str | None] = {}
            for index, hit in enumerate(result.hits):
                folder = hit.folder
                if folder not in owners:
                    owners[folder] = delete.enclosing_repo(hit.path)
                owner = owners[folder]
                file_id = f"h{index}"
                files[file_id] = {
                    "id": file_id, "path": hit.path, "size": hit.size,
                    "mtime_ns": hit.mtime_ns, "kind": delete.LEFTOVER,
                    "deletable": owner is None,
                    "steps": delete.manual_steps(hit.path, owner) if owner else [],
                }
            self.s.trace, self.s.hit_files = result, files
        return self._start("search", work)

    def search_result(self, _body: dict) -> dict:
        if not self.s.trace:
            raise ApiError("run a search first")
        data = self.s.trace.to_dict()
        for index, hit in enumerate(data["hits"]):
            info = self.s.hit_files.get(f"h{index}")
            if info is None:  # deleted earlier in this session
                hit.update({"id": "", "deletable": False, "steps": [], "gone": True})
            else:
                hit.update({"id": info["id"], "deletable": info["deletable"],
                            "steps": info["steps"], "gone": False})
        return data

    # deleting

    def delete(self, body: dict) -> dict:
        source = body.get("source")
        ids = [str(i) for i in body.get("ids", [])]
        mode = body.get("mode")
        pool = {"scan": self.s.scan_files, "search": self.s.hit_files}.get(source)
        report_dir = ""
        if source == "history":
            report_dir = str(self.s.report_dir)
            pool = {}
            for group in history_list(self.s.report_dir):
                for item in group["files"]:
                    pool[item["path"]] = {**item, "kind": delete.REPORT}
        if pool is None:
            raise ApiError("unknown source")
        if source == "search" and self.s.term_list is None:
            raise ApiError("review the search words first")
        unknown = [i for i in ids if i not in pool]
        if unknown:
            raise ApiError("unknown file ids; refresh the page")
        chosen = [pool[i] for i in ids]
        for entry in chosen:
            if source == "search" and not entry.get("deletable", False):
                raise ApiError("a chosen file is inside another repo")
        danger = {norm(pool[i]["path"]) for i in body.get("danger_ok", [])
                  if i in pool}
        confirm = body.get("confirm")
        try:
            outcomes = delete.perform(
                [_target(e) for e in chosen], self.s.repo, mode,
                confirm_count=int(confirm) if confirm not in (None, "") else None,
                danger_ok=danger, report_dir=report_dir)
        except (delete.DeleteError, ValueError) as error:
            raise ApiError(str(error))
        stamp = time.strftime("%H:%M:%S")
        for outcome in outcomes:
            self.s.log.append({"time": stamp, **asdict(outcome)})
            if outcome.done:
                for key in [k for k, v in pool.items() if v["path"] == outcome.path]:
                    pool.pop(key, None)
        return {"outcomes": [asdict(o) for o in outcomes]}

    # viewing a result

    def _locate(self, body: dict) -> tuple[str, str]:
        """(path on disk, git blob id); exactly one is set. Only files the
        server itself listed can be named, by id, never by path."""
        source, key = body.get("source"), str(body.get("id") or "")
        if source == "scan" and key in self.s.scan_files:
            return self.s.scan_files[key]["path"], ""
        if source == "search" and key in self.s.hit_files:
            return self.s.hit_files[key]["path"], ""
        if source == "blob" and self.s.scan and any(
                f.blob == key for f in self.s.scan.findings):
            return "", key
        raise ApiError("unknown file; refresh the page")

    def view(self, body: dict) -> dict:
        path, blob = self._locate(body)
        limit = VIEW_LIMIT
        if blob:
            data = next((d for _, d in gitsrc.read_blobs(self.s.repo, [blob])), b"")
            title = f"Old version from git history (blob {blob[:10]})"
        else:
            try:
                info = os.lstat(path)
            except OSError as error:
                raise ApiError(f"cannot open it: {error.strerror}")
            if is_link(info):
                raise ApiError("it is a link or junction; not opened")
            if is_cloud_only(info):
                raise ApiError("it exists only in the cloud; opening it would download it")
            with open(path, "rb") as handle:
                data = handle.read(limit + 1)
            title = path
        text = decode(data[:limit])
        if text is None:
            raise ApiError("it is a binary file; use Show in folder instead")
        return {"title": title, "text": text, "truncated": len(data) > limit}

    def reveal(self, body: dict) -> dict:
        path, blob = self._locate(body)
        if blob:
            raise ApiError("an old version from history has no file on disk")
        if os.name != "nt":
            raise ApiError("Show in folder works on Windows only for now")
        if not os.path.lexists(path):
            raise ApiError("it is no longer there")
        # Opens File Explorer with the file selected. The file itself is
        # never opened or run.
        subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        return {"ok": True}

    # history

    def save(self, body: dict) -> dict:
        written = []
        if body.get("scan") and self.s.scan:
            written += [str(p) for p in write_reports(self.s.scan, self.s.report_dir)]
        if body.get("search") and self.s.trace:
            written += [str(p) for p in write_trace_reports(self.s.trace,
                                                            self.s.report_dir)]
        if not written:
            raise ApiError("nothing to save yet")
        return {"written": written}

    def history(self, _body: dict) -> dict:
        return {"report_dir": str(self.s.report_dir),
                "groups": history_list(self.s.report_dir)}

    def text_report(self, body: dict) -> dict:
        if body.get("which") == "search" and self.s.trace:
            return {"text": render_trace_text(self.s.trace)}
        if self.s.scan:
            return {"text": render_text(self.s.scan)}
        raise ApiError("nothing to show yet")


ROUTES = {
    "state": "state", "stop": "stop", "browse": "browse", "repo": "set_repo",
    "scan": "scan", "scan-result": "scan_result",
    "words/propose": "propose_words", "words": "words",
    "words/review": "review_words", "places": "places",
    "estimate": "estimate", "estimate-result": "estimate_result",
    "search": "search", "search-result": "search_result",
    "delete": "delete", "save": "save", "history": "history",
    "text-report": "text_report", "view": "view", "reveal": "reveal",
}

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sweep: sign in</title><link rel="stylesheet" href="/static/style.css"></head>
<body class="login"><form method="post" action="/login" class="card">
<h1>Sweep</h1><p>This Sweep is open to the local network. Enter its password.</p>
<input type="password" name="password" autofocus required>
<button type="submit">Sign in</button>{error}</form></body></html>"""


def make_handler(api: Api, token: str, allowed_hosts: set[str],
                 password: str | None, sessions: set[str]):

    class Handler(BaseHTTPRequestHandler):
        server_version = f"sweep/{__version__}"

        def log_message(self, *_args) -> None:
            pass  # keep paths, which may be personal, out of the console

        # checks

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _logged_in(self) -> bool:
            if password is None:
                return True
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            value = cookie["sweep"].value if "sweep" in cookie else ""
            return any(hmac.compare_digest(value, s) for s in sessions)

        def _send(self, status: int, body: bytes, kind: str,
                  extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self'; "
                             "script-src 'self'; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data).encode("utf-8"),
                       "application/json; charset=utf-8")

        # GET

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._send(403, b"wrong host", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/static/style.css":
                return self._static("style.css", "text/css; charset=utf-8")
            if not self._logged_in():
                return self._send(200, LOGIN_PAGE.format(error="").encode(),
                                  "text/html; charset=utf-8")
            if path == "/":
                page = (STATIC / "index.html").read_text(encoding="utf-8")
                page = page.replace("__TOKEN__", token)
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/static/app.js":
                return self._static("app.js", "text/javascript; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def _static(self, name: str, kind: str) -> None:
            self._send(200, (STATIC / name).read_bytes(), kind)

        # POST

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._send(403, b"wrong host", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                return self._send(413, b"too large", "text/plain")
            raw = self.rfile.read(length)
            if self.path == "/login":
                return self._login(raw)
            if not self._logged_in():
                return self._json(401, {"error": "sign in first"})
            if not hmac.compare_digest(self.headers.get("X-Sweep-Token") or "", token):
                return self._json(403, {"error": "missing or wrong page token"})
            if not self.path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            name = ROUTES.get(self.path[len("/api/"):])
            if not name:
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "bad request"})
            try:
                self._json(200, getattr(api, name)(body))
            except ApiError as error:
                self._json(error.status, {"error": str(error)})
            except Exception as error:  # reported, the server keeps running
                self._json(500, {"error": f"{error.__class__.__name__}: {error}"})

        def _login(self, raw: bytes) -> None:
            if password is None:
                return self._send(303, b"", "text/plain", {"Location": "/"})
            given = parse_qs(raw.decode("utf-8", errors="replace")).get("password", [""])[0]
            if not hmac.compare_digest(given.encode(), password.encode()):
                time.sleep(1.0)  # slows down guessing
                page = LOGIN_PAGE.format(error="<p class='bad'>Wrong password.</p>")
                return self._send(401, page.encode(), "text/html; charset=utf-8")
            value = secrets.token_urlsafe(32)
            sessions.add(value)
            self._send(303, b"", "text/plain", {
                "Location": "/",
                "Set-Cookie": f"sweep={value}; HttpOnly; SameSite=Strict; Path=/"})

    return Handler


def _lan_addresses() -> list[str]:
    try:
        return sorted({a for a in socket.gethostbyname_ex(socket.gethostname())[2]
                       if not a.startswith("127.")})
    except OSError:
        return []


def make_server(port: int = 8765, *, lan: bool = False, password: str | None = None,
                report_dir: Path | None = None):
    """Build the server without starting it. Returns (server, token, urls)."""
    if lan and (not password or len(password) < 8):
        raise ValueError("LAN mode needs a password of at least 8 characters")
    bind = "0.0.0.0" if lan else "127.0.0.1"
    session = Session(report_dir or default_report_dir())
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((bind, port), BaseHTTPRequestHandler)
    real_port = server.server_address[1]
    hosts = {f"127.0.0.1:{real_port}", f"localhost:{real_port}"}
    urls = [f"http://127.0.0.1:{real_port}/"]
    if lan:
        for address in _lan_addresses():
            hosts.add(f"{address}:{real_port}")
            urls.append(f"http://{address}:{real_port}/")
    server.RequestHandlerClass = make_handler(
        Api(session), token, hosts, password if lan else None, set())
    server.daemon_threads = True
    return server, token, urls
