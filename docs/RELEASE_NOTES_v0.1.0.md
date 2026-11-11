# hermes-agent-mcp-bridge 0.1.0

Initial public release.

Expose a local Hermes agent over remote MCP with OAuth 2.1 public-client auth (DCR, CIMD, PKCE).

Official Hermes `mcp-serve` is local stdio only. This bridge is the remote HTTP path: a client completes OAuth 2.1 against the issuer, then calls `POST /mcp`. The Hermes agent stays on loopback. The remote client can queue a prompt and read the reply. It does not receive the agent's own tools.

## Highlights

- Streamable HTTP MCP tools: `ask_hermes`, `get_job`, `bridge_status`, and an optional bounded `ask_hermes_sync` (`limits.sync_ask_enabled`, off by default)
- OAuth 2.1 public client only (`token_endpoint_auth_method: none`). No client secret
- Discovery at `/.well-known/oauth-protected-resource` (and `/.well-known/oauth-protected-resource/mcp`) and `/.well-known/oauth-authorization-server`
- Dynamic Client Registration at `POST /oauth/register` and Client ID Metadata Documents
- PKCE S256, resource indicators, rotating refresh tokens, and RFC 9207 `iss` on every authorization redirect
- Owner consent at `GET /oauth/authorize`. Default `consent.exposure: public` keeps that page on the issuer origin. `tailnet` moves it to a second loopback port
- SQLite for clients, refresh tokens, and jobs. Access tokens are short-lived RS256 JWTs. The signing key is created on first run at `data/signing-key.pem` (mode `0600`)
- Hermes 0.21 runs API on `127.0.0.1:8642` (`POST /v1/runs`), or a CLI adapter you configure
- Default bind `127.0.0.1:8765`. Publish that port with your own HTTPS proxy. Do not publish the Hermes API port

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e .
cp config.example.yaml config.yaml
# set server.issuer and server.resource to the public HTTPS origin, with no trailing slash
mkdir -p secrets
umask 077
printf '%s\n' 'replace-with-a-long-random-string' > secrets/hermes_api_key
chmod 0600 secrets/hermes_api_key
.venv/bin/hermes-agent-mcp-bridge init --config config.yaml
.venv/bin/hermes-agent-mcp-bridge serve --config config.yaml
```

`init` prints the owner password once. Python 3.11 or newer. Package version `0.1.0`.

Give the client the MCP URL, which is `server.resource` (normally `https://your-host/mcp`). Do not paste a bearer token.

ChatGPT: developer-mode custom connector, `consent.exposure: public`. Cursor: remote `url` set to that same resource, no `auth` block and no static `Authorization` header. `redirects.private_schemes` defaults to `cursor`. Step-by-step checks are in `README.md` and `docs/CLIENTS.md`.

ChatGPT, Claude, and Cursor are expected to work and have not been verified in a live session by this repository's tests. Grok Bot is the remote client this design has been used with.

## Verify

```bash
curl -sS http://127.0.0.1:8765/healthz
curl -sS http://127.0.0.1:8765/.well-known/oauth-authorization-server
```

An unauthenticated `POST /mcp` returns 401 with a `WWW-Authenticate` header pointing at the protected-resource metadata.

Development checks: `ruff check hermes_agent_mcp_bridge tests` and `pytest -q` (18 tests). CI is `.github/workflows/ci.yml`.
