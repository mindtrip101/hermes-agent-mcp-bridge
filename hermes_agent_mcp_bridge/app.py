"""ASGI application. The public listener and the consent listener share one Runtime."""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from hermes_agent_mcp_bridge import __version__
from hermes_agent_mcp_bridge.config import Settings
from hermes_agent_mcp_bridge.errors import ConfigError
from hermes_agent_mcp_bridge.hermes import CliHermes, HermesBackend, HttpHermes
from hermes_agent_mcp_bridge.jobs import JobWorker
from hermes_agent_mcp_bridge.keys import load_or_create
from hermes_agent_mcp_bridge.mcp_rpc import handle_mcp
from hermes_agent_mcp_bridge.oauth import (
    authorize_get,
    authorize_post,
    jwks,
    metadata_document,
    protected_resource,
    register,
    revoke,
    token,
)
from hermes_agent_mcp_bridge.runtime import Runtime
from hermes_agent_mcp_bridge.scrub import install_scrubber
from hermes_agent_mcp_bridge.store import Store

Role = str


def build_runtime(
    settings: Settings,
    *,
    password: str,
    api_key: str = "",
    backend: HermesBackend | None = None,
    cimd_transport: object | None = None,
) -> Runtime:
    data = Path(settings.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    key, kid = load_or_create(data / "signing-key.pem")
    store = Store(data / "bridge.sqlite3")
    if backend is None:
        backend = _default_backend(settings, api_key, data)
    secrets = [password]
    if api_key:
        secrets.append(api_key)
    install_scrubber(secrets)
    runtime = Runtime(
        settings=settings,
        store=store,
        key=key,
        kid=kid,
        backend=backend,
        password=password,
        secret_values=secrets,
        cimd_transport=cimd_transport,  # type: ignore[arg-type]
    )
    runtime.worker = JobWorker(
        store,
        backend,
        interval=settings.limits.worker_interval_seconds,
        poll_interval=settings.hermes.poll_interval_seconds,
        run_timeout=settings.hermes.run_timeout_seconds,
        max_output=settings.limits.max_output_chars,
        secrets=secrets,
        retention_hours=settings.limits.job_retention_hours,
    )
    return runtime


def create_app(runtime: Runtime, *, role: Role = "public") -> Starlette:
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if role == "public" and runtime.worker is not None:
            runtime.worker.start()
        try:
            yield
        finally:
            if role == "public" and runtime.worker is not None:
                runtime.worker.stop()

    routes = _routes(runtime, role)
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(HostGate, allowed=runtime.settings.allowed_hostnames)
    return app


def _routes(runtime: Runtime, role: Role) -> list[Route]:
    async def healthz(_request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__}, headers={"cache-control": "no-store"})

    async def metadata(_request: Request) -> Response:
        return JSONResponse(metadata_document(runtime), headers={"cache-control": "public, max-age=60"})

    async def resource_meta(_request: Request) -> Response:
        return JSONResponse(protected_resource(runtime), headers={"cache-control": "no-store"})

    async def resource_meta_path(request: Request) -> Response:
        expected = urlsplit(runtime.settings.server.resource or "").path.lstrip("/")
        if request.path_params.get("suffix", "") != expected:
            return Response(status_code=404)
        return await resource_meta(request)

    async def do_register(request: Request) -> Response:
        return await register(request, runtime)

    async def do_authorize_get(request: Request) -> Response:
        if role == "public" and runtime.settings.consent.exposure == "tailnet":
            return Response(status_code=404)
        return await authorize_get(request, runtime)

    async def do_authorize_post(request: Request) -> Response:
        if role == "public" and runtime.settings.consent.exposure == "tailnet":
            return Response(status_code=404)
        return await authorize_post(request, runtime)

    async def do_token(request: Request) -> Response:
        return await token(request, runtime)

    async def do_revoke(request: Request) -> Response:
        return await revoke(request, runtime)

    async def do_jwks(_request: Request) -> Response:
        return jwks(runtime)

    async def do_mcp(request: Request) -> Response:
        return await handle_mcp(request, runtime)

    async def mcp_method_not_allowed(_request: Request) -> Response:
        return Response(status_code=405)

    async def mcp_delete(_request: Request) -> Response:
        return Response(status_code=200)

    async def missing(_request: Request) -> Response:
        return Response(status_code=404)

    public = [
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", endpoint=metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", endpoint=metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", endpoint=resource_meta, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/{suffix:path}", endpoint=resource_meta_path, methods=["GET"]),
        Route("/oauth/register", endpoint=do_register, methods=["POST"]),
        Route("/oauth/token", endpoint=do_token, methods=["POST"]),
        Route("/oauth/revoke", endpoint=do_revoke, methods=["POST"]),
        Route("/oauth/jwks", endpoint=do_jwks, methods=["GET"]),
        Route("/mcp", endpoint=do_mcp, methods=["POST"]),
        Route("/mcp", endpoint=mcp_method_not_allowed, methods=["GET"]),
        Route("/mcp", endpoint=mcp_delete, methods=["DELETE"]),
        Route("/oauth/authorize", endpoint=do_authorize_get, methods=["GET"]),
        Route("/oauth/authorize", endpoint=do_authorize_post, methods=["POST"]),
        Route("/{rest:path}", endpoint=missing, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    ]
    if role == "consent":
        return [
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/oauth/authorize", endpoint=do_authorize_get, methods=["GET"]),
            Route("/oauth/authorize", endpoint=do_authorize_post, methods=["POST"]),
            Route(
                "/{rest:path}",
                endpoint=missing,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            ),
        ]
    return public


