"""Tests for deleting and for the web interface.

Invented data, approved by the owner on 2026-11-12: "Morgan Reyes" is a
made-up person who borrowed this computer for a made-up project called
"lantern-budget", then left copies, notes and logs around. Nothing here is
a real person, project or address.

The Recycle Bin is never touched: the tests replace the function that would
call Windows with one that records the call and removes the temp file.
"""
import http.client
import json
import os
import threading
import time
import unittest
from pathlib import Path

from sweep import delete
from sweep.web import make_server

from . import support

PERSON = "Morgan Reyes"
EMAIL = "morgan.reyes@example.com"
PROJECT = "lantern-budget"
APP_MODULE = "lantern_budget_core.py"

MODULE_TEXT = (
    f'"""{PROJECT}: monthly budget planner by {PERSON} <{EMAIL}>."""\n\n'
    "LANTERN_SPLIT_RATIO = 0.35\n\n"
    "def lantern_rollover(month):\n"
    "    return month.carry_forward * LANTERN_SPLIT_RATIO\n"
) * 3
NOTES_TEXT = (
    f"# {PROJECT}\n\nMorgan's notes. Call the bank about lantern_rollover.\n"
    f"Contact: {EMAIL}\n") * 3


class FakeRecycleBin:
    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        os.remove(path)


def build_scene():
    """Morgan's repo, plus what Morgan left lying around."""
    base = support.make_temp_dir()
    repo = os.path.join(base, PROJECT)
    os.makedirs(repo)
    support.git(repo, "init", "-q")
    support.write(repo, f"src/{APP_MODULE}", MODULE_TEXT)
    support.write(repo, "NOTES.md", NOTES_TEXT)
    support.write(repo, ".gitignore", "scratch/\n")
    support.git(repo, "add", "-A")
    support.git(repo, "commit", "-q", "-m", "first budget planner")
    support.write(repo, "scratch/bank_export_draft.txt", f"export for {EMAIL}\n" * 3)
    support.write(repo, "untracked_todo.txt",
                  f"todo for {PERSON} <{EMAIL}>: lantern_rollover\n" * 3)

    around = os.path.join(base, "around")
    support.write(around, "Downloads/core_backup.py", MODULE_TEXT)          # exact copy
    support.write(around, "Desktop/budget notes.txt",                       # edited copy
                  NOTES_TEXT.replace("bank", "credit union"))
    support.write(around, "Temp/run.log", "lantern_rollover ran at 09:12\n")
    support.write(around, "Downloads/unrelated.txt", "nothing about anything\n" * 4)
    other = os.path.join(around, "SomeoneElsesRepo")
    os.makedirs(other)
    support.git(other, "init", "-q")
    support.write(other, "README.md", f"Mentions {PROJECT} and lantern_rollover.\n")
    return base, repo, around, other


