from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hermes_pr_reviewer.bridge import BridgeClient
from hermes_pr_reviewer.cli import ConfigError, load_settings
from hermes_pr_reviewer.events import parse_event
from hermes_pr_reviewer.forges import GitHubForge, OriginForge, RoutingForge
from hermes_pr_reviewer.prompt import build_prompt, format_review
from hermes_pr_reviewer.scrub import scrub
from hermes_pr_reviewer.server import WebhookServer
from hermes_pr_reviewer.service import ReviewService
from hermes_pr_reviewer.signatures import identify, keys_from_jwks, verify_github, verify_origin
from starlette.testclient import TestClient

from hermes_agent_mcp_bridge.app import build_runtime, create_app
from hermes_agent_mcp_bridge.hermes import FakeHermes
from tests.conftest import ISSUER, PASSWORD, RESOURCE, make_settings

SECRET = "test-webhook-secret"
HEAD = "a" * 40
BASE = "b" * 40


class Allow:
    def verify(self, provider: str, body: bytes, headers: dict[str, str], *, now: float) -> bool:
        return True


class RecordingForge:
    def __init__(self) -> None:
        self.diffs = ["print('ok')\n"]
        self.reviews: list[str] = []

    def fetch_diff(self, event: object) -> str:
        return self.diffs[0]

    def post_review(self, event: object, body: str) -> None:
        self.reviews.append(body)


class RecordingBridge:
    def __init__(self, reply: str = "Summary\n\nShort.\n\nRisks\n\nNone.\n\nSuggestions\n\nAdd a test.") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.block = False

    def ask(self, prompt: str, *, timeout: float) -> str:
        self.prompts.append(prompt)
        self.started.set()
        if self.block:
            self.release.wait(2)
        return self.reply


def _github_body(action: str = "opened", *, draft: bool = False) -> bytes:
    return json.dumps(
        {
            "action": action,
            "pull_request": {
                "number": 7,
                "title": "Add telemetry",
                "draft": draft,
                "head": {"sha": HEAD, "ref": "feature"},
                "base": {"sha": BASE, "ref": "main"},
            },
            "repository": {"name": "widgets", "owner": {"login": "acme"}},
            "installation": {"id": 99},
        }
    ).encode()


def _github_headers(body: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "x-github-event": "pull_request",
        "x-github-delivery": delivery,
        "x-hub-signature-256": f"sha256={digest}",
        "content-type": "application/json",
    }


def _service(bridge: RecordingBridge, forge: RecordingForge, *, sync: bool = True) -> ReviewService:
    return ReviewService(
        verifier=Allow(),
        bridge=bridge,
        forge=forge,
        sync=sync,
        review_drafts=False,
        max_prompt_chars=7000,
        timeout=5,
    )


def test_github_signature_and_stale_origin_signature() -> None:
    body = _github_body()
    headers = _github_headers(body)
    assert identify(headers) == "github"
    assert verify_github(SECRET, body, headers["x-hub-signature-256"]) is True
    assert verify_github(SECRET, body, "sha256=00") is False
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    raw = b'{"event":{"type":"pull_request.created"}}'
    now = 1_700_000_000
    digest = hashlib.sha256(f"whd_1.{now}.".encode() + raw).hexdigest().encode()
    signature = "v1ed," + base64.b64encode(private.sign(digest)).decode()
    origin_headers = {"webhook-id": "whd_1", "webhook-timestamp": str(now), "webhook-signature": signature}
    assert verify_origin(raw, origin_headers, [public], now=float(now)) is True
    assert verify_origin(raw, origin_headers, [public], now=float(now + 301)) is False
    numbers = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwks = json.dumps({"keys": [{"kty": "OKP", "crv": "Ed25519", "x": base64.urlsafe_b64encode(numbers).rstrip(b"=").decode()}]})
    assert keys_from_jwks(jwks.encode())


def test_parse_ignores_closed_and_drafts() -> None:
    opened = parse_event("github", _github_body(), _github_headers(_github_body()))
    assert opened is not None and opened.should_review and opened.owner == "acme"
    closed_body = _github_body("closed")
    closed = parse_event("github", closed_body, _github_headers(closed_body))
    assert closed is not None and closed.should_review is False
    draft_body = _github_body(draft=True)
    draft = parse_event("github", draft_body, _github_headers(draft_body))
    assert draft is not None and draft.draft is True


def test_prompt_scrubs_tokens_and_comment_does_not_approve() -> None:
    leaked = "ghp_" + ("a" * 20)
    cleaned = scrub(f"token {leaked}")
    assert leaked not in cleaned
    assert "[redacted]" in cleaned
    event = parse_event("github", _github_body(), _github_headers(_github_body()))
    assert event is not None
    prompt = build_prompt(event, "diff " + leaked, max_chars=7000)
    assert leaked not in prompt
    assert "Summary, Risks, and Suggestions" in prompt
    comment = format_review("Risks\n\nnone")
    assert "does not approve" in comment
    assert "Risks" in comment


def test_service_posts_one_comment_and_ignores_duplicates() -> None:
    bridge = RecordingBridge()
    forge = RecordingForge()
    service = _service(bridge, forge)
    body = _github_body()
    status, payload = service.handle(body, _github_headers(body))
    assert status == 200 and payload["status"] == "reviewed"
    assert forge.reviews and "does not approve" in forge.reviews[0]
    again, duplicate = service.handle(body, _github_headers(body))
    assert again == 202 and duplicate["status"] == "duplicate"
    assert len(bridge.prompts) == 1
    draft = _github_body(draft=True)
    ignored, info = service.handle(draft, _github_headers(draft, delivery="delivery-2"))
    assert ignored == 202 and info["status"] == "ignored"


