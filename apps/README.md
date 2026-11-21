# Companion apps

Private builds that sit next to `hermes-agent-mcp-bridge`. Nothing in this tree is submitted to a marketplace, published as a package, or made public.

| App | Folder | What it is |
|---|---|---|
| Hermes PR Reviewer | [pr-reviewer](pr-reviewer/README.md) | Webhook listener. On a pull request it sends the diff to Hermes through the bridge and posts a comment review. |
| Bridge Doctor | [bridge-doctor](bridge-doctor/README.md) | CLI that checks config, secrets, reachability, auth, tool list, and version. A GitHub Actions workflow runs the offline check. |
| Bridge Status Board | [status-board](status-board/README.md) | `bridge_status` and `recent_calls` on the bridge, plus `GET /status`. |

## Origin packaging that is still manual

Cursor's Origin docs describe an app as a dashboard record, not a file in this repository. Create it at `cursor.com/codebase/settings/apps` and leave it **Private**. This repository does not create that record, does not flip visibility, and does not submit a listing.

What the docs require before a marketplace submission, and what this tree does not do:

- Register an Ed25519 signing key and store only the private key in an environment variable.
- Set an HTTPS webhook URL. Origin signs deliveries with `webhook-signature: v1ed,...` over the hex SHA-256 of `webhook-id.webhook-timestamp.body`. GitHub deliveries use `X-Hub-Signature-256`. The reviewer verifies both.
- Subscribe the reviewer to `pull_request.created`, `pull_request.head_ref.pushed`, `pull_request.reopened`, and `pull_request.published`. Installation events arrive without a subscription.
- Grant `repository:pull_requests:read` and `repository:pull_requests:reviews:write`. The reviewer posts `verdict: comment` and does not approve.
- Mint installation tokens (`oit_…`) from a short-lived app JWT (`aud: origin-apps`). The reviewer does this per event. Tokens are not written to disk.
- Record a 5–10 minute demo that shows install, uninstall, and the main flow, and host it where a reviewer can watch without signing in.
- Write the marketplace description, pick a slug (permanent once listed), and choose a category.
- Make the app Public, then use Submit to Marketplace. Public needs at least one default scope.

The doctor is a CLI. Listing it would mean an Origin app whose install runs this command and posts a check run (`repository:checks:write`). `--publish origin` posts that run when an installation token is already in the environment. The status board is the bridge's MCP tools and HTML page. The Origin marketplace lists Origin apps, not MCP connectors, so a listing for the status board still needs a product decision.

Each app README has setup, a demo outline, and a draft marketplace description.
