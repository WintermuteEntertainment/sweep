"""Tests for Part B: fingerprint, term list, and the outside search.

The project name used here is not invented: it is the random name the
operating system gives the temporary folder on each run.
"""
import json
import os
import platform
import subprocess
import unittest
from pathlib import Path

from sweep import gitsrc
from sweep.cli import main
from sweep.fingerprint import build as build_fingerprint
from sweep.fingerprint import hash_bytes
from sweep.gitobjects import has_object, resolve_git_dir, shares_history
from sweep.places import fixed_drives, is_inside, known_places
from sweep.terms import (TermError, TermList, background_vocabulary,
                         build_candidates, classify, distinctive_file_name,
                         load_term_file, render_term_file, require_reviewed,
                         split_subwords, term_families)
from sweep.trace import MEDIUM, STRONG, WEAK, trace_repo
from sweep.tracereport import render_trace_text

from . import support

# A small stand-in for the common-word list, so most tests do not have to
# read the whole Python library.
COMMON = {"sweep", "test", "engine", "notes", "readme", "license", "this",
          "that", "with", "from", "have", "project", "module", "about",
          "line", "text", "some", "more", "here", "copy", "file", "print",
          "return", "import", "def", "old", "run", "log", "started", "tiny"}

LICENSE_TEXT = ("This text stands in for a license file that many projects "
                "share word for word.\n") * 4


def build_project() -> tuple[str, str, dict[str, str]]:
    repo = support.make_temp_dir()
    token = os.path.basename(repo).replace("-", "_")
    module = f"{token}_engine.py"
    texts = {
        "module": f"# {token} engine\n\ndef run_{token}():\n"
                  f"    return 'started {token}'\n" * 3,
        "readme": f"# {token}\n\nSome notes about the {token} project.\n" * 3,
        "old": f"old notes for {token}: this file is deleted later\n" * 3,
    }
    support.git(repo, "init", "-q")
    support.write(repo, f"src/{module}", texts["module"])
    support.write(repo, "README.md", texts["readme"])
    support.write(repo, "LICENSE", LICENSE_TEXT)
    support.write(repo, "old_notes.txt", texts["old"])
    support.write(repo, "tiny.txt", "x\n")
    support.git(repo, "add", "-A")
    support.git(repo, "commit", "-q", "-m", "first")
    support.git(repo, "rm", "-q", "old_notes.txt")
    support.git(repo, "commit", "-q", "-m", "remove old notes")
    return repo, token, texts


def make_term_list(repo: str, token: str, folder: str) -> TermList:
    terms, names, notes = build_candidates(repo, vocabulary=COMMON)
    path = os.path.join(folder, "terms.sweep-terms.txt")
    text = render_term_file(repo, terms, names, notes)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace("# reviewed: no", "# reviewed: yes"))
    return load_term_file(path)


