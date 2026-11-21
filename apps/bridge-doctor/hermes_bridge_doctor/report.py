"""Pass, fail, and skip lines. Details never include secret values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["pass", "fail", "skip"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def render(checks: list[Check], *, version: str) -> str:
    lines = [f"bridge-doctor {version}"]
    for item in checks:
        lines.append(f"[{item.status}] {item.name}: {item.detail}")
        if item.fix and item.status != "pass":
            lines.append(f"       fix: {item.fix}")
    failed = sum(1 for item in checks if item.status == "fail")
    skipped = sum(1 for item in checks if item.status == "skip")
    passed = sum(1 for item in checks if item.status == "pass")
    lines.append(f"{passed} passed, {failed} failed, {skipped} skipped")
    return "\n".join(lines) + "\n"


def exit_code(checks: list[Check], *, strict: bool) -> int:
    if any(item.status == "fail" for item in checks):
        return 1
    if strict and any(item.status == "skip" for item in checks):
        return 1
    return 0
