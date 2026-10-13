"""Tests for the globe's agent chat panel (static/js/agentpanel.js).

The panel is plain browser JavaScript, so these tests drive it through Node.
Nothing here opens a socket to a model or to the internet:

* The pure helpers (request body, transcript entries, client actions, error
  messages, health text) are called through a tiny harness that does
  ``global.window = global; require(<file>)`` and prints JSON.
* Every response and error fixture is produced by the real FastAPI route in
  ``sda.routes.agent``, mounted on a bare app with a scripted LLM client, so the
  JS is checked against what the server really sends rather than against shapes
  invented here. The one exception is the 429 body, which comes from the rate
  limit middleware (``sda/ratelimit.py``, the JSONResponse in ``dispatch``) and
  is copied from it by hand.
* The DOM layer is exercised against a small fake ``document`` defined in the
  harness, with ``fetch`` replaced by a scripted function.

The whole module skips when Node is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import agent as agent_module
from sda.agent import TOOLS_BY_NAME
from sda.demo_tles import DEMO_TLE_TEXT
from sda.llm import (
    AllProvidersFailedError,
    ChatResponse,
    LLMClient,
    OllamaProvider,
    ProviderRequestError,
    ProviderStatus,
    ToolCall,
)
from sda.routes import agent as agent_routes
from sda.tle_store import TLEStore

NODE = shutil.which("node")
JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "sda"
    / "templates"
    / "static"
    / "js"
    / "agentpanel.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

ISS = 25544
TIANHE = 48274


# --- Node harness ----------------------------------------------------------

_PURE_HARNESS = r"""
'use strict';
global.window = global;
require(process.env.AGENTPANEL_JS);
const P = window.SDAAgentPanel;
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
Promise.resolve()
  .then(() => (function (P, input) {
__BODY__
  })(P, input))
  .then((out) => { process.stdout.write(JSON.stringify(out === undefined ? null : out)); })
  .catch((err) => { process.stderr.write(String((err && err.stack) || err)); process.exit(1); });
