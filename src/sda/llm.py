"""Multi-provider LLM client for the SDA agent front door.

No vendor SDK is required: every provider is spoken to over plain HTTP with
httpx, which is already a hard dependency of this service. Three wire formats
are supported and normalized to one shape:

* Anthropic Messages API (``ANTHROPIC_API_KEY``)
* OpenAI-compatible chat completions (``OPENAI_API_KEY`` plus
  ``SDA_OPENAI_BASE_URL``; the same code path covers OpenRouter and Groq)
* Ollama ``/api/chat`` for a local model (``SDA_OLLAMA_URL``)

Providers are tried in the order given by ``SDA_LLM_PROVIDERS`` (default
``anthropic,openai,ollama``). A provider that raises is put into a cooldown so
the next request skips it instead of paying the timeout again; the cooldown
length grows with consecutive failures and resets on the first success.

Environment
-----------
======================  =====================================================
ANTHROPIC_API_KEY       enables the Anthropic provider
OPENAI_API_KEY          enables the OpenAI-compatible provider
SDA_OPENAI_BASE_URL     OpenAI-compatible base URL (default OpenAI itself)
SDA_OLLAMA_URL          enables the Ollama provider (e.g. http://127.0.0.1:11434)
SDA_LLM_MODEL           overrides the model for every provider
SDA_ANTHROPIC_MODEL     per-provider model override
SDA_OPENAI_MODEL        per-provider model override
SDA_OLLAMA_MODEL        per-provider model override
SDA_LLM_PROVIDERS       comma-separated provider order
SDA_LLM_TIMEOUT_S       per-request timeout in seconds (default 60)
SDA_LLM_COOLDOWN_S      base cooldown after a failure in seconds (default 30)
SDA_LLM_MAX_TOKENS      response token cap (default 1024)
======================  =====================================================

Ollama counts as configured only when ``SDA_OLLAMA_URL`` is set explicitly. That
keeps the "no provider configured" error meaningful on a machine with nothing
set up, rather than every request hanging on a localhost port that is closed.

The design of the fallback chain follows worldmonitor (AGPL-3.0, consulted as a
design reference only); the wire formats come from each vendor's public HTTP
documentation.

Canonical message shape used everywhere in this package::

    {"role": "system" | "user" | "assistant" | "tool",
     "content": str,
     "tool_calls": [ToolCall, ...],   # assistant turns only
     "tool_call_id": str,             # tool results only
     "name": str}                     # tool results only

All network access goes through :func:`_post_json`, which tests monkeypatch.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OLLAMA_MODEL = "llama3.1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_PROVIDER_ORDER = ("anthropic", "openai", "ollama")

ANTHROPIC_VERSION = "2023-06-01"

SETUP_INSTRUCTIONS = (
    "No LLM provider is configured. Set one of the following and restart:\n"
    "  ANTHROPIC_API_KEY=sk-ant-...            (Anthropic Messages API)\n"
    "  OPENAI_API_KEY=sk-...                   (OpenAI, OpenRouter or Groq;\n"
    "                                           set SDA_OPENAI_BASE_URL for the\n"
    "                                           latter two)\n"
    "  SDA_OLLAMA_URL=http://127.0.0.1:11434   (local Ollama, no key needed)\n"
    "Optional: SDA_LLM_MODEL to pick the model, SDA_LLM_PROVIDERS to set the\n"
    "fallback order (default anthropic,openai,ollama)."
)


# --- Errors ---------------------------------------------------------------

class LLMError(RuntimeError):
    """Base class for every failure raised by this module."""


class NoProviderConfiguredError(LLMError):
    """Raised when no provider has credentials or an endpoint configured."""

    def __init__(self, message: str = SETUP_INSTRUCTIONS) -> None:
        super().__init__(message)
        self.setup_instructions = SETUP_INSTRUCTIONS


class ProviderRequestError(LLMError):
    """A single provider failed (transport error, HTTP error, bad payload)."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.reason = message


