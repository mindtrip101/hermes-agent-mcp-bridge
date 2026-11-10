# Configuration

Copy `config.example.yaml` to `config.yaml`. Paths inside the file are resolved relative to the file, and `~` is expanded. Environment variables override the file. An empty environment variable is ignored.

`hermes-agent-mcp-bridge init --config config.yaml` creates the signing key and the owner password. `serve` reads both and will not start if the password is missing.

Secrets are not fields on the config model. The owner password is `HERMES_BRIDGE_OWNER_PASSWORD` or the password file. The Hermes API key is `HERMES_BRIDGE_HERMES_API_KEY` or `hermes.api_key_file`. Both files must be a single line, mode `0600` (the mode check is skipped when `server.dev_mode` is true), and at least 14 characters with no spaces.

## server

| Field | Default | Meaning |
|---|---|---|
| `bind_host` | `127.0.0.1` | Loopback, or a private address if `allow_non_loopback_bind` is true. `0.0.0.0` and `::` are refused. |
| `bind_port` | `8765` | Public listener. |
| `issuer` | required | Exact origin of this process. No path, no trailing slash. HTTPS unless `dev_mode`. |
| `resource` | `issuer + /mcp` | Exact resource indicator and token audience. No trailing slash. Same scheme, host, and port as the issuer. |
| `allowed_hosts` | empty | Extra `Host` header names. The issuer host and loopback names are always allowed. |
| `allow_non_loopback_bind` | false | Allow a private or link-local bind address. |
| `dev_mode` | false | Allow an `http://` issuer for local tests. |

`HERMES_BRIDGE_BIND_HOST`, `HERMES_BRIDGE_BIND_PORT`, `HERMES_BRIDGE_ISSUER`, `HERMES_BRIDGE_RESOURCE`, `HERMES_BRIDGE_DEV_MODE`, `HERMES_BRIDGE_ALLOW_NON_LOOPBACK_BIND`, `HERMES_BRIDGE_ALLOWED_HOSTS` (comma-separated).

## consent

| Field | Default | Meaning |
|---|---|---|
| `exposure` | `public` | `public` mounts the approval page on the issuer. `tailnet` serves it only on the consent port. |
| `bind_host` | `127.0.0.1` | Consent listener. Loopback only. |
| `bind_port` | `8766` | Must differ from the public port when exposure is `tailnet`. |
| `public_url` | unset | Required for `tailnet`. Forbidden for `public`. Exact origin, no trailing slash. |
| `owner_password_file` | `secrets/owner_password` | Created by `init`. |

`HERMES_BRIDGE_CONSENT_EXPOSURE`, `HERMES_BRIDGE_CONSENT_BIND_PORT`, `HERMES_BRIDGE_CONSENT_PUBLIC_URL`, `HERMES_BRIDGE_OWNER_PASSWORD_FILE`, `HERMES_BRIDGE_OWNER_PASSWORD`.

## hermes

| Field | Default | Meaning |
|---|---|---|
| `backend` | `http` | `http` calls `POST /v1/runs` and `GET /v1/runs/{id}`. `cli` runs `cli_argv`. |
| `base_url` | `http://127.0.0.1:8642` | Hermes API origin. No path. Loopback unless `allow_non_loopback`. Public IP addresses are refused even then. |
| `api_key_file` | `secrets/hermes_api_key` | Bearer token for the Hermes API. Required for `http` unless the env var is set. |
| `allow_non_loopback` | false | Allow a hostname or a private address, for a bridge running in a container. |
| `poll_interval_seconds` | `1` | Delay between run polls. |
| `run_timeout_seconds` | `300` | Wall clock for one run. |
| `instructions` | empty | Optional string sent as the runs API `instructions` field. 2000 characters at most. |
| `cli_argv` | empty | Required for `cli`. Must include the literal `{prompt_file}`. Must not contain `ignore-rules`. Config file only. |

`HERMES_BRIDGE_HERMES_BACKEND`, `HERMES_BRIDGE_HERMES_BASE_URL`, `HERMES_BRIDGE_HERMES_API_KEY_FILE`, `HERMES_BRIDGE_HERMES_ALLOW_NON_LOOPBACK`, `HERMES_BRIDGE_HERMES_API_KEY`.

The HTTP backend sends `Idempotency-Key` set to the bridge job id and does not trust `HTTP_PROXY` for that call.

## tokens

| Field | Default | Range |
|---|---|---|
| `access_ttl_seconds` | `900` | 60–900 |
| `refresh_ttl_seconds` | `2592000` (30 days) | 3600–7776000 |
| `code_ttl_seconds` | `120` | 30–600. Also how long the consent page stays valid. |
| `clock_skew_seconds` | `30` | 0–120 |

## limits

| Field | Default | Meaning |
|---|---|---|
| `max_prompt_chars` | `8000` | Reject longer prompts. |
| `max_output_chars` | `32000` | Truncate stored replies. `truncated` is true on the job. |
| `max_queue` | `8` | Queued plus running jobs. Further asks return `busy`. |
| `asks_per_hour` | `30` | Per subject. |
| `polls_per_minute` | `120` | `get_job` rate. |
| `sync_ask_enabled` | false | Register `ask_hermes_sync`. |
| `sync_timeout_seconds` | `30` | 1–120. The job keeps running after a sync timeout. |
| `job_retention_hours` | `168` | Delete finished jobs older than this at startup. |
| `worker_interval_seconds` | `0.5` | Idle delay in the worker. |

`HERMES_BRIDGE_SYNC_ASK` overrides `sync_ask_enabled`.

## redirects

`private_schemes` defaults to `["cursor"]`. A private-use URI is accepted only when its authority contains a dot. HTTPS URIs and loopback HTTP URIs do not need to be listed. Set the list to `[]` to refuse private-use schemes.

`HERMES_BRIDGE_PRIVATE_SCHEMES` is a comma-separated override.

Loopback matching ignores the port and treats `localhost`, `127.0.0.1`, and `::1` as the same host. HTTPS URIs match exactly.

## data_dir and subject

`data_dir` (default `data`, env `HERMES_BRIDGE_DATA_DIR`) holds `signing-key.pem` and `bridge.sqlite3`. Moving it is a new bridge: old clients and tokens will not be found, and a new key will not verify old access tokens.

`subject` defaults to `owner`. It is the only `sub` claim this process issues. Keep it a short token with no spaces.

## Host header

Requests whose `Host` name is not the issuer host, an `allowed_hosts` entry, or a loopback name are rejected with 400. This is the DNS-rebinding check. Behind Funnel the public name must be the issuer host, which is allowed.
