# Bridge Doctor

A command that checks a `hermes-agent-mcp-bridge` install and prints a pass, fail, or skip line for each check, plus a fix that does not contain secret values.

Checks:

| Check | What it looks at |
|---|---|
| `config` | The YAML file parses with the bridge's own loader. |
| `owner_password` | `HERMES_BRIDGE_OWNER_PASSWORD` or the password file. Mode `0600` outside dev mode. |
| `hermes_api_key` | The API key env var or file, unless the backend is `cli`. |
| `data_dir` | The data directory or its parent is writable. |
| `healthz` | `GET /healthz` returns `ok` and a version. |
| `version` | That version matches the installed package, or `--expect-version`. |
| `discovery` | Authorization-server metadata advertises S256, public-client auth, and registration. |
| `resource` | Protected-resource metadata names a resource. |
| `auth` | `POST /mcp` without a token returns 401 and a bearer challenge. |
| `tools` | An owner-password login can list `ask_hermes`, `get_job`, `bridge_status`, and `recent_calls`. |

Without `--online`, the network checks are skipped and the command still exits 0. `--strict` turns skips into a failing exit code. `--require-secrets` fails when the password or API key is missing. A missing file is a skip otherwise, so `config.example.yaml` passes in CI.

`--publish github` or `--publish origin` posts the report as a check run. GitHub needs `GITHUB_TOKEN`, `GITHUB_REPOSITORY` (`owner/name`), and `DOCTOR_HEAD_SHA`. Origin needs `ORIGIN_INSTALLATION_TOKEN`, `ORIGIN_OWNER`, `ORIGIN_REPO`, and `DOCTOR_HEAD_SHA`. The doctor does not mint that installation token. Mint it in the job, pass it in the environment, and do not print it.

## Setup

```bash
cd /path/to/hermes-agent-mcp-bridge
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pip install -e apps/bridge-doctor
cp apps/bridge-doctor/.env.example apps/bridge-doctor/.env
```

Offline, against the example file:

```bash
.venv/bin/hermes-bridge-doctor check --config config.example.yaml
```

Against a bridge you are about to use:

```bash
export HERMES_BRIDGE_OWNER_PASSWORD='the-owner-password'
.venv/bin/hermes-bridge-doctor check \
  --config config.yaml \
  --online http://127.0.0.1:8765 \
  --require-secrets
```

`--online` must be the issuer origin. The tool login uses the resource advertised by that process, so a config file for a different origin does not have to match. When they differ, the report skips `issuer_match` and tells you to point `--config` at the file for that bridge. The login registers a doctor client on the bridge. That client stays in the bridge database.

Exit 0 means nothing failed. Exit 1 means a check failed, or `--strict` saw a skip. Exit 2 is a usage error.

## CI

[`.github/workflows/bridge-doctor.yml`](../../.github/workflows/bridge-doctor.yml) runs the offline check on every pull request. Dispatch the workflow with an `online` input to probe a running bridge. The online step reads `secrets.HERMES_BRIDGE_OWNER_PASSWORD` when you want the tool-list check. It does not use `--require-secrets`, because the Hermes API key stays on the bridge host.

## Tests

```bash
pytest -q apps/bridge-doctor/tests
```

## Demo outline (about 6 minutes)

1. Run the doctor on `config.example.yaml` and read the skips. (1 minute)
2. Point it at a config whose password file is missing. Show the fail line and the fix, and show that the output has no secret. (1 minute)
3. Start the bridge. Run `--online` and `--require-secrets`. Walk the pass lines: health, version, discovery, auth, tools. (2 minutes)
4. Stop the bridge and run the same command. Show the health failure and the skipped follow-ups. (1 minute)
5. Optional: run `--publish github` or `--publish origin` on a throwaway commit and show the check run. Say the token came from the environment. (1 minute)

## Draft marketplace description

Bridge Doctor checks that a Hermes MCP bridge is ready before a client connects. It reads the config file, reports whether the owner password and Hermes API key are present without printing them, and can call a running bridge to confirm discovery, the OAuth challenge, the tool list, and the version. Each failing line includes a fix.

Run it on a laptop after install, or in CI. A pull request workflow in this repository runs the offline half. With a forge token, the same command can post the result as a check run.

The bridge stays on your machine. The doctor does not change bridge settings.

This description is a draft. It has not been submitted. Listing it on the Origin marketplace still needs an Origin app record, the `repository:checks:write` scope if you publish check runs, a demo video, and a human submission. The command itself is the product in this repository.