class AllProvidersFailedError(LLMError):
    """Every configured provider in the chain failed."""

    def __init__(self, errors: dict[str, str]) -> None:
        detail = "; ".join(f"{name}: {msg}" for name, msg in errors.items())
        super().__init__(f"all LLM providers failed ({detail})")
        self.errors = dict(errors)


# --- Normalized response types -------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": dict(self.arguments)}


@dataclass(frozen=True)
class ChatResponse:
    """Provider-independent result of one chat turn."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    provider: str = ""
    model: str = ""
    stop_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "provider": self.provider,
            "model": self.model,
            "stop_reason": self.stop_reason,
        }


@dataclass
class ProviderStatus:
    """Health snapshot for one provider in the chain."""

    name: str
    configured: bool
    model: str
    failures: int = 0
    last_error: str | None = None
    cooldown_remaining_s: float = 0.0

    @property
    def available(self) -> bool:
        return self.configured and self.cooldown_remaining_s <= 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "model": self.model,
            "failures": self.failures,
            "last_error": self.last_error,
            "cooldown_remaining_s": round(self.cooldown_remaining_s, 3),
            "available": self.available,
        }


@dataclass
class _Health:
    """Mutable failure bookkeeping for one provider."""

    failures: int = 0
    last_error: str | None = None
    cooldown_until: float = 0.0


# --- Transport ------------------------------------------------------------

def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """POST JSON and return the decoded object. The single network seam.

    Tests monkeypatch this function; nothing else in the package opens a socket
    for LLM traffic.
    """
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise ProviderRequestError("transport", f"request failed: {exc}") from exc

    if response.status_code >= 400:
        body = response.text[:500]
        raise ProviderRequestError("http", f"status {response.status_code}: {body}")

    try:
        decoded = response.json()
    except ValueError as exc:
        raise ProviderRequestError("http", f"response was not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ProviderRequestError("http", "response JSON was not an object")
    return decoded


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


# --- Message helpers ------------------------------------------------------

def _merge_consecutive(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge neighbouring messages that share a role (Anthropic alternates)."""
    merged: list[dict[str, Any]] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            prev_content = merged[-1]["content"]
            next_content = msg["content"]
            if isinstance(prev_content, list) and isinstance(next_content, list):
                merged[-1] = {"role": msg["role"], "content": [*prev_content, *next_content]}
                continue
        merged.append(msg)
    return merged


def _split_system(
    messages: Sequence[dict[str, Any]],
    system: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Pull system turns out of the conversation into one system string."""
    prefix = [system.strip()] if system else []
    rest: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            text = str(msg.get("content") or "").strip()
            if text:
                prefix.append(text)
        else:
            rest.append(dict(msg))
    return "\n\n".join(p for p in prefix if p), rest


def _as_tool_calls(raw: Iterable[Any]) -> tuple[ToolCall, ...]:
    """Coerce stored tool calls (ToolCall or dict) back into ToolCall objects."""
    out: list[ToolCall] = []
    for item in raw:
        if isinstance(item, ToolCall):
            out.append(item)
        elif isinstance(item, dict):
            out.append(
                ToolCall(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    arguments=dict(item.get("arguments") or {}),
                )
            )
    return tuple(out)


# --- Response normalizers (pure, unit-tested directly) --------------------

def normalize_anthropic(data: dict[str, Any]) -> ChatResponse:
    """Normalize an Anthropic Messages API response."""
    texts: list[str] = []
    calls: list[ToolCall] = []
    blocks = data.get("content")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                arguments = block.get("input")
                calls.append(
                    ToolCall(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        arguments=dict(arguments) if isinstance(arguments, dict) else {},
                    )
                )
    return ChatResponse(
        text="".join(texts).strip(),
        tool_calls=tuple(calls),
        provider="anthropic",
        model=str(data.get("model", "")),
        stop_reason=str(data.get("stop_reason") or ""),
    )


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """OpenAI sends tool arguments as a JSON string; Ollama sends an object."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def normalize_openai(data: dict[str, Any]) -> ChatResponse:
    """Normalize an OpenAI-compatible chat completion response."""
    choices = data.get("choices")
    choice: dict[str, Any] = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}

    calls: list[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for index, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                continue
            fn = item.get("function")
            fn = fn if isinstance(fn, dict) else {}
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(fn.get("name", "")),
                    arguments=_decode_arguments(fn.get("arguments")),
                )
            )

    return ChatResponse(
        text=str(message.get("content") or "").strip(),
        tool_calls=tuple(calls),
        provider="openai",
        model=str(data.get("model", "")),
        stop_reason=str(choice.get("finish_reason") or ""),
    )


