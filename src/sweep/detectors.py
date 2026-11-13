"""Detectors: find things in text that look personal.

Two kinds:
  1. Exact values: this machine's identity and the owner's private term file.
  2. Patterns: email addresses, IP addresses, MAC addresses, home-folder
     paths, private key blocks, credentials inside URLs.

Values that standards reserve for documentation (example.com, 192.0.2.0/24
and so on) are still reported, but marked "reserved" with low severity.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .identity import IdentityValue, normalize_mac

HIGH = "high"
MEDIUM = "medium"
LOW = "low"


@dataclass(frozen=True)
class Match:
    detector: str
    severity: str
    value: str
    start: int
    end: int
    reserved: bool = False
    note: str = ""


# --- patterns -------------------------------------------------------------

_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+-]*)@((?:[A-Za-z0-9-]+\.)+([A-Za-z]{2,24}))"
    r"(?![A-Za-z0-9-])"
)
# "icon@2x.png" and similar are file names, not addresses.
_NOT_A_TLD = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp",
    "css", "js", "ts", "json", "html", "py", "md", "txt",
}
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_TLDS = ("example", "test", "invalid", "localhost")

_IPV4 = re.compile(
    r"(?<![\w.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?!\w|\.\d)"
)
_DOC_NETS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
]
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]
_SHARED_NET = ipaddress.ip_network("100.64.0.0/10")
_LINK_LOCAL_NET = ipaddress.ip_network("169.254.0.0/16")

_MAC = re.compile(
    r"(?<![0-9A-Fa-f:-])"
    r"([0-9A-Fa-f]{2}(?:([:-])[0-9A-Fa-f]{2})(?:\2[0-9A-Fa-f]{2}){4})"
    r"(?![:-]?[0-9A-Fa-f])"
)
_MAC_DOC_PREFIX = "00005e0053"

_SEP = r"(?:\\{1,2}|/)"
_WIN_HOME = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:" + _SEP + r"Users" + _SEP
    + r"([^\\/\s\"'<>|:*?,;)\]}]+)",
    re.IGNORECASE,
)
_POSIX_HOME = re.compile(
    r"(?<![\w.:-])(?:(?:/mnt)?/[A-Za-z])?/(?:home|Users)/"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)"
)
# Folder names under a home root that are not a person.
_NOT_A_USER = {
    "public", "default", "shared", "all", "user", "username", "you",
    "yourname", "name", "me",
}

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

_URL_CREDENTIALS = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^/\s:@\"'<>]+:[^/\s@\"'<>]+@[^\s\"'<>/]+",
    re.IGNORECASE,
)


def classify_ipv4(text: str) -> tuple[str, str, bool, str] | None:
    """Return (detector, severity, reserved, note), or None to ignore."""
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return None
    if int(address) >= int(ipaddress.IPv4Address("240.0.0.0")):
        return None  # reserved block, broadcast and netmask-like values
    for net in _DOC_NETS:
        if address in net:
            return ("ip-documentation", LOW, True,
                    "reserved for documentation")
    for net in _PRIVATE_NETS:
        if address in net:
            return ("ip-private", MEDIUM, False, "private network address")
    if address in _SHARED_NET:
        return ("ip-private", MEDIUM, False,
                "shared address space (carrier NAT, some VPNs)")
    if address in _LINK_LOCAL_NET:
        return ("ip-link-local", LOW, False, "link-local address")
    return ("ip-public", LOW, False,
            "public address, or a version number that looks like one")


def _email_match(m: re.Match[str]) -> Match | None:
    local, domain, tld = m.group(1), m.group(2).lower(), m.group(3).lower()
    if tld in _NOT_A_TLD:
        return None
    if local.lower() == "git":
        return None  # "git@host:owner/repo" is a remote address
    reserved = (
        tld in _RESERVED_TLDS
        or any(domain == d or domain.endswith("." + d) for d in _RESERVED_DOMAINS)
    )
    return Match(
        "email", LOW if reserved else MEDIUM, m.group(0), m.start(), m.end(),
        reserved=reserved,
        note="reserved for documentation" if reserved else "",
    )


def _home_match(m: re.Match[str]) -> Match | None:
    name = m.group(1)
    if name.lower() in _NOT_A_USER or name[0] in "$%{<":
        return None
    return Match("home-path", MEDIUM, m.group(0), m.start(), m.end(),
                 note=f"home folder of '{name}'")


# --- file names ------------------------------------------------------------

_SENSITIVE_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd",
}
_SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".pfx", ".p12", ".ppk", ".kdbx", ".keystore", ".jks",
}
_ENV_TEMPLATE_WORDS = ("example", "sample", "template", "dist")


def sensitive_filename(path: str) -> str | None:
    """Return a reason if the file name suggests keys or credentials."""
    name = PurePosixPath(path.replace("\\", "/")).name
    lower = name.lower()
    if lower in _SENSITIVE_NAMES:
        return "file name suggests a private key or stored credentials"
    if lower == ".env" or lower.startswith(".env."):
        if any(word in lower for word in _ENV_TEMPLATE_WORDS):
            return None
        return "environment file, often holds passwords and keys"
    for suffix in _SENSITIVE_SUFFIXES:
        if lower.endswith(suffix):
            return f"'{suffix}' files usually hold keys or passwords"
    return None


# --- the matcher -----------------------------------------------------------

def load_terms(path: str) -> list[str]:
    """Read a private term file: one term per line, '#' starts a comment."""
    terms: list[str] = []
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            term = line.strip()
            if term and not term.startswith("#"):
                terms.append(term)
    return terms


class Matchers:
    """Everything needed to scan a piece of text."""

    def __init__(self, identity: list[IdentityValue], terms: list[str]):
        self._local_ips = {v.value for v in identity if v.kind == "local-ip"}
        self._macs = {
            normalize_mac(v.value) for v in identity if v.kind == "mac-address"
        }
        literals: list[tuple[str, str]] = [
            (v.kind, v.value)
            for v in identity
            if v.kind not in ("local-ip", "mac-address")
        ]
        literals += [("user-term", term) for term in terms]
        # Longest first, so the longest literal wins where two overlap.
        literals.sort(key=lambda item: len(item[1]), reverse=True)
        self._literal_kinds: dict[str, tuple[str, str]] = {}
        parts: list[str] = []
        for index, (kind, value) in enumerate(literals):
            group = f"g{index}"
            self._literal_kinds[group] = (kind, value)
            parts.append(f"(?P<{group}>{re.escape(value)})")
        self._literal_re = (
            re.compile(
                r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            if parts
            else None
        )

    def scan(self, text: str) -> list[Match]:
        literal: list[Match] = []
        if self._literal_re is not None:
            for m in self._literal_re.finditer(text):
                kind, _ = self._literal_kinds[m.lastgroup or ""]
                note = (
                    "from the private term file"
                    if kind == "user-term"
                    else "matches this machine"
                )
                literal.append(
                    Match(kind, HIGH, m.group(0), m.start(), m.end(), note=note)
                )

        general: list[Match] = []
        for m in _EMAIL.finditer(text):
            found = _email_match(m)
            if found:
                general.append(found)
        for m in _IPV4.finditer(text):
            value = m.group(1)
            if value in self._local_ips:
                general.append(Match("local-ip", HIGH, value, m.start(1),
                                     m.end(1), note="matches this machine"))
                continue
            kind = classify_ipv4(value)
            if kind:
                detector, severity, reserved, note = kind
                general.append(Match(detector, severity, value, m.start(1),
                                     m.end(1), reserved=reserved, note=note))
        for m in _MAC.finditer(text):
            value = m.group(1)
            digits = normalize_mac(value)
            if digits in ("000000000000", "ffffffffffff"):
                continue
            if digits in self._macs:
                general.append(Match("mac-address", HIGH, value, m.start(1),
                                     m.end(1), note="matches this machine"))
            elif digits.startswith(_MAC_DOC_PREFIX):
                general.append(Match("mac-documentation", LOW, value,
                                     m.start(1), m.end(1), reserved=True,
                                     note="reserved for documentation"))
            else:
                general.append(Match("mac-address", MEDIUM, value, m.start(1),
                                     m.end(1)))
        for pattern in (_WIN_HOME, _POSIX_HOME):
            for m in pattern.finditer(text):
                found = _home_match(m)
                if found:
                    general.append(found)
        for m in _PRIVATE_KEY.finditer(text):
            general.append(Match("private-key", HIGH, m.group(0), m.start(),
                                 m.end(), note="private key block"))
        for m in _URL_CREDENTIALS.finditer(text):
            general.append(Match("url-credentials", HIGH, m.group(0),
                                 m.start(), m.end(),
                                 note="user name and password inside a URL"))

        # A pattern match that sits inside an exact-value match says nothing
        # new, so drop it. One that contains an exact value is kept: an email
        # address containing the login name is its own finding.
        kept = [
            g for g in general
            if not any(l.start <= g.start and g.end <= l.end for l in literal)
        ]
        result = literal + kept
        result.sort(key=lambda item: (item.start, item.end))
        return result