def test_async_webhook_returns_before_the_agent_finishes() -> None:
    bridge = RecordingBridge()
    bridge.block = True
    forge = RecordingForge()
    service = _service(bridge, forge, sync=False)
    body = _github_body()
    started = time.monotonic()
    status, payload = service.handle(body, _github_headers(body, delivery="async-1"))
    elapsed = time.monotonic() - started
    assert status == 202 and payload["status"] == "accepted"
    assert elapsed < 0.5
    assert bridge.started.wait(2)
    bridge.release.set()


def test_webhook_server_reviews_a_signed_github_event() -> None:
    bridge = RecordingBridge()
    forge = RecordingForge()
    service = _service(bridge, forge)
    server = WebhookServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert isinstance(port, int)
        body = _github_body()
        response = httpx.post(f"http://127.0.0.1:{port}/webhook", content=body, headers=_github_headers(body))
        assert response.status_code == 200
        assert response.json()["status"] == "reviewed"
        health = httpx.get(f"http://127.0.0.1:{port}/healthz")
        assert health.status_code == 200
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_live_bridge_and_github_forge(tmp_path) -> None:
    fake = FakeHermes(output="Summary\n\nLooks small.\n\nRisks\n\nNone found.\n\nSuggestions\n\nShip it.")
    runtime = build_runtime(make_settings(tmp_path), password=PASSWORD, backend=fake)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            leaked = "ghp_" + ("b" * 16)
            return httpx.Response(200, text=f"diff\n+token {leaked}\n")
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    forge_http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with TestClient(create_app(runtime), base_url=ISSUER, follow_redirects=False) as bridge_http:
            bridge = BridgeClient(bridge_http, base_url=ISSUER, resource=RESOURCE, password=PASSWORD)
            service = ReviewService(
                verifier=Allow(),
                bridge=bridge,
                forge=RoutingForge(
                    github=GitHubForge(forge_http, api_base="https://api.github.com", token="ghs_test_token_value"),
                    origin=None,
                ),
                sync=True,
                review_drafts=False,
                max_prompt_chars=7000,
                timeout=10,
            )
            body = _github_body()
            status, payload = service.handle(body, _github_headers(body, delivery="live-1"))
            assert status == 200 and payload["status"] == "reviewed"
    finally:
        forge_http.close()
        runtime.worker.stop() if runtime.worker else None
        runtime.store.close()
    assert fake.prompts
    assert "ghp_" not in fake.prompts[0]
    assert "[redacted]" in fake.prompts[0]
    posted = captured["json"]
    assert isinstance(posted, dict)
    assert posted["event"] == "COMMENT"
    assert "Risks" in posted["body"]
    assert "does not approve" in posted["body"]


def test_live_bridge_and_origin_forge(tmp_path) -> None:
    fake = FakeHermes(output="Summary\n\nOrigin path.\n\nRisks\n\nCheck the token scope.\n\nSuggestions\n\nNone.")
    runtime = build_runtime(make_settings(tmp_path), password=PASSWORD, backend=fake)
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "oit_testtokenvalue", "expiresAt": "2026-01-01T00:00:00Z"})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"files": [{"filename": "a.py", "patch": "@@\n+print(1)\n"}]})
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "rev_1", "verdict": "comment"})

    forge_http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with TestClient(create_app(runtime), base_url=ISSUER, follow_redirects=False) as bridge_http:
            bridge = BridgeClient(bridge_http, base_url=ISSUER, resource=RESOURCE, password=PASSWORD)
            service = ReviewService(
                verifier=Allow(),
                bridge=bridge,
                forge=RoutingForge(
                    github=None,
                    origin=OriginForge(forge_http, api_base="https://api.cursor.com", app_id="app_01test", private_key=pem),
                ),
                sync=True,
                review_drafts=False,
                max_prompt_chars=7000,
                timeout=10,
            )
            payload = {
                "installationId": "i_01test",
                "event": {
                    "type": "pull_request.created",
                    "payload": {
                        "pullRequest": {
                            "number": "7",
                            "title": "Add telemetry",
                            "draft": False,
                            "head": {"ref": "feature", "sha": HEAD},
                            "base": {"ref": "main", "sha": BASE},
                        },
                        "repository": {"name": "widgets", "owner": {"slug": "acme"}},
                    },
                },
            }
            body = json.dumps(payload).encode()
            headers = {
                "user-agent": "Cursor-Origin-Webhook/1.0",
                "webhook-id": "whd_live",
                "webhook-installation-id": "i_01test",
                "webhook-timestamp": "1700000000",
                "webhook-signature": "v1ed,AAAA",
            }
            status, result = service.handle(body, headers)
            assert status == 200 and result["status"] == "reviewed"
    finally:
        forge_http.close()
        runtime.worker.stop() if runtime.worker else None
        runtime.store.close()
    posted = captured["json"]
    assert isinstance(posted, dict)
    assert posted["verdict"] == "comment"
    assert "Risks" in posted["body"]
    assert str(captured["auth"]).startswith("Bearer oit_")


def test_check_config_reports_presence_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_REVIEW_BRIDGE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("HERMES_BRIDGE_OWNER_PASSWORD", PASSWORD)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    settings = load_settings()
    assert settings.resource.endswith("/mcp")
    assert settings.password == PASSWORD
    with pytest.raises(ConfigError):
        load_settings({"HERMES_REVIEW_BRIDGE_URL": "http://127.0.0.1:8765"})