class HostGate:
    def __init__(self, app: ASGIApp, allowed: set[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not _host_allowed(_header(scope, b"host"), self.allowed):
            await _plain(send, 400)
            return
        await self.app(scope, receive, send)


def load_owner_password(settings: Settings, *, generate: bool) -> tuple[str, bool]:
    env = os.environ.get("HERMES_BRIDGE_OWNER_PASSWORD", "")
    if env:
        _check_secret(env)
        return env, False
    path = Path(settings.consent.owner_password_file)
    if path.is_file():
        _mode_0600(path, dev_mode=settings.server.dev_mode)
        text = _read_secret(path)
        _check_secret(text)
        return text, False
    if not generate:
        raise ConfigError("owner password is missing")
    password = _write_new_secret(path)
    return password, True


def load_api_key(settings: Settings) -> str:
    env = os.environ.get("HERMES_BRIDGE_HERMES_API_KEY", "")
    if env:
        _check_secret(env)
        return env
    path = Path(settings.hermes.api_key_file)
    if not path.is_file():
        if settings.hermes.backend == "http":
            raise ConfigError("hermes api key is missing")
        return ""
    _mode_0600(path, dev_mode=settings.server.dev_mode)
    text = _read_secret(path)
    _check_secret(text)
    return text


def _default_backend(settings: Settings, api_key: str, data: Path) -> HermesBackend:
    if settings.hermes.backend == "cli":
        return CliHermes(
            settings.hermes.cli_argv,
            timeout=settings.hermes.run_timeout_seconds,
            max_output=settings.limits.max_output_chars,
            workdir=data / "prompts",
        )
    if not api_key:
        raise ConfigError("hermes api key is missing")
    return HttpHermes(
        settings.hermes.base_url,
        api_key,
        instructions=settings.hermes.instructions,
    )


def _check_secret(value: str) -> None:
    if len(value) < 14 or any(ch.isspace() for ch in value):
        raise ConfigError("secret must be one line of at least 14 characters")


def _read_secret(path: Path) -> str:
    raw = path.read_bytes()
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw or b"\r" in raw or b"\x00" in raw:
        raise ConfigError("secret file must be a single line")
    return raw.decode("utf-8")


def _write_new_secret(path: Path) -> str:
    import secrets

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    password = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (password + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return password


def _mode_0600(path: Path, *, dev_mode: bool) -> None:
    if dev_mode:
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError("secret file must be mode 0600")


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _host_allowed(host: str, allowed: set[str]) -> bool:
    if not host or "\n" in host or "\r" in host:
        return False
    name = host
    if host.startswith("["):
        end = host.find("]")
        name = host[1:end] if end > 1 else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name.lower() in allowed


async def _plain(send: Send, status: int) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": b""})
