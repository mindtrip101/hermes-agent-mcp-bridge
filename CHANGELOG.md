# Changelog

## Unreleased

- Opt-in agent hub. Profiles can target an OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, llama.cpp) or Anthropic. `delegate_task` uses the cheapest runnable profile unless the task sets `strength` to `strong` or names a profile. Grok Bot (personal and business) and Cursor profiles are listed and are not called. The tools `list_agents`, `delegate_task`, and `task_status` are registered only when `hub.enabled` is true.

## 0.1.0

Initial public release.

- Streamable HTTP MCP server with `ask_hermes`, `get_job`, `bridge_status`, and an optional bounded `ask_hermes_sync`
- OAuth 2.1 authorization server for a public client: discovery (RFC 8414 and RFC 9728), dynamic client registration, Client ID Metadata Documents, PKCE S256, resource indicators, refresh-token rotation, RFC 9207 `iss`, and an owner consent page
- SQLite persistence for clients, refresh tokens, and jobs
- RSA signing key generated on first run and stored mode `0600`
- Hermes 0.21 runs API backend (`POST /v1/runs` on loopback), plus a CLI adapter
- Optional second listener so the consent page can stay off a public funnel
- Commands: `hermes-agent-mcp-bridge init` and `hermes-agent-mcp-bridge serve`
