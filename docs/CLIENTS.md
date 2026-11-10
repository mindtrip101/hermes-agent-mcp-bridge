# Clients

The bridge does not embed a client SDK. A client connects if it can complete OAuth 2.1 against the metadata this server publishes and then call MCP at `{issuer}/mcp`.

| Client | Registration | Redirect URIs this server accepts | Live test |
|---|---|---|---|
| Grok Bot | DCR or CIMD, whichever the client starts | Whatever HTTPS (or allowed loopback / private-scheme) URI the client registers or publishes | Used with the private deployment this project generalizes. Not re-tested by the automated suite. |
| ChatGPT developer mode | CIMD preferred, DCR supported. This server advertises both. | See below | Not yet. Use a separate test instance. |
| Claude custom connector | CIMD, with DCR as a fallback, according to public connector guides | `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback` are the URIs those guides name | Not yet |
| Cursor | DCR | `cursor://anysphere.cursor-mcp/oauth/callback`, `https://www.cursor.com/agents/mcp/oauth/callback`, and a loopback URI. Loopback ports are not compared. | Not yet |

Redirect URIs for Claude and Cursor are taken from public connector write-ups, not from a test against those products. If registration fails, copy the exact URI the client shows and compare it with [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Grok, Grok Bot, ChatGPT, OpenAI, Claude, Anthropic, and Cursor are trademarks of their respective owners. This project is not affiliated with them.

## Test on a separate instance

Do this before connecting a client you have not used with this bridge, including ChatGPT:

- A new `data_dir` (new SQLite file and a new signing key)
- A new loopback port
- A new public HTTPS origin, so the issuer string is not the one a live connector already trusts
- Do not copy the live signing key or the live database into the test directory
- Turn the test instance off when you are done, or leave it only if you mean to keep that client

A test instance that reuses the live issuer will fight the live client over client ids and refresh tokens.

## Shared connection steps

1. Give the client the MCP URL, which is the `resource` value, normally `https://your-host/mcp`.
2. Do not paste a bearer token into the client. The client must run the authorization-code flow.
3. Approve the consent page only after you read the client name and the redirect URI. A loopback or non-HTTPS redirect is called out on the page because any local program can receive that code.
4. Call `bridge_status`, then `ask_hermes` with a short prompt, then `get_job`.

## Grok Bot

Tested, as a custom connector against the private deployment this design comes from. Not re-verified by CI.

1. Add a custom connector whose URL is the MCP resource (`https://your-host/mcp`).
2. Leave the static authorization header empty.
3. Complete the consent page with the owner password.
4. The first tool call may be what opens the browser, depending on the product version.

If the connector reports an unknown client after you move the process or change `data_dir`, the old dynamic client id was stored in the previous database. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## ChatGPT

Expected to work with a developer-mode custom MCP connector. Not yet verified live. OpenAI's authentication guide (checked November 2026) is the source for the behavior below: <https://developers.openai.com/plugins/build/auth>.

This server is set up for the path that guide describes:

- Protected resource metadata at `/.well-known/oauth-protected-resource` and at `/.well-known/oauth-protected-resource/mcp`
- Authorization server metadata with `code_challenge_methods_supported: ["S256"]`
- `client_id_metadata_document_supported: true` and a `registration_endpoint`
- `token_endpoint_auth_methods_supported: ["none"]` only. ChatGPT's CIMD document also offers `private_key_jwt`. The intersection with this server is `none`, so ChatGPT uses the public client. This server does not verify private-key JWT client assertions.
- `authorization_response_iss_parameter_supported: true`, and every authorization redirect (success or error) includes `iss` equal to the metadata `issuer`. That is what lets ChatGPT use the stable redirect URI.
- The `resource` parameter is required and must equal the `resource` value in the protected-resource document. It is copied into the access token `aud` claim.

### Redirect URIs

When the authorization server meets the issuer-identification rules above, ChatGPT uses:

`https://chatgpt.com/connector_platform_oauth_redirect`

and the stable CIMD document:

`https://chatgpt.com/oauth/client.json`

When the server does not meet those rules, ChatGPT uses a per-connection redirect:

`https://chatgpt.com/connector/oauth/{callback_id}`

and a per-connection CIMD URL `https://chatgpt.com/oauth/{callback_id}/client.json`.

Both HTTPS forms are accepted here. DCR stores the exact URI ChatGPT registers. CIMD uses the redirect list inside the fetched document. Copy the URI from the connector's page in ChatGPT if the client reports a mismatch. The management page is the authority for which mode that connection is in.

`private_key_jwt` is not implemented. If a future ChatGPT CIMD document drops `none`, token requests will fail with `invalid_client` until that method is added.

### Connect

1. Stand up the separate test instance from the section above. Prefer `consent.exposure: public` so the approval page is on the same origin as the issuer.
2. In ChatGPT, enable developer mode and add a custom MCP connector. Paste the test instance's MCP URL (`https://test-host/mcp`).
3. Choose the registration method the UI offers. CIMD and DCR are both advertised. You do not create a client id yourself.
4. Start a tool call so ChatGPT opens the approval page. Enter the test instance's owner password. Deny if the redirect host is not `chatgpt.com`.
5. Run the checklist below. Then decide whether to keep the test instance.

### ChatGPT test checklist

Work through these on the test instance, not on a live connector.

Discovery

- `GET /.well-known/oauth-protected-resource` returns your `resource` and `authorization_servers: [issuer]`
- `GET /.well-known/oauth-authorization-server` includes S256, the registration endpoint, CIMD support, `token_endpoint_auth_methods_supported: ["none"]`, and `authorization_response_iss_parameter_supported: true`
- `POST /mcp` with no token returns 401 and `WWW-Authenticate` with `resource_metadata`

Registration

- CIMD: the bridge fetches the `client_id` URL ChatGPT sends (stable or per-connection) and the consent page shows the name from that document
- DCR: ChatGPT calls `POST /oauth/register` once. A restart of the bridge with the same `data_dir` does not force a new client id
- A second test connection may register another client. That is expected for DCR

Consent

- The page shows the redirect URI before you approve
- The redirect back to ChatGPT includes `code`, `state`, and `iss`
- `iss` matches the metadata issuer exactly, including scheme, host, and port, with no trailing slash
- A wrong owner password does not redirect. That attempt is discarded. Start the connection again
- Deny returns the browser to the redirect URI with `error=access_denied` and `iss`

Token refresh

- ChatGPT can call a tool after the access token expires (15 minutes by default) without a new consent, using the refresh token
- Reusing an already rotated refresh token fails, and the replacement token fails too (the family is revoked)

Tools

- `bridge_status` returns `hermes_reachable: true` while the gateway is up, and `false` when it is stopped
- `ask_hermes` returns a job id quickly
- `get_job` moves from `queued` or `running` to `succeeded` and includes `result`
- `ask_hermes_sync` is absent unless you enabled it. If enabled, a slow run returns `error.code = timeout` and a job id you can poll
- A call to any other tool name fails. There is no shell tool and no pass-through of Hermes tools

Expected errors

- `invalid_request` and no redirect when `redirect_uri` is not one of the registered URIs
- `invalid_target` when `resource` is missing or is not the exact configured resource
- `invalid_grant` when the code verifier is wrong, the code is reused, or a refresh token is reused
- `invalid_client` when the client id is unknown. The usual cause after a restart is a different `data_dir`, or a deleted database. A restart that keeps the same directory does not do this
- 401 on `/mcp` when the access token is expired, signed by a different key, or has the wrong audience

## Claude

Expected to work. Not verified live.

Public guides name `https://claude.ai/api/mcp/auth_callback` (and the `claude.com` host) as the redirect, and describe CIMD when the server advertises it. Add a custom connector with the MCP URL. Approve the consent page. If Claude sends a `client_id` that is an `https://` URL, this server fetches it and checks that the document's `client_id` matches the URL and that `none` is one of the token auth methods.

Claude Code's loopback redirect uses a port that changes. This server ignores the port on loopback HTTP redirects and treats `localhost`, `127.0.0.1`, and `[::1]` as the same host. The consent page warns when the redirect is loopback.

## Cursor

Expected to work. Not verified live.

Cursor's DCR request has been reported to include three redirect URIs: a `cursor://` URI, an HTTPS URI on `cursor.com`, and a loopback URI. This server keeps the ones it can use and returns that list. It does not fail the whole registration because one URI was dropped. The default config allows the `cursor` scheme when the authority contains a dot, which covers `cursor://anysphere.cursor-mcp/oauth/callback`. Set `redirects.private_schemes` to `[]` to refuse that scheme. The HTTPS and loopback URIs still register.

Add the MCP URL as a remote server. Do not rely on a static bearer. This bridge does not accept a static API token on `/mcp`.
