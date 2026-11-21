from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from hermes_bridge_doctor.checks import run_checks
from hermes_bridge_doctor.cli import main
from hermes_bridge_doctor.publish import PublishTarget, publish
from hermes_bridge_doctor.report import Check, render
from starlette.testclient import TestClient

from hermes_agent_mcp_bridge import __version__ as bridge_version
from hermes_agent_mcp_bridge.app import build_runtime, create_app
from hermes_agent_mcp_bridge.hermes import FakeHermes
from tests.conftest import ISSUER, PASSWORD, RESOURCE, make_settings

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clear_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(("HERMES_BRIDGE_", "HERMES_REVIEW_", "DOCTOR_", "ORIGIN_", "GITHUB_")):
            monkeypatch.delenv(key, raising=False)


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (value + "\n").encode())
    finally:
        os.close(fd)


def _config(tmp_path: Path) -> Path:
    _write_secret(tmp_path / "secrets" / "owner_password", PASSWORD)
    _write_secret(tmp_path / "secrets" / "hermes_api_key", "hermes-api-key-value")
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "server:",
                f"  issuer: {ISSUER}",
                f"  resource: {RESOURCE}",
                "  dev_mode: true",
                "consent:",
                "  owner_password_file: secrets/owner_password",
                "hermes:",
                "  api_key_file: secrets/hermes_api_key",
                "data_dir: data",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_example_config_passes_offline_and_strict_fails() -> None:
    example = ROOT / "config.example.yaml"
    with pytest.raises(SystemExit) as offline:
        main(["check", "--config", str(example)])
    assert offline.value.code == 0
    with pytest.raises(SystemExit) as strict:
        main(["check", "--config", str(example), "--strict"])
    assert strict.value.code == 1


def test_broken_config_fails_with_a_fix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("server:\n  issuer: http://bridge.example\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["check", "--config", str(path)])
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "[fail] config:" in output
    assert "fix:" in output


def test_online_check_logs_in_and_hides_the_password(tmp_path: Path) -> None:
    config = _config(tmp_path)
    fake = FakeHermes()
    runtime = build_runtime(make_settings(tmp_path / "runtime"), password=PASSWORD, backend=fake)
    try:
        with TestClient(create_app(runtime), base_url=ISSUER, follow_redirects=False) as http:
            checks = run_checks(config, online=ISSUER, require_secrets=True, http=http)
    finally:
        runtime.worker.stop() if runtime.worker else None
        runtime.store.close()
    rendered = render(checks, version="0.1.0")
    assert PASSWORD not in rendered
    assert "hermes-api-key-value" not in rendered
    failed = [item for item in checks if item.status != "pass"]
    assert failed == []
    assert any(item.name == "version" and bridge_version in item.detail for item in checks)
    assert any(item.name == "tools" and "recent_calls" in item.detail for item in checks)


def test_missing_secret_names_the_fix_without_a_value(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "server:",
                "  issuer: https://bridge.example.com",
                "  resource: https://bridge.example.com/mcp",
                "consent:",
                "  owner_password_file: secrets/owner_password",
                "hermes:",
                "  api_key_file: secrets/hermes_api_key",
                "",
            ]
        ),
        encoding="utf-8",
    )
    checks = run_checks(path, require_secrets=True)
    rendered = render(checks, version="0.1.0")
    owner = next(item for item in checks if item.name == "owner_password")
    api = next(item for item in checks if item.name == "hermes_api_key")
    assert owner.status == "fail"
    assert api.status == "fail"
    assert "0600" in owner.fix
    assert "owner_password" not in rendered or "present" not in owner.detail


def test_publish_github_and_origin_check_runs() -> None:
    sha = "c" * 40
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 1})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    checks = [Check("config", "pass", "parsed")]
    github = publish(
        checks,
        PublishTarget(
            kind="github",
            http=http,
            api_base="https://api.github.com",
            token="token-value",
            owner="acme",
            repo="widgets",
            head_sha=sha,
        ),
        version="0.1.0",
    )
    origin = publish(
        checks,
        PublishTarget(
            kind="origin",
            http=http,
            api_base="https://api.cursor.com",
            token="oit_publish",
            owner="acme",
            repo="widgets",
            head_sha=sha,
        ),
        version="0.1.0",
    )
    assert github.status == "pass"
    assert origin.status == "pass"
    github_body = json.loads(seen[0].content)
    origin_body = json.loads(seen[1].content)
    assert github_body["conclusion"] == "success"
    assert github_body["name"] == "bridge-doctor"
    assert origin_body["checkRun"]["conclusion"] == "success"
    assert origin_body["checkRun"]["key"] == "bridge-doctor"
    assert "token-value" not in json.dumps(github_body)
    assert "oit_publish" not in json.dumps(origin_body)