def snapshot(folder: str) -> list[tuple[str, int, int]]:
    found = []
    for base, _dirs, names in os.walk(folder):
        for name in names:
            full = os.path.join(base, name)
            info = os.lstat(full)
            found.append((full, info.st_size, info.st_mtime_ns))
    return sorted(found)


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo, cls.token, texts = build_project()
        cls.work = support.make_temp_dir()
        cls.outside = os.path.join(cls.work, "outside")
        cls.module = f"{cls.token}_engine.py"
        out = cls.outside
        support.write(out, "copies/renamed.txt", texts["module"])
        support.write(out, "old/kept_by_mistake.txt", texts["old"])
        support.write(out, "logs/run.log", f"12:00 started {cls.token}\n")
        support.write(out, "logs/wide.log", f"copy of {cls.token} made\n",
                      encoding="utf-16")
        support.write(out, f"names/{cls.module}", "different text entirely\n")
        support.write(out, "unrelated/other.txt", "nothing to see\n" * 5)
        support.write(out, "unrelated/LICENSE", LICENSE_TEXT)
        support.write(out, "unrelated/tiny.txt", "x\n")
        support.write(out, "node_modules/pkg/index.js", f"// {cls.token}\n")
        support.write(out, "reports/earlier.txt", f"report on {cls.token}\n")
        with open(os.path.join(out, "blob.data"), "wb") as handle:
            handle.write(bytes(range(256)) * 8 + cls.token.encode() * 4)
        support.write(out, "picture.png", f"not really a picture: {cls.token}\n")
        support.write(out, "copies/renamed.png", texts["readme"])
        support.git(cls.work, "clone", "-q", "--no-hardlinks", cls.repo,
                    os.path.join(out, "clone"))
        packed = os.path.join(out, "clone_packed")
        support.git(cls.work, "clone", "-q", "--no-hardlinks", cls.repo, packed)
        support.git(packed, "gc", "-q")
        cls.junction = ""
        if platform.system() == "Windows":
            link = os.path.join(out, "linked_logs")
            done = subprocess.run(
                ["cmd", "/c", "mklink", "/J", link, os.path.join(out, "logs")],
                capture_output=True)
            if done.returncode == 0:
                cls.junction = link
        cls.term_list = make_term_list(cls.repo, cls.token, cls.work)
        cls.before = snapshot(cls.outside)
        cls.repo_before = support.read_only_status(cls.repo)
        cls.result = trace_repo(
            cls.repo, cls.term_list, [("folder", cls.outside)],
            exclude=[os.path.join(cls.outside, "reports")])

    @classmethod
    def tearDownClass(cls):
        if cls.junction:
            os.rmdir(cls.junction)  # removes the junction, not its target
        support.remove_temp_dir(cls.work)
        support.remove_temp_dir(cls.repo)

    def hit(self, *parts):
        path = os.path.join(self.outside, *parts)
        found = [h for h in self.result.hits if h.path == path]
        return found[0] if found else None

    def test_exact_copy_under_another_name_is_strong(self):
        hit = self.hit("copies", "renamed.txt")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.strength, STRONG)
        self.assertEqual(hit.exact_copy_of, [f"src/{self.module}"])
        self.assertFalse(hit.weak_copy)

    def test_copy_of_a_file_deleted_from_the_repo_is_found(self):
        hit = self.hit("old", "kept_by_mistake.txt")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.exact_copy_of, ["old_notes.txt"])
        self.assertEqual(hit.strength, STRONG)

    def test_term_in_a_log_is_found(self):
        hit = self.hit("logs", "run.log")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.exact_copy_of, [])
        self.assertTrue(any(self.token in term.lower() for term in hit.terms))
        self.assertEqual(hit.line, 1)
        self.assertIn(self.token, hit.snippet)
        self.assertEqual(hit.strength, MEDIUM)  # the repo's own name

    def test_utf16_file_is_searched(self):
        hit = self.hit("logs", "wide.log")
        self.assertIsNotNone(hit)
        self.assertTrue(hit.terms)

    def test_same_file_name_with_other_content(self):
        hit = self.hit("names", self.module)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.name_match)
        self.assertEqual(hit.exact_copy_of, [])
        self.assertTrue(hit.name_terms)
        self.assertEqual(hit.strength, STRONG)

    def test_shared_license_text_is_only_weak(self):
        hit = self.hit("unrelated", "LICENSE")
        self.assertIsNotNone(hit)
        self.assertTrue(hit.weak_copy)
        self.assertEqual(hit.strength, WEAK)

    def test_unrelated_and_tiny_files_are_not_hits(self):
        self.assertIsNone(self.hit("unrelated", "other.txt"))
        self.assertIsNone(self.hit("unrelated", "tiny.txt"))
        self.assertGreaterEqual(
            self.result.fingerprint["too_small_to_fingerprint"], 1)

    def test_binary_file_is_not_searched_for_terms(self):
        self.assertIsNone(self.hit("blob.data"))
        entry = self.result.skipped["binary file (not searched for terms)"]
        self.assertEqual(entry["examples"], [os.path.join(self.outside, "blob.data")])

    def test_binary_file_type_is_not_opened_for_terms(self):
        self.assertIsNone(self.hit("picture.png"))
        entry = self.result.skipped["binary file type (not searched for terms)"]
        self.assertIn(os.path.join(self.outside, "picture.png"), entry["examples"])

    def test_binary_file_type_is_still_checked_for_exact_copies(self):
        hit = self.hit("copies", "renamed.png")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.exact_copy_of, ["README.md"])
        self.assertEqual(hit.terms, {})

    def test_clones_are_reported_and_not_searched_inside(self):
        expected = sorted(os.path.join(self.outside, name)
                          for name in ("clone", "clone_packed"))
        self.assertEqual(self.result.copies, expected)
        inside = [h for h in self.result.hits
                  if is_inside(h.path, expected[0]) or is_inside(h.path, expected[1])]
        self.assertEqual(inside, [])

    def test_dependency_folder_is_skipped_and_listed(self):
        self.assertIsNone(self.hit("node_modules", "pkg", "index.js"))
        self.assertIn("dependency or cache folder", self.result.skipped)

    def test_excluded_folder_is_skipped_and_listed(self):
        self.assertIsNone(self.hit("reports", "earlier.txt"))
        reasons = [r for r in self.result.skipped if r.startswith("left out on purpose")]
        self.assertEqual(len(reasons), 1)

    def test_junction_is_not_followed(self):
        if not self.junction:
            self.skipTest("could not create a junction on this machine")
        through = [h for h in self.result.hits if is_inside(h.path, self.junction)]
        self.assertEqual(through, [])
        entry = self.result.skipped["link or junction (not followed)"]
        self.assertIn(self.junction, entry["examples"])

    def test_nothing_outside_or_inside_the_repo_was_changed(self):
        self.assertEqual(snapshot(self.outside), self.before)
        self.assertEqual(support.read_only_status(self.repo), self.repo_before)

    def test_term_counts_and_totals(self):
        self.assertTrue(any(count > 0 for count in self.result.term_files.values()))
        self.assertGreater(self.result.searched["files_seen"], 5)
        self.assertEqual(self.result.scopes[0]["status"], "searched")

    def test_text_report(self):
        text = render_trace_text(self.result)
        self.assertIn("Part B", text)
        self.assertIn("renamed.txt", text)
        self.assertIn("exact copy of", text)
        self.assertIn("OTHER COPIES OF THIS REPO", text)
        self.assertIn("WHAT WAS NOT SEARCHED", text)

    def test_dependency_folders_can_be_included(self):
        result = trace_repo(self.repo, self.term_list,
                            [("folder", os.path.join(self.outside, "node_modules"))],
                            include_dependency_folders=True)
        self.assertEqual(len(result.hits), 1)

    def test_no_term_list_finds_exact_copies_only(self):
        result = trace_repo(self.repo, None, [("folder", self.outside)])
        names = sorted(os.path.basename(h.path) for h in result.hits)
        self.assertEqual(names, ["LICENSE", "kept_by_mistake.txt", "renamed.png",
                                 "renamed.txt"])

    def test_scope_rules(self):
        missing = os.path.join(self.work, "no-such-folder")
        result = trace_repo(self.repo, None, [
            ("folder", self.repo), ("folder", missing),
            ("folder", os.path.join(self.outside, "copies")),
            ("folder", self.outside)])
        status = {scope["path"]: scope["status"] for scope in result.scopes}
        self.assertTrue(status[self.repo].startswith("left out on purpose"))
        self.assertEqual(status[missing], "does not exist")
        self.assertEqual(status[self.outside], "searched")
        self.assertTrue(status[os.path.join(self.outside, "copies")]
                        .startswith("already covered"))
        copies = [h for h in result.hits if h.path.endswith("renamed.txt")]
        self.assertEqual(len(copies), 1)

    def test_single_file_as_scope(self):
        log = os.path.join(self.outside, "logs", "run.log")
        result = trace_repo(self.repo, self.term_list, [("shell history", log)])
        self.assertEqual([h.path for h in result.hits], [log])


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.token, self.texts = build_project()
        self.addCleanup(support.remove_temp_dir, self.repo)

    def test_hash_matches_git(self):
        data = self.texts["module"].encode("utf-8")
        path = os.path.join(self.repo, "src", f"{self.token}_engine.py")
        expected = support.git(self.repo, "hash-object", path).strip()
        self.assertEqual(hash_bytes(data, "sha1"), expected)

    def test_history_and_working_tree_are_both_covered(self):
        fingerprint = build_fingerprint(self.repo)
        old = self.texts["old"].encode("utf-8")
        self.assertEqual(fingerprint.lookup(len(old), hash_bytes(old, "sha1")),
                         ["old_notes.txt"])
        support.write(self.repo, "untracked_draft.txt", "a draft " * 20)
        fingerprint = build_fingerprint(self.repo)
        draft = ("a draft " * 20).encode("utf-8")
        self.assertEqual(fingerprint.lookup(len(draft), hash_bytes(draft, "sha1")),
                         ["untracked_draft.txt"])
        self.assertEqual(fingerprint.lookup(2, hash_bytes(b"x\n", "sha1")), None)

    def test_linked_working_tree_is_listed(self):
        extra = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, extra)
        tree = os.path.join(extra, "tree")
        support.git(self.repo, "worktree", "add", "-q", tree, "-b", "side")
        fingerprint = build_fingerprint(self.repo)
        self.assertIn(os.path.normpath(tree),
                      [os.path.normpath(p) for p in fingerprint.worktrees])
        result = trace_repo(self.repo, None, [("folder", extra)])
        self.assertEqual(result.hits, [])
        self.assertEqual(result.copies, [])
        self.assertTrue(any(r.startswith("left out on purpose")
                            for r in result.skipped))


