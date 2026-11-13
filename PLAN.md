# Sweep: plan for the initial build

Status: APPROVED by the owner on 2026-11-12. v0.1 and v0.2 are built and
tested. v0.3 has not been started. See HANDOFF.md for the current state.
Written 2026-11-12.

## What it does

People leave things lying around. A project's files, and personal details
inside them, end up in the repo, in its git history, and in copies, logs and
notes scattered over other folders and drives. Sweep finds those things and
reports them. Later it can offer to clean them up.

It runs on one machine, on demand. It makes no network calls and uses no AI
service. It costs nothing to run except disk reads and time.

## Hard rules

1. Sweep only gathers information and then OFFERS actions. It never deletes,
   moves, redacts or rewrites anything on its own. There is no automatic mode
   and no flag that skips the offer.
2. The initial build has no delete, move or redact code at all. It reads and
   reports. Offers come in a later version, once the reports are trusted.
3. Reports contain the personal data they found, so they are written outside
   the scanned repo by default, and never inside any git repo.
4. Sweep cannot prove that nothing is left. Every report lists what was
   searched, what was skipped and why, so "nothing found" is never read as
   "nothing exists".
5. No background work. No indexing service, no scheduled scans. It runs when
   asked and stops.

## Two parts

    +==============================================================+
    | PART A: inside the repo                                      |
    |   "What personal data is in this repo?"                      |
    |   working tree, untracked and ignored files, git history,    |
    |   commit authors and messages, remotes                       |
    +==============================================================+
    | PART B: outside the repo                                     |
    |   "Where else on this machine are traces of this repo?"      |
    |   copies, renamed copies, logs, notes, exports, temp files   |
    |   on the folders and drives you choose                       |
    +==============================================================+

Part A runs first. Its findings feed Part B.

## Part A: the repo pass

### What gets scanned

- Tracked files in the working tree
- Untracked files and ignored files (these are often the worst offenders)
- Every version of every file in git history, on all branches and tags
- Stashes
- Commit author and committer names and emails, and commit messages
- Remote URLs in the repo's git config

### How it finds personal data without a hand-written list

The tool has to work for anyone, so it cannot ship with a list of names.
It uses four sources, in this order of trust:

1. Machine identity, gathered at scan time from the machine it runs on:
   login name, home folder path, computer name, git user name and email,
   local IP addresses, network adapter MAC addresses. These are exact values,
   so a match is a strong finding. This is the generic replacement for a
   personal identifier list.
2. Pattern detectors for things that look personal wherever they came from:
   email addresses, IP addresses (private ranges flagged separately from
   public), MAC addresses, home-folder paths on Windows, Linux and macOS,
   and key or credential file types (.env, .pem, .key, SSH private keys,
   password databases).
3. Secret scanning, handed to gitleaks if it is installed. Sweep does not
   reimplement it. If gitleaks is absent, the report says secrets were not
   checked.
4. An optional private term file, kept outside every repo, for anything the
   owner wants added (family names, device nicknames, street names).

### Output

A findings report with: file, line, commit (for history findings), which
detector fired, and the matched value. Two formats: JSON for the tool itself
and for Part B, and a readable text report for the person.

## Part B: the leftover pass

### The idea

Build a fingerprint of the repo, then search for that fingerprint in places
the person chooses. Three kinds of fingerprint, strongest first:

1. File content hashes. Every file in the working tree and in history gets
   a hash. Any file elsewhere with the same size and hash is an exact copy.
   Size is compared first, so most files are never read. Strong evidence,
   no false positives, but it misses edited copies.
2. Distinctive file names. Names in the repo that are unlikely to exist by
   chance elsewhere (not README.md, not main.py).
3. Distinctive terms. Words and identifiers that are likely specific to this
   repo: the repo name, package and module names, unusual identifiers, and
   the personal values Part A found. These catch what hashes cannot: logs,
   notes, exports, chat transcripts and edited copies.

### Choosing the terms

This is the part most likely to go wrong. A term that is too common buries
the real hits in noise. The approach:

- Collect candidate tokens from file names, identifiers and text.
- Drop anything in a built-in list of common English words and common
  programming words.
- Score the rest: longer and more unusual tokens score higher; tokens that
  appear in the repo name, package names or Part A findings score highest.
- Write the top candidates to a term list file.
- STOP. The person reviews and edits the list before any search runs.
  Sweep never searches with a term list nobody has looked at.

### Scope

The person says where to look. Nothing is searched by default.

    +----------------+------------------------------------------+
    | Scope          | Meaning                                  |
    +----------------+------------------------------------------+
    | folder         | one or more named folders                |
    | drive          | one or more whole drives                 |
    | machine        | all local fixed drives                   |
    | network        | named network shares (v0.3, see below)   |
    +----------------+------------------------------------------+