"""


def run_js(body: str, data: Any = None, *, timeout: int = 30) -> Any:
    """Run a JS function body with ``P`` (the module) and ``input`` in scope."""
    assert NODE is not None
    script = _PURE_HARNESS.replace("__BODY__", body)
    proc = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env={**os.environ, "AGENTPANEL_JS": str(JS_PATH)},
    )
    if proc.returncode != 0:
        pytest.fail(
            f"node exited {proc.returncode}\nstderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        )
    return json.loads(proc.stdout)


# --- Fixtures from the real route -------------------------------------------

class ScriptedLLM(LLMClient):
    """Replays canned responses through the same type the route sees in service."""

    def __init__(
        self, responses: list[ChatResponse], statuses: list[ProviderStatus] | None = None
    ) -> None:
        super().__init__(providers=[])
        self.responses = list(responses)
        self.statuses = list(statuses or [])
        self.calls: list[list[dict[str, Any]]] = []

    def has_provider(self) -> bool:
        return True

    def status(self) -> list[ProviderStatus]:
        return self.statuses

    def chat(self, messages: Any, *args: Any, **kwargs: Any) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self.responses:
            return ChatResponse(text="No more scripted turns.", provider="scripted")
        return self.responses.pop(0)


def _tool_turn(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=tuple(calls),
        provider="ollama",
        model="llama3.1:8b",
        stop_reason="tool_calls",
    )


def _final_turn(text: str) -> ChatResponse:
    return ChatResponse(text=text, provider="ollama", model="llama3.1:8b", stop_reason="stop")


def _demo_store() -> TLEStore:
    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    return store


def _scripted_success() -> ScriptedLLM:
    return ScriptedLLM(
        [
            _tool_turn(
                ToolCall("c1", "list_satellites", {"filter": "ISS", "limit": 5}),
                ToolCall("c2", "get_satellite_state", {"norad_id": 999999}),
                ToolCall("c3", "fly_to", {"norad_id": ISS}),
                ToolCall(
                    "c4",
                    "fly_to",
                    {
                        "latitude_deg": 30.27,
                        "longitude_deg": -97.74,
                        "altitude_km": 1500.0,
                        "label": "Austin",
                    },
                ),
                ToolCall("c5", "fly_to", {"norad_id": 99999}),
                ToolCall("c6", "fly_to", {"norad_id": TIANHE, "altitude_km": 900.0}),
            ),
            _final_turn("The ISS is at about 420 km. Number 999999 is not tracked."),
        ]
    )


def _build_route_fixtures() -> dict[str, Any]:
    """Drive the real agent router and record what it sends back."""
    out: dict[str, Any] = {}
    store = _demo_store()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agent_module, "_default_store", lambda: store)
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SDA_OLLAMA_URL", "SDA_LLM_MODEL"):
            mp.delenv(key, raising=False)
        app = FastAPI()
        app.include_router(agent_routes.router)
        client = TestClient(app)
        user = {"role": "user", "content": "show me the ISS"}

        def record(response: Any) -> dict[str, Any]:
            return {"status": response.status_code, "body": response.json()}

        # 200: tools run, one of them fails, four fly_to actions come back.
        agent_routes.clear_sessions()
        mp.setattr(agent_routes, "get_client", _scripted_success)
        scene = {
            "camera": {"latitude_deg": 10.0, "longitude_deg": 20.0, "altitude_km": 9000.0},
            "selected": [{"norad_id": ISS, "name": "ISS (ZARYA)"}],
        }
        out["success"] = record(
            client.post("/agent/chat", json={"messages": [user], "scene": scene})
        )

        # 200 with stop_reason max_iterations (the model never stopped calling tools).
        capped = ScriptedLLM([_tool_turn(ToolCall("c1", "list_satellites", {"limit": 3}))])
        mp.setattr(agent_routes, "get_client", lambda: capped)
        out["capped"] = record(
            client.post("/agent/chat", json={"messages": [user], "max_iterations": 1})
        )

        # 422: the route's own empty-messages check, then pydantic validation.
        quiet = ScriptedLLM([])
        mp.setattr(agent_routes, "get_client", lambda: quiet)
        out["empty_422"] = record(client.post("/agent/chat", json={"messages": []}))
        out["validation_422"] = record(
            client.post("/agent/chat", json={"messages": [user], "max_iterations": 99})
        )

        # 503 and the matching /agent/health body: nothing configured.
        unconfigured = LLMClient()
        mp.setattr(agent_routes, "get_client", lambda: unconfigured)
        out["no_provider_503"] = record(client.post("/agent/chat", json={"messages": [user]}))
        out["health_unconfigured"] = record(client.get("/agent/health"))

        # Health with a local model configured and answering.
        mp.setenv("SDA_OLLAMA_URL", "http://127.0.0.1:11434")
        mp.setenv("SDA_OLLAMA_MODEL", "llama3.1:8b")
        ready = LLMClient(providers=[OllamaProvider()])
        mp.setattr(agent_routes, "get_client", lambda: ready)
        out["health_ready"] = record(client.get("/agent/health"))

        # 502, then health again with the provider cooling down after that failure.
        def failing_chat(self: Any, *args: Any, **kwargs: Any) -> ChatResponse:
            raise ProviderRequestError(
                "http", 'status 400: {"error":"Value looks like object, but can\'t find closing"}'
            )

        mp.setattr(OllamaProvider, "chat", failing_chat)
        out["providers_failed_502"] = record(
            client.post("/agent/chat", json={"messages": [user]})
        )
        out["health_cooling"] = record(client.get("/agent/health"))

        # The same 502 shape, raised directly, for a second provider name.
        class Broken(ScriptedLLM):
            def chat(self, *args: Any, **kwargs: Any) -> ChatResponse:
                raise AllProvidersFailedError({"ollama": "request failed: timed out"})

        broken = Broken([], [ProviderStatus("ollama", True, "llama3.1:8b", 1, "timed out", 30.0)])
        mp.setattr(agent_routes, "get_client", lambda: broken)
        out["timed_out_502"] = record(client.post("/agent/chat", json={"messages": [user]}))
    agent_routes.clear_sessions()

    # Copied from the JSONResponse in sda/ratelimit.py (RateLimitMiddleware.dispatch).
    out["rate_limited_429"] = {
        "status": 429,
        "body": {"detail": "Rate limit exceeded", "limit": 60, "retry_after_seconds": 7},
    }
    return out


@pytest.fixture(scope="module")
def fx() -> dict[str, Any]:
    return _build_route_fixtures()


# --- 1. The file itself ------------------------------------------------------

def test_script_passes_node_check() -> None:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "--check", str(JS_PATH)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr


def test_module_exports_init_and_focus() -> None:
    exported = run_js(
        "return {init: typeof P.init, focus: typeof P.focus,"
        " on_window: window.SDAAgentPanel === P};"
    )
    assert exported == {"init": "function", "focus": "function", "on_window": True}


def test_source_stays_inside_the_house_rules() -> None:
    source = JS_PATH.read_text(encoding="utf-8")
    # Untrusted model text must never go through an HTML parser.
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in source, sink
    # A hook blocks these two tokens in source; spelled in pieces so this file passes it too.
    for token in ("WGS" + "84", "EC" + "EF"):
        assert token not in source
    assert "—" not in source and "–" not in source
    assert source.isascii()
    assert "celestrak" not in source.lower()


# --- 2. Request body ---------------------------------------------------------

def test_first_turn_body_carries_only_the_message() -> None:
    body = run_js("return P.buildRequestBody({sessionId: null}, '  hello  ', null);")
    assert body == {"messages": [{"role": "user", "content": "hello"}]}


def test_follow_up_body_reuses_the_session_and_sends_only_the_new_turn() -> None:
    body = run_js(
        "return P.buildRequestBody({sessionId: 'abc123'}, 'and now?', input, 4);",
        {"camera": {"latitude_deg": 1.0, "longitude_deg": 2.0, "altitude_km": 3.0}},
    )
    assert body == {
        "session_id": "abc123",
        "messages": [{"role": "user", "content": "and now?"}],
        "scene": {"camera": {"latitude_deg": 1.0, "longitude_deg": 2.0, "altitude_km": 3.0}},
        "max_iterations": 4,
    }


def test_out_of_range_iteration_caps_are_left_out() -> None:
    bodies = run_js(
        "return input.map(function (n) {"
        " return P.buildRequestBody({sessionId: null}, 'x', null, n); });",
        [0, 13, -1, 2.5, "x", None],
    )
    assert all("max_iterations" not in body for body in bodies)
    edges = run_js(
        "return input.map(function (n) {"
        " return P.buildRequestBody({sessionId: null}, 'x', null, n); });",
        [1, 12],
    )
    assert [body["max_iterations"] for body in edges] == [1, 12]


def test_built_bodies_are_accepted_by_the_route_and_history_is_not_duplicated() -> None:
    """The route stores the conversation under session_id; resending it would double it."""
    store = _demo_store()
    llm = ScriptedLLM([_final_turn("first answer"), _final_turn("second answer")])
    first = run_js(
        "return P.buildRequestBody({sessionId: null}, 'first question', input);",
        {"camera": {"latitude_deg": 0.0, "longitude_deg": 0.0, "altitude_km": 20000.0}},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agent_module, "_default_store", lambda: store)
        mp.setattr(agent_routes, "get_client", lambda: llm)
        agent_routes.clear_sessions()
        app = FastAPI()
        app.include_router(agent_routes.router)
        client = TestClient(app)

        one = client.post("/agent/chat", json=first)
        assert one.status_code == 200
        second = run_js(
            "return P.buildRequestBody({sessionId: input}, 'second question', null);",
            one.json()["session_id"],
        )
        two = client.post("/agent/chat", json=second)
        assert two.status_code == 200
        agent_routes.clear_sessions()

    assert two.json()["session_id"] == one.json()["session_id"]
    seen = [m["content"] for m in llm.calls[-1]]
    assert seen == ["first question", "first answer", "second question"]


# --- 3. Scene ----------------------------------------------------------------

def test_scene_is_built_from_camera_selection_layers_and_time() -> None:
    scene = run_js(
        "return P.buildScene(input);",
        {
            "camera": {"latitude": 0.5235987755982988, "longitude": -1.0471975511965976,
                       "height": 12345678.9},
            "selectedId": ISS,
            "byNoradId": {str(ISS): {"noradId": ISS, "name": "ISS (ZARYA)"}},
            "visibleLayers": ["satellites", "orbits"],
            "timeIso": "2026-10-08T12:00:00.000Z",
        },
    )
    assert scene == {
        "camera": {"latitude_deg": 30.0, "longitude_deg": -60.0, "altitude_km": 12345.7},
        "visible_layers": ["satellites", "orbits"],
        "selected": [{"norad_id": ISS, "name": "ISS (ZARYA)"}],
        "time_utc": "2026-10-08T12:00:00.000Z",
    }


def test_scene_leaves_out_what_it_does_not_know() -> None:
    scene = run_js(
        "return P.buildScene(input);",
        {"camera": None, "selectedId": None, "byNoradId": {}, "visibleLayers": None},
    )
    assert scene == {"selected": []}


# --- 4. Responses become transcript entries ---------------------------------

def test_success_response_becomes_answer_then_tools_then_actions_then_meta(
    fx: dict[str, Any],
) -> None:
    assert fx["success"]["status"] == 200
    entries = run_js("return P.responseToEntries(input, 12);", fx["success"]["body"])

    kinds = [e["kind"] for e in entries]
    assert kinds == ["answer", *["tool"] * 6, *["action"] * 4, "meta"]
    assert entries[0]["text"] == "The ISS is at about 420 km. Number 999999 is not tracked."
    tools = entries[1:7]
    assert [t["ok"] for t in tools] == [True, False, True, True, True, True]
    assert tools[0]["text"] == "list_satellites(filter=ISS, limit=5)"
    assert tools[1]["text"].startswith("get_satellite_state(norad_id=999999) failed: ")
    assert "999999" in tools[1]["text"]
    assert tools[2]["text"] == "fly_to(norad_id=25544)"
    assert entries[-1] == {"kind": "meta", "text": "ollama / llama3.1:8b, 12 s"}


def test_meta_line_without_elapsed_time_or_provider(fx: dict[str, Any]) -> None:
    body = fx["success"]["body"]
    plain = run_js("return P.responseToEntries(input, null).pop();", body)
    assert plain == {"kind": "meta", "text": "ollama / llama3.1:8b"}
    bare = run_js(
        "input.provider = ''; input.model = ''; input.trace = []; input.actions = [];"
        " return P.responseToEntries(input, 3).map(function (e) { return e.kind; });",
        body,
    )
    assert bare == ["answer"]


def test_capped_response_says_so(fx: dict[str, Any]) -> None:
    body = fx["capped"]["body"]
    assert body["stop_reason"] == "max_iterations"
    entries = run_js("return P.responseToEntries(input, 40);", body)
    kinds = [e["kind"] for e in entries]
    assert kinds[0] == "answer" and "note" in kinds
    note = next(e for e in entries if e["kind"] == "note")
    assert "tool-call limit" in note["text"]


def test_empty_answer_text_gets_a_placeholder() -> None:
    entries = run_js(
        "return P.responseToEntries(input, null);",
        {"session_id": "s", "text": "", "actions": [], "trace": [], "provider": "", "model": ""},
    )
    assert entries == [{"kind": "note", "text": "The model returned no text."}]


def test_garbage_responses_do_not_throw() -> None:
    out = run_js(
        "return input.map(function (r) { return P.responseToEntries(r, null); });",
        [None, 5, "text", [], {"text": 7, "trace": "no", "actions": {}}, {"trace": [None, 3, {}]}],
    )
    assert all(isinstance(entries, list) for entries in out)


def test_disclaimer_is_read_from_the_response(fx: dict[str, Any]) -> None:
    body = fx["success"]["body"]
    assert body["disclaimer"]
    assert run_js("return P.disclaimerOf(input);", body) == body["disclaimer"]
    assert run_js("return P.disclaimerOf({});") == ""


def test_argument_summary_is_one_short_line() -> None:
    cases = [
        ({"filter": "ISS", "limit": 5}, "filter=ISS, limit=5"),
        (
            {"norad_ids": [48274, 53239], "hours": 0.5, "threshold_km": 10.0},
            "norad_ids=[48274,53239], hours=0.5, threshold_km=10",
        ),
        ({}, ""),
        (None, ""),
        ({"flag": True, "none": None}, "flag=true, none=null"),
        ({"time_utc": "x" * 60}, "time_utc=" + "x" * 29 + "..."),
        ({"note": "two\nlines  here"}, "note=two lines here"),
    ]
    got = run_js(
        "return input.map(function (a) { return P.summarizeArgs(a); });",
        [arguments for arguments, _ in cases],
    )
    assert got == [expected for _, expected in cases]


def test_argument_summary_is_capped_overall() -> None:
    many = {f"key{i}": f"value{i}" for i in range(40)}
    summary = run_js("return P.summarizeArgs(input);", many)
    assert len(summary) <= 90 and summary.endswith("...") and "\n" not in summary


def test_elapsed_time_formatting() -> None:
    cases = [(0, "0 s"), (7.4, "7 s"), (59, "59 s"), (60, "1 min 0 s"), (72, "1 min 12 s"),
             (-5, "0 s"), (None, "0 s"), ("x", "0 s")]
    got = run_js(
        "return input.map(function (s) { return P.formatElapsed(s); });",
        [seconds for seconds, _ in cases],
    )
    assert got == [expected for _, expected in cases]


# --- 5. Client actions -------------------------------------------------------

def test_extract_actions_returns_the_route_actions_and_ignores_unknown_types(
    fx: dict[str, Any],
) -> None:
    body = fx["success"]["body"]
    assert [a["type"] for a in body["actions"]] == ["fly_to"] * 4
    assert body["actions"][0] == {"type": "fly_to", "norad_id": ISS, "name": "ISS (ZARYA)"}

    mixed = {
        **body,
        "actions": [
            {"type": "highlight", "ids": [1, 2]},
            *body["actions"],
            None,
            7,
            "fly_to",
            {"no_type": True},
            {"type": 5},
        ],
    }
    assert run_js("return P.extractActions(input);", mixed) == body["actions"]
    assert run_js("return P.extractActions(input);", {"actions": "nope"}) == []
    assert run_js("return P.extractActions(null);") == []


_ON_GLOBE = {
    str(ISS): {"noradId": ISS, "name": "ISS (ZARYA)", "lastLat": 12.5, "lastLon": -40.0,
               "lastAlt": 420.5},
    str(TIANHE): {"noradId": TIANHE, "name": "CSS (TIANHE)", "lastLat": -3.0, "lastLon": 77.0,
                  "lastAlt": 385.0},
    "777": {"noradId": 777, "name": "NO POSITION YET"},
}

_COORDS = {"latitude_deg": 0.0, "longitude_deg": 0.0}

_PLAN_CASES: dict[str, dict[str, Any]] = {
    "satellite": {"type": "fly_to", "norad_id": ISS, "name": "ISS (ZARYA)"},
    "standoff": {"type": "fly_to", "norad_id": TIANHE, "altitude_km": 900.0},
    "coordinates": {"type": "fly_to", "latitude_deg": 30.27, "longitude_deg": -97.74,
                    "altitude_km": 1500.0, "label": "Austin"},
    "default_standoff": {"type": "fly_to", "latitude_deg": -10.0, "longitude_deg": 150.0},
    "missing": {"type": "fly_to", "norad_id": 99999},
    "missing_with_coordinates": {"type": "fly_to", "norad_id": 99999, "latitude_deg": 1.0,
                                 "longitude_deg": 2.0},
    "no_position": {"type": "fly_to", "norad_id": 777, "altitude_km": 500.0},
    "far": {"type": "fly_to", **_COORDS, "altitude_km": 9e9},
    "near": {"type": "fly_to", **_COORDS, "altitude_km": 0},
    "bad_empty": {"type": "fly_to"},
    "bad_half": {"type": "fly_to", "latitude_deg": 10.0},
    "bad_lat": {"type": "fly_to", "latitude_deg": 95.0, "longitude_deg": 0.0},
    "bad_lon": {"type": "fly_to", "latitude_deg": 0.0, "longitude_deg": 181.0},
    "bad_text": {"type": "fly_to", "latitude_deg": "north", "longitude_deg": 0.0},
    "bad_norad": {"type": "fly_to", "norad_id": "ISS"},
}


@pytest.fixture(scope="module")
def plans() -> dict[str, dict[str, Any]]:
    """Every fly_to case planned in one Node run (a Node start is the slow part)."""
    names = list(_PLAN_CASES)
    out = run_js(
        "return input.names.map(function (n) {"
        " return P.planFlyTo(input.cases[n], input.sats); });",
        {"names": names, "cases": _PLAN_CASES, "sats": _ON_GLOBE},
    )
    return dict(zip(names, out, strict=True))


def test_fly_to_a_satellite_selects_it_and_leaves_the_camera_to_the_page(
    plans: dict[str, Any],
) -> None:
    plan = plans["satellite"]
    assert plan["ok"] is True
    assert (plan["noradId"], plan["select"], plan["flight"]) == (ISS, True, None)
    assert plan["text"] == "Fly to ISS (ZARYA) (NORAD 25544)"


def test_fly_to_a_satellite_with_a_standoff_adds_a_camera_flight_above_it(
    plans: dict[str, Any],
) -> None:
    plan = plans["standoff"]
    assert plan["select"] is True
    assert plan["flight"] == {"lat": -3.0, "lon": 77.0, "heightKm": 1285.0}


def test_fly_to_coordinates_flies_the_camera_there(plans: dict[str, Any]) -> None:
    plan = plans["coordinates"]
    assert plan["ok"] is True and plan["select"] is False and plan["noradId"] is None
    assert plan["flight"] == {"lat": 30.27, "lon": -97.74, "heightKm": 1500.0}
    assert plan["text"] == "Fly to Austin (30.27, -97.74)"


def test_fly_to_coordinates_without_altitude_uses_the_default_standoff(
    plans: dict[str, Any],
) -> None:
    plan = plans["default_standoff"]
    default = run_js("return P.DEFAULT_FLY_HEIGHT_KM;")
    assert plan["flight"] == {"lat": -10.0, "lon": 150.0, "heightKm": default}
    assert plan["text"] == "Fly to -10, 150"


def test_fly_to_a_satellite_the_globe_does_not_have(plans: dict[str, Any]) -> None:
    plan = plans["missing"]
    assert plan["ok"] is False and plan["select"] is False and plan["flight"] is None
    assert "99999" in plan["reason"]


def test_fly_to_an_unknown_satellite_falls_back_to_its_coordinates(
    plans: dict[str, Any],
) -> None:
    plan = plans["missing_with_coordinates"]
    assert plan["ok"] is True and plan["select"] is False
    assert (plan["flight"]["lat"], plan["flight"]["lon"]) == (1.0, 2.0)


def test_fly_to_a_satellite_with_no_position_yet_still_selects_it(
    plans: dict[str, Any],
) -> None:
    plan = plans["no_position"]
    assert plan["ok"] is True and plan["select"] is True and plan["flight"] is None


def test_unusable_fly_to_is_reported_not_thrown(plans: dict[str, Any]) -> None:
    for name in [n for n in _PLAN_CASES if n.startswith("bad_")]:
        assert plans[name]["ok"] is False and plans[name]["reason"], name


def test_fly_to_standoff_is_clamped_to_a_sane_range(plans: dict[str, Any]) -> None:
    assert plans["far"]["flight"]["heightKm"] == 500000.0
    assert 0 < plans["near"]["flight"]["heightKm"] <= 1.0


# --- 6. Errors ---------------------------------------------------------------

@pytest.fixture(scope="module")
def errors(fx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every error shape described in one Node run."""
    cases: dict[str, Any] = {
        "no_provider": {"kind": "http", **fx["no_provider_503"]},
        "providers_failed": {"kind": "http", **fx["providers_failed_502"]},
        "timed_out": {"kind": "http", **fx["timed_out_502"]},
        "bare_502": {"kind": "http", "status": 502,
                     "body": {"detail": {"error": "llm_providers_failed"}}},
        "empty_422": {"kind": "http", **fx["empty_422"]},
        "validation_422": {"kind": "http", **fx["validation_422"]},
        "rate_limited": {"kind": "http", **fx["rate_limited_429"]},
        "not_found": {"kind": "http", "status": 404, "body": {"detail": "Not Found"}},
        "bare_500": {"kind": "http", "status": 500, "body": None},
        "text_500": {"kind": "http", "status": 500, "body": {"detail": "database is locked"}},
        "parse": {"kind": "parse", "status": 200},
        "network": {"kind": "network"},
        "timeout": {"kind": "timeout", "timeoutMs": 420000},
        "weird": {"kind": "weird", "message": "boom"},
        "nothing": None,
    }
    names = list(cases)
    out = run_js(
        "return input.names.map(function (n) { return P.describeError(input.cases[n]); });",
        {"names": names, "cases": cases},
    )
    return dict(zip(names, out, strict=True))


