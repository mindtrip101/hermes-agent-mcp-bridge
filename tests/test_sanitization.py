"""Fail if this tree contains private-deployment identifiers or key material."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IDENTITY = [
    "." + "ts" + ".net",
    "all" + "spark",
    "tail" + "062bb",
    "mind" + "trip",
    "sp" + "lan",
    "fix" + "256",
    "click" + "up",
    "out" + "look",
    "100" + ".88.",
    "172.28" + ".9.64",
    "/home/" + "mind" + "trip",
]
# Docs link to this repository. Drop that URL before the scan so the org slug
# is not treated as a private identifier. Home paths still fail.
_PUBLIC_REPO = "github.com/" + "mind" + "trip" + "101/hermes-agent-mcp-bridge"
PEM = "-----BEGIN " + "PRIVATE KEY-----"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "data", "secrets"}


def _files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts):
            continue
        ignored = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=ROOT, check=False)
        if ignored.returncode == 0:
            continue
        found.append(path)
    return found


def test_tree_has_no_private_identifiers_or_secrets() -> None:
    offenders: list[str] = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        if rel != ".gitleaks.toml":
            lowered = text.lower().replace(_PUBLIC_REPO, "")
            for needle in IDENTITY:
                if needle.lower() in lowered:
                    offenders.append(f"{rel}: {needle}")
        if PEM in text:
            offenders.append(f"{rel}: private key")
        if EMAIL.search(text):
            offenders.append(f"{rel}: email address")
    assert offenders == []


def test_gitignore_covers_local_secrets() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for name in ("data/", "secrets/", ".env", "*.pem", "*.sqlite3"):
        assert name in text
