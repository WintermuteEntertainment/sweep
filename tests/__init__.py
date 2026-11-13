"""Tests for Sweep.

Run from the repo root:  python -m unittest discover -s tests -t .

Test data rules:
  - No invented personal data.
  - Fixed values come only from ranges that standards reserve for
    documentation: example.com / example.org (RFC 2606), 192.0.2.0/24
    (RFC 5737), MAC 00-00-5E-00-53-xx (RFC 7042), and the first address of
    each private block named in RFC 1918 and RFC 6598.
  - Anything that must look like a real machine value (computer name, home
    folder) is read from the machine running the tests, and never stored.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
