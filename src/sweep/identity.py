"""Read this machine's own identifying values at scan time.

These are exact values (login name, computer name, git identity, local IP
addresses, network adapter MAC addresses), so finding one in a repo is a
strong sign of personal data. Nothing here is stored in the tool: it is read
fresh from the machine each run.
"""
from __future__ import annotations

import getpass
import os
import platform
import re
import socket
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

# Names shorter than this match too many ordinary words to be useful.
MIN_NAME_LENGTH = 3

_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}")


@dataclass(frozen=True)
class IdentityValue:
    kind: str
    value: str


def normalize_mac(text: str) -> str:
    """Return a MAC address as 12 lowercase hex digits."""
    return re.sub(r"[^0-9A-Fa-f]", "", text).lower()


def _run(args: list[str], cwd: str | None = None) -> str:
    try:
        done = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", errors="replace").strip()


def _mac_addresses() -> set[str]:
    found: set[str] = set()
    node = uuid.getnode()
    # uuid.getnode() returns a random number with the multicast bit set
    # when it cannot read a real address. Ignore that case.
    if not (node >> 40) & 1:
        found.add(f"{node:012x}")
    system = platform.system()
    if system == "Windows":
        for match in _MAC_RE.finditer(_run(["getmac", "/fo", "csv", "/nh"])):
            found.add(normalize_mac(match.group(0)))
    elif system == "Linux":
        net = Path("/sys/class/net")
        if net.is_dir():
            for entry in net.iterdir():
                try:
                    text = (entry / "address").read_text().strip()
                except OSError:
                    continue
                if _MAC_RE.fullmatch(text):
                    found.add(normalize_mac(text))
    found.discard("000000000000")
    found.discard("ffffffffffff")
    return found


def _local_ipv4() -> set[str]:
    found: set[str] = set()
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        addresses = []
    for address in addresses:
        if not address.startswith("127."):
            found.add(address)
    return found


def gather(repo: str | None = None) -> tuple[list[IdentityValue], list[str]]:
    """Return (identity values, notes about values that were left out)."""
    values: list[IdentityValue] = []
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str | None, *, name_like: bool = False) -> None:
        if not value:
            return
        value = value.strip()
        if not value:
            return
        if name_like and len(value) < MIN_NAME_LENGTH:
            notes.append(
                f"{kind} is shorter than {MIN_NAME_LENGTH} characters and was "
                "not searched for, because it would match ordinary words"
            )
            return
        key = (kind, value.lower())
        if key in seen:
            return
        seen.add(key)
        values.append(IdentityValue(kind, value))

    try:
        login = getpass.getuser()
    except Exception:  # getpass raises several unrelated error types
        login = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    add("login-name", login, name_like=True)

    try:
        home_name = Path.home().name
    except (OSError, RuntimeError):
        home_name = ""
    if home_name and home_name.lower() != login.lower():
        add("home-folder-name", home_name, name_like=True)

    computer_names = {
        socket.gethostname(),
        platform.node(),
        os.environ.get("COMPUTERNAME", ""),
    }
    for name in sorted(n for n in computer_names if n):
        short = name.split(".")[0]
        add("computer-name", short, name_like=True)
        if short.lower() != name.lower():
            add("computer-name", name, name_like=True)

    add("git-name", _run(["git", "config", "--global", "user.name"]), name_like=True)
    add("git-email", _run(["git", "config", "--global", "user.email"]))
    if repo:
        add("git-name", _run(["git", "config", "user.name"], cwd=repo), name_like=True)
        add("git-email", _run(["git", "config", "user.email"], cwd=repo))

    for address in sorted(_local_ipv4()):
        add("local-ip", address)
    for mac in sorted(_mac_addresses()):
        add("mac-address", ":".join(mac[i : i + 2] for i in range(0, 12, 2)))

    return values, notes
