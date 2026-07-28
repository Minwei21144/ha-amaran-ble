"""Fail CI when a public source tree contains common private artifacts."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".cmd",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
}
ALLOWED_MAC_PREFIXES = {
    "00:11:22",
    "02:00:00",
    "AA:BB:CC",
}
ALLOWED_EXAMPLE_KEYS = {
    "00112233445566778899AABBCCDDEEFF",
    "FFEEDDCCBBAA99887766554433221100",
}
MAC_RE = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])")
KEY_RE = re.compile(r"(?i)\b[0-9a-f]{32}\b")
PRIVATE_PATTERNS = {
    "Windows user path": re.compile(r"(?i)C:[\\/]+Users[\\/]+"),
    "Unix home path": re.compile(r"(?<![A-Za-z0-9_])/" + r"home/"),
    "10/8 address": re.compile(r"\b10(?:\.\d{1,3}){3}\b"),
    "192.168/16 address": re.compile(r"\b192\.168(?:\.\d{1,3}){2}\b"),
    "172.16/12 address": re.compile(
        r"\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b"
    ),
}
FORBIDDEN_SUFFIXES = {".log", ".zip", ".pyc"}
FORBIDDEN_NAMES = {
    "amaran-ble-ha-config.json",
    "id_rsa",
    "id_ed25519",
}


def iter_files() -> list[Path]:
    """Return files that belong to the publishable source tree."""
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
    ]


def validate_json(path: Path, failures: list[str]) -> None:
    """Validate JSON syntax."""
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        failures.append(f"{path.relative_to(ROOT)}: invalid JSON: {err}")


def scan_text(path: Path, failures: list[str]) -> None:
    """Scan a text file for likely private values."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        failures.append(f"{path.relative_to(ROOT)}: not valid UTF-8: {err}")
        return

    relative = path.relative_to(ROOT)
    for label, pattern in PRIVATE_PATTERNS.items():
        if pattern.search(text):
            failures.append(f"{relative}: contains a {label}")

    for match in MAC_RE.finditer(text):
        mac = match.group(0).upper()
        if mac[:8] not in ALLOWED_MAC_PREFIXES:
            failures.append(f"{relative}: contains a non-synthetic MAC address")

    for match in KEY_RE.finditer(text):
        key = match.group(0).upper()
        if key not in ALLOWED_EXAMPLE_KEYS:
            failures.append(f"{relative}: contains an unapproved 128-bit value")


def main() -> int:
    """Validate the repository and return a process exit code."""
    failures: list[str] = []
    for path in iter_files():
        relative = path.relative_to(ROOT)
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(f"{relative}: forbidden generated artifact")
            continue
        if path.name.lower() in FORBIDDEN_NAMES:
            failures.append(f"{relative}: forbidden secret/export filename")
            continue
        if path.suffix.lower() == ".json":
            validate_json(path, failures)
        if path.suffix.lower() in TEXT_SUFFIXES:
            scan_text(path, failures)

    if failures:
        print("Public tree validation failed:")
        for failure in sorted(set(failures)):
            print(f"- {failure}")
        return 1

    print("Public tree validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