def test_503_shows_the_servers_own_setup_hint(
    fx: dict[str, Any], errors: dict[str, Any]
) -> None:
    result = fx["no_provider_503"]
    assert result["status"] == 503
    out = errors["no_provider"]
    assert out["message"] == "The agent has no language model set up on this server."
    assert out["hint"] == result["body"]["detail"]["setup"]
    assert "SDA_OLLAMA_URL" in out["hint"]


def test_502_names_the_failed_provider_and_keeps_the_server_detail_quiet(
    fx: dict[str, Any], errors: dict[str, Any]
) -> None:
    result = fx["providers_failed_502"]
    assert result["status"] == 502
    assert result["body"]["detail"]["error"] == "llm_providers_failed"
    out = errors["providers_failed"]
    assert out["message"] == (
        "The language model (ollama) could not answer that. Try again in a moment."
    )
    assert "status 400" in out["hint"] and len(out["hint"]) <= 300


def test_502_without_a_provider_list_still_reads_as_a_sentence(errors: dict[str, Any]) -> None:
    assert errors["timed_out"]["message"].startswith(
        "The language model (ollama) could not answer"
    )
    assert errors["bare_502"]["message"] == (
        "The language model could not answer that. Try again in a moment."
    )


def test_422_from_the_route_and_from_validation(errors: dict[str, Any]) -> None:
    assert errors["empty_422"]["message"] == (
        "The server rejected the message: messages must not be empty."
    )
    bad = errors["validation_422"]["message"]
    assert bad.startswith("The server rejected the request (max_iterations: ")
    assert bad.endswith(").")


