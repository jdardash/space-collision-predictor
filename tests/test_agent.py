"""Tests for the LLM provider chain, the typed tool loop, the agent routes and
the MCP server.

Everything here runs offline. The one network seam in :mod:`sda.llm` is
``_post_json``; every test that needs a provider answer monkeypatches it, so no
socket is opened even where a provider looks configured. Where a test needs the
agent loop rather than the wire format, it drives :func:`sda.agent.run_agent`
with a scripted client that returns canned :class:`~sda.llm.ChatResponse`
objects, which keeps the loop deterministic without pretending to be an LLM.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import agent as agent_module
from sda import llm as llm_module
from sda import mcp_server
from sda.agent import (
    DISCLAIMER,
    TOOL_SPECS,
    TOOLS_BY_NAME,
    ToolArgumentError,
    ToolContext,
    execute_tool,
    run_agent,
    tool_definitions,
    validate_arguments,
)
from sda.demo_tles import DEMO_TLE_TEXT
from sda.llm import (
    DEFAULT_ANTHROPIC_MODEL,
    AllProvidersFailedError,
    AnthropicProvider,
    ChatResponse,
    LLMClient,
    NoProviderConfiguredError,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderRequestError,
    ToolCall,
    normalize_anthropic,
    normalize_ollama,
    normalize_openai,
    provider_order,
)
from sda.routes import agent as agent_routes
from sda.tle_store import TLEStore

# The demo catalog is a fixed text constant with March 2026 epochs, so a fixed
# evaluation time keeps every propagation in these tests reproducible.
FIXED_NOW = datetime(2026, 3, 23, tzinfo=UTC)
FIXED_TCA = "2026-03-23T00:00:00Z"

ISS = 25544
HST = 20580
TIANHE = 48274
WENTIAN = 53239

_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "SDA_ANTHROPIC_BASE_URL",
    "SDA_ANTHROPIC_MODEL",
    "SDA_LLM_COOLDOWN_S",
    "SDA_LLM_MAX_TOKENS",
    "SDA_LLM_MODEL",
    "SDA_LLM_PROVIDERS",
    "SDA_LLM_TIMEOUT_S",
    "SDA_MCP_TLE_FILE",
    "SDA_OLLAMA_MODEL",
    "SDA_OLLAMA_URL",
    "SDA_OPENAI_BASE_URL",
    "SDA_OPENAI_MODEL",
    "SDA_OPENAI_REFERER",
    "SDA_OPENAI_TITLE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No provider credentials leak in from the developer's shell."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    llm_module.reset_client()
    yield
    llm_module.reset_client()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any un-mocked provider call fails loudly instead of dialling out."""

    def _blocked(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(llm_module, "_post_json", _blocked)


@pytest.fixture(scope="session")
def demo_store() -> TLEStore:
    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    return store


@pytest.fixture
def ctx(demo_store: TLEStore) -> ToolContext:
    return ToolContext(store=demo_store, now=FIXED_NOW)


# --- Canned provider payloads --------------------------------------------

ANTHROPIC_TOOL_PAYLOAD: dict[str, Any] = {
    "id": "msg_01",
    "model": "claude-sonnet-5",
    "stop_reason": "tool_use",
    "content": [
        {"type": "text", "text": "Screening the pair now."},
        {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "run_conjunction_screen",
            "input": {"hours": 6.0, "threshold_km": 5.0},
        },
    ],
}

ANTHROPIC_TEXT_PAYLOAD: dict[str, Any] = {
    "id": "msg_02",
    "model": "claude-sonnet-5",
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "Closest approach is 1.2 km."}],
}

OPENAI_TOOL_PAYLOAD: dict[str, Any] = {
    "id": "chatcmpl-1",
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "Screening the pair now.",
                "tool_calls": [
                    {
                        "id": "call_01",
                        "type": "function",
                        "function": {
                            "name": "run_conjunction_screen",
                            "arguments": '{"hours": 6.0, "threshold_km": 5.0}',
                        },
                    }
                ],
            },
        }
    ],
}

OPENAI_TEXT_PAYLOAD: dict[str, Any] = {
    "id": "chatcmpl-2",
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Closest approach is 1.2 km."},
        }
    ],
}

