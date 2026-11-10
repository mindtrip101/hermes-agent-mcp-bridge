# Setup

Use a separate data directory, port, and issuer URL for any client you have not verified yet. Do not point a new client at an issuer a live connector already uses. ChatGPT in particular should be tried on its own instance. See [CLIENTS.md](CLIENTS.md).

## Prerequisites

- Python 3.11 or newer, or Docker
- Hermes Agent 0.21.x with the API server, on the same machine as the bridge
- An HTTPS name for remote clients. Tailscale Funnel is one way to get that name. A reverse proxy you already run is another
- The remote client's OAuth redirect URI, if it does not register one itself

The bridge binds to loopback. Do not publish the Hermes API port. Publish only the bridge.

## Hermes API

The supported backend is the Hermes runs API documented for the 0.21 API server. On the Hermes host, enable it and start the gateway. The listener in current 0.21 docs is `http://127.0.0.1:8642`.

In the Hermes environment file (the Hermes docs refer to `~/.hermes/.env`):

```
API_SERVER_ENABLED=true
API_SERVER_KEY=replace-with-a-long-random-string
```

Then start the gateway with the Hermes CLI (`hermes gateway`). A health check on that machine:

```bash
curl -sS -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8642/health
```

Write the same key to `secrets/hermes_api_key` with mode `0600`, or set `HERMES_BRIDGE_HERMES_API_KEY`. The bridge sends it only to that loopback URL.

The CLI adapter is a fallback when you cannot run the API server. It is off unless `hermes.backend` is `cli` and you set `hermes.cli_argv`. Confirm the argv against the Hermes version you installed. The prompt is written to a mode-`0600` file. The bridge refuses an argv that contains `ignore-rules`.

## Install with pip or uv

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

With uv:

```bash
uv venv
uv pip install -e .
```

Copy the example config and set `server.issuer` and `server.resource` to the exact public URLs you will give the client. `resource` defaults to `issuer + /mcp` when omitted. Neither value may have a trailing slash. Clients compare them as exact strings.

```bash
cp config.example.yaml config.yaml
cp .env.example .env   # optional
.venv/bin/hermes-agent-mcp-bridge init --config config.yaml
```

`init` creates the data directory, the signing key, and the owner password file. It prints the password once. Put the Hermes API key in place, then:

```bash
.venv/bin/hermes-agent-mcp-bridge serve --config config.yaml
```

Check from the same machine:

```bash
curl -sS http://127.0.0.1:8765/healthz
curl -sS http://127.0.0.1:8765/.well-known/oauth-protected-resource
curl -sS http://127.0.0.1:8765/.well-known/oauth-authorization-server
```

The authorization-server document must include `S256`, `registration_endpoint`, `client_id_metadata_document_supported: true`, `token_endpoint_auth_methods_supported: ["none"]`, and `authorization_response_iss_parameter_supported: true`.

## Install with Docker Compose

The compose file publishes `127.0.0.1:8765` only. Create the config, the owner password, and the signing key on the host with `init` first (the section above). The Hermes API is reached at `host.docker.internal:8642`, so the compose file sets `HERMES_BRIDGE_HERMES_ALLOW_NON_LOOPBACK`. Do not change that URL to a public address.

```bash
cp config.example.yaml config.yaml
# edit issuer and resource, then init, then write secrets/hermes_api_key
BRIDGE_UID="$(id -u)" BRIDGE_GID="$(id -g)" docker compose up --build
```

`BRIDGE_UID` and `BRIDGE_GID` make the container run as your user, which is what can read the mode-`0600` files `init` wrote. Use a different checkout directory for a test instance so it does not share `data/` with a live connector. The environment variable `HERMES_BRIDGE_DATA_DIR=/data` overrides `data_dir` inside the container and points at the mounted `./data` directory.

## Tailscale Funnel and Serve

Bind stays `127.0.0.1`. Tailscale connects to that port. Confirm the port is free before you publish it.

Do not run `tailscale serve reset`. Do not run `tailscale funnel reset`. Both clear existing configuration on the machine, including routes that have nothing to do with this bridge. Inspect first:

```bash
tailscale serve status
```

Background publish of the public bridge port (confirm the flags with `tailscale funnel --help` on your client version):

```bash
tailscale funnel --bg 8765
```

That is the public MCP and OAuth origin. Set `server.issuer` to the HTTPS origin Funnel shows, with no path and no trailing slash, and set `server.resource` to that origin plus `/mcp`.

If `consent.exposure` is `public` (the default), the approval page is on that same origin. The owner's browser must be able to open it, and the owner password is what stops anyone else who can reach the URL from approving.

If `consent.exposure` is `tailnet`, the approval page is only on `consent.bind_port` (8766 in the example). Publish that port to the tailnet and not to the public internet:

```bash
tailscale serve --bg 8766
```

Set `consent.public_url` to the HTTPS origin Serve shows for that port. The metadata `authorization_endpoint` then points at that origin. Clients that require the authorization endpoint to be on the issuer origin should stay on `consent.exposure: public`. ChatGPT's current documentation follows the URL in the metadata, and it also wants RFC 9207 `iss` on every redirect, which this server sends. That combination has not been verified against a live ChatGPT session. For the first ChatGPT test, use public consent on a separate test instance.

Check `tailscale serve status` again after either command and confirm you did not remove an existing route.

## Connect from Grok Bot

1. Use the public URL that ends in `/mcp`. Example shape: `https://bridge.example.com/mcp`.
2. Add it as a custom connector. Do not set a static `Authorization` header. Grok Bot discovers the OAuth metadata and runs the code flow with PKCE.
3. When the browser opens the approval page, compare the client name and the redirect URI, then enter the owner password.
4. Ask the connector to call `ask_hermes`, then `get_job` with the returned job id.

Grok Bot is the only remote client this design has been used with. The steps above match that connector's OAuth discovery behavior. Labels in the product UI can change. This repository's tests do not open a live session.

## Verify

From a second shell on the bridge machine, after you have an access token from the flow above:

```bash
curl -sS http://127.0.0.1:8765/healthz
```

A remote check of discovery, from any network that can reach the public HTTPS origin:

```bash
curl -sS "https://bridge.example.com/.well-known/oauth-protected-resource"
curl -sS "https://bridge.example.com/.well-known/oauth-authorization-server"
```

Replace the host with yours. An unauthenticated `POST` to `/mcp` must return `401` and a `WWW-Authenticate` header whose `resource_metadata` URL is the protected-resource document.

Then, in the client, submit a short prompt through `ask_hermes` and poll `get_job` until `status` is `succeeded`. `bridge_status` should report `hermes_reachable: true` while the gateway is up.
