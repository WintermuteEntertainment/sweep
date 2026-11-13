import os
import unittest
from pathlib import Path

from sweep.detectors import (HIGH, LOW, MEDIUM, Matchers, classify_ipv4,
                             load_terms, sensitive_filename)
from sweep.identity import IdentityValue, gather
from sweep.scan import decode

from . import support


def scan(text, identity=None, terms=None):
    return Matchers(identity or [], terms or []).scan(text)


def detectors(matches):
    return [match.detector for match in matches]


class EmailTests(unittest.TestCase):
    def test_reserved_domain_is_found_and_marked(self):
        matches = scan("write to someone@example.com today")
        self.assertEqual(detectors(matches), ["email"])
        self.assertEqual(matches[0].value, "someone@example.com")
        self.assertTrue(matches[0].reserved)
        self.assertEqual(matches[0].severity, LOW)

    def test_image_name_is_not_an_address(self):
        self.assertEqual(scan("see icon@2x.png for the logo"), [])

    def test_git_remote_form_is_not_an_address(self):
        self.assertEqual(scan("git@example.com:owner/repo.git"), [])


class IpTests(unittest.TestCase):
    def test_documentation_address(self):
        matches = scan("server at 192.0.2.44, port 80")
        self.assertEqual(detectors(matches), ["ip-documentation"])
        self.assertEqual(matches[0].value, "192.0.2.44")
        self.assertTrue(matches[0].reserved)

    def test_private_blocks(self):
        for address in ("10.0.0.0", "172.16.0.0", "192.168.0.0", "100.64.0.0"):
            kind = classify_ipv4(address)
            self.assertIsNotNone(kind, address)
            self.assertEqual(kind[0], "ip-private", address)
            self.assertEqual(kind[1], MEDIUM, address)

    def test_ignored_addresses(self):
        for address in ("127.0.0.1", "0.0.0.0", "255.255.255.0", "224.0.0.1",
                        "999.1.1.1", "01.2.3.4"):
            self.assertIsNone(classify_ipv4(address), address)

    def test_longer_dotted_numbers_are_not_addresses(self):
        self.assertEqual(scan("build 192.0.2.44.7 done"), [])
        self.assertEqual(scan("tag v192.0.2.44"), [])

    def test_address_at_end_of_sentence(self):
        self.assertEqual(detectors(scan("It is 192.0.2.44.")), ["ip-documentation"])


class MacTests(unittest.TestCase):
    def test_documentation_mac_both_separators(self):
        for text in ("00-00-5E-00-53-01", "00:00:5e:00:53:01"):
            matches = scan(f"adapter {text} up")
            self.assertEqual(detectors(matches), ["mac-documentation"], text)
            self.assertTrue(matches[0].reserved)

    def test_longer_hex_run_is_not_a_mac(self):
        self.assertEqual(scan("00:00:5e:00:53:01:02:03"), [])

    def test_mixed_separators_are_not_a_mac(self):
        self.assertEqual(scan("00:00-5e:00-53:01"), [])


class HomePathTests(unittest.TestCase):
    def setUp(self):
        self.home = str(Path.home())
        parent = Path.home().parent.name.lower()
        if parent not in ("users", "home"):
            self.skipTest("home folder is not under Users or home")

    def test_real_home_path_in_three_spellings(self):
        spellings = {self.home, self.home.replace("\\", "/"),
                     self.home.replace("\\", "\\\\")}
        for spelling in spellings:
            matches = scan(f'path = "{spelling}/project/file.txt"')
            self.assertIn("home-path", detectors(matches), spelling)

    def test_placeholder_folder_is_not_a_person(self):
        root = str(Path.home().parent)
        self.assertEqual(scan(os.path.join(root, "Public", "file.txt")), [])


