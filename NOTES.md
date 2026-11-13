# NOTES

Decisions and reasoning, newest first.

## 2026-11-12: web interface built

- Python's ThreadingHTTPServer, plain HTML/CSS/JS, no libraries.
- Security: Host header must name the server (blocks DNS rebinding);
  every API call needs a random per-start token embedded in the page
  (blocks other web sites); LAN mode needs a password (8+ characters),
  sets an HttpOnly SameSite=Strict cookie, and waits 1 s after a wrong
  password. LAN traffic is not encrypted; the console says so.
- The page never sends a path to delete. It sends ids of files the
  server listed; the server checks each file again (still there, not a
  link, not cloud-only, same size and modified time, which repo owns it).
- Files in another repo cannot be chosen at all, and the server refuses
  them even if a request names one. The page shows the manual git steps.
- Tracked files in the scanned repo need the DANGER warning accepted per
  file (decided: per file, not a section-wide switch).
- Recycle Bin uses the Windows shell with "warn before permanent delete",
  so a file too big for the bin triggers a Windows prompt on screen rather
  than silent destruction. Not available on Linux/macOS yet.
- Long jobs run one at a time in a thread; the page asks for progress
  every 0.6 s only while a job runs. Stop works for counting and
  searching; a Part A scan cannot be stopped part way (it is fast).
- Results stay in memory. Nothing is written unless "Save" is pressed.
  History deletes only files named like Sweep's own reports.
- Tests use the invented "Morgan Reyes" / "lantern-budget" scene, approved
  by the owner, and replace the Recycle Bin call so the real bin is never
  touched.

## 2026-11-12: web interface design agreed

- One page with a section per phase, not separate screens.
- "Search words" replaces "terms" in anything a person reads: "terms"
  sounds like terms of service.
- History stays, but small and opt-in: results live in the page, and only
  reports the owner saves are kept. The tool's purpose is cleaning up, so
  it should not leave its own files lying around, and it offers to delete
  them too.
- Recycle Bin is not searched by default, because files moved there by
  Sweep would be found again. A checkbox includes it.
- Delete arrives early, for leftovers outside the repo only, as an offer
  per file with Recycle Bin as the default.
- REVISED the same day: deleting inside the scanned repo IS allowed,
  since a cleanup tool that cannot clean the repo is not much use.
  Untracked and ignored files get the normal confirmation; tracked files
  get a DANGER warning first (history keeps them, and the deletion still
  needs a commit).
- Files in any OTHER git repo can never be deleted from Sweep, with no
  override. The hit says to remove it there by hand and shows how. Sweep
  shows commands, never runs them.
- History gets a Delete button per report and a "Delete all history"
  button. The second touches only files Sweep wrote (by name pattern), so
  a stray file in the report folder is never caught by it.
- Full design: PLAN.md, "Web interface".

## 2026-11-12: safety review (one focused pass)

A multi-agent review was tried first and stopped by the owner: it used a
lot of usage and no reviewer finished. A single pass over the hard rules
was done instead. Two defects were proven with experiments in temp
folders, fixed, and given regression tests:

1. Part A followed junctions inside ignored folders. `os.walk` in Python
   3.11 does not treat junctions as links, so a junction under an ignored
   folder led the scan out of the repo and it read files there. Fixed by a
   scandir walker that never follows links, junctions or any reparse point
   except cloud placeholders, and does not list cloud-only folders.
2. A scanned repo's own git settings could start a program. With
   `core.fsmonitor` set to a script, the scan ran it. Fixed by switching off
   fsmonitor, hooks path, external diff, pager and signature checks on
   every git call. Risk was real for untrusted repos.

Also tightened, without a separate proof:

- Every reparse point except cloud placeholders is now treated as a link,
  in both parts (was: symbolic links and junctions only).
- The term proposer and the fingerprint skip links and cloud-only files
  in the working tree (the proposer did not check before).
- Part B checks whether a search root itself is a cloud-only folder.
- A cloud-only folder is recognised by all three cloud attributes (was:
  one).

Checked and found sound: reports and term files are opened in "x" mode
and refused inside a git repo; the scanned repo is never written (index
timestamp test); Part B never runs git in folders it finds; clone
detection only reads files.

Not covered by this pass: correctness of detectors and term scoring, and
the Windows edge cases listed in HANDOFF.md.

## 2026-11-12: v0.2 built

- Exact copies are found with git's own content hash. Every file version
  in history already has one, so history is fingerprinted without reading
  it. Working-tree files are hashed the same way as they sit on disk, which
  also covers files whose line endings git changed on checkout.
- Size is checked before anything is read. A file whose size matches no
  file version in the repo cannot be a copy and is not opened for that.
- Files under 32 bytes are not fingerprinted: empty files and one-line
  stubs are identical all over a disk by chance.
- A copy of a file that many projects share (a license, `.gitignore`), or
  of a file under 128 bytes, is rated weak.
- Each file is read at most once, for both checks. The first 8 KB are read
  first; a binary file that is not a size match is dropped there.
