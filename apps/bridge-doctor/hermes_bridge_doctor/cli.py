"""`hermes-bridge-doctor check` prints pass, fail, and skip lines and exits 1 on failure."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from hermes_bridge_doctor import __version__
from hermes_bridge_doctor.checks import run_checks
from hermes_bridge_doctor.publish import PublishTarget, publish
from hermes_bridge_doctor.report import exit_code, render


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hermes-bridge-doctor")
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--online", default="", help="Issuer origin of a running bridge")
    parser.add_argument("--require-secrets", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Treat skipped checks as failures")
    parser.add_argument("--expect-version", default="")
    parser.add_argument("--publish", choices=("off", "github", "origin"), default="off")
    args = parser.parse_args(argv)
    checks = run_checks(
        args.config,
        online=args.online,
        require_secrets=args.require_secrets,
        expect_version=args.expect_version,
    )
    if args.publish != "off":
        target = _target(args.publish)
        if isinstance(target, str):
            from hermes_bridge_doctor.report import Check

            checks.append(Check("publish", "fail", target, "set the publish environment variables"))
        else:
            checks.append(publish(checks, target, version=__version__))
    sys.stdout.write(render(checks, version=__version__))
    raise SystemExit(exit_code(checks, strict=args.strict))


def _target(kind: str) -> PublishTarget | str:
    sha = os.environ.get("DOCTOR_HEAD_SHA", "")
    if kind == "github":
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" not in repository:
            return "GITHUB_REPOSITORY must look like owner/name"
        owner, repo = repository.split("/", 1)
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token or not sha:
            return "set GITHUB_TOKEN and DOCTOR_HEAD_SHA"
        return PublishTarget(
            kind="github",
            http=httpx.Client(timeout=20),
            api_base=os.environ.get("GITHUB_API_BASE", "https://api.github.com"),
            token=token,
            owner=owner,
            repo=repo,
            head_sha=sha,
        )
    token = os.environ.get("ORIGIN_INSTALLATION_TOKEN", "")
    owner = os.environ.get("ORIGIN_OWNER", "")
    repo = os.environ.get("ORIGIN_REPO", "")
    if not token or not owner or not repo or not sha:
        return "set ORIGIN_INSTALLATION_TOKEN, ORIGIN_OWNER, ORIGIN_REPO, and DOCTOR_HEAD_SHA"
    return PublishTarget(
        kind="origin",
        http=httpx.Client(timeout=20),
        api_base=os.environ.get("ORIGIN_API_BASE", "https://api.cursor.com"),
        token=token,
        owner=owner,
        repo=repo,
        head_sha=sha,
    )
