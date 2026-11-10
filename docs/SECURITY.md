# Security

The bridge is a prompt queue in front of a local agent, with OAuth in front of the queue. It is not a sandbox.

## Threat model

You are the owner of one machine. A remote MCP client, and anyone who can drive that client, can submit prompts that the local Hermes agent will run with whatever tools you have enabled on the agent. The bridge does not see those tools and cannot take them away.

What the bridge does:

- Hides the agent API. The API key and the loopback port are not given to the remote client.
- Requires OAuth 2.1 with PKCE S256 before any tool call. There is no anonymous mode and no static bearer on `/mcp`.
- Requires the owner password before an authorization code is issued. A stolen authorization request is not enough.
- Binds to loopback unless you explicitly allow a private address. `0.0.0.0` and `::` are refused.
- Stores the signing key, the owner password file, the API key file, and the SQLite database with mode `0600` (the data directory is `0700`).
- Stores refresh tokens and authorization codes as SHA-256 hashes.
- Rotates refresh tokens. Presenting a token that was already used revokes that token's family, including access-token IDs already issued.
- Rejects a `resource` value that is not the exact configured resource, and puts that resource in the token audience.
- Fetches Client ID Metadata Documents over HTTPS only, with certificate verification, no redirects, a size cap, and a DNS check that refuses addresses that are not globally routable. TLS still has to match the hostname.
- Drops redirect URIs that are not HTTPS, loopback HTTP, or an allowed private-use scheme. One rejected URI does not discard the rest of a registration.
- Scrubs the owner password, the agent API key, bearer tokens, and PEM blocks from logs and from text stored as a job result.
- Does not write an access log, so authorization query strings are not recorded by this process.
- Ignores `X-Forwarded-For`. uvicorn is started with proxy headers disabled. Behind Tailscale Funnel every peer looks like loopback, so rate limits are global on that path. The owner password and PKCE are the controls that still work there.

What it does not do:

- It does not restrict Hermes tools. A prompt can ask the agent to use the terminal, the network, or any other tool that agent profile allows. Tighten that on the Hermes side before you expose the bridge.
- It does not authenticate multiple people. Everyone who knows the owner password can approve a client.
- It does not terminate mutual TLS. ChatGPT can present a client certificate to an HTTPS server that is configured for it. Tailscale Funnel terminates TLS, and this process speaks HTTP on loopback, so that certificate is not visible here.
- It does not implement `private_key_jwt`. Metadata advertises only `none`.
- It does not expire a refresh token when you change the owner password. Delete `data/bridge.sqlite3` (and expect every client to register again) or call the revocation endpoint with the refresh token.

## Tool allowlist

The MCP server registers only:

- `ask_hermes`
- `get_job`
- `bridge_status`
- `ask_hermes_sync` when `limits.sync_ask_enabled` is true

Any other `tools/call` name returns an error result. Job ids are unguessable, and `get_job` only returns a job for the token's subject. There is one subject, `owner` by default.

Prompts are length-capped. The queue is depth-capped. Asks are rate-limited. One worker runs one agent call at a time. Output stored in SQLite is truncated to `max_output_chars`. Job rows are deleted after `job_retention_hours`.

## Consent

The approval page shows the client name, the exact redirect URI, and the scope (`hermes:ask`). Approving requires the owner password. A wrong password discards that request. Denying redirects back to the client with `access_denied` and `iss`.

`consent.exposure: public` serves the page on the issuer origin. Anyone who can open that URL can *see* the page for a request they started themselves. They cannot mint a token without the password. Prefer a long password (`init` generates one).

`consent.exposure: tailnet` does not mount `/oauth/authorize` on the public port. Run a second listener and publish it only inside your tailnet (Tailscale Serve, not Funnel). The metadata authorization endpoint then uses `consent.public_url`, which may be a different origin from the issuer. Some clients insist that every endpoint share the issuer origin. If a client does, use public consent.

The consent page warns when the redirect is not HTTPS, because a local process can bind a loopback port and receive the code. That is inherent to desktop OAuth. Approve those only when you just started the login.

Private-use schemes such as `cursor` are off the strict reading of RFC 8252 section 8.4 when the scheme name has no dot. They are accepted only if you list the scheme and the URI authority contains a dot. Remove `cursor` from `redirects.private_schemes` to refuse it.

## Tokens and keys

Access tokens are RS256 JWTs (`typ: at+jwt`) with a default lifetime of 15 minutes, and never longer than 15 minutes. The resource server checks the signature, `iss`, `aud`, `exp`, `nbf`, `scope`, and that the `jti` is not revoked. Headers that embed a key (`jwk`, `jku`, and the `x5*` variants) are rejected.

Refresh tokens are opaque, stored hashed, and rotated on every use. Default lifetime is 30 days, capped at 90.

The signing key is created on first run at `data/signing-key.pem` if it is missing. If that file is group- or world-readable, the process refuses to start. To rotate it, stop the bridge, move the pem aside, and start again. Every existing access token stops verifying. Refresh tokens in SQLite will still be accepted and will be signed with the new key, so delete the SQLite file at the same time if you want a full cut-over. Clients must register and consent again.

Do not commit `data/`, `secrets/`, `.env`, or any `*.pem`.

## What is public if you enable Funnel

Funnel publishes the public port. That includes:

- Discovery documents
- The DCR endpoint (unauthenticated, rate limited, public clients only)
- The token and revocation endpoints
- JWKS
- `/mcp`, which still requires a valid access token
- The consent page, if exposure is `public`

It does not include the Hermes port, the signing key, or the database, unless you bind those on a published port yourself. Do not.

A tailnet-only consent port should appear in `tailscale serve status` and not in the funnel configuration.