def normalize_ollama(data: dict[str, Any]) -> ChatResponse:
    """Normalize an Ollama /api/chat response."""
    message = data.get("message")
    message = message if isinstance(message, dict) else {}

    calls: list[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for index, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                continue
            fn = item.get("function")
            fn = fn if isinstance(fn, dict) else {}
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(fn.get("name", "")),
                    arguments=_decode_arguments(fn.get("arguments")),
                )
            )

    reason = "tool_calls" if calls else str(data.get("done_reason") or "")
    return ChatResponse(
        text=str(message.get("content") or "").strip(),
        tool_calls=tuple(calls),
        provider="ollama",
        model=str(data.get("model", "")),
        stop_reason=reason,
    )


# --- Providers ------------------------------------------------------------

class Provider(ABC):
    """One LLM endpoint that can answer a chat turn."""

    name: str = "provider"

    @abstractmethod
    def is_configured(self) -> bool:
        """True when the environment carries everything this provider needs."""

    @abstractmethod
    def model(self) -> str:
        """Model identifier this provider would use right now."""

    @abstractmethod
    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> ChatResponse:
        """Run one turn and return the normalized response."""

    # Shared helpers

    @staticmethod
    def _timeout() -> float:
        return _env_float("SDA_LLM_TIMEOUT_S", 60.0)

    @staticmethod
    def _override_model(default: str, specific_env: str) -> str:
        return _env("SDA_LLM_MODEL") or _env(specific_env) or default


