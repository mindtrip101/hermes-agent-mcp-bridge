# Troubleshooting

Logs are warnings only, and access logs are off. Errors from the OAuth endpoints are JSON (`error` and `error_description`) or, when the redirect URI is already known to belong to the client, a redirect with `error` and `iss`.

## invalid_request

`invalid_request` on the authorization endpoint usually means one of:

- `redirect_uri` is missing, or it is not one of the URIs stored for that client. The response is JSON and there is **no** redirect. The server will not send a browser to a URI the client did not register.
- `state` is missing or contains a space or a non-ASCII character.
- `code_challenge_method` is not `S256`, or `code_challenge` is not a 43-character unpadded base64url digest.
- `response_type` is not `code`. When the redirect URI is valid this one is a redirect with `unsupported_response_type` instead.

A redirect URI mismatch after a ChatGPT change is the common case. ChatGPT picks one of two URIs:

- `https://chatgpt.com/connector_platform_oauth_redirect` when the metadata advertises issuer identification and the redirect includes a matching `iss`
- `https://chatgpt.com/connector/oauth/{callback_id}` otherwise

If you allowlisted only one of them on an older server, the other fails. This server does not use a global allowlist. It accepts the HTTPS URI the client registers (DCR) or publishes (CIMD). A mismatch still happens when:

- The consent was issued for one URI and the token request presents a different one, including a trailing slash or a different port on an HTTPS URI
- The client registered a per-connection URI and you are retrying with the stable URI, or the reverse
- A private-use URI was dropped at registration because the scheme is not listed, and the client then uses that URI

DCR returns the redirect URIs that were actually stored. Compare that list with the URI in the failing request. Loopback HTTP ignores the port. HTTPS does not.

`iss` is compared by clients as an exact string. A trailing slash, an extra port, or `http` versus `https` makes ChatGPT treat the server as failing issuer identification and switch redirect URIs. Set `server.issuer` to the origin with no trailing slash and use that same string in `authorization_servers`.

## invalid_target

The `resource` parameter is missing or is not exactly `server.resource`. Clients send back the `resource` value from the protected-resource document. If the connector was configured with `https://host/mcp` but the document says `https://host`, or the other way around, every token request fails. Make the document, the connector URL, and the token audience the same string. No trailing slash.

## invalid_client and a stale client id after restart

A restart that keeps the same `data_dir` keeps DCR clients and refresh tokens. You should not see `invalid_client` for a client that registered before the restart.

You will see it when:

- `data_dir` changed, or the SQLite file was deleted, so the client id is gone
- The process is a second instance with its own data directory
- The client id is a CIMD URL and the document could not be fetched, the document's `client_id` does not match the URL, or the document does not list `none` as a token auth method
- The token request included a client secret or an `Authorization` header. This server only accepts public clients

CIMD clients are not rows in the client table. They are cached for an hour after a successful fetch. A restart does not forget them as an identity, because the identity is the URL, but the next request refetches if the cache is cold. A network failure on that fetch is `invalid_client` until the fetch works again.

If you rotate the signing key and also delete the database, every connected client must register and consent again. That is expected. If you only restart the process, they should not.

## invalid_grant

The authorization code was already used, expired, or the `code_verifier` does not match the challenge. The code is single-use. A wrong verifier burns it.

A refresh token that was already exchanged is `invalid_grant`, and so is the token that replaced it: reuse revokes the family. The user has to consent again. This is the recovery from a stolen refresh token, and it is also what you see if two clients present the same refresh token.

## 401 on /mcp

The access token is missing, expired (15 minutes by default), signed by a different key, or its `aud` is not the resource. The `WWW-Authenticate` header points at the protected-resource metadata so the client can start OAuth again. A 401 immediately after a restart, with the same `data_dir`, means the key file changed or the token is simply old. Refresh tokens are how the client gets a new access token without consent.

`insufficient_scope` is a 403-class OAuth error carried as 401 with `error="insufficient_scope"` when the token does not contain `hermes:ask`. This server only issues that scope.

## Consent page

A wrong password returns a short HTML error and does not redirect. Start the client connection again. Eight failures in fifteen minutes on the public listener lock the password form; behind Funnel every client shares that bucket because the peer address is loopback.

If `/oauth/authorize` is 404 on the public port, `consent.exposure` is `tailnet` and the page is on the consent port. The metadata `authorization_endpoint` is the URL the browser must open.

## Jobs

`busy` means the queue is at `max_queue`. `rate_limited` means the hourly ask budget or the poll budget is spent. `timeout` on a sync call means the HTTP wait ended. Call `get_job`. The run is still going until it succeeds, fails, or hits `run_timeout_seconds`.

`hermes_reachable: false` means `GET /health` on the Hermes API failed. The bridge does not start Hermes for you. Check the API key file, the base URL, and that the gateway is listening on loopback.

`hermes_error` means the runs API returned a non-success status or the CLI process exited non-zero. The bridge does not store the agent's error text, because that text can contain paths and prompt contents.

## Host 400

A 400 with an empty body on every request is the `Host` check. The header's hostname must be the issuer host, a loopback name, or listed in `server.allowed_hosts`.