class GitObjectTests(unittest.TestCase):
    def setUp(self):
        self.repo, _token, _texts = build_project()
        self.addCleanup(support.remove_temp_dir, self.repo)
        self.anchors = gitsrc.anchor_commits(self.repo)
        self.git_dir = os.path.join(self.repo, ".git")

    def test_loose_then_packed(self):
        self.assertTrue(self.anchors)
        for anchor in self.anchors:
            self.assertTrue(has_object(self.git_dir, anchor))
        support.git(self.repo, "gc", "-q")
        loose = os.path.join(self.git_dir, "objects", self.anchors[0][:2],
                             self.anchors[0][2:])
        self.assertFalse(os.path.exists(loose))
        for anchor in self.anchors:
            self.assertTrue(has_object(self.git_dir, anchor))

    def test_absent_object(self):
        absent = hash_bytes(b"not in any repo " * 4, "sha1")
        self.assertFalse(has_object(self.git_dir, absent))
        support.git(self.repo, "gc", "-q")
        self.assertFalse(has_object(self.git_dir, absent))
        self.assertFalse(has_object(self.git_dir, "not-hex"))

    def test_unrelated_repo_does_not_share_history(self):
        other = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, other)
        support.git(other, "init", "-q")
        support.write(other, "a.txt", "another project\n")
        support.git(other, "add", "-A")
        support.git(other, "commit", "-q", "-m", "other")
        self.assertFalse(shares_history(os.path.join(other, ".git"), self.anchors))
        self.assertTrue(shares_history(self.git_dir, self.anchors))

    def test_pointer_file_of_a_linked_tree(self):
        extra = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, extra)
        tree = os.path.join(extra, "tree")
        support.git(self.repo, "worktree", "add", "-q", tree, "-b", "side")
        resolved = resolve_git_dir(os.path.join(tree, ".git"))
        self.assertEqual(os.path.normcase(os.path.normpath(resolved)),
                         os.path.normcase(os.path.normpath(self.git_dir)))


