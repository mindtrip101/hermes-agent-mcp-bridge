from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

from hermes_agent_mcp_bridge import SCOPE
from hermes_agent_mcp_bridge.app import build_runtime, create_app
from hermes_agent_mcp_bridge.cimd import assert_public_https
from hermes_agent_mcp_bridge.errors import OAuthError
from hermes_agent_mcp_bridge.pkce import s256
from hermes_agent_mcp_bridge.redirects import filter_redirects
from hermes_agent_mcp_bridge.tokens import encode_access_token
from tests.conftest import (
    CHATGPT_REDIRECT,
    ISSUER,
    PASSWORD,
    RESOURCE,
    VERIFIER,
    login,
    make_settings,
    register_client,
)

CALLBACK_REDIRECT = "https://chatgpt.com/connector/oauth/callback-example"


def test_discovery_advertises_public_client_pkce_and_issuer(client: TestClient) -> None:
    protected = client.get("/.well-known/oauth-protected-resource")
    assert protected.status_code == 200
    body = protected.json()
    assert body["resource"] == RESOURCE
    assert body["authorization_servers"] == [ISSUER]
    assert body["bearer_methods_supported"] == ["header"]
    path_meta = client.get("/.well-known/oauth-protected-resource/mcp")
    assert path_meta.json()["resource"] == RESOURCE

    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == ISSUER
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert "S256" in metadata["code_challenge_methods_supported"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["registration_endpoint"] == ISSUER + "/oauth/register"
    assert metadata["scopes_supported"] == [SCOPE]
    oidc = client.get("/.well-known/openid-configuration")
    assert oidc.json()["issuer"] == ISSUER
    assert "openid" not in json.dumps(metadata["scopes_supported"])


def test_dcr_pkce_refresh_rotation_and_resource_rejection(client: TestClient) -> None:
    client_id = register_client(client, [CHATGPT_REDIRECT, CALLBACK_REDIRECT], name="ChatGPT")
    registered = client.post(
        "/oauth/register",
        json={"redirect_uris": [CHATGPT_REDIRECT], "token_endpoint_auth_method": "client_secret_basic"},
    )
    assert registered.status_code == 400
    assert registered.json()["error"] == "invalid_client_metadata"

    mismatch = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://attacker.example/callback",
            "state": "state-value-1",
            "code_challenge": s256(VERIFIER),
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["error"] == "invalid_request"
    assert "location" not in mismatch.headers

    wrong_resource = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CHATGPT_REDIRECT,
            "state": "state-value-1",
            "code_challenge": s256(VERIFIER),
            "code_challenge_method": "S256",
            "resource": ISSUER + "/other",
        },
    )
    assert wrong_resource.status_code == 302
    error_query = parse_qs(urlsplit(wrong_resource.headers["location"]).query)
    assert error_query["error"] == ["invalid_target"]
    assert error_query["iss"] == [ISSUER]
    assert error_query["state"] == ["state-value-1"]

    missing_pkce = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CHATGPT_REDIRECT,
            "state": "state-value-1",
            "resource": RESOURCE,
        },
    )
    assert parse_qs(urlsplit(missing_pkce.headers["location"]).query)["error"] == ["invalid_request"]
    assert parse_qs(urlsplit(missing_pkce.headers["location"]).query)["iss"] == [ISSUER]

    bad_password = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK_REDIRECT,
            "state": "state-value-1",
            "code_challenge": s256(VERIFIER),
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )
    request_id = bad_password.text.split('name="request_id" value="', 1)[1].split('"', 1)[0]
    rejected = client.post(
        "/oauth/authorize",
        data={"request_id": request_id, "password": "not-the-owner-password", "decision": "approve"},
    )
    assert rejected.status_code == 401

    body, query = login(client, client_id=client_id, redirect_uri=CHATGPT_REDIRECT)
    assert query["iss"] == [ISSUER]
    assert query["state"] == ["state-value-1"]
    access = str(body["access_token"])
    claims = jwt.decode(access, options={"verify_signature": False}, algorithms=["RS256"])
    assert claims["aud"] == RESOURCE
    assert claims["iss"] == ISSUER
    assert claims["scope"] == SCOPE

    bad_resource = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": body["refresh_token"],
            "resource": ISSUER + "/other",
        },
    )
    assert bad_resource.json()["error"] == "invalid_target"

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": body["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()["refresh_token"]
    assert second != body["refresh_token"]
    replay = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": body["refresh_token"]},
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    family = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": second},
    )
    assert family.status_code == 400

    secret = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": "x" * 20,
            "refresh_token": "nope",
        },
    )
    assert secret.status_code == 401

    reused = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": query["code"][0],
            "redirect_uri": CHATGPT_REDIRECT,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
        },
    )
    assert reused.status_code == 400
    assert reused.json()["error"] == "invalid_grant"

    wrong_verifier_page = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CHATGPT_REDIRECT,
            "state": "state-value-2",
            "code_challenge": s256("b" * 64),
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )
    request_id = wrong_verifier_page.text.split('name="request_id" value="', 1)[1].split('"', 1)[0]
    approved = client.post(
        "/oauth/authorize",
        data={"request_id": request_id, "password": PASSWORD, "decision": "approve"},
    )
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    bad_verifier = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": CHATGPT_REDIRECT,
            "code_verifier": "c" * 64,
            "resource": RESOURCE,
        },
    )
    assert bad_verifier.json()["error"] == "invalid_grant"