Always excluded: the repo being fingerprinted, operating system folders,
and files that exist only in the cloud (reading a cloud placeholder file
makes the sync client download it, so Sweep checks the file attributes and
skips them, and lists them as skipped).

A "known leftover places" list is offered per operating system as a cheap
first scope: temp folders, Downloads, Desktop, editor backup files, and the
working folders of AI coding assistants.

### Optional manifest

If a small manifest file exists listing files a session created outside the
repo, Sweep reads it as an extra hint. It never depends on one.

### Search engine

ripgrep when installed (fast, takes a file of fixed strings), with a plain
Python fallback. Binary files and files over a size limit are skipped by
the term search and listed as skipped. Hash matching still covers them.

### Output

A report of hits grouped by location, ranked by strength of evidence:
exact copy, then file name match, then number of distinct terms matched.

## What Sweep cannot see (stated in every report)

- Inside archives (zip, 7z, tar) and disk images
- Inside binary document formats for term search (docx, pdf, sqlite)
- Encrypted volumes that are not unlocked
- Cloud-only files
- Other machines, phones, and cloud accounts
- Copies that were both renamed and edited, if no chosen term survives

Some of these can be added later. The first two are the most worth it.

## Build order

    +=======+===================================================+
    | v0.1  | Part A, report only                               |
    |       | working tree + untracked + ignored + history      |
    |       | machine identity, pattern detectors, term file    |
    |       | JSON and text reports                             |
    +-------+---------------------------------------------------+
    | v0.2  | Part B, report only                               |
    |       | hashes, file names, term list with review step    |
    |       | scopes: folder, drive, machine                    |
    +-------+---------------------------------------------------+
    | v0.3  | Reach                                             |
    |       | network shares, archives, document formats,       |
    |       | gitleaks hand-off                                 |
    +-------+---------------------------------------------------+
    | v0.4  | Offers                                            |
    |       | per finding: move to quarantine folder, send to   |
    |       | Recycle Bin, redact (shown as a diff first),      |
    |       | or print the history-rewrite command to run by    |
    |       | hand. Every action asked, every action logged.    |
    |       | Pre-push check that blocks a push on findings.    |
    +=======+===================================================+

"Initial build" means v0.1 and v0.2. Each is checked against a real repo
before the next starts.

## Technology

- Python 3.11, standard library only for the core. No install step beyond
  Python and git.
- Optional helpers, used when present: ripgrep, gitleaks, git-filter-repo.
- Command line tool: `sweep repo <path>` for Part A,
  `sweep terms <path>` to build the term list,
  `sweep trace <path> --scope ...` for Part B.
- Windows first, written so Linux and macOS work without a rewrite.

## How it gets tested

No invented personal data. Options, to be decided:

- Run read-only against real repos on this machine and check the findings
  by hand.
- For values that must appear in a test, use only values reserved by
  standards for documentation (example.com, the 192.0.2.0/24 address range,
  the 00-00-5E-00-53-xx MAC range). These are published reserved values,
  not invented ones.

## Decisions on the open questions (owner, 2026-11-12)

1. Name: "sweep".
2. Test data: automated tests may use the standards-reserved documentation
   values. At least one test run must also be against a real repo.
3. Reports show matched values in full, for now.
4. Private repo now, public later.
5. Reports go to a `sweep-reports` folder in the user's home folder.

## Differences between this plan and v0.1 as built

- The gitleaks hand-off is not built (it is scheduled for v0.3, and
  gitleaks is not installed on the build machine). Every report says that
  secrets were not checked.
- IPv6 addresses are not detected yet.
- The command is `sweep repo <path>`. `sweep terms` and `sweep trace`
  belong to v0.2.

## Differences between this plan and v0.2 as built

- ripgrep is not used. Cloud-only files must never be opened, and ripgrep
  cannot skip a file by its Windows attributes, so it cannot be handed a
  folder. Sweep walks the folders itself and reads each file at most once,
  for both the exact-copy check and the term search.
- This machine's identity values found by Part A are NOT added to the term
  list. They appear all over the machine, so they cannot tell one repo's
  traces from anything else.
- The common-word list is built at run time from the text of Python's own
  standard library, plus a short built-in list. No word list is shipped.
- The review step is enforced: the term file is written with
  "reviewed: no" and `sweep trace` refuses it until a person changes that
  to "reviewed: yes".
- Added: other folders that share the repo's git history (clones) are
  reported as one line each and not searched file by file.
- Added: a `--no-terms` mode that looks for exact copies only.
- Not built: the optional manifest of files a session created outside the
  repo. Nothing writes such a manifest yet, so there is nothing to read.
- The network scope is still v0.3. A network share can be searched now by
  giving it as a `--scope` folder; `--machine` covers local fixed drives
  only.

## Web interface (agreed with the owner 2026-11-12, NOT BUILT)

