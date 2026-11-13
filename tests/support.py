"""Helpers shared by the tests."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile

GIT_TEST_IDENTITY = [
    "-c", "user.name=Example Author",
    "-c", "user.email=author@example.com",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
]


def make_temp_dir() -> str:
    return tempfile.mkdtemp(prefix="sweep-test-")


def remove_temp_dir(path: str) -> None:
    """Remove a folder this test run created. Git marks its object files
    read-only, so clear that flag when a removal fails."""
    def retry(function, target, _error):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=retry)


def git(repo: str, *args: str) -> str:
    done = subprocess.run(
        ["git", *GIT_TEST_IDENTITY, "-C", repo, *args],
        capture_output=True, stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf-8", errors="replace"))
    return done.stdout.decode("utf-8", errors="replace")


def read_only_status(repo: str) -> str:
    """`git status` without its usual side effect of rewriting the index
    file, so a test can use it to check that nothing was written."""
    done = subprocess.run(
        ["git", "--no-optional-locks", "-C", repo, "status", "--porcelain",
         "--ignored"],
        capture_output=True, stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf-8", errors="replace"))
    return done.stdout.decode("utf-8", errors="replace")


def write(repo: str, relative: str, text: str, encoding: str = "utf-8") -> None:
    full = os.path.join(repo, relative)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
