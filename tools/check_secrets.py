#!/usr/bin/env python3
"""Refuse to commit if a real secret is about to be published.

Only values whose NAME marks them as sensitive are treated as secrets --
SITE_NAME and the test user-agent live in .env too, and flagging those
trains you to ignore the tool, which is worse than not having it.

    ./.venv/bin/python tools/check_secrets.py    # exit 1 = do not commit
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SENSITIVE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)

# Shapes worth catching even if they never appear in .env.
PATTERNS = {
    "OGAds API token": r"\b\d{4,6}\|[A-Za-z0-9]{32,}\b",
    "Fernet ciphertext/key": r"\bgAAAAA[A-Za-z0-9_\-=]{20,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


def main() -> int:
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not staged:
        print("nothing staged")
        return 0

    blob = ""
    for name in staged:
        path = ROOT / name
        if path.exists():
            blob += path.read_text(errors="ignore")

    problems = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if SENSITIVE.search(key) and len(value) >= 8 and value in blob:
                problems.append(f"{key} from .env appears in a staged file")

    for label, pat in PATTERNS.items():
        if re.search(pat, blob):
            problems.append(f"a {label} appears in a staged file")

    for name in staged:
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            problems.append(f"{name} is staged and must never be committed")

    if problems:
        print("REFUSING TO COMMIT:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"scanned {len(staged)} staged files - no secrets found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