def test_clients_refresh_and_jobs_survive_restart(tmp_path, fake) -> None:
    settings = make_settings(tmp_path)
    first_runtime = build_runtime(settings, password=PASSWORD, backend=fake)
    app = create_app(first_runtime, role="public")
    with TestClient(app, base_url=ISSUER, follow_redirects=False) as http:
        client_id = register_client(http, [CHATGPT_REDIRECT])
        body, _query = login(http, client_id=client_id, redirect_uri=CHATGPT_REDIRECT)
        token = str(body["access_token"])
        asked = http.post(
            "/mcp",
            headers={"authorization": f"Bearer {token}", "accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask_hermes", "arguments": {"prompt": "persist me"}}},
        )
        assert asked.status_code == 200
        job_id = asked.json()["result"]["structuredContent"]["job_id"]
        deadline = 0
        status = ""
        while deadline < 50 and status != "succeeded":
            polled = http.post(
                "/mcp",
                headers={"authorization": f"Bearer {token}", "accept": "application/json"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_job", "arguments": {"job_id": job_id}}},
            )
            status = polled.json()["result"]["structuredContent"]["status"]
            deadline += 1
            time.sleep(0.05)
        assert status == "succeeded"
        refresh = str(body["refresh_token"])
    first_runtime.store.close()

    second = build_runtime(settings, password=PASSWORD, backend=fake)
    try:
        with TestClient(create_app(second, role="public"), base_url=ISSUER, follow_redirects=False) as http:
            again = http.post(
                "/oauth/token",
                data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh},
            )
            assert again.status_code == 200, again.text
            new_access = again.json()["access_token"]
            job = http.post(
                "/mcp",
                headers={"authorization": f"Bearer {new_access}", "accept": "application/json"},
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_job", "arguments": {"job_id": job_id}}},
            )
            payload = job.json()["result"]["structuredContent"]
            assert payload["status"] == "succeeded"
            assert payload["result"] == "bridge-ok"
            stale = http.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "hbc_deleted-client-id-value",
                    "redirect_uri": CHATGPT_REDIRECT,
                    "state": "state-value-1",
                    "code_challenge": s256(VERIFIER),
                    "code_challenge_method": "S256",
                    "resource": RESOURCE,
                },
            )
            assert stale.status_code == 400
            assert stale.json()["error"] == "invalid_client"
            still = http.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": CHATGPT_REDIRECT,
                    "state": "state-value-9",
                    "code_challenge": s256(VERIFIER),
                    "code_challenge_method": "S256",
                    "resource": RESOURCE,
                },
            )
            assert still.status_code == 200
    finally:
        second.store.close()


