"""Streamable HTTP MCP, JSON responses, allowlisted tools only."""

from __future__ import annotations

import json
import time
from typing import Any

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hermes_agent_mcp_bridge import SCOPE, SERVER_NAME, __version__
from hermes_agent_mcp_bridge.oauth import resource_metadata_url
from hermes_agent_mcp_bridge.runtime import Runtime
from hermes_agent_mcp_bridge.store import JobRecord
from hermes_agent_mcp_bridge.tokens import decode_access_token

_SUPPORTED = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
_MESSAGES = {
    "invalid_input": "The prompt was empty or too long.",
    "rate_limited": "Too many requests. Wait and try again.",
    "busy": "The queue is full.",
    "timeout": "The wait elapsed. Poll get_job.",
    "hermes_error": "The local agent run failed.",
    "not_found": "No job with that id was found.",
    "interrupted": "The local agent run was interrupted.",
}


def tools(runtime: Runtime) -> list[dict[str, Any]]:
    prompt_max = runtime.settings.limits.max_prompt_chars
    listed = [
        _tool(
            "ask_hermes",
            "Queue a prompt for the local Hermes agent. Returns a job id. Poll get_job for the reply.",
            {"prompt": {"type": "string", "minLength": 1, "maxLength": prompt_max}},
            ["prompt"],
            read_only=False,
            open_world=True,
        ),
        _tool(
            "get_job",
            "Read one queued job, including the reply when it has finished.",
            {"job_id": {"type": "string", "minLength": 4, "maxLength": 80}},
            ["job_id"],
            read_only=True,
            open_world=False,
        ),
        _tool(
            "bridge_status",
            "Report bridge version, queue depth, and whether the local agent API answered a health check.",
            {},
            [],
            read_only=True,
            open_world=False,
        ),
    ]
    if runtime.settings.limits.sync_ask_enabled:
        listed.append(
            _tool(
                "ask_hermes_sync",
                "Queue a prompt and wait up to the configured limit for the reply. On timeout, poll get_job.",
                {"prompt": {"type": "string", "minLength": 1, "maxLength": prompt_max}},
                ["prompt"],
                read_only=False,
                open_world=True,
            )
        )
    return listed


async def handle_mcp(request: Request, runtime: Runtime) -> Response:
    header_version = request.headers.get("mcp-protocol-version")
    if header_version is not None and header_version not in _SUPPORTED:
        return Response(status_code=400)
    accept = request.headers.get("accept", "")
    if accept and "application/json" not in accept and "*/*" not in accept:
        if "text/event-stream" in accept:
            body = await _dispatch(request, runtime)
            if isinstance(body, Response):
                return body
            if body is None:
                return Response(status_code=202)
            payload = json.dumps(body, separators=(",", ":"))
            return Response(
                content=f"event: message\ndata: {payload}\n\n",
                media_type="text/event-stream",
                headers={"cache-control": "no-store"},
            )
        return Response(status_code=406)
    body = await _dispatch(request, runtime)
    if isinstance(body, Response):
        return body
    if body is None:
        return Response(status_code=202)
    return JSONResponse(body, headers={"cache-control": "no-store"})


async def _dispatch(request: Request, runtime: Runtime) -> dict[str, Any] | Response | None:
    claims = _authenticate(request, runtime)
    if isinstance(claims, Response):
        return claims
    raw = await request.body()
    if len(raw) > 65_536:
        return _rpc_error(None, -32700, "Parse error")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return _rpc_error(None, -32700, "Parse error")
    if isinstance(message, list) or not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    method = message.get("method")
    rpc_id = message.get("id")
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if not isinstance(method, str) or "id" not in message:
        return _rpc_error(rpc_id if _id_ok(rpc_id) else None, -32600, "Invalid Request")
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        requested = str(params.get("protocolVersion", "2025-06-18"))
        version = requested if requested in _SUPPORTED else "2025-06-18"
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tools(runtime)}}
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict):
            return _rpc_error(rpc_id, -32602, "Invalid params")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": _call(runtime, str(claims["sub"]), params)}
    return _rpc_error(rpc_id, -32601, "Method not found")