class TermTests(unittest.TestCase):
    def test_subwords(self):
        self.assertEqual(split_subwords("FileServer_v2"), ["File", "Server", "v", "2"])
        self.assertEqual(split_subwords("HTTPServer"), ["HTTP", "Server"])

    def test_classify(self):
        self.assertEqual(classify("engine", COMMON), "")
        self.assertEqual(classify("test_engine", COMMON), "compound")
        self.assertEqual(classify("zzqengine", COMMON), "unknown")
        self.assertEqual(classify("deadbeefcafe", COMMON), "")      # hex
        self.assertEqual(classify("a1b2c3d4e5", COMMON), "")        # mostly digits

    def test_file_names(self):
        self.assertEqual(distinctive_file_name("README.md", COMMON), 0)
        self.assertEqual(distinctive_file_name("notes.txt", COMMON), 0)
        self.assertEqual(distinctive_file_name("test_engine_notes.txt", COMMON), 1)
        self.assertEqual(distinctive_file_name("zzqengine.py", COMMON), 2)

    def test_families(self):
        families = term_families(["filesrv", "FileSrv_Core", "other"])
        self.assertEqual(families["filesrv_core"], "filesrv")
        self.assertEqual(families["other"], "other")

    def test_candidates_for_a_real_small_repo(self):
        repo, token, _ = build_project()
        self.addCleanup(support.remove_temp_dir, repo)
        terms, names, _notes = build_candidates(repo, vocabulary=COMMON)
        found = {c.term.lower() for c in terms}
        self.assertIn(os.path.basename(repo).lower(), found)
        self.assertIn(token.lower(), found)
        self.assertNotIn("engine", found)
        self.assertNotIn("project", found)
        self.assertEqual(terms[0].score, 100.0)
        self.assertIn(f"{token}_engine.py", names)
        self.assertNotIn("README.md", names)

    def test_term_file_round_trip_and_review_gate(self):
        repo, token, _ = build_project()
        self.addCleanup(support.remove_temp_dir, repo)
        folder = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, folder)
        terms, names, notes = build_candidates(repo, vocabulary=COMMON)
        text = render_term_file(repo, terms, names, notes)
        path = os.path.join(folder, "t.sweep-terms.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        loaded = load_term_file(path)
        self.assertEqual(loaded.terms, [c.term for c in terms])
        self.assertEqual(loaded.file_names, names)
        self.assertFalse(loaded.reviewed)
        with self.assertRaises(TermError):
            require_reviewed(loaded)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text.replace("# reviewed: no", "# reviewed: yes"))
        require_reviewed(load_term_file(path))

    def test_short_term_is_refused(self):
        folder = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, folder)
        path = os.path.join(folder, "t.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# reviewed: yes\n[terms]\nabc\n")
        with self.assertRaises(TermError):
            load_term_file(path)

    def test_real_background_vocabulary(self):
        words, sources = background_vocabulary()
        for word in ("import", "return", "network", "dashboard", "deploy"):
            self.assertIn(word, words)
        self.assertGreater(len(words), 5000)
        self.assertGreater(len(sources), 1)


