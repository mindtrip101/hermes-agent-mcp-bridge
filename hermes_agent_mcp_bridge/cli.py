"""Command line. `init` creates the signing key and owner password. `serve` does not print them."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from hermes_agent_mcp_bridge.app import build_runtime, create_app, load_api_key, load_owner_password
from hermes_agent_mcp_bridge.config import load_settings
from hermes_agent_mcp_bridge.errors import ConfigError


def main(argv: list[str] | None = None) -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="hermes-agent-mcp-bridge")
    parser.add_argument("command", choices=("init", "serve"))
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        config_path = args.config or _config_from_env()
        settings = load_settings(config_path)
        if args.command == "init":
            _init(settings)
            return
        password, _created = load_owner_password(settings, generate=False)
        api_key = load_api_key(settings)
        runtime = build_runtime(settings, password=password, api_key=api_key)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    _serve(runtime)


def _init(settings: object) -> None:
    from hermes_agent_mcp_bridge.app import build_runtime
    from hermes_agent_mcp_bridge.config import Settings

    assert isinstance(settings, Settings)
    password, created = load_owner_password(settings, generate=True)
    api_key = ""
    key_path = Path(settings.hermes.api_key_file)
    if key_path.is_file() or os.environ.get("HERMES_BRIDGE_HERMES_API_KEY"):
        api_key = load_api_key(settings)
    # Touch the signing key and database without starting a listener.
    runtime = build_runtime(settings, password=password, api_key=api_key, backend=_IdleBackend())
    runtime.store.close()
    if created:
        print("Owner password (shown once):")
        print(password)
    else:
        print("Owner password file already exists; it was not regenerated.")
    print(f"Signing key: {Path(settings.data_dir) / 'signing-key.pem'}")


def _serve(runtime: object) -> None:
    from hermes_agent_mcp_bridge.runtime import Runtime

    assert isinstance(runtime, Runtime)
    public = create_app(runtime, role="public")
    if runtime.settings.consent.exposure == "tailnet":
        consent = create_app(runtime, role="consent")
        thread = threading.Thread(
            target=_uvicorn,
            args=(consent, runtime.settings.consent.bind_host, runtime.settings.consent.bind_port),
            name="hermes-bridge-consent",
            daemon=True,
        )
        thread.start()
    _uvicorn(public, runtime.settings.server.bind_host, runtime.settings.server.bind_port)


def _uvicorn(app: object, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=30,
        limit_concurrency=16,
        proxy_headers=False,
        forwarded_allow_ips="",
    )


def _config_from_env() -> Path:
    raw = os.environ.get("HERMES_BRIDGE_CONFIG", "")
    if not raw:
        raise ConfigError("pass --config or set HERMES_BRIDGE_CONFIG")
    return Path(raw)


class _IdleBackend:
    """Placeholder so `init` can create files without calling Hermes."""

    def start(self, prompt: str, *, job_id: str) -> str:
        raise ConfigError("init does not run jobs")

    def poll(self, run_id: str) -> object:
        raise ConfigError("init does not run jobs")

    def health(self) -> bool:
        return False

    def request_stop(self) -> None:
        return None


if __name__ == "__main__":
    main()
