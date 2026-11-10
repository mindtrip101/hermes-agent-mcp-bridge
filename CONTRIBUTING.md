# Contributing

Bugs and patches are welcome. Please keep the tree free of live hostnames, account names, tokens, and private keys.

## Develop

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check hermes_agent_mcp_bridge tests
.venv/bin/pytest -q
```

Tests use a fake Hermes backend. Do not point them at a live agent or add credentials to the tree. `init` writes `data/` and `secrets/` when you run it locally. Those paths are gitignored. The sanitization test skips gitignored files and still fails if a private key or an email address is tracked.

`tests/test_sanitization.py` is the check CI runs for identifiers that must not appear in a public tree. `.gitleaks.toml` is the same check for the gitleaks job. If you add a rule, keep the forbidden strings out of source files other than that config. The test builds its needles by concatenation so the source does not contain them.

## Runtime rules

- New MCP tools need an explicit decision. The default is that the remote client can queue a prompt and read the result, and nothing else.
- OAuth stays a public client: PKCE S256, no client secret, no `private_key_jwt` unless someone implements the assertion check and the tests for it.
- State that has to survive a restart goes in SQLite. Do not keep refresh tokens or DCR clients only in memory.
- The signing key stays on disk, mode `0600`, created on first run, and never committed.
- Do not log tokens, the owner password, the Hermes API key, or prompt bodies.

## License

Contributions are under the MIT license in `LICENSE`.