class DeleteRuleTests(unittest.TestCase):
    def setUp(self):
        self.base, self.repo, self.around, self.other = build_scene()
        self.addCleanup(support.remove_temp_dir, self.base)
        self.fake = FakeRecycleBin()
        original = delete.send_to_recycle_bin
        delete.send_to_recycle_bin = self.fake
        self.addCleanup(setattr, delete, "send_to_recycle_bin", original)

    def target(self, path, kind):
        info = os.lstat(path)
        return delete.Target(path, info.st_size, info.st_mtime_ns, kind)

    def test_enclosing_repo(self):
        self.assertEqual(delete.enclosing_repo(os.path.join(self.repo, "NOTES.md")), self.repo)
        self.assertEqual(delete.enclosing_repo(os.path.join(self.other, "README.md")), self.other)
        self.assertIsNone(delete.enclosing_repo(os.path.join(self.around, "Temp", "run.log")))

    def test_leftover_is_deleted_permanently_with_the_right_count(self):
        path = os.path.join(self.around, "Temp", "run.log")
        with self.assertRaises(delete.DeleteError):
            delete.perform([self.target(path, delete.LEFTOVER)], self.repo,
                           delete.PERMANENT, confirm_count=2)
        self.assertTrue(os.path.exists(path))
        outcomes = delete.perform([self.target(path, delete.LEFTOVER)], self.repo,
                                  delete.PERMANENT, confirm_count=1)
        self.assertTrue(outcomes[0].done)
        self.assertFalse(os.path.exists(path))

    def test_file_in_another_repo_is_never_deleted(self):
        path = os.path.join(self.other, "README.md")
        if not delete.recycle_supported():
            self.skipTest("Recycle Bin is Windows only")
        outcomes = delete.perform([self.target(path, delete.LEFTOVER)], self.repo,
                                  delete.RECYCLE)
        self.assertFalse(outcomes[0].done)
        self.assertIn("another git repo", outcomes[0].message)
        self.assertTrue(any("git -C" in step for step in outcomes[0].steps))
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.fake.calls, [])

    def test_changed_file_is_left_alone(self):
        path = os.path.join(self.around, "Temp", "run.log")
        target = self.target(path, delete.LEFTOVER)
        time.sleep(0.05)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("one more line\n")
        outcomes = delete.perform([target], self.repo, delete.PERMANENT, confirm_count=1)
        self.assertFalse(outcomes[0].done)
        self.assertIn("changed", outcomes[0].message)
        self.assertTrue(os.path.exists(path))

    def test_tracked_file_needs_the_danger_warning(self):
        path = os.path.join(self.repo, "NOTES.md")
        target = self.target(path, delete.REPO_TRACKED)
        outcomes = delete.perform([target], self.repo, delete.PERMANENT, confirm_count=1)
        self.assertFalse(outcomes[0].done)
        self.assertTrue(any("DANGER" in step for step in outcomes[0].steps))
        outcomes = delete.perform([target], self.repo, delete.PERMANENT, confirm_count=1,
                                  danger_ok={os.path.normcase(os.path.normpath(path))})
        self.assertTrue(outcomes[0].done)

    def test_untracked_repo_file_can_go(self):
        path = os.path.join(self.repo, "untracked_todo.txt")
        outcomes = delete.perform([self.target(path, delete.REPO_UNTRACKED)], self.repo,
                                  delete.PERMANENT, confirm_count=1)
        self.assertTrue(outcomes[0].done)

    def test_report_must_be_in_the_report_folder_and_named_like_one(self):
        reports = os.path.join(self.base, "reports")
        support.write(reports, "x-1.sweep-report.txt", "report\n")
        support.write(reports, "keep-me.txt", "not a report\n")
        good = self.target(os.path.join(reports, "x-1.sweep-report.txt"), delete.REPORT)
        bad = self.target(os.path.join(reports, "keep-me.txt"), delete.REPORT)
        outcomes = delete.perform([good, bad], "", delete.PERMANENT, confirm_count=2,
                                  report_dir=reports)
        self.assertEqual([o.done for o in outcomes], [True, False])
        self.assertTrue(os.path.exists(os.path.join(reports, "keep-me.txt")))