- Files of binary types (images, video, archives, programs, documents) are
  not opened for the term search at all. They are still checked for exact
  copies. Reason: opening files is the slow part of a first run.
- ripgrep was dropped. It cannot skip a file by Windows attributes, so a
  folder handed to it could make a sync client download cloud-only files.
- Cloud-only files (offline, recall-on-open, recall-on-data-access) are
  never opened. A folder marked recall-on-open is not listed.
- Symbolic links and junctions are never followed, to avoid loops and
  counting things twice. Junctions are recognised by their reparse tag,
  because Python 3.11 does not call them links. A scope that is itself a
  junction is resolved first, so it can still be searched.
- Common words come from Python's standard library text, read at run
  time, plus a short built-in list. This keeps the tool free of shipped
  word lists. It still lets ordinary technical words through, which is why
  the review step is enforced and why the Part B report shows how many
  files each term hit.
- A plain ending on a common word (s, es, ed, ing, er, ers, ly) is still a
  common word.
- Folder names starting with a dot do not boost a term: they name tools,
  not the project.
- Terms that contain another term count once as evidence, so one
  occurrence cannot look like two.
- Strength of a hit: strong = an exact copy, or a matching file name plus
  a term, or three separate terms. Medium = a matching file name, two
  terms, or the repo's own name. Weak = anything less.
- Clones are detected by reading the other folder's object store as plain
  files (loose objects and pack indexes). Sweep does not run git there:
  a repo's own settings can make git start other programs, which is why
  git itself refuses to run in a folder owned by someone else.
- A folder that shares the repo's history is reported as one line and not
  searched, because every file in it would be an exact copy.
- The repo itself, its linked working trees, the report folder, the term
  file and operating system folders are never searched.
- The Recycle Bin is one of the known places. Deleted copies are exactly
  the kind of leftover this tool is for.
- A flaky test was found and fixed: plain `git status` rewrites the index
  file, so the tests that check "nothing was written" now use
  `git --no-optional-locks status`.

## 2026-11-12: v0.1 built

- Plan approved by the owner. Answers to the open questions are recorded
  in PLAN.md.
- Standard library only. Tests use `unittest`, so nothing has to be
  installed to run them.
- History is read with one `git log --raw` call (which file versions each
  commit introduced) and one `git cat-file --batch` process (their
  content). Each file version is read once, however many commits carry it.
- History covers all branches, tags, stashes and the reflog. Reflog-only
  commits are not pushed, but they are still personal data in the repo
  folder, so they are reported.
- Every git call uses `--no-optional-locks`, so git does not refresh its
  index as a side effect. A test checks the index timestamp is unchanged.
- Exact machine values are matched whole, ignoring case, and not inside a
  longer word. Names shorter than 3 characters are not searched for.
- A machine's own IP or MAC address is rated high; any other private IP or
  MAC is medium; documentation-reserved values are low and marked
  "reserved".
- Shared address space (100.64.0.0/10) is treated as private, because some
  VPNs hand out addresses from it.
- Ignored files are searched, except folders of downloaded dependencies and
  caches (node_modules, .venv, __pycache__ and similar). Those are listed
  in the report as skipped.
- Reports list findings grouped by value, because "which values are in
  here, and where" is the question a person asks first.
- In the text report, a finding in a file version that is still in the
  current commit is counted once (in the working tree), not twice.
- The exit code is 0 when the scan completes, whatever it found, and 2 on
  an error. A "fail when there are findings" option can come with the
  pre-push check in v0.4.
- Test values beyond the documentation ranges: the first address of each
  private block (10.0.0.0, 172.16.0.0, 192.168.0.0, 100.64.0.0), taken
  from RFC 1918 and RFC 6598, to test the private-address rule. Told to
  the owner the same day.

## 2026-11-12: project started

- Origin: a manual cleanup of another repo before publishing it. The work
  was: clean the repo, then look through the rest of the file system for
  anything personal left behind. Sweep automates that.
- Decision: generic tool, usable by anyone. No built-in personal identifier
  list and none derived from one person's cleanup session.
- Decision: two parts. A looks inside the repo (working tree, history).
  B looks outside it (other folders and drives).
- Decision (hard rule): gather and offer only. Never delete on its own.
- Idea adopted for Part B: when the repo is first scanned, build a list of
  terms likely to be specific to it, then search for those terms elsewhere.
  Scope is configurable: folder, drive, machine, network.
- Added to that idea:
  - File hashes alongside terms. Hashes find exact copies with no false
    positives; terms find edited copies, logs and notes. Each covers what
    the other misses.
  - The machine's own identity (login name, home path, computer name, git
    identity, IPs, MACs) read at scan time. This gives exact personal
    values to look for without anyone writing a list.
  - A review step: the person sees and edits the term list before any
    search runs.
- A manifest of files created outside the repo is supported as a hint,
  never required. People are messy; the tool cannot assume a record exists.
- Known limit, accepted: the tool cannot prove absence. Reports state what
  was and was not searched.
- Secret scanning is handed to gitleaks instead of rebuilt.
- Cloud-only placeholder files are skipped, because reading them triggers
  a download.