def test_429_says_how_long_to_wait(errors: dict[str, Any]) -> None:
    assert errors["rate_limited"]["message"] == (
        "Too many requests just now. Wait 7 s and try again."
    )


def test_other_failures_each_get_one_plain_sentence(errors: dict[str, Any]) -> None:
    assert errors["not_found"]["message"] == (
        "This server has no agent endpoint. It may be an older build."
    )
    assert errors["bare_500"]["message"] == "The server answered with an error (HTTP 500)."
    assert errors["parse"]["message"] == (
        "The server sent a reply the panel could not read (HTTP 200)."
    )
    assert errors["network"]["message"] == (
        "Could not reach the server. Check that it is running, then try again."
    )
    assert errors["timeout"]["message"] == (
        "No answer after 7 min 0 s. The model may still be working on it; "
        "wait a minute and try again."
    )
    assert errors["weird"]["message"] == "Something went wrong: boom"
    assert errors["nothing"]["message"] == "Something went wrong."


def test_a_500_with_a_text_detail_keeps_it_as_the_hint(errors: dict[str, Any]) -> None:
    assert errors["text_500"]["hint"] == "database is locked"
    assert errors["bare_500"]["hint"] == ""


def test_thrown_values_are_classified_by_how_they_failed() -> None:
    out = run_js(
        "return [P.classifyThrown(input[0], false), P.classifyThrown(input[1], true),"
        " P.classifyThrown(input[2], false), P.classifyThrown(null, false)];",
        [{"name": "TypeError", "message": "Failed to fetch"},
         {"name": "AbortError", "message": "aborted"},
         {"kind": "http", "status": 502, "body": {}}],
    )
    assert out[0]["kind"] == "network"
    assert out[1]["kind"] == "timeout"
    assert out[2] == {"kind": "http", "status": 502, "body": {}}
    assert out[3]["kind"] == "network"


# --- 7. Health line ----------------------------------------------------------

