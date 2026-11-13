# HANDOFF

Last updated: 2026-11-12

## Where things stand

- v0.1 (Part A, the repo pass, report only) is built and tested.
- v0.2 (Part B, the leftover pass, report only) is built and tested.
- v0.3 (network shares, archives, documents, gitleaks) is NOT started.
  Ask the owner before starting it.
- Repo created 2026-11-12 with `git init`. Branch `master`.
  First commit 3494bfa on 2026-11-12. `.claude/launch.json` is left out on purpose (unused).
- Remote: https://github.com/WintermuteEntertainment/sweep, PUBLIC since
  2026-11-12 (owner's decision), license GPL-3.0. Every commit was checked
  for personal data first; the only personal value is the owner's commit
  email, which the owner approved. Keep personal details out of every
  future commit, and run `sweep repo` on this repo before pushing.
- One focused safety review done 2026-11-12: two real defects found and
  fixed (see NOTES.md). No review of detector or term-scoring correctness.
- Web interface BUILT 2026-11-12 (design in PLAN.md, "Web interface").
  Start it with `python -m sweep web` (opens a browser on
  http://127.0.0.1:8765/). `--lan` asks for a password and also listens
  on the local network. Code: `src/sweep/web.py`, `src/sweep/delete.py`,
  `src/sweep/static/`.
- Checked in the browser pane on the invented "Morgan Reyes" scene: all
  four sections and the delete dialog work, no console errors. A stray
  "null" on the page was found and fixed.
- NOT verified: a real move to the Recycle Bin (tests replace that call so
  nothing lands in the real bin), LAN mode from another machine, light
  colour scheme, and a run on a large real folder through the page.
- Leftover from the browser check: the invented scene is still at
  `%TEMP%\sweep-test-n8zh79qj` (made by Claude, nothing personal). Remove
  it when convenient.
- Owner approved invented test data for this repo, framed as another
  person using the machine for their own repo and cleaning up after.

## Read first

1. `PLAN.md`: what the tool does, the hard rules, build order, decisions,
   and where the build differs from the plan
2. `NOTES.md`: decisions and the reasons behind them

## How to run it

No install is needed. From this folder, in PowerShell:

    $env:PYTHONPATH = 'src'

Part A, inside the repo:

    python -m sweep repo <repo>

Part B, outside the repo, in two steps with a review in between:

    python -m sweep terms <repo>
    (open the term file it names, delete ordinary words, add anything
     missing, change "# reviewed: no" to "# reviewed: yes")
    python -m sweep trace <repo> --terms <term file> --scope <folder>

Where to look, for `trace` (nothing is searched by default):
`--scope FOLDER` (repeatable), `--drive LETTER`, `--known-places`,
`--machine`. Use `--no-terms` in place of `--terms` to look for exact
copies only.

Everything is written to `sweep-reports` in the home folder:
`.sweep-report.*` (Part A), `.sweep-terms.txt`, `.sweep-trace.*` (Part B).

## How to test it

    python -m unittest discover -s tests -t .

110 tests, all passing on 2026-11-12 (Windows 11, Python 3.11.9, git 2.53).
About 50 seconds: each test builds small git repos in the system temp
folder and removes them afterwards.

## Checked against real data (2026-11-12, read-only)

Part A, on a real repo: 33 tracked, 1 untracked and 40 ignored files,
20 commits, 60 file versions, 5.6 seconds. For four exact values the count
in tracked files equalled an independent `git grep` count. One history-only
finding was confirmed with `git show`. `git status` and the timestamp of
`.git/index` were identical before and after.

Part B, on the same repo with a 17-term list pruned for the test:

    +------------------+---------+--------+--------------------------+
    | Folder searched  | Files   | Hits   | Time                     |
    +------------------+---------+--------+--------------------------+
    | a Downloads      | 1,797   | 28     | 70 s first run (before   |
    | folder           |         |        | speed fixes), 3 s after  |
    | a folder of      | 1,770   | 5      | 54 s first run, 2.4 s    |
    | other repos      |         |        | when repeated            |
    +------------------+---------+--------+--------------------------+

- Two exact-copy findings were confirmed independently: one with
  `git hash-object` plus `git cat-file -e`, one with `cmp`.
- The walker's totals (362 folders, 1,770 files) equalled an independent
  `os.walk` count.
- The hits were identical before and after each speed change.

## Speed: what is known and what is not

- Opening a file for the first time costs about 30 ms on the build
  machine. Repeat runs are over 10 times faster. The cause was not
  measured (antivirus scanning on first open is a guess).
- So a first run costs roughly 30 ms per file that has to be opened.
  Files of binary types and files whose size matches nothing are not
  opened.
- Not yet run on a whole drive, on the known places, or on a folder with
  cloud-only files. Measure before estimating: a drive with a million
  openable files could take hours on a first run.

## What is in the code

    src/sweep/identity.py     reads this machine's identity at scan time
    src/sweep/detectors.py    exact values and patterns (Part A)
    src/sweep/gitsrc.py       read-only git access to the scanned repo
    src/sweep/scan.py         Part A scan
    src/sweep/fingerprint.py  a hash for every file version in the repo
    src/sweep/terms.py        common-word list, term proposals, term file
    src/sweep/gitobjects.py   reads another folder's .git as plain files
    src/sweep/places.py       drives, known leftover places, exclusions
    src/sweep/trace.py        Part B search
    src/sweep/report.py       Part A reports, shared file writing
    src/sweep/tracereport.py  Part B text report
    src/sweep/cli.py          command line
    tests/                    unittest, standard library only

## Known gaps

Part A:
- Secrets are not checked (gitleaks hand-off is planned for v0.3).
- IPv6 addresses are not detected.
- Binary files, archives and documents are not opened.
- A Windows user folder name containing a space is cut at the space.
- Version numbers with four parts can be reported as public IP addresses.
- For history findings, at most five commits are recorded per file version.

Part B:
- The proposed term list still contains ordinary technical words. The
  review step is not optional.
- Terms match only ASCII letters without regard to case. A term with
  accented letters matches in its exact case only.
- A copy that was edited and no longer holds any term is not found.
- Clone detection reads loose objects and version 2 pack indexes. It does
  not follow "alternates" files or multi-pack indexes.
- Junctions and symbolic links are never followed. They are listed, so the
  target can be given as its own `--scope`.
- No automated test covers a cloud-only file, because the tests cannot
  create one. The attribute check was confirmed by listing a real synced
  folder, but no cloud-only file has been through a real run.
- Not run on Linux or macOS.

## Next step

Owner decides: commit; whether to get an independent review; whether to
run Part B on the known places or a whole drive; then v0.3.

## Standing rules for anyone working here

- The tool only gathers information and offers actions. It never deletes,
  moves or rewrites anything by itself. No flag bypasses this.
- No invented personal data in tests or docs. See `tests/__init__.py` for
  which fixed values are allowed. Project names in tests come from the
  random name of the temp folder.
- Keep the owner's personal details (names, hosts, addresses, paths,
  project names) out of this repo. It is meant to become a generic tool.
  Run `sweep repo` on this repo before any commit that will be published.
- Reports and term files are never written inside a git repo.
- Sweep does not run git inside folders it finds while searching.
- Keep this file and `NOTES.md` current with every change.

## Environment checked 2026-11-12

- Python 3.11.9: installed
- git 2.53.0: installed
- ripgrep 14.1.1: installed, not used (see PLAN.md)
- gitleaks: not installed
- git-filter-repo: not installed