def test_cimd_accepts_chatgpt_document_and_rejects_private_key_only(client: TestClient, monkeypatch) -> None:
    document = {
        "client_id": "https://chatgpt.com/oauth/client.json",
        "client_name": "ChatGPT",
        "redirect_uris": [CHATGPT_REDIRECT, CALLBACK_REDIRECT],
        "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        "token_endpoint_auth_method": "private_key_jwt",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == document["client_id"]
        return httpx.Response(200, json=document)

    from hermes_agent_mcp_bridge.runtime import Runtime

    found = _runtime_from_app(client)
    assert isinstance(found, Runtime)
    found.cimd_transport = httpx.MockTransport(handler)
    body, query = login(client, client_id="https://chatgpt.com/oauth/client.json", redirect_uri=CHATGPT_REDIRECT)
    assert query["iss"] == [ISSUER]
    assert body["token_type"] == "Bearer"

    only_jwt = dict(document)
    only_jwt["client_id"] = "https://chatgpt.com/oauth/other.json"
    only_jwt["token_endpoint_auth_methods_supported"] = ["private_key_jwt"]
    only_jwt["token_endpoint_auth_method"] = "private_key_jwt"

    def other(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=only_jwt)

    found.cimd_transport = httpx.MockTransport(other)
    rejected = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": only_jwt["client_id"],
            "redirect_uri": CHATGPT_REDIRECT,
            "state": "state-value-1",
            "code_challenge": s256(VERIFIER),
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "invalid_client"


def test_cimd_blocks_private_addresses() -> None:
    with pytest.raises(OAuthError):
        assert_public_https("http://chatgpt.com/oauth/client.json")
    with pytest.raises(OAuthError):
        assert_public_https("https://127.0.0.1/client.json")
    with pytest.raises(OAuthError):
        assert_public_https("https://169.254.169.254/latest")
    with pytest.raises(OAuthError):
        assert_public_https("https://metadata.example/client.json", resolve=lambda _host, _port: ["10.1.2.3"])
    assert_public_https("https://metadata.example/client.json", resolve=lambda _host, _port: ["1.1.1.1"])


def test_registration_keeps_usable_redirects_and_loopback_ignores_port(client: TestClient) -> None:
    response = client.post(
        "/oauth/register",
        json={
            "redirect_uris": [
                "https://www.cursor.com/agents/mcp/oauth/callback",
                "cursor://anysphere.cursor-mcp/oauth/callback",
                "http://127.0.0.1/callback",
                "http://evil.example/callback",
            ]
        },
    )
    assert response.status_code == 201
    kept = response.json()["redirect_uris"]
    assert "http://evil.example/callback" not in kept
    assert "cursor://anysphere.cursor-mcp/oauth/callback" in kept
    client_id = response.json()["client_id"]
    page = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://localhost:54321/callback",
            "state": "state-value-1",
            "code_challenge": s256(VERIFIER),
            "code_challenge_method": "S256",
            "resource": RESOURCE,
        },
    )
    assert page.status_code == 200
    assert "local address" in page.text


def test_expired_token_is_rejected(client: TestClient, runtime) -> None:
    client_id = register_client(client, [CHATGPT_REDIRECT])
    token = encode_access_token(
        key=runtime.key,
        kid=runtime.kid,
        issuer=ISSUER,
        subject="owner",
        resource=RESOURCE,
        jti="expired-jti-value",
        client_id=client_id,
        ttl=60,
        now=1_000,
    )
    response = client.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}", "accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


def test_redirect_filter_rejects_an_empty_remainder() -> None:
    with pytest.raises(Exception):
        filter_redirects(["http://evil.example/callback"], private_schemes=["cursor"])


def _runtime_from_app(client: TestClient):
    # Starlette stores the app the TestClient was given.
    app = client.app
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        runtime = getattr(endpoint, "__closure__", None)
        if not runtime:
            continue
        for cell in runtime:
            value = cell.cell_contents
            if value.__class__.__name__ == "Runtime":
                return value
    raise AssertionError("runtime not found")
