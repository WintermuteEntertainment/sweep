import json
import os
import subprocess
import unittest
from pathlib import Path

from sweep import gitsrc
from sweep.cli import main
from sweep.identity import gather
from sweep.report import ReportError, check_report_dir, render_text, write_reports
from sweep.scan import scan_repo

from . import support

EMAIL_IN_HISTORY = "someone@example.com"
EMAIL_IN_HEAD = "current@example.com"
EMAIL_IN_MESSAGE = "admin@example.org"
EMAIL_IN_DEPENDENCY = "packager@example.net"
IP_IN_IGNORED = "192.0.2.44"
MAC_IN_UNTRACKED = "00-00-5E-00-53-01"
REMOTE_URL = "https://username:" + "password@example.com/owner/repo.git"


def build_repo() -> str:
    """A small repo that leaves something in each place Sweep looks."""
    repo = support.make_temp_dir()
    support.git(repo, "init", "-q")
    support.write(repo, "keep.txt", f"still here: {EMAIL_IN_HEAD}\n")
    support.write(repo, "notes.txt", f"line one\ncontact: {EMAIL_IN_HISTORY}\n")
    support.write(repo, ".gitignore", "local.log\nnode_modules/\nscratch/\n")
    support.git(repo, "add", "-A")
    support.git(repo, "commit", "-q", "-m", "first commit")
    support.git(repo, "rm", "-q", "notes.txt")
    support.git(repo, "commit", "-q", "-m", f"remove notes, ask {EMAIL_IN_MESSAGE}")
    support.write(repo, "local.log", f"connected to {IP_IN_IGNORED}\n")
    support.write(repo, "draft.txt", f"adapter {MAC_IN_UNTRACKED}\n")
    support.write(repo, "scratch/deep/out.txt", f"sent to {IP_IN_IGNORED}\n")
    support.write(repo, "node_modules/pkg/index.js", f"// {EMAIL_IN_DEPENDENCY}\n")
    support.git(repo, "remote", "add", "origin", REMOTE_URL)
    return repo


class ScanRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = build_repo()
        cls.status_before = support.read_only_status(cls.repo)
        cls.index_before = os.stat(os.path.join(cls.repo, ".git", "index")).st_mtime_ns
        cls.result = scan_repo(cls.repo, identity=[])

    @classmethod
    def tearDownClass(cls):
        support.remove_temp_dir(cls.repo)

    def found(self, value, **where):
        return [
            f for f in self.result.findings
            if f.value == value and all(getattr(f, k) == v for k, v in where.items())
        ]

    def test_deleted_file_is_found_in_history(self):
        hits = self.found(EMAIL_IN_HISTORY, area="history")
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].in_head)
        self.assertEqual(hits[0].path, "notes.txt")
        self.assertEqual(hits[0].line, 2)
        self.assertEqual(len(hits[0].commits), 1)

    def test_deleted_file_is_not_in_working_tree(self):
        self.assertEqual(self.found(EMAIL_IN_HISTORY, area="working-tree"), [])

    def test_committed_file_is_marked_as_in_current_commit(self):
        tree = self.found(EMAIL_IN_HEAD, area="working-tree")
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0].status, "tracked")
        history = self.found(EMAIL_IN_HEAD, area="history")
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].in_head)
        self.assertEqual(self.result.searched["history"]["commits"], 2)

    def test_text_summary_counts_a_committed_line_once(self):
        from sweep.report import summarize
        count, distinct = summarize(self.result)["low"]
        values = {f.value for f in self.result.findings if f.severity == "low"}
        self.assertEqual(distinct, len(values))
        self.assertEqual(count, len([
            f for f in self.result.findings
            if f.severity == "low" and not (f.area == "history" and f.in_head)]))

    def test_ignored_file_is_searched(self):
        hits = self.found(IP_IN_IGNORED, area="working-tree", path="local.log")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].status, "ignored")

    def test_ignored_folder_is_searched(self):
        hits = self.found(IP_IN_IGNORED, path="scratch/deep/out.txt")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].status, "ignored")

    def test_untracked_file_is_searched(self):
        hits = self.found(MAC_IN_UNTRACKED, area="working-tree")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].status, "untracked")

    def test_dependency_folder_is_skipped_and_listed(self):
        self.assertEqual(self.found(EMAIL_IN_DEPENDENCY), [])
        skipped = [s for s in self.result.skipped if s.path == "node_modules/"]
        self.assertEqual(len(skipped), 1)

    def test_commit_message_is_searched(self):
        hits = self.found(EMAIL_IN_MESSAGE, area="commit-message")
        self.assertEqual(len(hits), 1)

    def test_remote_url_is_searched(self):
        hits = [f for f in self.result.findings
                if f.area == "remote" and f.detector == "url-credentials"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.result.remotes[0]["name"], "origin")

    def test_commit_identities_are_listed(self):
        self.assertEqual(len(self.result.commit_identities), 1)
        person = self.result.commit_identities[0]
        self.assertEqual(person["email"], "author@example.com")
        self.assertEqual(person["authored"], 2)

    def test_counts(self):
        tree = self.result.searched["working_tree"]
        self.assertEqual(tree, {"tracked": 2, "untracked": 1, "ignored": 2})

    def test_secrets_are_declared_unchecked(self):
        self.assertTrue(any("Secrets" in note for note in self.result.not_checked))

    def test_scan_changes_nothing_in_the_repo(self):
        index_after = os.stat(os.path.join(self.repo, ".git", "index")).st_mtime_ns
        self.assertEqual(index_after, self.index_before)
        self.assertEqual(support.read_only_status(self.repo), self.status_before)

    def test_text_report_shows_values_in_full(self):
        text = render_text(self.result)
        for value in (EMAIL_IN_HISTORY, IP_IN_IGNORED, MAC_IN_UNTRACKED,
                      EMAIL_IN_MESSAGE):
            self.assertIn(value, text)
        self.assertIn("history only", text)