class Client:
    def __init__(self, port, token=None, host=None):
        self.port, self.token = port, token
        self.host = host or f"127.0.0.1:{port}"
        self.cookie = ""

    def request(self, method, path, body=None, headers=None, form=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        sent = {"Host": self.host}
        if self.token:
            sent["X-Sweep-Token"] = self.token
        if self.cookie:
            sent["Cookie"] = self.cookie
        data = None
        if form is not None:
            data = form.encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        connection.close()
        return response.status, raw

    def api(self, name, body=None, expect=200):
        status, raw = self.request("POST", f"/api/{name}", body or {})
        data = json.loads(raw)
        if status != expect:
            raise AssertionError(f"{name}: {status} {data}")
        return data

    def wait(self):
        for _ in range(600):
            job = self.api("state")["job"]
            if job["state"] != "running":
                if job["state"] != "done":
                    raise AssertionError(f"job ended {job}")
                return job
            time.sleep(0.2)
        raise AssertionError("job did not finish")


def start(**kwargs):
    server, token, urls = make_server(0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, token, server.server_address[1]


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.reports = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, self.reports)
        self.server, self.token, self.port = start(report_dir=Path(self.reports))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_listens_on_this_machine_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_page_carries_the_token(self):
        status, raw = Client(self.port).request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.token.encode(), raw)

    def test_api_without_token_is_refused(self):
        status, _ = Client(self.port).request("POST", "/api/state", {})
        self.assertEqual(status, 403)

    def test_wrong_host_is_refused(self):
        client = Client(self.port, self.token, host="attacker.example:80")
        self.assertEqual(client.request("GET", "/")[0], 403)
        self.assertEqual(client.request("POST", "/api/state", {})[0], 403)

    def test_lan_needs_a_password(self):
        with self.assertRaises(ValueError):
            make_server(0, lan=True, password=None)
        with self.assertRaises(ValueError):
            make_server(0, lan=True, password="short")

    def test_lan_login(self):
        server, token, port = start(lan=True, password="correct horse",
                                    report_dir=Path(self.reports))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.server_address[0], "0.0.0.0")
        client = Client(port, token)
        status, raw = client.request("GET", "/")
        self.assertNotIn(token.encode(), raw)                 # login page only
        self.assertEqual(client.request("POST", "/api/state", {})[0], 401)
        self.assertEqual(client.request("POST", "/login", form="password=wrong")[0], 401)
        self.assertEqual(client.request("POST", "/login", form="password=correct%20horse")[0], 303)
        self.assertEqual(client.request("POST", "/api/state", {})[0], 200)


