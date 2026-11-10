from __future__ import annotations

import stat
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from hermes_agent_mcp_bridge.app import build_runtime, create_app
from hermes_agent_mcp_bridge.config import (
    ConsentSettings,
    HermesSettings,
    LimitSettings,
    ServerSettings,
    Settings,
)
from hermes_agent_mcp_bridge.errors import ConfigError
from hermes_agent_mcp_bridge.hermes import CliHermes, FakeHermes, HttpHermes
from tests.conftest import ISSUER, PASSWORD, RESOURCE, login, make_settings, register_client

CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def _rpc(http: TestClient, token: str, method: str, params: dict | None = None, rpc_id: int = 1) -> httpx.Response:
    body: dict[str, object] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return http.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream"},
        json=body,
    )


def test_tools_are_allowlisted_and_jobs_round_trip(client: TestClient, fake: FakeHermes) -> None:
    anonymous = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert anonymous.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in anonymous.headers["www-authenticate"]

    client_id = register_client(client, [CHATGPT_REDIRECT])
    token = str(login(client, client_id=client_id, redirect_uri=CHATGPT_REDIRECT)[0]["access_token"])
    listed = _rpc(client, token, "tools/list")
    names = [item["name"] for item in listed.json()["result"]["tools"]]
    assert names == ["ask_hermes", "get_job", "bridge_status"]
    unknown = _rpc(client, token, "tools/call", {"name": "shell", "arguments": {"command": "id"}})
    assert unknown.json()["result"]["isError"] is True

    fake.output = "answer-text"
    asked = _rpc(client, token, "tools/call", {"name": "ask_hermes", "arguments": {"prompt": "hello"}})
    job_id = asked.json()["result"]["structuredContent"]["job_id"]
    payload = {"status": "queued"}
    for _ in range(40):
        payload = _rpc(
            client, token, "tools/call", {"name": "get_job", "arguments": {"job_id": job_id}}
        ).json()["result"]["structuredContent"]
        if payload["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert payload["result"] == "answer-text"
    status = _rpc(client, token, "tools/call", {"name": "bridge_status", "arguments": {}})
    assert status.json()["result"]["structuredContent"]["hermes_reachable"] is True
    assert fake.prompts == ["hello"]


def test_sync_wait_returns_timeout_and_secret_is_scrubbed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.limits.sync_ask_enabled = True
    fake = FakeHermes(output=PASSWORD + "-leaked", delay_polls=100)
    runtime = build_runtime(settings, password=PASSWORD, backend=fake)
    try:
        with TestClient(create_app(runtime), base_url=ISSUER, follow_redirects=False) as http:
            client_id = register_client(http, [CHATGPT_REDIRECT])
            token = str(login(http, client_id=client_id, redirect_uri=CHATGPT_REDIRECT)[0]["access_token"])
            names = [item["name"] for item in _rpc(http, token, "tools/list").json()["result"]["tools"]]
            assert "ask_hermes_sync" in names
            started = time.monotonic()
            synced = _rpc(http, token, "tools/call", {"name": "ask_hermes_sync", "arguments": {"prompt": "wait"}})
            assert time.monotonic() - started < 5
            body = synced.json()["result"]["structuredContent"]
            assert body["error"]["code"] == "timeout"
            fake.delay_polls = 0
            job_id = body["job_id"]
            result = ""
            for _ in range(40):
                polled = _rpc(http, token, "tools/call", {"name": "get_job", "arguments": {"job_id": job_id}})
                payload = polled.json()["result"]["structuredContent"]
                if payload["status"] == "succeeded":
                    result = payload["result"]
                    break
                time.sleep(0.05)
            assert PASSWORD not in result
            assert "[redacted]" in result
    finally:
        runtime.worker.stop() if runtime.worker else None
        runtime.store.close()


def test_tailnet_consent_is_not_on_the_public_listener(tmp_path: Path, fake: FakeHermes) -> None:
    settings = Settings(
        server=ServerSettings(issuer=ISSUER, resource=RESOURCE, dev_mode=True),
        consent=ConsentSettings(
            exposure="tailnet",
            public_url="http://127.0.0.1:8766",
            owner_password_file=str(tmp_path / "owner-password"),
        ),
        hermes=HermesSettings(api_key_file=str(tmp_path / "hermes-api-key"), poll_interval_seconds=0.05),
        limits=LimitSettings(worker_interval_seconds=0.05),
        data_dir=str(tmp_path / "data"),
    )
    runtime = build_runtime(settings, password=PASSWORD, backend=fake)
    try:
        public = TestClient(create_app(runtime, role="public"), base_url=ISSUER, follow_redirects=False)
        consent = TestClient(create_app(runtime, role="consent"), base_url="http://127.0.0.1:8766", follow_redirects=False)
        with public, consent:
            assert "8766/oauth/authorize" in public.get("/.well-known/oauth-authorization-server").json()["authorization_endpoint"]
            hidden = public.get("/oauth/authorize")
            assert hidden.status_code == 404
            visible = consent.get("/oauth/authorize")
            assert visible.status_code == 400
            assert consent.get("/oauth/token").status_code == 404
    finally:
        runtime.store.close()


def test_http_backend_uses_runs_api() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["key"] = request.headers["idempotency-key"]
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(202, json={"run_id": "run_abc", "status": "started"})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"run_id": "run_abc", "status": "completed", "output": "done"})

    backend = HttpHermes(
        "http://127.0.0.1:8642",
        "local-api-key-value",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert backend.health() is True
        run_id = backend.start("hello", job_id="job_abc")
        assert run_id == "run_abc"
        assert seen["key"] == "job_abc"
        assert seen["auth"] == "Bearer local-api-key-value"
        view = backend.poll(run_id)
        assert view.ok is True
        assert view.output == "done"
    finally:
        backend.close()


def test_cli_backend_keeps_the_prompt_out_of_argv(tmp_path: Path) -> None:
    seen = tmp_path / "argv.txt"
    script = tmp_path / "run.py"
    script.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[2]).write_text('\\n'.join(sys.argv))\n"
        "print(pathlib.Path(sys.argv[1]).read_text())\n",
        encoding="utf-8",
    )
    backend = CliHermes(
        [sys.executable, str(script), "{prompt_file}", str(seen)],
        timeout=5,
        max_output=1000,
        workdir=tmp_path / "prompts",
    )
    run_id = backend.start("secret-prompt-value", job_id="job_cli")
    view = backend.poll(run_id)
    assert view.output is not None
    assert view.output.strip() == "secret-prompt-value"
    assert "secret-prompt-value" not in seen.read_text(encoding="utf-8")
    prompt_file = tmp_path / "prompts" / "job_cli.prompt"
    assert stat.S_IMODE(prompt_file.stat().st_mode) & 0o077 == 0