class SecretShapeTests(unittest.TestCase):
    def test_private_key_block(self):
        header = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        matches = scan(header + "\n")
        self.assertEqual(detectors(matches), ["private-key"])
        self.assertEqual(matches[0].severity, HIGH)

    def test_credentials_in_url(self):
        url = "https://username:" + "password@example.com/owner/repo.git"
        self.assertIn("url-credentials", detectors(scan(f"origin {url}")))

    def test_plain_url_has_no_credentials(self):
        self.assertEqual(scan("https://example.com:8080/path"), [])


class IdentityTests(unittest.TestCase):
    """These use the real values of the machine running the tests."""

    def setUp(self):
        self.identity, _ = gather()
        names = [v for v in self.identity if v.kind == "computer-name"]
        if not names:
            self.skipTest("this machine reports no usable computer name")
        self.name = names[0].value

    def test_real_computer_name_is_found(self):
        matches = scan(f"copied from {self.name} last night", self.identity)
        found = [m for m in matches if m.detector == "computer-name"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, HIGH)

    def test_match_ignores_case(self):
        matches = scan(f"host={self.name.swapcase()};", self.identity)
        self.assertIn("computer-name", detectors(matches))

    def test_name_inside_a_longer_word_is_not_found(self):
        matches = scan(f"x{self.name}y", self.identity)
        self.assertNotIn("computer-name", detectors(matches))

    def test_machine_ip_is_marked_high(self):
        addresses = [v.value for v in self.identity if v.kind == "local-ip"]
        if not addresses:
            self.skipTest("this machine reports no local address")
        matches = scan(f"listening on {addresses[0]}:8080", self.identity)
        self.assertEqual(detectors(matches), ["local-ip"])
        self.assertEqual(matches[0].severity, HIGH)

    def test_machine_mac_is_marked_high_in_either_spelling(self):
        macs = [v.value for v in self.identity if v.kind == "mac-address"]
        if not macs:
            self.skipTest("this machine reports no adapter address")
        for spelling in (macs[0], macs[0].replace(":", "-").upper()):
            matches = scan(f"adapter {spelling}", self.identity)
            self.assertEqual(detectors(matches), ["mac-address"], spelling)
            self.assertEqual(matches[0].severity, HIGH)

    def test_email_inside_exact_value_is_reported_once(self):
        identity = [IdentityValue("git-email", "author@example.com")]
        matches = scan("by author@example.com", identity)
        self.assertEqual(detectors(matches), ["git-email"])


class TermFileTests(unittest.TestCase):
    def test_terms_load_and_match(self):
        folder = support.make_temp_dir()
        self.addCleanup(support.remove_temp_dir, folder)
        path = os.path.join(folder, "terms.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# private terms\n\nsweep-test-term\n")
        terms = load_terms(path)
        self.assertEqual(terms, ["sweep-test-term"])
        matches = scan("a line with Sweep-Test-Term in it", terms=terms)
        self.assertEqual(detectors(matches), ["user-term"])
        self.assertEqual(matches[0].severity, HIGH)


class FileNameTests(unittest.TestCase):
    def test_sensitive_names(self):
        for path in (".env", "config/.env.production", "keys/server.pem",
                     "home/.ssh/id_ed25519", "vault.kdbx"):
            self.assertIsNotNone(sensitive_filename(path), path)

    def test_ordinary_names(self):
        for path in ("README.md", ".env.example", "src/environment.py",
                     "keyboard.txt"):
            self.assertIsNone(sensitive_filename(path), path)


class DecodeTests(unittest.TestCase):
    def test_utf16_with_and_without_mark(self):
        text = "log line for someone@example.com\n"
        self.assertIn("someone@example.com", decode(text.encode("utf-16")))
        self.assertIn("someone@example.com", decode(text.encode("utf-16-le")))

    def test_binary_is_not_text(self):
        self.assertIsNone(decode(bytes(range(256)) * 4))

    def test_utf8_mark_is_removed(self):
        self.assertEqual(decode("﻿hello".encode("utf-8")), "hello")


if __name__ == "__main__":
    unittest.main()
