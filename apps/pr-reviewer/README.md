# Hermes PR Reviewer

On a pull request, this process reads the diff, asks the local Hermes agent through the MCP bridge, and posts a comment. The comment summarizes risks and suggestions. It uses the GitHub review event `COMMENT` and the Origin verdict `comment`. It does not approve the pull request.

The webhook handler speaks both GitHub (`X-Hub-Signature-256`) and Origin (`webhook-signature: v1ed,…`, verified against the Origin JWKS). One listener can serve either forge. See [apps/README.md](../README.md) for the Origin dashboard steps this repo does not perform.

## Setup

Run the bridge first. The reviewer is a second process.

```bash
cd /path/to/hermes-agent-mcp-bridge
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pip install -e apps/pr-reviewer
cp apps/pr-reviewer/.env.example apps/pr-reviewer/.env
```

Export the variables. The process does not read the `.env` file for you. Minimum for a GitHub token demo:

```bash
export HERMES_REVIEW_BRIDGE_URL=http://127.0.0.1:8765
export HERMES_REVIEW_BRIDGE_RESOURCE=http://127.0.0.1:8765/mcp
export HERMES_BRIDGE_OWNER_PASSWORD='the-owner-password'
export GITHUB_WEBHOOK_SECRET='a-long-random-string'
export GITHUB_TOKEN='a-token-that-can-read-the-diff-and-post-a-review'
export HERMES_REVIEW_SYNC=true
.venv/bin/hermes-pr-reviewer check-config
.venv/bin/hermes-pr-reviewer serve
```

`check-config` prints `set` or `missing`. It does not print secret values.

`serve` binds `127.0.0.1:8780` and accepts `POST /webhook`. Put HTTPS in front of that port before either forge can deliver to it. Leave `HERMES_REVIEW_SYNC` unset in production so the handler returns `202` before the agent finishes. Origin waits about 10 seconds for the response headers.

GitHub App credentials (`GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` or `GITHUB_APP_PRIVATE_KEY_FILE`) are used when `GITHUB_TOKEN` is empty. The installation id comes from the webhook.

Origin credentials are `ORIGIN_APP_ID` and `ORIGIN_APP_PRIVATE_KEY` (or `ORIGIN_APP_PRIVATE_KEY_FILE`). At startup the process loads `ORIGIN_JWKS_URL` (default `https://api.cursor.com/v1/origin/keys`) and rejects deliveries it cannot verify. Subscribe the app to:

- `pull_request.created`
- `pull_request.head_ref.pushed`
- `pull_request.reopened`
- `pull_request.published`

Scopes: `repository:pull_requests:read`, `repository:pull_requests:reviews:write`.

Draft pull requests are skipped unless `HERMES_REVIEW_DRAFTS=true`. The same delivery id is ignored for the life of the process. Diffs are truncated to the bridge prompt limit, and text that looks like a bearer token, a GitHub token, an `oit_` token, or a PEM private key is replaced with `[redacted]` before it is sent to the agent or posted.

## Tests

From the repository root, after `pip install -e ".[dev]"` and `pip install -e apps/pr-reviewer`:

```bash
pytest -q apps/pr-reviewer/tests
```

The root `pytest` run includes this suite. Tests use the bridge's in-memory Hermes double and do not call GitHub or Origin.

## Demo outline (about 8 minutes)

1. Show the bridge `GET /healthz` and `hermes-pr-reviewer check-config` with secrets present and absent. (1 minute)
2. Start `serve` with `HERMES_REVIEW_SYNC=true`. Point a GitHub or Origin webhook at the tunnel URL ending in `/webhook`. (1 minute)
3. Open a small pull request that changes one function. (1 minute)
4. Show the webhook returning `reviewed`, then the comment on the pull request: Summary, Risks, Suggestions, and the line that says the comment does not approve. (2 minutes)
5. Push another commit to the same pull request and show a second comment. Show a draft pull request producing no comment. (2 minutes)
6. Open the bridge status page and show the `ask_hermes` call that the review created. Do not show the owner password or the app private key. (1 minute)

## Draft marketplace description

Hermes PR Reviewer comments on pull requests. When a pull request opens or its head moves, the app reads the diff, asks a Hermes agent through your hermes-agent-mcp-bridge, and posts a review comment with a summary, risks, and suggestions. The comment does not approve the change. The agent and its tools stay on your machine. The app only sees the diff you already host in the repository and the text the agent returns.

Install it on the repositories you want reviewed. The bridge must already be running and reachable from the app. Uninstall removes the webhook. Existing comments stay on the pull requests.

This description is a draft. It has not been submitted.