@pytest.fixture(scope="module")
def health(fx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases: dict[str, Any] = {
        "ready": fx["health_ready"]["body"],
        "unconfigured": fx["health_unconfigured"]["body"],
        "cooling": fx["health_cooling"]["body"],
        "unreachable": None,
        "malformed": {"providers": "x"},
    }
    names = list(cases)
    out = run_js(
        "return input.names.map(function (n) { return P.describeHealth(input.cases[n]); });",
        {"names": names, "cases": cases},
    )
    return dict(zip(names, out, strict=True))


def test_health_ready_names_the_provider_and_model(
    fx: dict[str, Any], health: dict[str, Any]
) -> None:
    body = fx["health_ready"]["body"]
    assert body["configured"] is True and body["active_provider"] == "ollama"
    assert health["ready"] == {
        "state": "ready", "text": "Answers from ollama (llama3.1:8b).", "hint": ""
    }


def test_health_unconfigured_carries_the_servers_setup_text(
    fx: dict[str, Any], health: dict[str, Any]
) -> None:
    body = fx["health_unconfigured"]["body"]
    assert body["configured"] is False and body["setup"]
    out = health["unconfigured"]
    assert out["state"] == "unconfigured"
    assert out["text"] == "No language model is configured on this server."
    assert out["hint"] == body["setup"]


def test_health_cooling_down_after_a_failure(
    fx: dict[str, Any], health: dict[str, Any]
) -> None:
    assert fx["providers_failed_502"]["status"] == 502
    body = fx["health_cooling"]["body"]
    assert body["configured"] is True and body["active_provider"] is None
    out = health["cooling"]
    assert out["state"] == "cooling"
    assert out["text"].startswith("ollama is cooling down after a failure (")
    assert "status 400" in out["hint"]


def test_health_unreachable_and_malformed(health: dict[str, Any]) -> None:
    assert health["unreachable"]["state"] == "unreachable"
    assert health["malformed"]["state"] == "unreachable"


# --- 8. Starter prompts ------------------------------------------------------

def test_every_starter_prompt_leans_on_a_tool_the_agent_really_has() -> None:
    starters = run_js("return P.STARTERS;")
    assert 3 <= len(starters) <= 6
    for starter in starters:
        assert starter["label"] and starter["prompt"]
        assert len(starter["label"]) <= 26, starter["label"]
        assert starter["tools"], starter
        for tool in starter["tools"]:
            assert tool in TOOLS_BY_NAME, tool


# --- 9. The panel against a fake document ------------------------------------
#
# A minimal DOM (elements, classList, textContent, events that bubble and can be
# stopped) and a scripted fetch. It checks wiring, not looks: what the panel
# builds, what it sends, what it does with the answer, and that nothing it does
# can take the globe down.

_DOM_PRELUDE = r"""
'use strict';
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = [];
  this.listeners = {};
  this.attrs = {};
  this.style = {};
  this.parentNode = null;
  this.className = '';
  this.disabled = false;
  this.hidden = false;
  this.value = '';
  this.scrollTop = 0;
  this.scrollHeight = 0;
  this._text = '';
  var self = this;
  function tokens() { return self.className.split(/\s+/).filter(Boolean); }
  this.classList = {
    add: function (c) {
      var t = tokens(); if (t.indexOf(c) < 0) t.push(c); self.className = t.join(' ');
    },
    remove: function (c) {
      self.className = tokens().filter(function (x) { return x !== c; }).join(' ');
    },
    contains: function (c) { return tokens().indexOf(c) >= 0; },
    toggle: function (c) {
      if (tokens().indexOf(c) >= 0) this.remove(c); else this.add(c);
    }
  };
}
Object.defineProperty(El.prototype, 'textContent', {
  get: function () {
    return this._text + this.children.map(function (c) { return c.textContent; }).join('');
  },
  set: function (v) { this._text = String(v); this.children = []; }
});
Object.defineProperty(El.prototype, 'firstChild', {
  get: function () { return this.children[0] || null; }
});
Object.defineProperty(El.prototype, 'parentElement', {
  get: function () { return this.parentNode; }
});
El.prototype.appendChild = function (c) { c.parentNode = this; this.children.push(c); return c; };
El.prototype.removeChild = function (c) {
  var i = this.children.indexOf(c);
  if (i >= 0) { this.children.splice(i, 1); c.parentNode = null; }
  return c;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) {
  return this.attrs[k] === undefined ? null : this.attrs[k];
};
El.prototype.addEventListener = function (t, f) {
  (this.listeners[t] = this.listeners[t] || []).push(f);
};
El.prototype.focus = function () { global.document.activeElement = this; };
El.prototype.blur = function () {
  if (global.document.activeElement === this) global.document.activeElement = null;
};
El.prototype.scrollIntoView = function () {};
El.prototype.dispatch = function (type, init) {
  var ev = init || {};
  ev.type = type;
  ev.target = ev.target || this;
  ev.defaultPrevented = false;
  var stopped = false, stoppedNow = false;
  ev.stopPropagation = function () { stopped = true; };
  ev.stopImmediatePropagation = function () { stopped = true; stoppedNow = true; };
  ev.preventDefault = function () { ev.defaultPrevented = true; };
  if (type === 'click' && this.disabled) return ev;      // a disabled control swallows clicks
  for (var node = this; node; node = node.parentNode) {
    var list = (node.listeners[type] || []).slice();
    for (var i = 0; i < list.length; i++) {
      list[i].call(node, ev);
      if (stoppedNow) break;
    }
    if (stopped) break;
  }
  return ev;
};

global.window = global;
global.document = { head: new El('head'), activeElement: null,
                    createElement: function (t) { return new El(t); } };
global.Cesium = { Cartesian3: { fromDegrees: function (lon, lat, h) {
  return { lon: lon, lat: lat, h: h };
} } };
require(process.env.AGENTPANEL_JS);
const P = window.SDAAgentPanel;
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));

function walk(el, fn) { fn(el); el.children.forEach(function (c) { walk(c, fn); }); }
function all(root, cls) {
  var out = [];
  walk(root, function (e) { if (e.classList.contains(cls)) out.push(e); });
  return out;
}
function one(root, cls) { return all(root, cls)[0] || null; }
function text(root, cls) { return all(root, cls).map(function (e) { return e.textContent; }); }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function waitFor(pred, ms) {
  var until = Date.now() + (ms || 2000);
  return new Promise(function (resolve, reject) {
    (function tick() {
      var ok = false;
      try { ok = pred(); } catch (e) { ok = false; }
      if (ok) return resolve(true);
      if (Date.now() > until) return reject(new Error('waitFor timed out: ' + pred));
      setTimeout(tick, 5);
    })();
  });
}

var calls = [];
var routes = {};
global.fetch = function (url, opts) {
  opts = opts || {};
  var method = (opts.method || 'GET').toUpperCase();
  var call = { url: url, method: method, body: opts.body ? JSON.parse(opts.body) : null };
  calls.push(call);
  var handler = routes[method + ' ' + url.replace(/^https?:\/\/[^/]+/, '')];
  if (!handler) return Promise.reject(new TypeError('no route for ' + method + ' ' + url));
  return Promise.resolve(handler(call, opts)).then(function (r) {
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      text: function () {
        return Promise.resolve(r.raw !== undefined ? r.raw : JSON.stringify(r.body));
      }
    };
  });
};
function posts() { return calls.filter(function (c) { return c.method === 'POST'; }); }

function makeCtx(over) {
  var root = new El('body');
  var rootKeys = [];
  ['keydown', 'keyup', 'keypress'].forEach(function (t) {
    root.addEventListener(t, function (e) { rootKeys.push(t + ':' + e.key); });
  });
  var mount = new El('div');
  root.appendChild(mount);
  var rec = { flights: [], selected: [], intel: [] };
  var ctx = {
    viewer: { camera: {
      flyTo: function (o) { rec.flights.push(o); },
      positionCartographic: {
        latitude: 0.5235987755982988, longitude: -1.0471975511965976, height: 9000000
      }
    } },
    satEngine: { selectedId: null, byNoradId: {
      '25544': {
        noradId: 25544, name: 'ISS (ZARYA)', lastLat: 12.5, lastLon: -40, lastAlt: 420.5
      },
      '48274': { noradId: 48274, name: 'CSS (TIANHE)', lastLat: -3, lastLon: 77, lastAlt: 385 }
    } },
    selectSatellite: function (id) { rec.selected.push(id); },
    addIntelItem: function (t) { rec.intel.push(t); },
    api: 'http://srv', isStatic: false, mount: mount
  };
  Object.keys(over || {}).forEach(function (k) { ctx[k] = over[k]; });
  return { ctx: ctx, root: root, mount: mount, rec: rec, rootKeys: rootKeys };
}
function key(el, k, extra) {
  var out = [];
  ['keydown', 'keypress', 'keyup'].forEach(function (t) {
    var ev = Object.assign({ key: k }, extra || {});
    el.dispatch(t, ev);
    out.push(ev);
  });
  return out;
}
function type(el, value) { el.value = value; }

const h = {
  El, P, input, calls, routes, posts, makeCtx, walk, all, one, text, wait, waitFor, key, type
};
Promise.resolve()
  .then(() => (async function (h) {
__BODY__
  })(h))
  .then(async (out) => {
    // Nothing the panel started may still be ticking once a scenario is over.
    await wait(40);
    const live = process.getActiveResourcesInfo()
      .filter((n) => n === 'Timeout' || n === 'Immediate');
    if (live.length) throw new Error('timers left running: ' + live.length);
    process.stdout.write(JSON.stringify(out === undefined ? null : out));
  })
  .catch((err) => { process.stderr.write(String((err && err.stack) || err)); process.exit(1); });
"""


def run_dom(body: str, data: Any = None, *, timeout: int = 20) -> Any:
    """Run an async scenario against the fake document. ``h`` carries the helpers."""
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", _DOM_PRELUDE.replace("__BODY__", body)],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env={**os.environ, "AGENTPANEL_JS": str(JS_PATH)},
    )
    if proc.returncode != 0:
        pytest.fail(
            f"node exited {proc.returncode}\nstderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        )
    return json.loads(proc.stdout)


_BOOT = r"""
  const fx = h.input.fx;
  const t = h.makeCtx(h.input.ctx || {});
  h.routes['GET /agent/health'] = () => (h.input.healthRoute || fx.health_ready);
  h.routes['POST /agent/chat'] = (call) => (h.input.chatRoute || fx.success);
  P.init(t.ctx);
  const sendBtn = () => h.one(t.mount, 'agentpanel-send');
  const box = () => h.one(t.mount, 'agentpanel-input');
  const log = () => h.one(t.mount, 'agentpanel-log');
  const ask = (text) => { h.type(box(), text); h.key(box(), 'Enter'); };
  const idle = () => h.waitFor(() => !sendBtn().disabled);
"""


def _dom(scenario: str, fx: dict[str, Any], **extra: Any) -> Any:
    return run_dom(_BOOT + scenario, {"fx": fx, **extra})


def test_static_build_gets_a_one_line_note_and_no_requests() -> None:
    out = run_dom(
        """
        const t = h.makeCtx({ isStatic: true });
        P.init(t.ctx);
        await h.wait(30);
        return { text: t.mount.textContent, calls: h.calls.length,
                 inputs: h.all(t.mount, 'agentpanel-input').length };
        """,
        {},
    )
    assert "live server" in out["text"]
    assert out["calls"] == 0 and out["inputs"] == 0


def test_panel_uses_the_pages_markup_and_shows_the_health_line(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        await h.waitFor(() => !/^Checking/.test(h.text(t.mount, 'agentpanel-health')[0]));
        const panel = t.mount.children[0];
        const h3 = panel.children[0];
        const body = panel.children[1];
        const first = { cls: panel.className, h3: h3.tagName, body: body.className,
                        health: h.text(t.mount, 'agentpanel-health')[0],
                        get: h.calls[0], styles: document.head.children.length };
        h3.dispatch('click', {});
        const collapsed = panel.classList.contains('collapsed');
        h3.dispatch('click', {});
        return { first, collapsed, reopened: !panel.classList.contains('collapsed'),
                 buttons: h.all(t.mount, 'mode-btn').length,
                 starters: h.all(t.mount, 'agentpanel-starter').length };
        """,
        fx,
    )
    assert "panel" in out["first"]["cls"].split()
    assert out["first"]["h3"] == "H3" and out["first"]["body"] == "panel-body"
    assert out["first"]["health"] == "Answers from ollama (llama3.1:8b)."
    assert out["first"]["get"] == {"url": "http://srv/agent/health", "method": "GET", "body": None}
    assert out["first"]["styles"] == 1
    assert out["collapsed"] is True and out["reopened"] is True
    assert out["buttons"] >= 2
    assert out["starters"] == len(run_js("return P.STARTERS;"))


def test_health_line_shows_the_servers_setup_hint_when_nothing_is_configured(
    fx: dict[str, Any],
) -> None:
    out = _dom(
        """
        await h.waitFor(() => h.text(t.mount, 'agentpanel-hint')[0]);
        return { health: h.text(t.mount, 'agentpanel-health')[0],
                 hint: h.text(t.mount, 'agentpanel-hint')[0] };
        """,
        fx,
        healthRoute={"status": 200, "body": fx["health_unconfigured"]["body"]},
    )
    assert out["health"] == "No language model is configured on this server."
    assert "SDA_OLLAMA_URL" in out["hint"]


def test_unreachable_health_does_not_block_the_panel() -> None:
    out = run_dom(
        """
        const t = h.makeCtx({});
        P.init(t.ctx);                       // no routes: every fetch rejects like a dead server
        await h.waitFor(() => !/^Checking/.test(h.text(t.mount, 'agentpanel-health')[0]));
        return { health: h.text(t.mount, 'agentpanel-health')[0],
                 enabled: !h.one(t.mount, 'agentpanel-send').disabled };
        """,
        {},
    )
    assert out["health"] == "The agent status could not be read from the server."
    assert out["enabled"] is True


def test_sending_shows_a_working_state_then_the_answer_tools_and_provider(
    fx: dict[str, Any],
) -> None:
    out = _dom(
        """
        ask('show me the ISS');
        const during = {
          disabled: sendBtn().disabled,
          status: h.text(t.mount, 'agentpanel-status')[0],
          cleared: box().value
        };
        await idle();
        const after = { status: h.text(t.mount, 'agentpanel-status')[0],
                        users: h.text(t.mount, 'agentpanel-user'),
                        answers: h.text(t.mount, 'agentpanel-answer'),
                        tools: h.text(t.mount, 'agentpanel-tool'),
                        meta: h.text(t.mount, 'agentpanel-meta'),
                        disclaimer: h.text(t.mount, 'agentpanel-disclaimer')[0] };
        return { during, after, post: h.posts()[0], count: h.posts().length };
        """,
        fx,
    )
    assert out["during"]["disabled"] is True
    assert out["during"]["status"].startswith("Working")
    assert out["during"]["cleared"] == ""
    assert out["count"] == 1
    post = out["post"]
    assert post["url"] == "http://srv/agent/chat"
    assert post["body"]["messages"] == [{"role": "user", "content": "show me the ISS"}]
    assert "session_id" not in post["body"]
    assert post["body"]["scene"]["camera"] == {
        "latitude_deg": 30.0, "longitude_deg": -60.0, "altitude_km": 9000.0}
    after = out["after"]
    assert after["status"] == ""
    assert after["users"] == ["show me the ISS"]
    assert after["answers"] == [fx["success"]["body"]["text"]]
    assert "list_satellites(filter=ISS, limit=5)" in after["tools"]
    assert len(after["tools"]) == 6
    assert after["meta"][0].startswith("ollama / llama3.1:8b, ")
    assert after["disclaimer"] == fx["success"]["body"]["disclaimer"]


def test_follow_up_sends_the_session_id_and_only_the_new_turn(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        ask('first'); await idle();
        ask('second'); await idle();
        return h.posts().map(function (p) { return p.body; });
        """,
        fx,
    )
    first, second = out
    assert "session_id" not in first
    assert second["session_id"] == fx["success"]["body"]["session_id"]
    assert second["messages"] == [{"role": "user", "content": "second"}]


def test_scene_carries_the_selected_satellite(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        t.ctx.satEngine.selectedId = 25544;
        ask('what is this one'); await idle();
        return h.posts()[0].body.scene;
        """,
        fx,
    )
    assert out["selected"] == [{"norad_id": ISS, "name": "ISS (ZARYA)"}]
    assert out["time_utc"].endswith("Z")


def test_client_actions_move_the_globe(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        ask('show me stuff'); await idle();
        return { selected: t.rec.selected, flights: t.rec.flights, intel: t.rec.intel,
                 chips: h.text(t.mount, 'agentpanel-action'),
                 notes: h.text(t.mount, 'agentpanel-note') };
        """,
        fx,
    )
    # ISS and Tianhe are on the globe; 99999 is not and had no coordinates.
    assert out["selected"] == [ISS, TIANHE]
    flights = out["flights"]
    assert [f["destination"] for f in flights] == [
        {"lon": -97.74, "lat": 30.27, "h": 1500000.0},      # Austin, coordinates form
        {"lon": 77.0, "lat": -3.0, "h": 1285000.0},         # Tianhe, satellite plus standoff
    ]
    assert all(f["duration"] > 0 and f["orientation"]["pitch"] < 0 for f in flights)
    assert len(out["intel"]) == 3 and all(line.startswith("Agent: ") for line in out["intel"])
    assert len(out["chips"]) == 4
    assert any("99999" in n for n in out["notes"])


def test_action_chip_replays_its_view(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        ask('x'); await idle();
        const before = t.rec.selected.length;
        h.all(t.mount, 'agentpanel-action')[0].dispatch('click', {});
        return { added: t.rec.selected.slice(before) };
        """,
        fx,
    )
    assert out["added"] == [ISS]


def test_unknown_action_types_are_ignored_without_failing(fx: dict[str, Any]) -> None:
    body = {**fx["success"]["body"], "actions": [{"type": "highlight", "ids": [1]}, None]}
    out = _dom(
        """
        ask('x'); await idle();
        return { selected: t.rec.selected, flights: t.rec.flights.length,
                 answers: h.text(t.mount, 'agentpanel-answer').length };
        """,
        fx,
        chatRoute={"status": 200, "body": body},
    )
    assert out == {"selected": [], "flights": 0, "answers": 1}


def test_a_failing_request_keeps_the_message_and_frees_the_input(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        ask('why is this broken');
        await idle();
        return { users: h.text(t.mount, 'agentpanel-user'),
                 errors: h.text(t.mount, 'agentpanel-error'),
                 enabled: !sendBtn().disabled,
                 retry: h.all(t.mount, 'agentpanel-retry').length,
                 healthReads: h.calls.filter(function (c) { return c.method === 'GET'; }).length };
        """,
        fx,
        chatRoute={"status": 502, "body": fx["providers_failed_502"]["body"]},
    )
    assert out["users"] == ["why is this broken"]
    assert len(out["errors"]) == 1
    assert "The language model (ollama) could not answer that." in out["errors"][0]
    assert out["enabled"] is True and out["retry"] == 1
    assert out["healthReads"] == 2   # once at start, once to refresh the status line


def test_not_configured_503_shows_the_setup_text(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        ask('hello'); await idle();
        return {
          errors: h.text(t.mount, 'agentpanel-error'),
          hints: h.text(t.mount, 'agentpanel-hint')
        };
        """,
        fx,
        chatRoute={"status": 503, "body": fx["no_provider_503"]["body"]},
    )
    assert "no language model set up" in out["errors"][0]
    assert any("SDA_OLLAMA_URL" in hint for hint in out["hints"])


def test_retry_resends_without_a_second_copy_of_the_message(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        let attempt = 0;
        h.routes['POST /agent/chat'] = () => (++attempt === 1
          ? { status: 502, body: h.input.fx.providers_failed_502.body }
          : { status: 200, body: h.input.fx.success.body });
        ask('try me'); await idle();
        h.one(t.mount, 'agentpanel-retry').dispatch('click', {});
        await h.waitFor(() => h.posts().length === 2 && !sendBtn().disabled);
        return { users: h.text(t.mount, 'agentpanel-user'),
                 answers: h.text(t.mount, 'agentpanel-answer').length,
                 bodies: h.posts().map(function (p) { return p.body.messages[0].content; }),
                 retryLeft: h.all(t.mount, 'agentpanel-retry').length };
        """,
        fx,
    )
    assert out["users"] == ["try me"]
    assert out["bodies"] == ["try me", "try me"]
    assert out["answers"] == 1 and out["retryLeft"] == 0


def test_only_one_request_is_in_flight_at_a_time(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        let release;
        h.routes['POST /agent/chat'] = () => new Promise(function (resolve) {
          release = function () { resolve({ status: 200, body: h.input.fx.success.body }); };
        });
        ask('one');
        ask('two');                      // Enter again while busy
        // and the button, which a real browser would not even fire
        sendBtn().dispatch('click', {});
        await h.wait(30);
        const mid = { posts: h.posts().length, disabled: sendBtn().disabled };
        release();
        await idle();
        return { mid, posts: h.posts().length, users: h.text(t.mount, 'agentpanel-user') };
        """,
        fx,
    )
    assert out["mid"] == {"posts": 1, "disabled": True}
    assert out["posts"] == 1 and out["users"] == ["one"]


def test_the_client_gives_up_after_its_own_timeout_and_recovers(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        h.routes['POST /agent/chat'] = (call, opts) => new Promise(function (resolve, reject) {
          opts.signal.addEventListener('abort', function () {
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          });
        });
        ask('slow one');
        await idle();
        return {
          errors: h.text(t.mount, 'agentpanel-error'),
          users: h.text(t.mount, 'agentpanel-user')
        };
        """,
        fx,
        ctx={"timeoutMs": 40},
    )
    assert out["users"] == ["slow one"]
    assert out["errors"][0].startswith("No answer after 0 s.")


def test_the_default_client_timeout_is_well_above_the_servers(fx: dict[str, Any]) -> None:
    # SDA_LLM_TIMEOUT_S defaults to 60 s per provider call; a turn is up to six calls.
    assert run_js("return P.REQUEST_TIMEOUT_MS;") >= 6 * 60 * 1000


def test_unreadable_replies_are_reported_not_thrown(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        ask('x'); await idle();
        return { errors: h.text(t.mount, 'agentpanel-error'), enabled: !sendBtn().disabled };
        """,
        fx,
        chatRoute={"status": 200, "raw": "<html>proxy page</html>"},
    )
    assert "could not read" in out["errors"][0] and out["enabled"] is True


def test_model_text_is_shown_as_text_not_markup(fx: dict[str, Any]) -> None:
    evil = "<img src=x onerror=alert(1)><script>boom()</script>"
    body = {**fx["success"]["body"], "text": evil}
    out = _dom(
        """
        ask('x'); await idle();
        const tags = [];
        h.walk(t.mount, function (e) { tags.push(e.tagName); });
        return { answers: h.text(t.mount, 'agentpanel-answer'), tags: tags };
        """,
        fx,
        chatRoute={"status": 200, "body": body},
    )
    assert out["answers"] == [evil]
    assert "IMG" not in out["tags"] and "SCRIPT" not in out["tags"]


def test_keys_typed_in_the_panel_never_reach_the_page_handler(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        h.key(box(), 'i');
        h.key(h.one(t.mount, 'agentpanel-starter'), ' ');
        const outside = new h.El('div'); t.root.appendChild(outside);
        h.key(outside, 'i');
        return { rootKeys: t.rootKeys };
        """,
        fx,
    )
    # Only the key pressed outside the panel got through (three events: down, press, up).
    assert out["rootKeys"] == ["keydown:i", "keypress:i", "keyup:i"]


def test_enter_sends_but_shift_enter_and_composition_do_not(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        h.type(box(), 'a line');
        h.key(box(), 'Enter', { shiftKey: true });
        h.key(box(), 'Enter', { isComposing: true });
        const none = h.posts().length;
        const ev = h.key(box(), 'Enter')[0];
        await idle();
        return { none, sent: h.posts().length, prevented: ev.defaultPrevented };
        """,
        fx,
    )
    assert out == {"none": 0, "sent": 1, "prevented": True}


def test_blank_input_sends_nothing(fx: dict[str, Any]) -> None:
    out = _dom("ask('   '); await h.wait(20); return h.posts().length;", fx)
    assert out == 0


def test_starter_buttons_send_their_prompt_in_one_click(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        const buttons = h.all(t.mount, 'agentpanel-starter');
        buttons[1].dispatch('click', {});
        await idle();
        return { sent: h.posts().map(p => p.body.messages[0].content),
                 prompts: P.STARTERS.map(s => s.prompt),
                 labels: buttons.map(b => b.textContent) };
        """,
        fx,
    )
    assert out["sent"] == [out["prompts"][1]]
    assert out["labels"] == [s["label"] for s in run_js("return P.STARTERS;")]


def test_focus_puts_the_cursor_in_the_input_and_opens_a_collapsed_panel(
    fx: dict[str, Any],
) -> None:
    out = _dom(
        """
        const panel = t.mount.children[0];
        panel.classList.add('collapsed');
        P.focus();
        return { focused: document.activeElement === box(),
                 open: !panel.classList.contains('collapsed') };
        """,
        fx,
    )
    assert out == {"focused": True, "open": True}


def test_new_chat_clears_the_transcript_and_the_session(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        ask('first'); await idle();
        h.one(t.mount, 'agentpanel-clear').dispatch('click', {});
        const emptied = log().children.length;
        ask('again'); await idle();
        return { emptied, second: h.posts()[1].body };
        """,
        fx,
    )
    assert out["emptied"] == 0
    assert "session_id" not in out["second"]


def test_the_globe_keeps_working_when_its_own_callbacks_throw(fx: dict[str, Any]) -> None:
    out = _dom(
        """
        t.ctx.selectSatellite = function () { throw new Error('select broke'); };
        t.ctx.addIntelItem = function () { throw new Error('feed broke'); };
        t.ctx.viewer.camera.flyTo = function () { throw new Error('camera broke'); };
        t.ctx.viewer.camera.positionCartographic = null;
        ask('x'); await idle();
        return { answers: h.text(t.mount, 'agentpanel-answer').length,
                 enabled: !sendBtn().disabled, body: h.posts()[0].body };
        """,
        fx,
    )
    assert out["answers"] == 1 and out["enabled"] is True
    assert "camera" not in out["body"]["scene"]


def test_init_survives_missing_context_pieces(fx: dict[str, Any]) -> None:
    out = run_dom(
        """
        P.init(null);
        P.init({});
        const t = h.makeCtx({
          viewer: null, satEngine: null, selectSatellite: null, addIntelItem: null
        });
        h.routes['GET /agent/health'] = () => ({ status: 200, body: h.input.health });
        P.init(t.ctx);
        await h.waitFor(() => !/^Checking/.test(h.text(t.mount, 'agentpanel-health')[0]));
        P.focus();
        return true;
        """,
        {"health": fx["health_ready"]["body"]},
    )
    assert out is True


def test_microphone_button_is_hidden_without_speech_recognition() -> None:
    out = run_dom(
        """
        const t = h.makeCtx({});
        P.init(t.ctx);
        return h.all(t.mount, 'agentpanel-mic').length;
        """,
        {},
    )
    assert out == 0


def test_microphone_button_dictates_into_the_input_when_the_browser_can() -> None:
    out = run_dom(
        """
        const made = [];
        function Recognition() { made.push(this); this.started = 0; }
        Recognition.prototype.start = function () { this.started++; };
        Recognition.prototype.stop = function () {};
        window.SpeechRecognition = Recognition;
        const t = h.makeCtx({});
        P.init(t.ctx);
        const mic = h.one(t.mount, 'agentpanel-mic');
        const box = h.one(t.mount, 'agentpanel-input');
        mic.dispatch('click', {});
        const rec = made[0];
        rec.onresult({ results: [[{ transcript: 'show the space station' }]] });
        rec.onend();
        const dictated = box.value;
        h.type(box, 'first part');
        rec.onresult({ results: [[{ transcript: 'second part' }]] });
        rec.onerror({ error: 'not-allowed' });
        return { present: !!mic, started: rec.started, dictated, value: box.value,
                 status: h.text(t.mount, 'agentpanel-status')[0] };
        """,
        {},
    )
    assert out["present"] is True and out["started"] == 1
    assert out["dictated"] == "show the space station"
    assert out["value"] == "first part second part"
    assert "Microphone" in out["status"]
