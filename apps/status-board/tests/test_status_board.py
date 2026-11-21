from __future__ import annotations

import json

import pytest
from hermes_status_board.cli import main
from hermes_status_board.connector import connector_document
from starlette.testclient import TestClient

from hermes_agent_mcp_bridge.app import build_runtime, create_app
from hermes_agent_mcp_bridge.hermes import FakeHermes
from tests.conftest import ISSUER, PASSWORD, login, make_settings, register_client

PROMPT = "unique-prompt-secret-value"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def _rpc(http: TestClient, token: str, method: str, params: dict | None = None) -> object:
    body: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = http.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}", "accept": "application/json"},
        json=body,
    )
    assert response.status_code == 200
    return response.json()


def test_status_page_hides_prompts_and_job_ids(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_BRIDGE_STATUS_TOKEN", raising=False)
    runtime = build_runtime(make_settings(tmp_path), password=PASSWORD, backend=FakeHermes())
    try:
        with TestClient(create_app(runtime), base_url=ISSUER, follow_redirects=False) as http:
            client_id = register_client(http, [CHATGPT_REDIRECT])
            token = str(login(http, client_id=client_id, redirect_uri=CHATGPT_REDIRECT)[0]["access_token"])
            asked = _rpc(http, token, "tools/call", {"name": "ask_hermes", "arguments": {"prompt": PROMPT}})
            job_id = asked["result"]["structuredContent"]["job_id"]  # type: ignore[index]
            listed = _rpc(http, token, "tools/call", {"name": "recent_calls", "arguments": {"limit": 10}})
            encoded = json.dumps(listed)
            assert PROMPT not in encoded
            assert job_id in encoded
            page = http.get("/status")
            assert page.status_code == 200
            assert "Hermes bridge status" in page.text
            assert "ask_hermes" in page.text
            assert PROMPT not in page.text
            assert job_id not in page.text
            assert "job_" not in page.text
            document = http.get("/status.json")
            assert document.status_code == 200
            assert "job_id" not in document.text
            assert document.json()["hermes_reachable"] is True
            rejected = _rpc(http, token, "tools/call", {"name": "<img src=x>", "arguments": {}})
            assert rejected["result"]["isError"] is True  # type: ignore[index]
            escaped = http.get("/status")
            assert "<img" not in escaped.text
            assert "&lt;img" in escaped.text
            bad_limit = _rpc(http, token, "tools/call", {"name": "recent_calls", "arguments": {"limit": 0}})
            assert bad_limit["result"]["isError"] is True  # type: ignore[index]
            consent = TestClient(create_app(runtime, role="consent"), base_url="http://127.0.0.1:8766", follow_redirects=False)
            with consent:
                assert consent.get("/status").status_code == 404
    finally:
        runtime.worker.stop() if runtime.worker else None
        runtime.store.close()


def test_status_token_is_a_bearer_header(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_BRIDGE_STATUS_TOKEN", "status-token-value")
    runtime = build_runtime(make_settings(tmp_path), password=PASSWORD, backend=FakeHermes())
    try:
        with TestClient(create_app(runtime), base_url=ISSUER, follow_redirects=False) as http:
            assert http.get("/status").status_code == 401
            assert http.get("/status", params={"token": "status-token-value"}).status_code == 401
            opened = http.get("/status", headers={"authorization": "Bearer status-token-value"})
            assert opened.status_code == 200
            wrong = http.get("/status", headers={"authorization": "Bearer other-token-value"})
            assert wrong.status_code == 401
    finally:
        runtime.worker.stop() if runtime.worker else None
        runtime.store.close()


def test_connector_document_and_cli(capsys: pytest.CaptureFixture[str]) -> None:
    document = connector_document("https://bridge.example.com/mcp")
    assert document["mcpServers"]["hermes-bridge-status"]["url"].endswith("/mcp")  # type: ignore[index]
    connector_document("http://127.0.0.1:8765/mcp")
    with pytest.raises(ValueError):
        connector_document("http://bridge.example.com/mcp")
    with pytest.raises(SystemExit):
        main(["connector", "--resource", "http://bridge.example.com/mcp"])
    main(["connector", "--resource", "https://bridge.example.com/mcp"])
    output = capsys.readouterr().out
    assert "hermes-bridge-status" in output