class WebScenarioTests(unittest.TestCase):
    """Morgan's whole cleanup, through the web API."""

    @classmethod
    def setUpClass(cls):
        cls.base, cls.repo, cls.around, cls.other = build_scene()
        cls.reports = os.path.join(cls.base, "reports")
        cls.fake = FakeRecycleBin()
        cls.original = delete.send_to_recycle_bin
        delete.send_to_recycle_bin = cls.fake
        cls.server, token, port = start(report_dir=Path(cls.reports))
        cls.client = Client(port, token)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        delete.send_to_recycle_bin = cls.original
        support.remove_temp_dir(cls.base)

    def test_full_cleanup(self):
        c = self.client
        info = c.api("repo", {"path": self.repo})
        self.assertEqual(os.path.normcase(info["repo"]), os.path.normcase(self.repo))

        # 1: inside the repo
        c.api("scan"); c.wait()
        scan = c.api("scan-result")
        values = {f["value"] for f in scan["findings"]}
        self.assertIn(EMAIL, values)
        files = {f["relative"]: f for f in scan["files"]}
        self.assertEqual(files["NOTES.md"]["status"], "tracked")
        self.assertEqual(files["untracked_todo.txt"]["status"], "untracked")
        self.assertEqual(files["scratch/bank_export_draft.txt"]["status"], "ignored")

        refused = c.api("delete", {"source": "scan", "ids": [files["NOTES.md"]["id"]],
                                   "mode": "permanent", "confirm": 1})
        self.assertFalse(refused["outcomes"][0]["done"])
        self.assertTrue(os.path.exists(os.path.join(self.repo, "NOTES.md")))

        gone = c.api("delete", {"source": "scan", "mode": "permanent", "confirm": 1,
                                "ids": [files["untracked_todo.txt"]["id"]]})
        self.assertTrue(gone["outcomes"][0]["done"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "untracked_todo.txt")))

        # viewing: by id only, text comes back, history blobs too
        shown = c.api("view", {"source": "scan", "id": files["NOTES.md"]["id"]})
        self.assertIn("lantern_rollover", shown["text"])
        c.api("view", {"source": "scan", "id": "no-such-id"}, expect=400)
        c.api("view", {"source": "search", "id": "h0"}, expect=400)   # no search yet
        blob = next(f["blob"] for f in scan["findings"] if f["area"] == "history")
        self.assertIn(EMAIL, c.api("view", {"source": "blob", "id": blob})["text"])
        c.api("view", {"source": "blob", "id": "0" * 40}, expect=400)
        c.api("reveal", {"source": "blob", "id": blob}, expect=400)

        # 2: search words; the search is refused until they are reviewed
        c.api("search", {"scopes": [{"path": self.around}]}, expect=400)
        c.api("words/propose"); c.wait()
        words = c.api("words")
        proposed = [w["term"].lower() for w in words["words"]]
        self.assertIn(PROJECT, proposed)
        self.assertTrue(any("lantern" in w for w in proposed))
        c.api("words/review", {"words": ["abc"]}, expect=400)
        c.api("words/review", {"words": [PROJECT, "lantern_rollover", "LANTERN_SPLIT_RATIO"],
                               "file_names": [APP_MODULE]})

        # 3: count, then search
        c.api("estimate", {"scopes": [{"path": self.around}]}); c.wait()
        estimate = c.api("estimate-result")["estimate"]
        self.assertEqual(estimate["files"], 5)   # its .git folder is not counted
        c.api("search", {"scopes": [{"path": self.around}]}); c.wait()
        result = c.api("search-result")
        hits = {os.path.relpath(h["path"], self.around).replace("\\", "/"): h
                for h in result["hits"]}
        self.assertEqual(hits["Downloads/core_backup.py"]["strength"], "strong")
        self.assertTrue(hits["Downloads/core_backup.py"]["exact_copy_of"])
        self.assertIn("Desktop/budget notes.txt", hits)
        self.assertIn("Temp/run.log", hits)
        self.assertNotIn("Downloads/unrelated.txt", hits)
        viewed = c.api("view", {"source": "search", "id": hits["Temp/run.log"]["id"]})
        self.assertIn("lantern_rollover", viewed["text"])
        other = hits["SomeoneElsesRepo/README.md"]
        self.assertFalse(other["deletable"])
        self.assertTrue(other["steps"])

        # 4: delete; another repo's file is refused outright
        c.api("delete", {"source": "search", "ids": [other["id"]], "mode": "permanent",
                         "confirm": 1}, expect=400)
        backup = hits["Downloads/core_backup.py"]
        c.api("delete", {"source": "search", "ids": [backup["id"]], "mode": "permanent",
                         "confirm": 2}, expect=400)
        done = c.api("delete", {"source": "search", "ids": [backup["id"]],
                                "mode": "permanent", "confirm": 1})
        self.assertTrue(done["outcomes"][0]["done"])
        self.assertFalse(os.path.exists(backup["path"]))
        after = c.api("search-result")
        self.assertTrue(any(h["gone"] for h in after["hits"]))
        if delete.recycle_supported():
            log_hit = hits["Temp/run.log"]
            recycled = c.api("delete", {"source": "search", "ids": [log_hit["id"]],
                                        "mode": "recycle"})
            self.assertTrue(recycled["outcomes"][0]["done"])
            self.assertEqual(self.fake.calls, [log_hit["path"]])
        self.assertTrue(os.path.exists(os.path.join(self.other, "README.md")))

        # history: save, list, delete it all
        self.assertEqual(c.api("history")["groups"], [])
        saved = c.api("save", {"scan": True, "search": True})["written"]
        self.assertEqual(len(saved), 4)
        support.write(self.reports, "my own note.txt", "keep this\n")
        groups = c.api("history")["groups"]
        paths = [f["path"] for g in groups for f in g["files"]]
        self.assertEqual(len(paths), 4)
        c.api("delete", {"source": "history", "ids": paths, "mode": "permanent",
                         "confirm": 4})
        self.assertEqual(c.api("history")["groups"], [])
        self.assertTrue(os.path.exists(os.path.join(self.reports, "my own note.txt")))
        log = c.api("state")["log"]
        self.assertTrue(any(entry["action"] == "deleted permanently" for entry in log))


if __name__ == "__main__":
    unittest.main()