class AnthropicProvider(Provider):
    """Anthropic Messages API (https://api.anthropic.com/v1/messages)."""

    name = "anthropic"

    def is_configured(self) -> bool:
        return bool(_env("ANTHROPIC_API_KEY"))

    def model(self) -> str:
        return self._override_model(DEFAULT_ANTHROPIC_MODEL, "SDA_ANTHROPIC_MODEL")

    def _base_url(self) -> str:
        return _env("SDA_ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    @staticmethod
    def _encode_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        encoded: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = str(msg.get("content") or "")
            if role == "tool":
                encoded.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": str(msg.get("tool_call_id") or ""),
                                "content": content,
                            }
                        ],
                    }
                )
                continue
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in _as_tool_calls(msg.get("tool_calls") or []):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": "(no content)"})
            encoded.append({"role": "assistant" if role == "assistant" else "user",
                            "content": blocks})
        return _merge_consecutive(encoded)

    @staticmethod
    def encode_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical tool specs into Anthropic tool definitions."""
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> ChatResponse:
        system_text, rest = _split_system(messages, system)
        payload: dict[str, Any] = {
            "model": self.model(),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": self._encode_messages(rest),
        }
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = self.encode_tools(tools)

        data = _post_json(
            f"{self._base_url()}/v1/messages",
            {
                "x-api-key": _env("ANTHROPIC_API_KEY"),
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            payload,
            self._timeout(),
        )
        response = normalize_anthropic(data)
        return ChatResponse(
            text=response.text,
            tool_calls=response.tool_calls,
            provider=self.name,
            model=response.model or payload["model"],
            stop_reason=response.stop_reason,
        )


class OpenAICompatibleProvider(Provider):
    """OpenAI chat completions, and anything that speaks the same wire format.

    OpenRouter and Groq are reached by pointing ``SDA_OPENAI_BASE_URL`` at them.
    """

    name = "openai"

    def is_configured(self) -> bool:
        return bool(_env("OPENAI_API_KEY"))

    def model(self) -> str:
        return self._override_model(DEFAULT_OPENAI_MODEL, "SDA_OPENAI_MODEL")

    def _base_url(self) -> str:
        return _env("SDA_OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL).rstrip("/")

    @staticmethod
    def encode_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical messages into OpenAI chat-completion messages."""
        encoded: list[dict[str, Any]] = []
        for msg in messages:
            role = str(msg.get("role") or "user")
            content = str(msg.get("content") or "")
            if role == "tool":
                encoded.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(msg.get("tool_call_id") or ""),
                        "content": content,
                    }
                )
                continue
            item: dict[str, Any] = {"role": role, "content": content}
            calls = _as_tool_calls(msg.get("tool_calls") or [])
            if calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, sort_keys=True),
                        },
                    }
                    for call in calls
                ]
            encoded.append(item)
        return encoded

    @staticmethod
    def encode_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical tool specs into OpenAI function definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters")
                    or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> ChatResponse:
        outgoing: list[dict[str, Any]] = []
        if system:
            outgoing.append({"role": "system", "content": system})
        outgoing.extend(self.encode_messages(messages))

        payload: dict[str, Any] = {
            "model": self.model(),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": outgoing,
        }
        if tools:
            payload["tools"] = self.encode_tools(tools)

        headers = {
            "Authorization": f"Bearer {_env('OPENAI_API_KEY')}",
            "content-type": "application/json",
        }
        referer = _env("SDA_OPENAI_REFERER")
        if referer:
            # OpenRouter attributes traffic with these two optional headers.
            headers["HTTP-Referer"] = referer
            headers["X-Title"] = _env("SDA_OPENAI_TITLE", "space-collision-predictor")

        data = _post_json(
            f"{self._base_url()}/chat/completions", headers, payload, self._timeout()
        )
        response = normalize_openai(data)
        return ChatResponse(
            text=response.text,
            tool_calls=response.tool_calls,
            provider=self.name,
            model=response.model or payload["model"],
            stop_reason=response.stop_reason,
        )