Comes before v0.3. Brings forward the first offer (delete) from v0.4, for
Part B leftovers only.

### Server

- Python's built-in HTTP server. Plain HTML, JavaScript and CSS. Nothing
  to install.
- Listens on 127.0.0.1 only by default.
- LAN access is an option, and only with a password.

### Layout: one page, four sections in order

Each section unlocks when the one above it is done.

    +===============================================================+
    | SWEEP                          [repo: pick folder]   [Stop]   |
    +===============================================================+
    | 1  INSIDE THE REPO                                    [Run]   |
    |    Findings grouped by value, high first. Click to see each   |
    |    place. Filter: working tree / history only / ignored.      |
    |    Report only (see "Deleting" below).                        |
    +---------------------------------------------------------------+
    | 2  SEARCH WORDS                                               |
    |    Proposed list as checkboxes, with file count and reason.   |
    |    Untick ordinary words, add your own.                       |
    |                                  [I've reviewed this list]    |
    +---------------------------------------------------------------+
    | 3  WHERE TO LOOK                    (unlocks after review)    |
    |    Known places, drives, added folders. File count and time   |
    |    estimate shown before it runs. Recycle Bin unticked by     |
    |    default.                                          [Search] |
    +---------------------------------------------------------------+
    | 4  LEFTOVERS FOUND                                            |
    |    By folder, strong first. Tick files, then:                 |
    |    [Move to Recycle Bin]   [Delete permanently...]            |
    +---------------------------------------------------------------+
    | HISTORY                                                       |
    |    Saved reports, with a way to delete them (see below).      |
    +===============================================================+

### Naming

The user-facing name for "terms" is **Search words**, everywhere in the
interface. ("Terms" reads as "terms of service".) Code and file names may
keep "terms" until a separate rename.

### History, kept small on purpose

The point of the tool is to clean up, not to make more files. So:

- Results live in the page. Nothing is written unless the owner presses
  "Save report".
- The History section lists only reports the owner chose to save.
- Saved reports hold personal data and are themselves leftovers. History
  offers to delete them, with the same confirmation as section 4:
  - per report: a Delete button on each one
  - all at once: a "Delete all history" button, which lists what it will
    remove and needs the number of reports typed to confirm
- "Delete all history" removes only files Sweep itself wrote in its report
  folder (names ending in .sweep-report.*, .sweep-trace.* or
  .sweep-terms.txt). Anything else in that folder is left alone and listed.
- Should the reports be sent to the Recycle Bin, as in section 4, they
  are still on the machine. The confirmation says so, and offers permanent
  delete as the separate, typed-confirmation choice.

### Recycle Bin

- Not searched by default. A checkbox in section 3 includes it.
- Reason: files Sweep moved there would show up again in the next search.

### Deleting (hard rules carry over)

Sweep still never deletes on its own. Delete is an offer, per file, that
the owner confirms.

Applies everywhere:

- Nothing is pre-ticked, not even strong hits.
- Move to Recycle Bin is the default. Permanent delete is a separate
  button that needs the number of files typed to confirm.
- Each file is checked again just before the action. If its size or
  modified time changed since the search, it is left alone and reported.
- Every action is listed on the page; written to disk only if saved.

Three kinds of file, three rules (owner's decision, 2026-11-12):

    +---------------------+----------------------------------------------+
    | Leftovers outside   | Section 4, after the search words are        |
    | any repo            | reviewed. Tick and delete.                   |
    +---------------------+----------------------------------------------+
    | Files in the        | Section 1. Deleting IS allowed: without it   |
    | scanned repo        | the tool is not much use. Untracked and      |
    |                     | ignored files: same confirmation as above.   |
    |                     | Tracked files: a DANGER warning first,       |
    |                     | because removing the file leaves it in git   |
    |                     | history and the deletion still has to be     |
    |                     | committed. The warning says how to finish    |
    |                     | the job (see below).                         |
    +---------------------+----------------------------------------------+
    | Files in ANOTHER    | Never deletable from Sweep. No override. The |
    | git repo            | hit shows a warning: this file belongs to    |
    |                     | another repo, remove it there by hand, and   |
    |                     | how (see below).                             |
    +---------------------+----------------------------------------------+

What the warnings tell the person to do (Sweep shows the commands with the
real paths filled in, and runs none of them):

- Remove a tracked file from a repo, going forward:
  `git -C <repo> rm <file>`, then `git -C <repo> commit`.
- Untracked or ignored file in another repo: delete it in File Explorer or
  with `del`; git has nothing to undo.
- The text was committed earlier: it stays in history until history is
  rewritten (git-filter-repo), and anyone who already pulled it keeps it.
  Sweep explains this but never rewrites history itself.

### Still to settle when building

- Whether DANGER for tracked files is a per-file warning or a section-wide
  switch that is off on each page load. Leaning per file.
