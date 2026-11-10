"""OAuth 2.1 authorization server: DCR, CIMD, PKCE, consent, refresh rotation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from html import escape
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from hermes_agent_mcp_bridge import SCOPE
from hermes_agent_mcp_bridge.cimd import client_from_document, fetch_document, is_cimd_id
from hermes_agent_mcp_bridge.errors import OAuthError
from hermes_agent_mcp_bridge.keys import jwk
from hermes_agent_mcp_bridge.pkce import challenge_ok
from hermes_agent_mcp_bridge.redirects import filter_redirects, matches_any, warns_loopback
from hermes_agent_mcp_bridge.runtime import Runtime
from hermes_agent_mcp_bridge.store import ClientRecord, PendingAuth
from hermes_agent_mcp_bridge.tokens import encode_access_token

_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}


def metadata_document(runtime: Runtime) -> dict[str, object]:
    issuer = runtime.settings.server.issuer
    return {
        "issuer": issuer,
        "authorization_endpoint": runtime.settings.authorization_endpoint,
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "jwks_uri": f"{issuer}/oauth/jwks",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_modes_supported": ["query"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "scopes_supported": [SCOPE],
    }


def protected_resource(runtime: Runtime) -> dict[str, object]:
    return {
        "resource": runtime.settings.server.resource,
        "authorization_servers": [runtime.settings.server.issuer],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    }


def resource_metadata_url(resource: str) -> str:
    parts = urlsplit(resource)
    suffix = parts.path.lstrip("/")
    path = "/.well-known/oauth-protected-resource"
    if suffix:
        path = f"{path}/{suffix}"
    return f"{parts.scheme}://{parts.netloc}{path}"


def json_error(status: int, code: str, description: str = "") -> JSONResponse:
    body: dict[str, str] = {"error": code}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status, headers=_NO_STORE)


async def register(request: Request, runtime: Runtime) -> Response:
    if not runtime.limiter.allow("dcr", 60, 3600):
        return json_error(429, "slow_down", "Too many registration attempts.")
    try:
        body = await request.json()
    except Exception:
        return json_error(400, "invalid_client_metadata", "Registration body must be JSON.")
    if not isinstance(body, dict):
        return json_error(400, "invalid_client_metadata", "Registration body must be a JSON object.")
    if "client_secret" in body or body.get("token_endpoint_auth_method") not in {None, "none"}:
        return json_error(400, "invalid_client_metadata", "Only public clients are accepted.")
    grants = body.get("grant_types", ["authorization_code", "refresh_token"])
    if not isinstance(grants, list) or any(
        not isinstance(item, str) or item not in {"authorization_code", "refresh_token"} for item in grants
    ):
        return json_error(400, "invalid_client_metadata", "Unsupported grant type.")
    responses = body.get("response_types", ["code"])
    if responses != ["code"]:
        return json_error(400, "invalid_client_metadata", "Only the code response type is accepted.")
    try:
        redirects = filter_redirects(
            body.get("redirect_uris"),
            private_schemes=runtime.settings.redirects.private_schemes,
        )
    except Exception:
        return json_error(400, "invalid_redirect_uri", "No usable redirect URI was registered.")
    name = _clean_name(body.get("client_name"))
    client_id = "hbc_" + secrets.token_urlsafe(24)
    runtime.store.insert_client(client_id, name, redirects)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": name,
            "redirect_uris": redirects,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
        headers=_NO_STORE,
    )


async def authorize_get(request: Request, runtime: Runtime) -> Response:
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    try:
        client = _resolve_client(runtime, client_id)
    except OAuthError as exc:
        return json_error(exc.status, exc.code, exc.description)
    if not redirect_uri or not matches_any(redirect_uri, client.redirect_uris):
        return json_error(400, "invalid_request", "redirect_uri does not match the registered client.")
    state = params.get("state", "")
    if not _state_ok(state):
        return _redirect_error(redirect_uri=redirect_uri, error="invalid_request", state=state, runtime=runtime)
    if params.get("response_type") != "code":
        return _redirect_error(
            redirect_uri=redirect_uri, error="unsupported_response_type", state=state, runtime=runtime
        )
    if params.get("code_challenge_method") != "S256" or not challenge_ok(params.get("code_challenge", "")):
        return _redirect_error(redirect_uri=redirect_uri, error="invalid_request", state=state, runtime=runtime)
    resource = params.get("resource", "")
    if resource != runtime.settings.server.resource:
        return _redirect_error(redirect_uri=redirect_uri, error="invalid_target", state=state, runtime=runtime)
    scope = _scope(params.get("scope"))
    if scope is None:
        return _redirect_error(redirect_uri=redirect_uri, error="invalid_scope", state=state, runtime=runtime)
    pending = PendingAuth(
        request_id=secrets.token_urlsafe(24),
        client_id=client.client_id,
        client_name=client.client_name,
        redirect_uri=redirect_uri,
        challenge=params["code_challenge"],
        resource=resource,
        scope=scope,
        state=state,
    )
    runtime.store.save_pending(pending, ttl=runtime.settings.tokens.code_ttl_seconds)
    return HTMLResponse(_consent_page(pending), headers=_security_headers())


async def authorize_post(request: Request, runtime: Runtime) -> Response:
    form = await request.form()
    request_id = str(form.get("request_id", ""))
    decision = str(form.get("decision", ""))
    pending = runtime.store.pop_pending(request_id) if request_id else None
    if pending is None:
        return HTMLResponse(_message("This approval request expired. Start the connection again."), status_code=400)
    if decision == "deny":
        return _redirect_error(
            redirect_uri=pending.redirect_uri, error="access_denied", state=pending.state, runtime=runtime
        )
    if decision != "approve":
        return HTMLResponse(_message("The approval decision was not recognized."), status_code=400)
    ip_key = _rate_key(request)
    if not runtime.limiter.allow(f"password:{ip_key}", 8, 900):
        return HTMLResponse(_message("Too many attempts. Wait and try again."), status_code=429)
    given = str(form.get("password", ""))
    if not _password_ok(given, runtime.password):
        return HTMLResponse(_message("The password was not accepted."), status_code=401, headers=_security_headers())
    code = secrets.token_urlsafe(32)
    runtime.store.save_code(
        code=code,
        client_id=pending.client_id,
        redirect_uri=pending.redirect_uri,
        challenge=pending.challenge,
        resource=pending.resource,
        scope=pending.scope,
        subject=runtime.settings.subject,
        state=pending.state,
        ttl=runtime.settings.tokens.code_ttl_seconds,
    )
    query = urlencode({"code": code, "state": pending.state, "iss": runtime.settings.server.issuer})
    sep = "&" if "?" in pending.redirect_uri else "?"
    return RedirectResponse(pending.redirect_uri + sep + query, status_code=302, headers=_NO_STORE)


async def token(request: Request, runtime: Runtime) -> Response:
    if request.headers.get("authorization"):
        return json_error(401, "invalid_client", "Public clients do not send a client secret.")
    form = await request.form()
    if "client_secret" in form:
        return json_error(401, "invalid_client", "Public clients do not send a client secret.")
    client_id = str(form.get("client_id", ""))
    try:
        client = _resolve_client(runtime, client_id)
    except OAuthError as exc:
        return json_error(exc.status, exc.code, exc.description)
    grant = str(form.get("grant_type", ""))
    if grant == "authorization_code":
        return _authorization_code(form, runtime, client)
    if grant == "refresh_token":
        return _refresh(form, runtime, client)
    return json_error(400, "unsupported_grant_type", "Unsupported grant type.")


async def revoke(request: Request, runtime: Runtime) -> Response:
    form = await request.form()
    presented = str(form.get("token", ""))
    if presented:
        runtime.store.revoke_refresh(presented)
        if presented.count(".") == 2:
            try:
                import jwt

                claims = jwt.decode(presented, options={"verify_signature": False}, algorithms=["RS256"])
            except Exception:
                claims = {}
            jti = claims.get("jti") if isinstance(claims, dict) else None
            if isinstance(jti, str):
                runtime.store.revoke_jti(jti, ttl=runtime.settings.tokens.access_ttl_seconds)
    return Response(status_code=200, headers=_NO_STORE)


def jwks(runtime: Runtime) -> JSONResponse:
    return JSONResponse({"keys": [jwk(runtime.key, runtime.kid)]}, headers={"cache-control": "public, max-age=300"})


def _authorization_code(form: Any, runtime: Runtime, client: ClientRecord) -> Response:
    code = str(form.get("code", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    verifier = str(form.get("code_verifier", ""))
    resource = str(form.get("resource", ""))
    if resource != runtime.settings.server.resource:
        return json_error(400, "invalid_target", "resource does not match this server.")
    if not matches_any(redirect_uri, client.redirect_uris):
        return json_error(400, "invalid_grant", "redirect_uri does not match the authorization request.")
    refresh = "hbr_" + secrets.token_urlsafe(32)
    jti = secrets.token_urlsafe(16)
    status, scope = runtime.store.redeem_code(
        code=code,
        client_id=client.client_id,
        redirect_uri=redirect_uri,
        verifier=verifier,
        resource=resource,
        refresh_token=refresh,
        jti=jti,
        subject_expected=runtime.settings.subject,
        access_ttl=runtime.settings.tokens.access_ttl_seconds,
        refresh_ttl=runtime.settings.tokens.refresh_ttl_seconds,
    )
    if status != "ok":
        return json_error(400, "invalid_grant", "The authorization code was rejected.")
    return _issued(
        runtime,
        subject=runtime.settings.subject,
        client_id=client.client_id,
        scope=scope,
        refresh=refresh,
        jti=jti,
    )


def _refresh(form: Any, runtime: Runtime, client: ClientRecord) -> Response:
    presented = str(form.get("refresh_token", ""))
    resource_raw = form.get("resource")
    resource = str(resource_raw) if resource_raw else None
    if resource is not None and resource != runtime.settings.server.resource:
        return json_error(400, "invalid_target", "resource does not match this server.")
    scope_raw = form.get("scope")
    if scope_raw and _scope(str(scope_raw)) is None:
        return json_error(400, "invalid_scope", "The requested scope is not available.")
    new_refresh = "hbr_" + secrets.token_urlsafe(32)
    jti = secrets.token_urlsafe(16)
    status, subject, scope = runtime.store.redeem_refresh(
        refresh_token=presented,
        client_id=client.client_id,
        resource=resource,
        new_refresh=new_refresh,
        jti=jti,
        access_ttl=runtime.settings.tokens.access_ttl_seconds,
        refresh_ttl=runtime.settings.tokens.refresh_ttl_seconds,
    )
    if status != "ok":
        return json_error(400, "invalid_grant", "The refresh token was rejected.")
    return _issued(runtime, subject=subject, client_id=client.client_id, scope=scope, refresh=new_refresh, jti=jti)


def _issued(
    runtime: Runtime,
    *,
    subject: str,
    client_id: str,
    scope: str,
    refresh: str,
    jti: str,
) -> JSONResponse:
    access = encode_access_token(
        key=runtime.key,
        kid=runtime.kid,
        issuer=runtime.settings.server.issuer,
        subject=subject,
        resource=runtime.settings.server.resource or "",
        jti=jti,
        client_id=client_id,
        ttl=runtime.settings.tokens.access_ttl_seconds,
        scope=scope,
    )
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": runtime.settings.tokens.access_ttl_seconds,
            "refresh_token": refresh,
            "scope": scope,
        },
        headers=_NO_STORE,
    )


def _resolve_client(runtime: Runtime, client_id: str) -> ClientRecord:
    if not client_id or len(client_id) > 512:
        raise OAuthError(400, "invalid_client", "Unknown client.")
    if is_cimd_id(client_id):
        cached = runtime.store.get_cimd(client_id, max_age=3600)
        document = cached
        if document is None:
            document = fetch_document(
                client_id,
                transport=runtime.cimd_transport,
                check_network=runtime.cimd_transport is None,
            )
            name, redirects = client_from_document(
                client_id,
                document,
                private_schemes=runtime.settings.redirects.private_schemes,
            )
            runtime.store.cache_cimd(client_id, document)
            return ClientRecord(client_id=client_id, client_name=name, redirect_uris=redirects)
        name, redirects = client_from_document(
            client_id,
            document,
            private_schemes=runtime.settings.redirects.private_schemes,
        )
        return ClientRecord(client_id=client_id, client_name=name, redirect_uris=redirects)
    record = runtime.store.get_client(client_id)
    if record is None:
        raise OAuthError(400, "invalid_client", "Unknown client.")
    return record


def _redirect_error(*, redirect_uri: str, error: str, state: str, runtime: Runtime) -> RedirectResponse:
    params: dict[str, str] = {"error": error, "iss": runtime.settings.server.issuer}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=302, headers=_NO_STORE)


def _scope(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return SCOPE
    if raw.split(" ") == [SCOPE]:
        return SCOPE
    return None


def _state_ok(state: str) -> bool:
    if not 1 <= len(state) <= 512:
        return False
    return all(32 < ord(ch) < 127 for ch in state)


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        return "Remote MCP client"
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return (cleaned or "Remote MCP client")[:80]


def _password_ok(given: str, expected: str) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256(given.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _rate_key(request: Request) -> str:
    client = request.scope.get("client")
    ip = str(client[0]) if isinstance(client, tuple) and client else ""
    if ip in {"127.0.0.1", "::1"}:
        return "local"
    return ip or "unknown"


def _security_headers() -> dict[str, str]:
    return {
        **_NO_STORE,
        "content-security-policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
    }


def _consent_page(pending: PendingAuth) -> str:
    warning = ""
    if warns_loopback(pending.redirect_uri):
        warning = (
            "<p><strong>Warning:</strong> this client redirects to a local address or a "
            "non-HTTPS application. Any program on that machine can receive the authorization code. "
            "Approve only if you just started this connection.</p>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Approve connection</title>
</head>
<body>
<h1>Approve connection</h1>
<p>A remote client wants to submit prompts to the local agent on this machine.</p>
<dl>
<dt>Client</dt><dd>{escape(pending.client_name)}</dd>
<dt>Redirect</dt><dd><code>{escape(pending.redirect_uri)}</code></dd>
<dt>Scope</dt><dd>{escape(pending.scope)}</dd>
</dl>
{warning}
<form method="post" action="/oauth/authorize">
<input type="hidden" name="request_id" value="{escape(pending.request_id)}">
<label>Owner password <input type="password" name="password" autocomplete="current-password" required></label>
<button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" formnovalidate>Deny</button>
</form>
</body>
</html>
"""


def _message(text: str) -> str:
    body = escape(text)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>Connection</title></head><body><p>{body}</p></body></html>"
    )
