"""Agent routes: chat over the orbital engine, tool catalogue, provider health.

Three endpoints under ``/agent``:

* ``POST /agent/chat``   one agent turn (typed tools, client actions, trace)
* ``GET  /agent/tools``  the tool catalogue the model is given
* ``GET  /agent/health`` provider chain status (which one would answer, and why)

Sessions are kept in memory only, capped in both count and length. There is no
persistence and no cross-process sharing: restart the service and every session
is gone, which is the right behaviour for a stateless analysis front end.

When no LLM provider is configured, ``POST /agent/chat`` answers 503 with the
setup instructions from :mod:`sda.llm` rather than a generic error, so a fresh
checkout tells the operator exactly which environment variable to set.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sda.agent import DISCLAIMER, MAX_ITERATIONS, run_agent, tool_catalogue
from sda.llm import (
    SETUP_INSTRUCTIONS,
    AllProvidersFailedError,
    NoProviderConfiguredError,
    get_client,
)

router = APIRouter(prefix="/agent", tags=["Agent"])

MAX_SESSIONS = 64
MAX_SESSION_MESSAGES = 40

# session id -> canonical message list (see sda.llm for the message shape)
_sessions: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()


def _get_session(session_id: str) -> list[dict[str, Any]]:
    history = _sessions.get(session_id)
    if history is None:
        history = []
        _sessions[session_id] = history
    _sessions.move_to_end(session_id)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)
    return history


def _store_session(session_id: str, messages: list[dict[str, Any]]) -> None:
    trimmed = messages[-MAX_SESSION_MESSAGES:]
    _sessions[session_id] = trimmed
    _sessions.move_to_end(session_id)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def clear_sessions() -> int:
    """Drop every in-memory session. Returns how many were removed."""
    count = len(_sessions)
    _sessions.clear()
    return count


# --- Payloads -------------------------------------------------------------

class AgentMessage(BaseModel):
    """One conversation turn supplied by the client."""

    role: str = Field(default="user", description="user or assistant")
    content: str = Field(description="Plain text content of the turn")


class AgentChatRequest(BaseModel):
    """A chat turn plus the optional scene the operator is looking at."""

    session_id: str | None = Field(
        default=None,
        description="Reuse a previous session id to continue that conversation.",
    )
    messages: list[AgentMessage] = Field(
        default_factory=list,
        description="New turns to append. Usually a single user message.",
    )
    scene: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Client scene description: camera {latitude_deg, longitude_deg, "
            "altitude_km}, visible_layers, selected, time_utc."
        ),
    )
    max_iterations: int = Field(
        default=MAX_ITERATIONS,
        ge=1,
        le=12,
        description="Cap on provider round trips for this turn.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Any close approaches under 5 km today?",
                        }
                    ],
                    "scene": {
                        "camera": {
                            "latitude_deg": 37.4,
                            "longitude_deg": -122.1,
                            "altitude_km": 12000.0,
                        },
                        "visible_layers": ["stations", "debris"],
                        "selected": [{"norad_id": 25544, "name": "ISS (ZARYA)"}],
                    },
                }
            ]
        }
    }


class AgentChatResponse(BaseModel):
    """Final text, client-side actions, and the executed tool trace."""

    session_id: str
    text: str
    actions: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    iterations: int = 0
    provider: str = ""
    model: str = ""
    stop_reason: str = "final"
    disclaimer: str = DISCLAIMER


# --- Endpoints ------------------------------------------------------------

@router.post(
    "/chat",
    response_model=AgentChatResponse,
    summary="Ask the orbital engine a question in natural language",
    response_description=(
        "Final answer, client-side view actions, and every tool the model ran"
    ),
)
def agent_chat(request: AgentChatRequest) -> AgentChatResponse:
    """Run one agent turn against the live TLE catalog.

    The model may call any tool in ``GET /agent/tools``. Server-side tools run
    in process against the same catalog the REST API serves; client-side tools
    (fly_to) are returned in ``actions`` for the caller to carry out.
    """
    client = get_client()
    if not client.has_provider():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_llm_provider_configured",
                "message": "The agent needs an LLM provider.",
                "setup": SETUP_INSTRUCTIONS,
                "providers": [s.to_dict() for s in client.status()],
            },
        )

    if not request.messages:
        raise HTTPException(status_code=422, detail="messages must not be empty")

    session_id = request.session_id or uuid.uuid4().hex
    history = list(_get_session(session_id))
    history.extend(
        {"role": m.role if m.role in ("user", "assistant") else "user", "content": m.content}
        for m in request.messages
    )

    try:
        result = run_agent(
            history,
            scene=request.scene,
            client=client,
            max_iterations=request.max_iterations,
        )
    except NoProviderConfiguredError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_llm_provider_configured",
                "message": str(exc),
                "setup": SETUP_INSTRUCTIONS,
            },
        ) from exc
    except AllProvidersFailedError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "llm_providers_failed",
                "message": str(exc),
                "providers": [s.to_dict() for s in client.status()],
            },
        ) from exc

    conversation = [*result.messages, {"role": "assistant", "content": result.text}]
    _store_session(session_id, conversation)

    payload = result.to_dict()
    return AgentChatResponse(
        session_id=session_id,
        text=payload["text"],
        actions=payload["actions"],
        trace=payload["trace"],
        iterations=payload["iterations"],
        provider=payload["provider"],
        model=payload["model"],
        stop_reason=payload["stop_reason"],
    )


@router.get(
    "/tools",
    summary="Typed tool catalogue offered to the model",
    response_description="Tool names, descriptions and JSON schemas",
)
def agent_tools() -> dict[str, Any]:
    """The exact tool set the agent loop and the MCP server expose."""
    catalogue = tool_catalogue()
    return {
        "count": len(catalogue),
        "tools": catalogue,
        "disclaimer": DISCLAIMER,
    }


@router.get(
    "/health",
    summary="LLM provider chain status",
    response_description="Per-provider configuration, failure count and cooldown",
)
def agent_health() -> dict[str, Any]:
    """Which providers are configured, which is next, and what last failed."""
    client = get_client()
    statuses = [s.to_dict() for s in client.status()]
    available = [s["name"] for s in statuses if s["available"]]
    return {
        "configured": client.has_provider(),
        "active_provider": available[0] if available else None,
        "providers": statuses,
        "sessions": len(_sessions),
        "max_sessions": MAX_SESSIONS,
        "setup": None if client.has_provider() else SETUP_INSTRUCTIONS,
    }
