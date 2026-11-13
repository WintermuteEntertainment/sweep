# Sweep

Finds personal data inside a git repo, and traces of that repo elsewhere on
the machine. It reports what it finds. It never deletes, moves or changes
anything on its own.

Status: v0.2. Both parts work and report only. Offering cleanup actions is
planned for later. See `PLAN.md`.

Needs Python 3.11 or newer and git. Nothing to install.

    set PYTHONPATH=src

## Part A: what personal data is in this repo?

    python -m sweep repo <path to a git repo>

Looks for:

- This machine's own identity, read at scan time: login name, computer
  name, git name and email, local IP addresses, network adapter addresses
- Email addresses, IP addresses, MAC addresses, home-folder paths
- Private key blocks, passwords inside URLs, key and credential file names
- Terms from an optional private term file (`--terms FILE`)

Looks in: tracked, untracked and ignored files; every file version in git
history on all branches, tags, stashes and the reflog; commit messages;
commit author and committer identities; remote URLs.

## Part B: where else are traces of this repo?

Step 1. Ask for a term list:

    python -m sweep terms <path to a git repo>

Step 2. Open the file it names. Delete ordinary words, add anything
missing, and change `# reviewed: no` to `# reviewed: yes`. Sweep refuses to
search with a list nobody has read.

Step 3. Say where to look:

    python -m sweep trace <repo> --terms <term file> --scope <folder>

Other ways to say where: `--drive D`, `--known-places` (temp, Downloads,
Desktop, Recycle Bin, editor history, AI assistant folders), `--machine`
(every local fixed drive). Nothing is searched unless you say so.

It finds:

- Exact copies of any file version the repo holds or ever held, whatever
  the copy is called now
- Files with the same name as a distinctive file in the repo
- Files containing your reviewed terms
- Other folders that share the repo's git history

It never follows links or junctions, and never opens a file that exists
only in the cloud.

## Output

Everything goes to `sweep-reports` in your home folder: a `.txt` file to
read and a `.json` file with every finding. The reports contain the
personal data that was found, so keep them out of repos and shared folders.

## What it does not check

Secrets such as API keys (use a secret scanner as well), IPv6 addresses,
and the insides of archives, documents and other binary files. Every report
lists what was and was not searched. A clean report is not proof that
nothing is there.

## Test

    python -m unittest discover -s tests -t .