class PlaceTests(unittest.TestCase):
    def test_known_places_exist(self):
        places = known_places()
        self.assertTrue(places)
        for _label, path in places:
            self.assertTrue(os.path.exists(path), path)

    def test_fixed_drives(self):
        drives = fixed_drives()
        self.assertTrue(drives)
        for drive in drives:
            self.assertTrue(os.path.isdir(drive), drive)

    def test_is_inside(self):
        home = str(Path.home())
        self.assertTrue(is_inside(os.path.join(home, "a", "b"), home))
        self.assertTrue(is_inside(home, home))
        self.assertFalse(is_inside(home + "-other", home))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.token, texts = build_project()
        self.addCleanup(support.remove_temp_dir, self.repo)
        self.work = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, self.work)
        self.out = os.path.join(self.work, "out")
        self.outside = os.path.join(self.work, "outside")
        support.write(self.outside, "renamed.txt", texts["module"])

    def test_trace_needs_a_scope_and_a_term_choice(self):
        self.assertEqual(main(["trace", self.repo, "--out", self.out,
                               "--no-terms"]), 2)
        self.assertEqual(main(["trace", self.repo, "--out", self.out,
                               "--scope", self.outside]), 2)

    def test_exact_copies_only(self):
        code = main(["trace", self.repo, "--out", self.out, "--no-terms",
                     "--scope", self.outside])
        self.assertEqual(code, 0)
        written = sorted(os.listdir(self.out))
        self.assertEqual(len(written), 2)
        with open(os.path.join(self.out, written[0]), encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["part"], "B")
        self.assertEqual(len(data["hits"]), 1)

    def test_terms_then_trace_with_the_review_gate(self):
        self.assertEqual(main(["terms", self.repo, "--out", self.out]), 0)
        term_file = os.path.join(self.out, os.listdir(self.out)[0])
        self.assertTrue(term_file.endswith(".sweep-terms.txt"))
        args = ["trace", self.repo, "--out", self.out, "--terms", term_file,
                "--scope", self.outside]
        self.assertEqual(main(args), 2)       # not reviewed yet
        with open(term_file, encoding="utf-8") as handle:
            text = handle.read()
        with open(term_file, "w", encoding="utf-8") as handle:
            handle.write(text.replace("# reviewed: no", "# reviewed: yes"))
        self.assertEqual(main(args), 0)
        endings = sorted(name.split(".", 1)[1] for name in os.listdir(self.out))
        self.assertEqual(endings, ["sweep-terms.txt", "sweep-trace.json",
                                   "sweep-trace.txt"])


if __name__ == "__main__":
    unittest.main()
