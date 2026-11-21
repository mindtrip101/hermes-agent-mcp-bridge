"""Routing, provider adapters, and the hub MCP tools. HTTP is mocked."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from hermes_agent_mcp_bridge.app import build_runtime, create_app
from hermes_agent_mcp_bridge.config import load_settings
from hermes_agent_mcp_bridge.errors import ConfigError
from hermes_agent_mcp_bridge.hub.adapters import (
    ANTHROPIC_VERSION,
    AnthropicAdapter,
    OpenAICompatibleAdapter,
    StubAdapter,
)
from hermes_agent_mcp_bridge.hub.config import AgentProfile, HubSettings
from hermes_agent_mcp_bridge.hub.errors import AdapterError, HubError
from hermes_agent_mcp_bridge.hub.router import route
from hermes_agent_mcp_bridge.hub.service import Hub
from hermes_agent_mcp_bridge.mcp_rpc import tools
from tests.conftest import ISSUER, PASSWORD, login, make_settings, register_client

CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
OPENAI_KEY = "openai-test-key-value"
ANTHROPIC_KEY = "anthropic-test-key-value"
ROOT = Path(__file__).resolve().parents[1]


def _local(**overrides: object) -> AgentProfile:
    data: dict[str, object] = {
        "name": "local",
        "provider": "openai_compatible",
        "model": "llama3.1",
        "system_prompt": "Be brief.",
        "tools_allowed": ["read_file"],
        "cost_tier": 0,
        "base_url": "http://127.0.0.1:11434/v1",
    }
    data.update(overrides)
    return AgentProfile(**data)  # type: ignore[arg-type]


def _openai(**overrides: object) -> AgentProfile:
    data: dict[str, object] = {
        "name": "openai",
        "provider": "openai_compatible",
        "model": "gpt-test",
        "cost_tier": 2,
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "token_param": "max_completion_tokens",
    }
    data.update(overrides)
    return AgentProfile(**data)  # type: ignore[arg-type]


def _anthropic(**overrides: object) -> AgentProfile:
    data: dict[str, object] = {
        "name": "anthropic",
        "provider": "anthropic",
        "model": "claude-test",
        "system_prompt": "Be careful.",
        "cost_tier": 3,
        "api_key_env": "ANTHROPIC_API_KEY",
    }
    data.update(overrides)
    return AgentProfile(**data)  # type: ignore[arg-type]


def _grok() -> AgentProfile:
    return AgentProfile(name="grok-personal", provider="grok", model="grok", account="personal", cost_tier=3)


def _cursor() -> AgentProfile:
    return AgentProfile(name="cursor", provider="cursor", model="cursor-agent", cost_tier=2)


def sample_settings(**overrides: object) -> HubSettings:
    data: dict[str, object] = {
        "enabled": True,
        "strong_cost_tier": 3,
        "request_timeout_seconds": 5,
        "agents": [_local(), _openai(), _anthropic(), _grok(), _cursor()],
    }
    data.update(overrides)
    return HubSettings(**data)  # type: ignore[arg-type]


def _profiles() -> list[AgentProfile]:
    return [_local(), _openai(), _anthropic(), _grok(), _cursor()]


def test_route_prefers_the_cheapest_runnable_profile() -> None:
    decision = route(_profiles(), agent=None, from_agent=None, strength="any", strong_cost_tier=3)
    assert decision.profile.name == "local"
    assert decision.reason == "lowest cost_tier among runnable profiles"
    assert decision.runnable is True


def test_route_breaks_cost_ties_by_name() -> None:
    agents = [
        _local(name="beta"),
        _local(name="alpha"),
    ]
    decision = route(agents, agent=None, from_agent=None, strength="any", strong_cost_tier=3)
    assert decision.profile.name == "alpha"


def test_route_skips_stubs_and_the_caller() -> None:
    decision = route(_profiles(), agent=None, from_agent="local", strength="any", strong_cost_tier=3)
    assert decision.profile.name == "openai"
    only_stubs = [_grok(), _cursor()]
    with pytest.raises(HubError) as exc:
        route(only_stubs, agent=None, from_agent=None, strength="strong", strong_cost_tier=3)
    assert exc.value.code == "no_agent"


def test_route_strong_skips_profiles_below_the_tier() -> None:
    decision = route(_profiles(), agent=None, from_agent=None, strength="strong", strong_cost_tier=3)
    assert decision.profile.name == "anthropic"
    assert "strong tier" in decision.reason
    with pytest.raises(HubError) as exc:
        route([_local(), _openai()], agent=None, from_agent=None, strength="strong", strong_cost_tier=3)
    assert exc.value.code == "no_strong_agent"


def test_route_explicit_profile_overrides_the_policy() -> None:
    decision = route(_profiles(), agent="local", from_agent=None, strength="strong", strong_cost_tier=3)
    assert decision.profile.name == "local"
    assert decision.reason == "explicit profile"
    stub = route(_profiles(), agent="grok-personal", from_agent="local", strength="any", strong_cost_tier=3)
    assert stub.runnable is False
    with pytest.raises(HubError) as missing:
        route(_profiles(), agent="missing", from_agent=None, strength="any", strong_cost_tier=3)
    assert missing.value.code == "unknown_agent"
    with pytest.raises(HubError) as loop:
        route(_profiles(), agent="local", from_agent="local", strength="any", strong_cost_tier=3)
    assert loop.value.code == "self_handoff"


def test_openai_adapter_posts_chat_completions_and_omits_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    text = OpenAICompatibleAdapter(timeout=5, transport=httpx.MockTransport(handler)).complete(
        _openai(), task="hello", system="sys"
    )
    body = seen["body"]
    assert text == "ok"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer " + OPENAI_KEY
    assert isinstance(body, dict)
    assert body["model"] == "gpt-test"
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body
    assert body["stream"] is False
    assert "tools" not in body
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hello"}


def test_local_adapter_omits_the_authorization_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        body = json.loads(request.content.decode("utf-8"))
        assert body["max_tokens"] == 1024
        assert "read_file" not in request.content.decode("utf-8")
        assert "tools" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "local"}]}}]})

    text = OpenAICompatibleAdapter(timeout=5, transport=httpx.MockTransport(handler)).complete(
        _local(), task="hello", system=""
    )
    assert text == "local"


def test_openai_adapter_hides_provider_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": OPENAI_KEY}})

    with pytest.raises(AdapterError) as auth:
        OpenAICompatibleAdapter(timeout=5, transport=httpx.MockTransport(unauthorized)).complete(
            _openai(), task="hello", system=""
        )
    assert auth.value.code == "auth_failed"
    assert OPENAI_KEY not in str(auth.value)

    calls = {"n": 0}

    def redirect(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(302, headers={"location": "https://example.invalid/steal"})

    with pytest.raises(AdapterError) as bounced:
        OpenAICompatibleAdapter(timeout=5, transport=httpx.MockTransport(redirect)).complete(
            _openai(), task="hello", system=""
        )
    assert bounced.value.code == "provider_error"
    assert calls["n"] == 1

    def timed_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    with pytest.raises(AdapterError) as timeout:
        OpenAICompatibleAdapter(timeout=5, transport=httpx.MockTransport(timed_out)).complete(
            _openai(), task="hello", system=""
        )
    assert timeout.value.code == "timeout"

    def garbage(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json " + OPENAI_KEY.encode("utf-8"))

    with pytest.raises(AdapterError) as bad:
        OpenAICompatibleAdapter(timeout=5, transport=httpx.MockTransport(garbage)).complete(
            _openai(), task="hello", system=""
        )
    assert bad.value.code == "provider_error"
    assert OPENAI_KEY not in str(bad.value)

    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    with pytest.raises(AdapterError) as blank:
        OpenAICompatibleAdapter(timeout=5, transport=httpx.MockTransport(empty)).complete(
            _openai(), task="hello", system=""
        )
    assert blank.value.code == "empty_completion"


def test_anthropic_adapter_posts_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "done"}, {"type": "text", "text": " more"}]},
        )

    text = AnthropicAdapter(timeout=5, transport=httpx.MockTransport(handler)).complete(
        _anthropic(), task="hello", system="Be careful."
    )
    body = seen["body"]
    assert text == "done more"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["key"] == ANTHROPIC_KEY
    assert seen["version"] == ANTHROPIC_VERSION
    assert isinstance(body, dict)
    assert body["model"] == "claude-test"
    assert body["max_tokens"] == 1024
    assert body["system"] == "Be careful."
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(AdapterError) as missing:
        AnthropicAdapter(timeout=5, transport=httpx.MockTransport(handler)).complete(
            _anthropic(), task="hello", system=""
        )
    assert missing.value.code == "missing_api_key"


def test_stub_adapter_refuses_to_call() -> None:
    with pytest.raises(AdapterError) as exc:
        StubAdapter("grok").complete(_grok(), task="hello", system="")
    assert exc.value.code == "provider_stub"
    with pytest.raises(AdapterError) as cursor:
        StubAdapter("cursor").complete(_cursor(), task="hello", system="")
    assert cursor.value.code == "provider_stub"


def _transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def test_delegate_uses_the_cheap_profile_and_keeps_the_inbox(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        body = json.loads(request.content.decode("utf-8"))
        assert body["messages"][-1]["content"] == "summarize"
        assert "read_file" not in request.content.decode("utf-8")
        return httpx.Response(200, json={"choices": [{"message": {"content": "local-answer"}}]})

    hub = Hub.open(sample_settings(), data_dir=tmp_path, secrets=[], transport=_transport(handler))
    try:
        listed = hub.call("list_agents", "owner", {})
        encoded = json.dumps(listed.payload)
        assert listed.payload["policy"] == "prefer_cheap"
        by_name = {item["name"]: item for item in listed.payload["agents"]}
        assert by_name["local"]["runnable"] is True
        assert by_name["local"]["endpoint"] == "local"
        assert by_name["local"]["tools_allowed"] == ["read_file"]
        assert by_name["grok-personal"]["runnable"] is False
        assert by_name["grok-personal"]["account"] == "personal"
        assert "Grok Bot" in by_name["grok-personal"]["detail"]
        assert by_name["cursor"]["runnable"] is False
        assert "11434" not in encoded
        assert "OPENAI_API_KEY" not in encoded
        result = hub.call("delegate_task", "owner", {"task": "summarize"})
        assert result.is_error is False
        assert result.payload["agent"] == "local"
        assert result.payload["result"] == "local-answer"
        status = hub.call("task_status", "owner", {"task_id": result.payload["task_id"]})
        assert status.payload["result"] == "local-answer"
        with pytest.raises(HubError) as hidden:
            hub.call("task_status", "other", {"task_id": result.payload["task_id"]})
        assert hidden.value.code == "not_found"
        mode = stat.S_IMODE((tmp_path / "hub.sqlite3").stat().st_mode)
        assert mode & 0o077 == 0
    finally:
        hub.close()


def test_delegate_strong_from_agent_and_scrub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append({"host": request.url.host, "body": body})
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "strong-answer"}]})
        if request.url.host == "api.openai.com":
            return httpx.Response(200, json={"choices": [{"message": {"content": "leak " + OPENAI_KEY}}]})
        raise AssertionError(request.url.host)

    hub = Hub.open(
        sample_settings(),
        data_dir=tmp_path,
        secrets=[OPENAI_KEY],
        transport=_transport(handler),
    )
    try:
        strong = hub.call("delegate_task", "owner", {"task": "review", "strength": "strong"})
        assert strong.payload["agent"] == "anthropic"
        assert strong.payload["result"] == "strong-answer"
        handed = hub.call(
            "delegate_task",
            "owner",
            {"task": "rewrite", "from_agent": "local"},
        )
        assert handed.payload["agent"] == "openai"
        assert handed.payload["from_agent"] == "local"
        assert handed.payload["result"] == "leak [redacted]"
        assert OPENAI_KEY not in json.dumps(handed.payload)
        openai_call = seen[1]
        body = openai_call["body"]
        assert isinstance(body, dict)
        assert body["messages"][0]["content"] == (
            "This task was delegated by the agent profile local. Return the result to the caller."
        )
        with pytest.raises(HubError) as loop:
            hub.call("delegate_task", "owner", {"task": "x", "agent": "local", "from_agent": "local"})
        assert loop.value.code == "self_handoff"
    finally:
        hub.close()


def test_delegate_to_a_stub_is_stored_and_not_called(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("stub profiles must not make an HTTP call")

    hub = Hub.open(sample_settings(), data_dir=tmp_path, secrets=[], transport=_transport(handler))
    try:
        result = hub.call("delegate_task", "owner", {"task": "ping", "agent": "cursor"})
        assert result.is_error is True
        assert result.payload["status"] == "unsupported"
        assert result.payload["error"]["code"] == "provider_stub"
        assert "no outbound adapter" in result.payload["error"]["detail"]
        status = hub.call("task_status", "owner", {"task_id": result.payload["task_id"]})
        assert status.payload["status"] == "unsupported"
    finally:
        hub.close()


def test_delegate_to_a_stub_rejects_a_blank_task(tmp_path: Path) -> None:
    hub = Hub.open(sample_settings(), data_dir=tmp_path, secrets=[], transport=None)
    try:
        with pytest.raises(HubError) as exc:
            hub.call("delegate_task", "owner", {"task": "   "})
        assert exc.value.code == "invalid_input"
    finally:
        hub.close()


def test_restart_marks_a_running_task_interrupted(tmp_path: Path) -> None:
    hub = Hub.open(sample_settings(), data_dir=tmp_path, secrets=[], transport=None)
    task_id = hub._inbox.create(
        owner_sub="owner",
        from_agent="",
        agent="local",
        provider="openai_compatible",
        model="llama3.1",
        cost_tier=0,
        strength="any",
        route_reason="test",
        task_text="pending",
    )
    hub.close()
    again = Hub.open(sample_settings(), data_dir=tmp_path, secrets=[], transport=None)
    try:
        status = again.call("task_status", "owner", {"task_id": task_id})
        assert status.payload["status"] == "failed"
        assert status.payload["error"]["code"] == "interrupted"
    finally:
        again.close()


def test_reply_is_truncated(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello world"}}]})

    hub = Hub.open(
        sample_settings(max_output_chars=5),
        data_dir=tmp_path,
        secrets=[],
        transport=_transport(handler),
    )
    try:
        result = hub.call("delegate_task", "owner", {"task": "hi", "agent": "local"})
        assert result.payload["result"] == "hello"
        assert result.payload["truncated"] is True
    finally:
        hub.close()


def test_unexpected_provider_exception_is_stored_without_its_text(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom " + OPENAI_KEY)

    hub = Hub.open(sample_settings(), data_dir=tmp_path, secrets=[], transport=_transport(handler))
    try:
        result = hub.call("delegate_task", "owner", {"task": "hi", "agent": "local"})
        assert result.payload["status"] == "failed"
        assert result.payload["error"]["code"] == "provider_error"
        assert OPENAI_KEY not in json.dumps(result.payload)
    finally:
        hub.close()


def test_provider_error_body_is_not_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom " + OPENAI_KEY)

    hub = Hub.open(sample_settings(), data_dir=tmp_path, secrets=[OPENAI_KEY], transport=_transport(handler))
    try:
        result = hub.call("delegate_task", "owner", {"task": "hi", "agent": "openai"})
        assert result.is_error is True
        assert result.payload["error"]["code"] == "provider_error"
        assert OPENAI_KEY not in json.dumps(result.payload)
    finally:
        hub.close()


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "server:\n  issuer: http://127.0.0.1:8765\n  dev_mode: true\n" + body,
        encoding="utf-8",
    )
    return path


def test_config_accepts_a_private_model_server_only_with_the_flag(tmp_path: Path) -> None:
    blocked = _config(
        tmp_path,
        "hub:\n  agents:\n    - name: spark\n      provider: openai_compatible\n"
        "      model: local-model\n      cost_tier: 0\n      base_url: http://192.168.1.20:8000/v1\n",
    )
    with pytest.raises(ConfigError, match="loopback"):
        load_settings(blocked)
    allowed = _config(
        tmp_path,
        "hub:\n  agents:\n    - name: spark\n      provider: openai_compatible\n"
        "      model: local-model\n      cost_tier: 0\n"
        "      base_url: http://192.168.1.20:8000/v1/\n      allow_private_base_url: true\n",
    )
    settings = load_settings(allowed)
    assert settings.hub.agents[0].base_url == "http://192.168.1.20:8000/v1"


def test_config_rejects_stub_endpoints_duplicates_and_pasted_keys(tmp_path: Path) -> None:
    pasted = _config(
        tmp_path,
        "hub:\n  agents:\n    - name: local\n      provider: openai_compatible\n"
        "      model: llama3.1\n      cost_tier: 0\n      base_url: http://127.0.0.1:11434/v1\n"
        "      api_key: pasted-secret-value\n",
    )
    with pytest.raises(ConfigError, match="invalid config"):
        load_settings(pasted)
    stub = _config(
        tmp_path,
        "hub:\n  agents:\n    - name: grok-personal\n      provider: grok\n      model: grok\n"
        "      account: personal\n      base_url: https://example.invalid\n",
    )
    with pytest.raises(ConfigError, match="stub providers"):
        load_settings(stub)
    duplicate = _config(
        tmp_path,
        "hub:\n  agents:\n    - name: local\n      provider: openai_compatible\n"
        "      model: llama3.1\n      cost_tier: 0\n      base_url: http://127.0.0.1:11434/v1\n"
        "    - name: local\n      provider: openai_compatible\n      model: llama3.1\n"
        "      cost_tier: 0\n      base_url: http://127.0.0.1:11434/v1\n",
    )
    with pytest.raises(ConfigError, match="unique"):
        load_settings(duplicate)
    policy = _config(tmp_path, "hub:\n  policy: fastest\n")
    with pytest.raises(ConfigError, match="prefer_cheap"):
        load_settings(policy)


def test_env_overrides_hub_switches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _config(tmp_path, "hub:\n  enabled: false\n  strong_cost_tier: 3\n")
    monkeypatch.setenv("HERMES_BRIDGE_HUB_ENABLED", "true")
    monkeypatch.setenv("HERMES_BRIDGE_HUB_STRONG_COST_TIER", "4")
    monkeypatch.setenv("HERMES_BRIDGE_HUB_TIMEOUT_SECONDS", "12")
    settings = load_settings(path)
    assert settings.hub.enabled is True
    assert settings.hub.strong_cost_tier == 4
    assert settings.hub.request_timeout_seconds == 12.0
    monkeypatch.setenv("HERMES_BRIDGE_HUB_TIMEOUT_SECONDS", "nope")
    with pytest.raises(ConfigError, match="numeric"):
        load_settings(path)


def test_example_config_loads_with_the_hub_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("HERMES_BRIDGE_"):
            monkeypatch.delenv(key)
    settings = load_settings(ROOT / "config.example.yaml")
    assert settings.hub.enabled is False
    assert [agent.name for agent in settings.hub.agents] == [
        "local",
        "openai",
        "anthropic",
        "grok-personal",
        "grok-business",
        "cursor",
    ]
    assert settings.hub.agents[1].token_param == "max_completion_tokens"
    assert settings.hub.agents[3].account == "personal"
    assert settings.hub.agents[4].account == "business"


def test_hub_tools_stay_unregistered_by_default(runtime: object) -> None:
    from hermes_agent_mcp_bridge.runtime import Runtime

    assert isinstance(runtime, Runtime)
    names = [item["name"] for item in tools(runtime)]
    assert names == ["ask_hermes", "get_job", "bridge_status"]


def test_tool_order_when_sync_and_hub_are_on(tmp_path: Path, fake: object) -> None:
    from hermes_agent_mcp_bridge.hermes import FakeHermes

    assert isinstance(fake, FakeHermes)
    settings = make_settings(tmp_path).model_copy(update={"hub": sample_settings()})
    settings.limits.sync_ask_enabled = True
    runtime = build_runtime(settings, password=PASSWORD, backend=fake)
    try:
        names = [item["name"] for item in tools(runtime)]
        assert names == [
            "ask_hermes",
            "get_job",
            "bridge_status",
            "ask_hermes_sync",
            "list_agents",
            "delegate_task",
            "task_status",
        ]
    finally:
        if runtime.hub is not None:
            runtime.hub.close()
        runtime.store.close()


def _rpc(http: TestClient, token: str, method: str, params: dict | None = None) -> httpx.Response:
    body: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return http.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream"},
        json=body,
    )


def test_mcp_delegate_round_trip(tmp_path: Path, fake: object) -> None:
    from hermes_agent_mcp_bridge.hermes import FakeHermes

    assert isinstance(fake, FakeHermes)
    fake.output = "hermes-answer"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        return httpx.Response(200, json={"choices": [{"message": {"content": "from-local"}}]})

    settings = make_settings(tmp_path).model_copy(update={"hub": sample_settings()})
    runtime = build_runtime(
        settings,
        password=PASSWORD,
        backend=fake,
        hub_transport=_transport(handler),
    )
    try:
        with TestClient(create_app(runtime), base_url=ISSUER, follow_redirects=False) as http:
            anonymous = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert anonymous.status_code == 401
            client_id = register_client(http, [CHATGPT_REDIRECT])
            token = str(login(http, client_id=client_id, redirect_uri=CHATGPT_REDIRECT)[0]["access_token"])
            names = [item["name"] for item in _rpc(http, token, "tools/list").json()["result"]["tools"]]
            assert names[:3] == ["ask_hermes", "get_job", "bridge_status"]
            assert names[-3:] == ["list_agents", "delegate_task", "task_status"]
            delegated = _rpc(
                http,
                token,
                "tools/call",
                {"name": "delegate_task", "arguments": {"task": "summarize"}},
            )
            payload = delegated.json()["result"]
            assert payload["isError"] is False
            assert payload["structuredContent"]["result"] == "from-local"
            task_id = payload["structuredContent"]["task_id"]
            status = _rpc(http, token, "tools/call", {"name": "task_status", "arguments": {"task_id": task_id}})
            assert status.json()["result"]["structuredContent"]["agent"] == "local"
            stub = _rpc(
                http,
                token,
                "tools/call",
                {"name": "delegate_task", "arguments": {"task": "ping", "agent": "grok-personal"}},
            )
            stub_body = stub.json()["result"]
            assert stub_body["isError"] is True
            assert stub_body["structuredContent"]["error"]["code"] == "provider_stub"
            assert "personal" in stub_body["structuredContent"]["error"]["detail"]
            asked = _rpc(http, token, "tools/call", {"name": "ask_hermes", "arguments": {"prompt": "hello"}})
            assert asked.json()["result"]["structuredContent"]["status"] == "queued"
    finally:
        if runtime.worker is not None:
            runtime.worker.stop()
        if runtime.hub is not None:
            runtime.hub.close()
        runtime.store.close()