OLLAMA_TOOL_PAYLOAD: dict[str, Any] = {
    "model": "llama3.1",
    "done_reason": "stop",
    "message": {
        "role": "assistant",
        "content": "Screening the pair now.",
        "tool_calls": [
            {
                "function": {
                    "name": "run_conjunction_screen",
                    "arguments": {"hours": 6.0, "threshold_km": 5.0},
                }
            }
        ],
    },
}


def _install_seam(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[dict[str, Any]]:
    """Replace the single network seam and record every request it sees."""
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, headers: dict[str, str], payload: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout_s": timeout_s}
        )
        result: dict[str, Any] = handler(url, headers, payload)
        return result

    monkeypatch.setattr(llm_module, "_post_json", fake_post)
    return calls


class _Clock:
    """Monotonic clock the cooldown tests can advance by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


# --- 1. Provider response normalization ----------------------------------

def test_normalize_anthropic_reads_text_and_tool_use_blocks() -> None:
    response = normalize_anthropic(ANTHROPIC_TOOL_PAYLOAD)

    assert response.provider == "anthropic"
    assert response.model == "claude-sonnet-5"
    assert response.stop_reason == "tool_use"
    assert response.text == "Screening the pair now."
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.id, call.name) == ("toolu_01", "run_conjunction_screen")
    assert call.arguments == {"hours": 6.0, "threshold_km": 5.0}


def test_normalize_openai_decodes_json_string_arguments() -> None:
    response = normalize_openai(OPENAI_TOOL_PAYLOAD)

    assert response.provider == "openai"
    assert response.model == "gpt-4o-mini"
    assert response.stop_reason == "tool_calls"
    assert response.text == "Screening the pair now."
    call = response.tool_calls[0]
    assert (call.id, call.name) == ("call_01", "run_conjunction_screen")
    # OpenAI sends arguments as a JSON string; the normalizer hands back a dict.
    assert call.arguments == {"hours": 6.0, "threshold_km": 5.0}


def test_normalize_ollama_reads_object_arguments_and_synthesises_ids() -> None:
    response = normalize_ollama(OLLAMA_TOOL_PAYLOAD)

    assert response.provider == "ollama"
    assert response.model == "llama3.1"
    assert response.stop_reason == "tool_calls"
    call = response.tool_calls[0]
    assert call.name == "run_conjunction_screen"
    assert call.arguments == {"hours": 6.0, "threshold_km": 5.0}
    # Ollama omits tool-call ids, so the normalizer supplies a stable one.
    assert call.id == "call_0"


def test_normalizers_survive_junk_payloads() -> None:
    for normalize in (normalize_anthropic, normalize_openai, normalize_ollama):
        response = normalize({})
        assert response.text == ""
        assert response.tool_calls == ()


def test_normalize_openai_ignores_unparseable_arguments() -> None:
    payload = json.loads(json.dumps(OPENAI_TOOL_PAYLOAD))
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"

    call = normalize_openai(payload).tool_calls[0]

    assert call.name == "run_conjunction_screen"
    assert call.arguments == {}


# --- 2. Providers on the wire (mocked transport) -------------------------

def test_anthropic_provider_posts_messages_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = _install_seam(monkeypatch, lambda *_: ANTHROPIC_TOOL_PAYLOAD)

    provider = AnthropicProvider()
    assert provider.is_configured() is True
    assert provider.model() == DEFAULT_ANTHROPIC_MODEL

    response = provider.chat(
        [{"role": "user", "content": "screen the CSS modules"}],
        tools=tool_definitions(),
        system="be brief",
    )

    request = calls[0]
    assert request["url"].endswith("/v1/messages")
    assert request["headers"]["x-api-key"] == "test-key"
    assert request["headers"]["anthropic-version"] == llm_module.ANTHROPIC_VERSION
    assert request["payload"]["system"] == "be brief"
    assert all("input_schema" in tool for tool in request["payload"]["tools"])
    assert response.provider == "anthropic"
    assert response.tool_calls[0].name == "run_conjunction_screen"


def test_openai_provider_posts_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SDA_OPENAI_BASE_URL", "https://openrouter.example/api/v1")
    calls = _install_seam(monkeypatch, lambda *_: OPENAI_TOOL_PAYLOAD)

    response = OpenAICompatibleProvider().chat(
        [{"role": "user", "content": "hello"}], tools=tool_definitions(), system="be brief"
    )

    request = calls[0]
    assert request["url"] == "https://openrouter.example/api/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["payload"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert request["payload"]["tools"][0]["type"] == "function"
    assert response.provider == "openai"
    assert response.tool_calls[0].arguments["threshold_km"] == 5.0


def test_ollama_provider_needs_an_explicit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider()
    # Unset on purpose: a local server nobody asked for must not count as
    # configured, or the "no provider" setup message would never be reachable.
    assert provider.is_configured() is False

    monkeypatch.setenv("SDA_OLLAMA_URL", "http://127.0.0.1:11434/")
    monkeypatch.setenv("SDA_LLM_MODEL", "qwen2.5")
    calls = _install_seam(monkeypatch, lambda *_: OLLAMA_TOOL_PAYLOAD)

    assert provider.is_configured() is True
    response = provider.chat([{"role": "user", "content": "hello"}], tools=tool_definitions())

    request = calls[0]
    assert request["url"] == "http://127.0.0.1:11434/api/chat"
    assert request["payload"]["stream"] is False
    # SDA_LLM_MODEL overrides every provider default.
    assert request["payload"]["model"] == "qwen2.5"
    assert response.provider == "ollama"


def test_ollama_provider_replays_tool_arguments_as_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /api/chat answers HTTP 400 when a replayed tool call carries its arguments
    # as the JSON string OpenAI uses, so the second turn of every tool loop died.
    monkeypatch.setenv("SDA_OLLAMA_URL", "http://127.0.0.1:11434")
    calls = _install_seam(monkeypatch, lambda *_: OLLAMA_TOOL_PAYLOAD)

    tool_call = {
        "id": "call_0",
        "name": "run_conjunction_screen",
        "arguments": {"threshold_km": 5.0},
    }
    history = [
        {"role": "user", "content": "screen the CSS modules"},
        {"role": "assistant", "content": "", "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call_0", "content": '{"events": []}'},
    ]

    OllamaProvider().chat(history, tools=tool_definitions())

    assistant = calls[0]["payload"]["messages"][1]
    assert assistant["tool_calls"][0]["function"] == {
        "name": "run_conjunction_screen",
        "arguments": {"threshold_km": 5.0},
    }
    # The OpenAI encoding of the same turn is unchanged: a string, as that API requires.
    openai_turn = OpenAICompatibleProvider.encode_messages(history)[1]
    assert isinstance(openai_turn["tool_calls"][0]["function"]["arguments"], str)


def test_provider_order_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert provider_order() == ["anthropic", "openai", "ollama"]

    monkeypatch.setenv("SDA_LLM_PROVIDERS", "ollama, openai , nonsense")
    assert provider_order() == ["ollama", "openai"]

    monkeypatch.setenv("SDA_LLM_PROVIDERS", "nonsense")
    assert provider_order() == ["anthropic", "openai", "ollama"]


# --- 3. Chain health, fallback and cooldown ------------------------------

def test_no_provider_configured_carries_setup_instructions() -> None:
    client = LLMClient()

    assert client.has_provider() is False
    with pytest.raises(NoProviderConfiguredError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert "ANTHROPIC_API_KEY" in excinfo.value.setup_instructions
    assert [s.available for s in client.status()] == [False, False, False]


def test_chain_falls_back_to_the_next_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def handler(url: str, _headers: dict[str, str], _payload: dict[str, Any]) -> dict[str, Any]:
        if "/v1/messages" in url:
            raise ProviderRequestError("anthropic", "status 529: overloaded")
        return OPENAI_TEXT_PAYLOAD

    calls = _install_seam(monkeypatch, handler)
    clock = _Clock()
    client = LLMClient(providers=llm_module.build_providers(), cooldown_s=30.0, clock=clock)

    response = client.chat([{"role": "user", "content": "hi"}])

    assert response.provider == "openai"
    assert response.text == "Closest approach is 1.2 km."
    assert [c["url"].rsplit("/", 1)[-1] for c in calls] == ["messages", "completions"]

    status = {s.name: s for s in client.status()}
    assert status["anthropic"].failures == 1
    assert "529" in (status["anthropic"].last_error or "")
    assert status["anthropic"].cooldown_remaining_s == pytest.approx(30.0)
    assert status["anthropic"].available is False
    assert status["openai"].available is True


def test_cooldown_skips_the_failed_provider_then_lets_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state = {"anthropic_broken": True}

    def handler(url: str, _headers: dict[str, str], _payload: dict[str, Any]) -> dict[str, Any]:
        if "/v1/messages" in url:
            if state["anthropic_broken"]:
                raise ProviderRequestError("anthropic", "status 500")
            return ANTHROPIC_TEXT_PAYLOAD
        return OPENAI_TEXT_PAYLOAD

    calls = _install_seam(monkeypatch, handler)
    clock = _Clock()
    client = LLMClient(providers=llm_module.build_providers(), cooldown_s=30.0, clock=clock)

    client.chat([{"role": "user", "content": "one"}])
    assert len(calls) == 2

    # Still cooling: the chain goes straight to OpenAI without a second attempt.
    client.chat([{"role": "user", "content": "two"}])
    assert len(calls) == 3
    assert "/v1/messages" not in calls[2]["url"]

    state["anthropic_broken"] = False
    clock.advance(31.0)
    response = client.chat([{"role": "user", "content": "three"}])

    assert response.provider == "anthropic"
    status = {s.name: s for s in client.status()}
    assert status["anthropic"].failures == 0
    assert status["anthropic"].cooldown_remaining_s == 0.0


def test_all_providers_failing_raises_with_every_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def handler(url: str, _headers: dict[str, str], _payload: dict[str, Any]) -> dict[str, Any]:
        raise ProviderRequestError("http", f"status 500 from {url}")

    _install_seam(monkeypatch, handler)
    client = LLMClient(providers=llm_module.build_providers(), cooldown_s=30.0, clock=_Clock())

    with pytest.raises(AllProvidersFailedError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])

    assert set(excinfo.value.errors) == {"anthropic", "openai"}


# --- 4. Tool schemas -----------------------------------------------------

def test_tool_catalogue_covers_every_promised_capability() -> None:
    names = [spec.name for spec in TOOL_SPECS]

    assert names == sorted(set(names), key=names.index)  # no duplicates
    assert set(names) == {
        "list_satellites",
        "get_satellite_state",
        "run_conjunction_screen",
        "explain_pc",
        "estimate_decay",
        "plan_maneuver",
        "predict_passes",
        "fly_to",
        "summarize_scene",
    }
    # fly_to is the only tool the server refuses to execute itself.
    assert [spec.name for spec in TOOL_SPECS if spec.client_action] == ["fly_to"]


def test_tool_schemas_are_valid_json_schema_objects() -> None:
    allowed = {"string", "number", "integer", "boolean", "object", "array"}

    for spec in TOOL_SPECS:
        schema = spec.parameters
        assert spec.description.strip()
        assert schema["type"] == "object"
        properties = schema["properties"]
        assert properties, f"{spec.name} has no properties"
        for key, prop in properties.items():
            assert prop["type"] in allowed, f"{spec.name}.{key}"
            assert prop["description"].strip(), f"{spec.name}.{key}"
            if prop["type"] == "array":
                assert "items" in prop, f"{spec.name}.{key}"
        for required in schema.get("required", []):
            assert required in properties, f"{spec.name} requires unknown {required}"
        # Round trips through strict JSON, which is what a provider gets.
        assert json.loads(json.dumps(schema)) == schema


def test_tool_definitions_encode_for_every_provider() -> None:
    definitions = tool_definitions()

    anthropic = AnthropicProvider.encode_tools(definitions)
    assert {t["name"] for t in anthropic} == {d["name"] for d in definitions}
    assert all(t["input_schema"]["type"] == "object" for t in anthropic)

    openai = OpenAICompatibleProvider.encode_tools(definitions)
    assert all(t["type"] == "function" for t in openai)
    assert all(t["function"]["parameters"]["type"] == "object" for t in openai)


def test_validate_arguments_coerces_checks_and_defaults() -> None:
    schema = TOOLS_BY_NAME["get_satellite_state"].parameters

    # A model that sends the id as a string still gets a working call.
    assert validate_arguments(schema, {"norad_id": "25544"})["norad_id"] == 25544

    with pytest.raises(ToolArgumentError):
        validate_arguments(schema, {})
    with pytest.raises(ToolArgumentError):
        validate_arguments(schema, {"norad_id": 25.5})

    screen = TOOLS_BY_NAME["run_conjunction_screen"].parameters
    validated = validate_arguments(screen, {"hours": 6, "invented_key": "dropped"})
    assert validated["hours"] == 6.0
    assert "invented_key" not in validated
    assert validated["threshold_km"] == screen["properties"]["threshold_km"]["default"]


# --- 5. Tool execution ---------------------------------------------------

def test_execute_tool_never_raises_on_a_bad_call(ctx: ToolContext) -> None:
    unknown = execute_tool("teleport", {}, ctx)
    assert unknown.ok is False
    assert "unknown tool" in (unknown.error or "")

    missing = execute_tool("get_satellite_state", {"norad_id": 99999}, ctx)
    assert missing.ok is False
    assert "99999" in (missing.error or "")
    assert missing.duration_ms >= 0.0


def test_conjunction_screen_tool_reports_events_and_risk(ctx: ToolContext) -> None:
    invocation = execute_tool(
        "run_conjunction_screen",
        {"norad_ids": [TIANHE, WENTIAN], "hours": 0.5, "threshold_km": 10.0},
        ctx,
    )

    assert invocation.ok, invocation.error
    result = invocation.result
    assert result["satellites_screened"] == 2
    assert result["events_found"] >= 1
    assert result["risk_counts"]
    event = result["events"][0]
    assert {event["primary_norad_id"], event["secondary_norad_id"]} == {TIANHE, WENTIAN}
    assert event["risk"] in {"CRITICAL", "HIGH", "MODERATE", "LOW", "NEGLIGIBLE"}
    # Whatever the tool returns has to survive strict JSON.
    json.dumps(invocation.to_dict())


def test_summarize_scene_falls_back_to_the_context_scene(demo_store: TLEStore) -> None:
    scene = {
        "camera": {"latitude_deg": 37.4, "longitude_deg": -122.1, "altitude_km": 12000.0},
        "visible_layers": ["stations"],
        "selected": [{"norad_id": ISS, "name": "ISS (ZARYA)"}],
    }
    ctx = ToolContext(store=demo_store, scene=scene, now=FIXED_NOW)

    result = execute_tool("summarize_scene", {}, ctx).result

    assert "stations" in result["summary"]
    assert str(ISS) in result["summary"]
    assert result["visible_layers"] == ["stations"]


# --- 6. The agent loop ---------------------------------------------------

class ScriptedLLM(LLMClient):
    """Stand-in for :class:`sda.llm.LLMClient` that replays canned responses.

    It subclasses the real client so the loop and the routes are exercised
    through the same type they see in production, with an empty provider chain
    that can never reach the network.
    """

    def __init__(self, responses: list[ChatResponse]) -> None:
        super().__init__(providers=[])
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def has_provider(self) -> bool:
        return True

    def status(self) -> list[Any]:
        return []

    def chat(
        self,
        messages: Any,
        tools: Any = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> ChatResponse:
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "system": system,
            }
        )
        if not self.responses:
            return ChatResponse(text="No more scripted turns.", provider="scripted")
        return self.responses.pop(0)


def _tool_turn(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=calls,
        provider="scripted",
        model="scripted-1",
        stop_reason="tool_calls",
    )


def _final_turn(text: str) -> ChatResponse:
    return ChatResponse(text=text, provider="scripted", model="scripted-1", stop_reason="end_turn")


def _tool_messages(llm: ScriptedLLM) -> list[dict[str, Any]]:
    return [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]


def test_agent_loop_runs_a_conjunction_screen(demo_store: TLEStore) -> None:
    llm = ScriptedLLM(
        [
            _tool_turn(
                ToolCall(
                    id="call_screen",
                    name="run_conjunction_screen",
                    arguments={
                        "norad_ids": [TIANHE, WENTIAN],
                        "hours": 0.5,
                        "threshold_km": 10.0,
                    },
                )
            ),
            _final_turn("One CRITICAL pair, 0.0 km miss distance."),
        ]
    )

    result = run_agent(
        [{"role": "user", "content": "screen the CSS modules"}],
        store=demo_store,
        client=llm,
        now=FIXED_NOW,
    )

    assert result.stop_reason == "final"
    assert result.iterations == 2
    assert result.text == "One CRITICAL pair, 0.0 km miss distance."
    assert [t.name for t in result.trace] == ["run_conjunction_screen"]
    assert result.trace[0].ok
    assert result.trace[0].result["events_found"] >= 1

    # The system prompt carries the units rule and the disclaimer.
    system = llm.calls[0]["system"]
    assert "km/s" in system
    assert DISCLAIMER in system
    assert {t["name"] for t in llm.calls[0]["tools"]} == set(TOOLS_BY_NAME)

    # The tool result goes back to the model as a tool message, not an exception.
    tool_message = _tool_messages(llm)[0]
    assert tool_message["name"] == "run_conjunction_screen"
    assert json.loads(tool_message["content"])["events_found"] >= 1
    json.dumps(result.to_dict())


def test_agent_loop_explains_collision_probability(demo_store: TLEStore) -> None:
    llm = ScriptedLLM(
        [
            _tool_turn(
                ToolCall(
                    id="call_pc",
                    name="explain_pc",
                    arguments={
                        "primary_norad_id": ISS,
                        "secondary_norad_id": HST,
                        "tca_utc": FIXED_TCA,
                        "monte_carlo_samples": 20000,
                    },
                )
            ),
            _final_turn("Pc is negligible at this miss distance."),
        ]
    )

    result = run_agent(
        [{"role": "user", "content": "explain the Pc for ISS against Hubble"}],
        store=demo_store,
        client=llm,
        now=FIXED_NOW,
    )

    payload = result.trace[0].result
    assert result.trace[0].ok, result.trace[0].error
    assert payload["primary_norad_id"] == ISS
    assert payload["secondary_norad_id"] == HST
    assert set(payload["methods"]) >= {"analytic_2d", "foster", "chan"}
    # The Monte Carlo cross-check either ran or said why it could not.
    assert "monte_carlo_2d" in payload["methods"] or "monte_carlo_note" in payload
    assert payload["combined_sigma_km"] > 0.0
    assert payload["sigma_primary_km"] > 0.0
    assert "dilution" in payload["interpretation"]
    assert payload["note"] == DISCLAIMER
    # Pc results reach the model as strict JSON, non-finite values included.
    json.loads(_tool_messages(llm)[0]["content"])


def test_fly_to_returns_a_client_action_and_runs_nothing(demo_store: TLEStore) -> None:
    llm = ScriptedLLM(
        [
            _tool_turn(
                ToolCall(id="c1", name="get_satellite_state", arguments={"norad_id": ISS}),
                ToolCall(
                    id="c2",
                    name="fly_to",
                    arguments={"norad_id": ISS, "label": "ISS"},
                ),
            ),
            _final_turn("Centred on the ISS."),
        ]
    )

    result = run_agent(
        [{"role": "user", "content": "show me the ISS"}],
        store=demo_store,
        client=llm,
        now=FIXED_NOW,
    )

    assert len(result.actions) == 1
    action = result.actions[0]
    assert action["type"] == "fly_to"
    assert action["norad_id"] == ISS
    assert action["name"] == "ISS (ZARYA)"
    assert result.trace[1].result["executed"] is False
    # The state tool still ran server side in the same turn.
    assert result.trace[0].result["frame"] == "ECI (TEME)"


def test_fly_to_without_a_target_is_a_tool_error_not_an_exception(ctx: ToolContext) -> None:
    invocation = execute_tool("fly_to", {"label": "somewhere"}, ctx)

    assert invocation.ok is False
    assert "norad_id" in (invocation.error or "")


def test_tool_failures_are_handed_back_to_the_model(demo_store: TLEStore) -> None:
    llm = ScriptedLLM(
        [
            _tool_turn(
                ToolCall(id="c1", name="get_satellite_state", arguments={"norad_id": 99999})
            ),
            _final_turn("NORAD 99999 is not in this catalog."),
        ]
    )

    result = run_agent(
        [{"role": "user", "content": "where is 99999"}],
        store=demo_store,
        client=llm,
        now=FIXED_NOW,
    )

    assert result.stop_reason == "final"
    assert result.trace[0].ok is False
    assert "error" in json.loads(_tool_messages(llm)[0]["content"])


def test_agent_loop_stops_at_the_iteration_cap(demo_store: TLEStore) -> None:
    looping = [
        _tool_turn(ToolCall(id=f"c{i}", name="list_satellites", arguments={"limit": 1}))
        for i in range(6)
    ]
    llm = ScriptedLLM(looping)

    result = run_agent(
        [{"role": "user", "content": "keep listing"}],
        store=demo_store,
        client=llm,
        now=FIXED_NOW,
        max_iterations=3,
    )

    assert result.stop_reason == "max_iterations"
    assert result.iterations == 3
    assert len(result.trace) == 3
    assert result.text


# --- 7. Routes -----------------------------------------------------------

@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch, demo_store: TLEStore) -> Any:
    """A minimal app carrying only the agent router, over the demo catalog."""
    monkeypatch.setattr(agent_module, "_default_store", lambda: demo_store)
    agent_routes.clear_sessions()
    app = FastAPI()
    app.include_router(agent_routes.router)
    with TestClient(app) as client:
        yield client
    agent_routes.clear_sessions()


def test_chat_answers_503_with_setup_instructions(api_client: TestClient) -> None:
    response = api_client.post(
        "/agent/chat", json={"messages": [{"role": "user", "content": "hello"}]}
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "no_llm_provider_configured"
    assert "ANTHROPIC_API_KEY" in detail["setup"]
    assert {p["name"] for p in detail["providers"]} == {"anthropic", "openai", "ollama"}


def test_health_reports_the_provider_chain(api_client: TestClient) -> None:
    body = api_client.get("/agent/health").json()

    assert body["configured"] is False
    assert body["active_provider"] is None
    assert [p["name"] for p in body["providers"]] == ["anthropic", "openai", "ollama"]
    assert all(p["failures"] == 0 for p in body["providers"])
    assert "OPENAI_API_KEY" in body["setup"]


def test_tools_endpoint_publishes_the_catalogue(api_client: TestClient) -> None:
    body = api_client.get("/agent/tools").json()

    assert body["count"] == len(TOOL_SPECS)
    fly_to = next(t for t in body["tools"] if t["name"] == "fly_to")
    assert fly_to["client_action"] is True
    assert fly_to["parameters"]["type"] == "object"
    assert body["disclaimer"] == DISCLAIMER


def test_chat_runs_a_tool_then_answers(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLLM(
        [
            _tool_turn(
                ToolCall(
                    id="call_screen",
                    name="run_conjunction_screen",
                    arguments={
                        "norad_ids": [TIANHE, WENTIAN],
                        "hours": 0.5,
                        "threshold_km": 10.0,
                    },
                ),
                ToolCall(id="call_fly", name="fly_to", arguments={"norad_id": TIANHE}),
            ),
            _final_turn("CSS (TIANHE) and CSS (WENTIAN) are co-located; miss distance 0.0 km."),
        ]
    )
    monkeypatch.setattr(agent_routes, "get_client", lambda: llm)

    response = api_client.post(
        "/agent/chat",
        json={
            "messages": [{"role": "user", "content": "screen the CSS modules and show Tianhe"}],
            "scene": {
                "camera": {"latitude_deg": 0.0, "longitude_deg": 0.0, "altitude_km": 20000.0},
                "visible_layers": ["stations"],
                "selected": [{"norad_id": TIANHE}],
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["text"].startswith("CSS (TIANHE)")
    assert [t["name"] for t in body["trace"]] == ["run_conjunction_screen", "fly_to"]
    assert body["trace"][0]["ok"] is True
    assert body["actions"] == [{"type": "fly_to", "norad_id": TIANHE, "name": "CSS (TIANHE)"}]
    assert body["stop_reason"] == "final"
    assert body["disclaimer"] == DISCLAIMER

    # A second turn on the same session id sees the first exchange.
    llm.responses.append(_final_turn("Still 0.0 km."))
    follow_up = api_client.post(
        "/agent/chat",
        json={
            "session_id": body["session_id"],
            "messages": [{"role": "user", "content": "and now?"}],
        },
    )
    assert follow_up.status_code == 200
    assert follow_up.json()["session_id"] == body["session_id"]
    first_message = llm.calls[-1]["messages"][0]
    assert first_message["content"] == "screen the CSS modules and show Tianhe"


def test_chat_rejects_an_empty_message_list(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_routes, "get_client", lambda: ScriptedLLM([]))

    response = api_client.post("/agent/chat", json={"messages": []})

    assert response.status_code == 422


def test_chat_answers_502_when_every_provider_fails(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingLLM(ScriptedLLM):
        def chat(self, *args: Any, **kwargs: Any) -> ChatResponse:
            raise AllProvidersFailedError({"anthropic": "status 500", "openai": "timeout"})

    monkeypatch.setattr(agent_routes, "get_client", lambda: FailingLLM([]))

    response = api_client.post(
        "/agent/chat", json={"messages": [{"role": "user", "content": "hello"}]}
    )

    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "llm_providers_failed"


# --- 8. MCP server -------------------------------------------------------

@pytest.fixture
def mcp_context(demo_store: TLEStore) -> Any:
    mcp_server.set_context(ToolContext(store=demo_store, now=FIXED_NOW))
    yield
    mcp_server.set_context(None)


def test_mcp_initialize_declares_tools_and_units(mcp_context: None) -> None:
    response = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )

    assert response is not None
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == mcp_server.SERVER_NAME
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert "TEME" in result["instructions"]
    assert DISCLAIMER in result["instructions"]


def test_mcp_tools_list_matches_the_agent_catalogue(mcp_context: None) -> None:
    response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert response is not None
    tools = response["result"]["tools"]
    assert {t["name"] for t in tools} == set(TOOLS_BY_NAME)
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    assert all(t["description"].strip() for t in tools)
    fly_to = next(t for t in tools if t["name"] == "fly_to")
    assert "does not execute it" in fly_to["description"]


def test_mcp_tools_call_runs_a_screen(mcp_context: None) -> None:
    response = mcp_server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "run_conjunction_screen",
                "arguments": {
                    "norad_ids": [TIANHE, WENTIAN],
                    "hours": 0.5,
                    "threshold_km": 10.0,
                },
            },
        }
    )

    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload["events_found"] >= 1
    assert payload["satellites_screened"] == 2


def test_mcp_tools_call_reports_tool_errors_as_content(mcp_context: None) -> None:
    response = mcp_server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "teleport", "arguments": {}},
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert "Tool error" in response["result"]["content"][0]["text"]
    # A tool failure is a result, not a JSON-RPC error.
    assert "error" not in response


def test_mcp_rejects_unknown_methods_and_ignores_notifications(mcp_context: None) -> None:
    unknown = mcp_server.handle_message({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
    assert unknown is not None
    assert unknown["error"]["code"] == mcp_server.METHOD_NOT_FOUND

    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert mcp_server.handle_message(notification) is None

    ping = mcp_server.handle_message({"jsonrpc": "2.0", "id": 6, "method": "ping"})
    assert ping is not None
    assert ping["result"] == {}


def test_mcp_serve_round_trips_over_stdio(mcp_context: None) -> None:
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "{not json",
    ]
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()

    assert mcp_server.serve(stdin, stdout) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [r.get("id") for r in responses] == [1, 2, None]
    assert responses[0]["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert len(responses[1]["result"]["tools"]) == len(TOOL_SPECS)
    assert responses[2]["error"]["code"] == mcp_server.PARSE_ERROR


def test_mcp_default_context_seeds_the_demo_catalog() -> None:
    mcp_server.set_context(None)
    try:
        context = mcp_server.get_context()
        assert context.store.count() > 0
        assert context.store.get(ISS) is not None
    finally:
        mcp_server.set_context(None)
