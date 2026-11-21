# Bridge Status Board

A status view of the bridge: whether it is up, whether the local Hermes API answered, how deep the queue is, and the recent MCP tool calls.

The tools and the page are part of `hermes-agent-mcp-bridge`, not a second server.

| Surface | Auth | What you see |
|---|---|---|
| MCP `bridge_status` | OAuth access token | Version, queue depth, Hermes reachability, sync-ask flag. |
| MCP `recent_calls` | OAuth access token | Tool name, ok or error code, time, and job id. No prompt and no reply. |
| `GET /status` | Open, or `HERMES_BRIDGE_STATUS_TOKEN` when set | HTML for the same health data and recent calls. Job ids and prompts are omitted. |
| `GET /status.json` | Same as `/status` | That document as JSON. |

`recent_calls` takes an optional `limit` from 1 to 50. The default is 20. The bridge keeps the newest 200 calls.

Set `HERMES_BRIDGE_STATUS_TOKEN` when the bridge port is reachable beyond the machine, including through a tunnel. Send `Authorization: Bearer`. A query-string token is rejected. When the variable is unset, `/status` is as reachable as `/healthz`.

This fits a Grok Bot or Cursor custom MCP connector better than a separate web app. The connector URL is the bridge resource (`https://bridge.example.com/mcp`). After the owner-password login, ask for `bridge_status` and then `recent_calls`.

## Setup

Install and start the bridge as in the repository README. Then:

```bash
cp apps/status-board/.env.example apps/status-board/.env
export HERMES_BRIDGE_RESOURCE=http://127.0.0.1:8765/mcp
python -m hermes_status_board connector --resource "$HERMES_BRIDGE_RESOURCE"
```

That prints a client snippet:

```json
{
  "mcpServers": {
    "hermes-bridge-status": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

Use an `https` URL for any host that is not loopback. The command rejects public `http` URLs.

Open the page on the bridge machine:

```bash
curl -sS http://127.0.0.1:8765/status
```

With a token:

```bash
curl -sS -H "Authorization: Bearer $HERMES_BRIDGE_STATUS_TOKEN" http://127.0.0.1:8765/status
```

In Grok Bot, add a custom connector for the resource URL. Do not set a static bearer. Approve the consent page with the owner password, then ask the connector to call `bridge_status` and `recent_calls`.

## Tests

```bash
pytest -q apps/status-board/tests
```

## Demo outline (about 6 minutes)

1. Show `GET /healthz` and then the HTML status page with an empty call table. (1 minute)
2. From a connected client, call `bridge_status`, then `ask_hermes` with a short prompt, then `get_job`. (2 minutes)
3. Refresh `/status` and show the tool names, times, and ok/fail column. Show that the prompt text is not on the page. (1 minute)
4. Call `recent_calls` from the client and show the job id there, then show that `/status.json` does not include it. (1 minute)
5. Set `HERMES_BRIDGE_STATUS_TOKEN`, restart the bridge, and show that the page returns 401 until the bearer header is sent. (1 minute)

Do not put the owner password or the status token in the recording.

## Draft marketplace description

Bridge Status Board shows whether your Hermes MCP bridge is healthy and which tool calls it has handled. Connect a Grok Bot or another remote MCP client to the bridge and call `bridge_status` and `recent_calls`. The same process serves a small status page at `/status` for a person sitting at the machine.

The page lists tool names and outcomes. It does not list prompts or job ids. The authenticated `recent_calls` tool can show job ids so you can poll `get_job`. An optional status token locks the page when the bridge is exposed past localhost.

The agent stays on your machine. This board only reads bridge metadata.

This description is a draft. It has not been submitted. The Origin marketplace lists Origin apps (install, webhook, installation token), and this board is an MCP connector plus a page on the bridge. A listing still needs a decision to wrap it as an Origin app, a private app record, a demo video, and a human submission.