class RealIdentityScanTests(unittest.TestCase):
    """Uses the real computer name of the machine running the tests."""

    def test_machine_name_in_a_file_is_a_high_finding(self):
        identity, _ = gather()
        names = [v.value for v in identity if v.kind == "computer-name"]
        if not names:
            self.skipTest("this machine reports no usable computer name")
        repo = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, repo)
        support.git(repo, "init", "-q")
        support.write(repo, "setup.md", f"Run this on {names[0]} first.\n")
        result = scan_repo(repo)
        hits = [f for f in result.findings if f.detector == "computer-name"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].severity, "high")
        self.assertEqual(hits[0].status, "untracked")


class EdgeCaseTests(unittest.TestCase):
    def test_repo_with_no_commits(self):
        repo = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, repo)
        support.git(repo, "init", "-q")
        support.write(repo, "a.txt", f"see {IP_IN_IGNORED}\n")
        result = scan_repo(repo, identity=[])
        self.assertEqual(result.searched["history"]["commits"], 0)
        self.assertEqual([f.value for f in result.findings], [IP_IN_IGNORED])

    def test_folder_that_is_not_a_repo(self):
        folder = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, folder)
        with self.assertRaises(gitsrc.GitError):
            scan_repo(folder, identity=[])

    def test_utf16_file_is_read(self):
        repo = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, repo)
        support.git(repo, "init", "-q")
        support.write(repo, "copy.log", f"to {EMAIL_IN_HISTORY}\n", encoding="utf-16")
        result = scan_repo(repo, identity=[])
        self.assertEqual([f.value for f in result.findings], [EMAIL_IN_HISTORY])

    def test_no_history_flag(self):
        repo = build_repo()
        self.addCleanup(support.remove_temp_dir, repo)
        result = scan_repo(repo, identity=[], history=False)
        self.assertNotIn("history", result.searched)
        self.assertEqual([f for f in result.findings if f.area == "history"], [])
        self.assertTrue(any("history" in note.lower() for note in result.not_checked))


class SafetyTests(unittest.TestCase):
    """Regression tests for the two defects found in the 2026-11-12 review."""

    def test_junction_in_an_ignored_folder_is_not_followed(self):
        if os.name != "nt":
            self.skipTest("junctions are a Windows feature")
        base = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, base)
        repo = os.path.join(base, "repo")
        outside = os.path.join(base, "outside")
        support.write(outside, "far.txt", f"reach {IP_IN_IGNORED} here\n")
        support.write(repo, ".gitignore", "scratch/\n")
        support.write(repo, "scratch/near.txt", "nothing here\n")
        support.git(repo, "init", "-q")
        link = os.path.join(repo, "scratch", "jump")
        done = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                              capture_output=True)
        if done.returncode != 0:
            self.skipTest("could not create a junction")
        self.addCleanup(os.rmdir, link)  # removes the junction, not its target
        result = scan_repo(repo, identity=[])
        self.assertEqual([f for f in result.findings if f.value == IP_IN_IGNORED], [])
        skipped = [s for s in result.skipped if s.path == "scratch/jump"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason, "link or junction (not followed)")

    def test_repo_settings_cannot_start_a_program(self):
        base = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, base)
        repo = os.path.join(base, "repo")
        marker = os.path.join(base, "MARKER").replace("\\", "/")
        hook = os.path.join(base, "hook.sh").replace("\\", "/")
        support.write(base, "hook.sh", f"#!/bin/sh\necho ran > '{marker}'\n")
        support.write(repo, "a.txt", "hello\n")
        support.git(repo, "init", "-q")
        support.git(repo, "add", "a.txt")
        support.git(repo, "config", "core.fsmonitor", hook)
        support.git(repo, "config", "core.fsmonitorHookVersion", "2")
        scan_repo(repo, identity=[])
        self.assertFalse(os.path.exists(marker))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.repo = build_repo()
        self.addCleanup(support.remove_temp_dir, self.repo)
        self.out = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, self.out)

    def test_reports_are_written_and_never_overwritten(self):
        result = scan_repo(self.repo, identity=[])
        first = write_reports(result, Path(self.out))
        second = write_reports(result, Path(self.out))
        self.assertEqual(len({*first, *second}), 4)
        for path in (*first, *second):
            self.assertTrue(path.exists())
        with open(first[0], encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["part"], "A")
        self.assertEqual(len(data["findings"]), len(result.findings))

    def test_report_folder_inside_a_repo_is_refused(self):
        inside = Path(self.repo) / "reports" / "new"
        with self.assertRaises(ReportError):
            check_report_dir(inside)
        self.assertFalse((Path(self.repo) / "reports").exists())

    def test_command_line_run(self):
        code = main(["repo", self.repo, "--out", self.out])
        self.assertEqual(code, 0)
        written = sorted(os.listdir(self.out))
        self.assertEqual(len(written), 2)
        self.assertTrue(written[0].endswith(".sweep-report.json"))

    def test_command_line_refuses_report_folder_in_repo(self):
        code = main(["repo", self.repo, "--out", os.path.join(self.repo, "out")])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "out")))


if __name__ == "__main__":
    unittest.main()
