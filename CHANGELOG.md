# Changelog

## 0.1.0

Initial public release.

- Streamable HTTP MCP server with `ask_hermes`, `get_job`, `bridge_status`, and an optional bounded `ask_hermes_sync`
- OAuth 2.1 authorization server: discovery (RFC 8414 and RFC 9728), dynamic client registration, Client ID Metadata Documents, PKCE S256, resource indicators, refresh-token rotation, RFC 9207 `iss`, and an owner consent page
- SQLite persistence for clients, refresh tokens, and jobs
- RSA signing key generated on first run and stored mode `0600`
- Hermes 0.21 runs API backend, plus a CLI adapter
- Optional second listener so the consent page can stay off a public funnel
