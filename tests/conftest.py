from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
from hermes_agent_mcp_bridge.hermes import FakeHermes
from hermes_agent_mcp_bridge.pkce import s256
from hermes_agent_mcp_bridge.runtime import Runtime

PASSWORD = "test-owner-password"
VERIFIER = "a" * 64
RESOURCE = "http://127.0.0.1:8765/mcp"
ISSUER = "http://127.0.0.1:8765"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    limits = LimitSettings(worker_interval_seconds=0.05, sync_timeout_seconds=1)
    if "limits" in overrides:
        limits = overrides.pop("limits")  # type: ignore[assignment]
    settings = Settings(
        server=ServerSettings(issuer=ISSUER, resource=RESOURCE, dev_mode=True),
        consent=ConsentSettings(owner_password_file=str(tmp_path / "owner-password")),
        hermes=HermesSettings(
            api_key_file=str(tmp_path / "hermes-api-key"),
            poll_interval_seconds=0.05,
            run_timeout_seconds=5,
        ),
        limits=limits,
        data_dir=str(tmp_path / "data"),
    )
    return settings


@pytest.fixture
def fake() -> FakeHermes:
    return FakeHermes()


@pytest.fixture
def runtime(tmp_path: Path, fake: FakeHermes) -> Iterator[Runtime]:
    settings = make_settings(tmp_path)
    built = build_runtime(settings, password=PASSWORD, backend=fake)
    yield built
    built.store.close()


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    app = create_app(runtime, role="public")
    with TestClient(app, base_url=ISSUER, follow_redirects=False) as http:
        yield http


def register_client(http: TestClient, redirects: list[str], name: str = "Example client") -> str:
    response = http.post(
        "/oauth/register",
        json={
            "client_name": name,
            "redirect_uris": redirects,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    assert isinstance(client_id, str)
    return client_id


def login(
    http: TestClient,
    *,
    client_id: str,
    redirect_uri: str,
    resource: str = RESOURCE,
    verifier: str = VERIFIER,
    state: str = "state-value-1",
    extra: dict[str, str] | None = None,
    password: str = PASSWORD,
) -> tuple[dict[str, object], dict[str, list[str]]]:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": s256(verifier),
        "code_challenge_method": "S256",
        "resource": resource,
    }
    if extra:
        params.update(extra)
    page = http.get("/oauth/authorize", params=params)
    assert page.status_code == 200, page.text
    match = re.search(r'name="request_id" value="([^"]+)"', page.text)
    assert match is not None
    approved = http.post(
        "/oauth/authorize",
        data={"request_id": match.group(1), "password": password, "decision": "approve"},
    )
    assert approved.status_code == 302, approved.text
    query = parse_qs(urlsplit(approved.headers["location"]).query)
    token = http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": query["code"][0],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert isinstance(body, dict)
    return body, query