def test_config_refuses_public_bind_and_trailing_slash(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(server=ServerSettings(issuer="http://127.0.0.1:8765/", dev_mode=True), data_dir=str(tmp_path))
    with pytest.raises(ValidationError):
        Settings(
            server=ServerSettings(issuer="http://127.0.0.1:8765", dev_mode=True, bind_host="0.0.0.0"),
            data_dir=str(tmp_path),
        )
    with pytest.raises(ValidationError):
        Settings(
            server=ServerSettings(issuer="https://bridge.example.com", dev_mode=False),
            hermes=HermesSettings(base_url="https://agent.example"),
            data_dir=str(tmp_path),
        )
    config = tmp_path / "config.yaml"
    config.write_text("server:\n  issuer: http://127.0.0.1:8765/\n  dev_mode: true\n", encoding="utf-8")
    from hermes_agent_mcp_bridge.config import load_settings

    with pytest.raises(ConfigError, match="trailing slash"):
        load_settings(config)


def test_init_prints_the_owner_password_once(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_BRIDGE_OWNER_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_BRIDGE_HERMES_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "server:\n  issuer: http://127.0.0.1:8765\n  dev_mode: true\ndata_dir: data\n",
        encoding="utf-8",
    )
    from hermes_agent_mcp_bridge.cli import main

    main(["init", "--config", str(config)])
    first = capsys.readouterr().out
    assert "shown once" in first
    key = tmp_path / "data" / "signing-key.pem"
    assert key.is_file()
    assert stat.S_IMODE(key.stat().st_mode) & 0o077 == 0
    main(["init", "--config", str(config)])
    second = capsys.readouterr().out
    assert "not regenerated" in second


def test_signing_key_is_created_with_mode_0600(tmp_path: Path, fake: FakeHermes) -> None:
    runtime = build_runtime(make_settings(tmp_path), password=PASSWORD, backend=fake)
    try:
        key_path = Path(runtime.settings.data_dir) / "signing-key.pem"
        assert key_path.is_file()
        assert stat.S_IMODE(key_path.stat().st_mode) & 0o077 == 0
        assert "PRIVATE KEY" in key_path.read_text(encoding="utf-8")
    finally:
        runtime.store.close()
