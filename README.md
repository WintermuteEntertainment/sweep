# Sweep

Clean up after a project before you share it.

Sweep looks for personal data inside a git repository, then looks around
the rest of your computer for the copies, notes, logs and drafts the
project left behind. It shows you what it found and where. Nothing is
deleted unless you choose the file and confirm.

People are messy. A project leaves traces in Downloads, on the Desktop, in
temp folders, in editor backups, in an old copy of the repo on another
drive. Sweep is for finding them.

> Status: early (v0.1). Windows is the main platform so far. It runs on
> Linux and macOS, but the Recycle Bin and "Show in folder" are Windows
> only, and it has had far less testing there.

## What it does

**1. Inside the repo.** Searches the files you can see, the untracked and
ignored files you might not, every old version in the git history,
commit messages, commit authors and remote addresses. It looks for:

- your own identity, read from the machine at the moment it runs: login
  name, home folder path, computer name, git name and email, local IP
  addresses, network adapter addresses
- email addresses, IP addresses, MAC addresses and home folder paths in
  general
- private key blocks, passwords inside URLs, and file names like `.env`,
  `id_rsa` or `*.pem`
- any extra words you give it

**2. Search words.** Sweep proposes a list of words and file names that
are likely to belong only to this project. You untick the ordinary ones
and add your own. Nothing is searched until you mark the list as reviewed.

**3. Where to look.** You choose: common leftover places (temp, Downloads,
Desktop, editor history, AI assistant folders), whole drives, or any
folder. It can count the files first and estimate how long a search will
take.

**4. Leftovers found.** Results are ranked by how sure Sweep is:

- **exact copies** of any file the repo holds or ever held, whatever the
  copy is called now
- files with the **same name** as a distinctive file in the repo
- files containing your **search words**
- other folders that share the repo's git history

Every result shows the matching text highlighted, with links to view the
file read-only or show it in its folder.

## Safety

Sweep is meant to be safe to point at your whole computer.

- **It reads.** Scanning and searching never change anything.
- **Deleting is always your choice.** You tick each file. Nothing is
  pre-ticked. Moving to the Recycle Bin is the default; deleting
  permanently means typing the number of files to confirm.
- **Each file is checked again just before it is deleted.** If it changed
  since the search, moved, or became a link, it is left alone.
- **Files in other git repos are never deleted.** A mention of your project
  in someone else's repo is shown with the steps to remove it yourself.
- **Files git tracks in your repo** need a separate warning accepted first,
  because deleting them leaves them in the history.
- **It never follows links or junctions**, and never opens files that live
  only in the cloud (opening one would download it).
- **It never runs git in folders it finds**, and switches off the git
  settings that can start other programs in the repo you scan.
- **It saves nothing unless you ask.** Results live in the page. Saved
  reports contain the personal data they found, so they can be deleted
  from the page too.

It cannot prove that nothing is left. Every report lists what it searched
and what it skipped.

## Run it

Needs Python 3.11 or newer and git. There is nothing to install.

```
git clone https://github.com/WintermuteEntertainment/sweep.git
cd sweep
```

Windows (PowerShell):

```
$env:PYTHONPATH = 'src'
python -m sweep web
```

Linux or macOS:

```
PYTHONPATH=src python3 -m sweep web
```

Your browser opens at `http://127.0.0.1:8765/`. Pick a repo and work down
the page.

### On your local network

```
python -m sweep web --lan
```

This asks you to set a password and also listens on your network.
Traffic is not encrypted, so use it only on a network you trust. Without
`--lan`, Sweep only accepts connections from the same computer.

### From the command line

The same scans without the page. These write reports to `sweep-reports`
in your home folder and never delete anything.

```
python -m sweep repo  <repo>
python -m sweep terms <repo>
python -m sweep trace <repo> --terms <reviewed term file> --scope <folder>
```

`trace` also takes `--drive D`, `--known-places`, `--machine` and
`--no-terms` (exact copies only). Run any command with `--help` for the
rest.

## What it does not check

- secrets such as API keys and tokens (use a secret scanner such as
  [gitleaks](https://github.com/gitleaks/gitleaks) as well)
- IPv6 addresses
- the inside of archives, documents and databases (exact copies of them are
  still found)
- encrypted volumes, files that exist only in the cloud, other machines,
  phones and online accounts
- copies that were both renamed and edited so that none of the search
  words survived

## How it works, briefly

- Exact copies are found with git's own content hashes. Every old version
  in history already has one, so the history costs nothing to fingerprint.
  A file elsewhere is only read if its size matches something in the repo.
- Proposed search words are compared against a list of ordinary words
  built on the spot from the text of Python's own standard library, so no
  word list is shipped.
- Each file is read at most once, for both checks. Files of binary types
  are only read when their size makes them a possible exact copy.

## Tests

```
python -m unittest discover -s tests -t .
```

Standard library only. The tests build small repos in your temp folder and
remove them afterwards. Some use your machine's real computer name or
network address to check that matching works, without storing them. The
end-to-end test uses an invented person and project. The tests never touch
your real Recycle Bin.

## License

GPL-3.0. See [LICENSE](LICENSE).