class OllamaProvider(Provider):
    """Local Ollama server, /api/chat with tool support."""

    name = "ollama"

    def is_configured(self) -> bool:
        return bool(_env("SDA_OLLAMA_URL"))

    def model(self) -> str:
        return self._override_model(DEFAULT_OLLAMA_MODEL, "SDA_OLLAMA_MODEL")

    def _base_url(self) -> str:
        return _env("SDA_OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")

    @staticmethod
    def encode_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI-shaped messages, except tool arguments stay a JSON object.

        /api/chat rejects a replayed tool call whose arguments are a string
        with HTTP 400, which ends every tool loop on its second turn.
        """
        encoded = OpenAICompatibleProvider.encode_messages(messages)
        for item in encoded:
            for call in item.get("tool_calls") or []:
                function = call["function"]
                function["arguments"] = _decode_arguments(function["arguments"])
        return encoded

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> ChatResponse:
        outgoing: list[dict[str, Any]] = []
        if system:
            outgoing.append({"role": "system", "content": system})
        outgoing.extend(self.encode_messages(messages))

        payload: dict[str, Any] = {
            "model": self.model(),
            "messages": outgoing,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = OpenAICompatibleProvider.encode_tools(tools)

        data = _post_json(
            f"{self._base_url()}/api/chat",
            {"content-type": "application/json"},
            payload,
            self._timeout(),
        )
        response = normalize_ollama(data)
        return ChatResponse(
            text=response.text,
            tool_calls=response.tool_calls,
            provider=self.name,
            model=response.model or payload["model"],
            stop_reason=response.stop_reason,
        )


_PROVIDER_FACTORIES: dict[str, Callable[[], Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatibleProvider,
    "ollama": OllamaProvider,
}


def provider_order() -> list[str]:
    """Provider names in the order the chain will try them."""
    raw = _env("SDA_LLM_PROVIDERS")
    if not raw:
        return list(DEFAULT_PROVIDER_ORDER)
    names = [part.strip().lower() for part in raw.split(",")]
    ordered = [name for name in names if name in _PROVIDER_FACTORIES]
    return ordered or list(DEFAULT_PROVIDER_ORDER)


def build_providers() -> list[Provider]:
    """Instantiate the provider chain in configured order."""
    return [_PROVIDER_FACTORIES[name]() for name in provider_order()]


# --- The chain ------------------------------------------------------------

@dataclass
class LLMClient:
    """Provider chain with per-provider failure counts and cooldowns."""

    providers: list[Provider] = field(default_factory=build_providers)
    cooldown_s: float = field(default_factory=lambda: _env_float("SDA_LLM_COOLDOWN_S", 30.0))
    clock: Callable[[], float] = time.monotonic
    _health: dict[str, _Health] = field(default_factory=dict, init=False, repr=False)

    def _health_of(self, name: str) -> _Health:
        return self._health.setdefault(name, _Health())

    def configured(self) -> list[Provider]:
        """Providers whose environment is set up, in chain order."""
        return [p for p in self.providers if p.is_configured()]

    def has_provider(self) -> bool:
        return bool(self.configured())

    def status(self) -> list[ProviderStatus]:
        """Health snapshot for every provider in the chain."""
        now = self.clock()
        out: list[ProviderStatus] = []
        for provider in self.providers:
            health = self._health_of(provider.name)
            out.append(
                ProviderStatus(
                    name=provider.name,
                    configured=provider.is_configured(),
                    model=provider.model(),
                    failures=health.failures,
                    last_error=health.last_error,
                    cooldown_remaining_s=max(0.0, health.cooldown_until - now),
                )
            )
        return out

    def _record_failure(self, name: str, message: str) -> None:
        health = self._health_of(name)
        health.failures += 1
        health.last_error = message[:300]
        backoff = self.cooldown_s * min(health.failures, 5)
        health.cooldown_until = self.clock() + backoff

    def _record_success(self, name: str) -> None:
        health = self._health_of(name)
        health.failures = 0
        health.last_error = None
        health.cooldown_until = 0.0

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> ChatResponse:
        """Try each configured provider in order until one answers.

        Raises
        ------
        NoProviderConfiguredError
            When nothing in the chain has credentials or an endpoint.
        AllProvidersFailedError
            When every configured provider raised.
        """
        candidates = self.configured()
        if not candidates:
            raise NoProviderConfiguredError()

        now = self.clock()
        warm = [p for p in candidates if self._health_of(p.name).cooldown_until <= now]
        # If everything is cooling down, still try rather than fail outright:
        # a cooldown is a hint about ordering, not a hard circuit breaker.
        attempts = warm or candidates

        cap = max_tokens if max_tokens is not None else _env_int("SDA_LLM_MAX_TOKENS", 1024)
        errors: dict[str, str] = {}
        for provider in attempts:
            try:
                response = provider.chat(
                    messages=messages,
                    tools=tools,
                    system=system,
                    max_tokens=cap,
                    temperature=temperature,
                )
            except Exception as exc:  # any provider fault falls through to the next
                message = str(exc) or exc.__class__.__name__
                errors[provider.name] = message
                self._record_failure(provider.name, message)
                continue
            self._record_success(provider.name)
            return response

        raise AllProvidersFailedError(errors)


_client: LLMClient | None = None


def get_client() -> LLMClient:
    """Process-wide client, rebuilt lazily so env changes are picked up once."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_client() -> None:
    """Drop the cached client (used by tests and after env changes)."""
    global _client
    _client = None