def _call(runtime: Runtime, owner: str, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _tool_error("invalid_input")
    allowed = {item["name"] for item in tools(runtime)}
    if name not in allowed:
        return _tool_error("invalid_input")
    if name == "bridge_status":
        return _ok(_status_payload(runtime))
    if name == "get_job":
        if not runtime.limiter.allow(f"poll:{owner}", runtime.settings.limits.polls_per_minute, 60):
            return _tool_error("rate_limited")
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str):
            return _tool_error("invalid_input")
        job = runtime.store.get_job(job_id, owner)
        if job is None:
            return _tool_error("not_found")
        return _ok(_job_payload(job))
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _tool_error("invalid_input")
    if len(prompt) > runtime.settings.limits.max_prompt_chars:
        return _tool_error("invalid_input")
    if not runtime.limiter.allow(f"ask:{owner}", runtime.settings.limits.asks_per_hour, 3600):
        return _tool_error("rate_limited")
    if runtime.store.queue_depth() >= runtime.settings.limits.max_queue:
        return _tool_error("busy")
    job_id = runtime.store.create_job(owner_sub=owner, prompt=prompt)
    if name == "ask_hermes_sync":
        return _ok(_wait(runtime, owner, job_id))
    return _ok({"job_id": job_id, "status": "queued"})


def _wait(runtime: Runtime, owner: str, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + runtime.settings.limits.sync_timeout_seconds
    while time.monotonic() < deadline:
        job = runtime.store.get_job(job_id, owner)
        if job is not None and job.status in {"succeeded", "failed"}:
            return _job_payload(job)
        time.sleep(0.05)
    job = runtime.store.get_job(job_id, owner)
    payload = _job_payload(job) if job is not None else {"job_id": job_id, "status": "queued"}
    payload["error"] = {"code": "timeout", "message": _MESSAGES["timeout"]}
    return payload


def _status_payload(runtime: Runtime) -> dict[str, Any]:
    try:
        reachable = bool(runtime.backend.health())
    except Exception:
        reachable = False
    return {
        "version": __version__,
        "queue_depth": runtime.store.queue_depth(),
        "hermes_reachable": reachable,
        "sync_ask_enabled": runtime.settings.limits.sync_ask_enabled,
    }


def _job_payload(job: JobRecord) -> dict[str, Any]:
    body: dict[str, Any] = {"job_id": job.job_id, "status": job.status, "truncated": job.truncated}
    if job.status == "succeeded":
        body["result"] = job.result_text or ""
    if job.error_code:
        body["error"] = {"code": job.error_code, "message": _MESSAGES.get(job.error_code, "The request failed.")}
    return body


def _authenticate(request: Request, runtime: Runtime) -> dict[str, Any] | Response:
    header = request.headers.get("authorization")
    token = _bearer(header)
    if token is None:
        return _unauthorized(runtime, "invalid_token")
    try:
        claims = decode_access_token(
            token,
            key=runtime.key,
            kid=runtime.kid,
            issuer=runtime.settings.server.issuer,
            resource=runtime.settings.server.resource or "",
            clock_skew=runtime.settings.tokens.clock_skew_seconds,
            max_ttl=runtime.settings.tokens.access_ttl_seconds,
        )
    except jwt.PyJWTError:
        return _unauthorized(runtime, "invalid_token")
    jti = claims.get("jti")
    if not isinstance(jti, str) or runtime.store.jti_revoked(jti):
        return _unauthorized(runtime, "invalid_token")
    sub = claims.get("sub")
    if not isinstance(sub, str) or sub != runtime.settings.subject:
        return _unauthorized(runtime, "invalid_token")
    scope = claims.get("scope")
    if not isinstance(scope, str) or SCOPE not in scope.split(" "):
        return _unauthorized(runtime, "insufficient_scope")
    return claims


def _bearer(header: str | None) -> str | None:
    if header is None or header != header.strip() or "\n" in header or "\r" in header:
        return None
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or len(parts[1]) > 8192:
        return None
    return parts[1]


def _unauthorized(runtime: Runtime, error: str) -> Response:
    metadata = resource_metadata_url(runtime.settings.server.resource or "")
    challenge = f'Bearer error="{error}", resource_metadata="{metadata}"'
    if error == "insufficient_scope":
        challenge += f', scope="{SCOPE}"'
    return Response(status_code=401, headers={"www-authenticate": challenge, "cache-control": "no-store"})


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool,
    open_world: bool,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "openWorldHint": open_world,
        },
    }


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(code: str) -> dict[str, Any]:
    payload = {"error": {"code": code, "message": _MESSAGES.get(code, "The request failed.")}}
    body = _ok(payload)
    body["isError"] = True
    return body


def _rpc_error(rpc_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _id_ok(value: object) -> bool:
    return value is None or isinstance(value, str | int) and not isinstance(value, bool)
