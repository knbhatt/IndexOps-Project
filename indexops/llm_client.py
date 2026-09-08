"""The ONLY place in the codebase that talks to an LLM.

Design:
- One provider for now (Gemini via its OpenAI-compatible endpoint), driven entirely
  by environment variables: LLM_PROVIDER, LLM_MODEL, GEMINI_API_KEY.
- Adding a provider later = one new entry in PROVIDERS (+ an API-key env var).
  The agent loop never imports a vendor SDK and never changes.
- The agent loop only ever sees provider-neutral types: LLMResponse / LLMToolCall,
  and message dicts in the OpenAI chat format (the de-facto interchange format).

Prompt caching: Gemini applies implicit prefix caching automatically when the
leading part of the request (system prompt + tool definitions) is byte-identical
across calls. The agent loop therefore keeps those two blocks first and static.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIStatusError

load_dotenv()

# ---------------------------------------------------------------------------
# Provider registry. Add a second provider here later; nothing else changes.
# ---------------------------------------------------------------------------
PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        # gemini-2.5-flash is retired for new users; 3.x *-flash free tier is only 20 req/day,
        # while *-flash-lite is 500 req/day -> the only workable free choice for this project.
        "default_model": "gemini-3.5-flash-lite",
    },
}

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
if LLM_PROVIDER not in PROVIDERS:
    raise RuntimeError(f"Unsupported LLM_PROVIDER '{LLM_PROVIDER}'. Known: {list(PROVIDERS)}")

_cfg = PROVIDERS[LLM_PROVIDER]
LLM_MODEL = os.getenv("LLM_MODEL", _cfg["default_model"])
LLM_BASE_URL = os.getenv("LLM_BASE_URL", _cfg["base_url"])
LLM_API_KEY = os.getenv(_cfg["api_key_env"], "")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "4"))

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not LLM_API_KEY:
            raise RuntimeError(f"{_cfg['api_key_env']} is not set (put it in .env)")
        _client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, max_retries=0)
    return _client


# ---------------------------------------------------------------------------
# Provider-neutral response types used by the agent loop.
# ---------------------------------------------------------------------------
@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw_message: dict[str, Any] = field(default_factory=dict)  # to append back into history

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def describe() -> dict[str, str]:
    """What the client is configured to use (for /health and logs). Never exposes the key."""
    return {"provider": LLM_PROVIDER, "model": LLM_MODEL, "base_url": LLM_BASE_URL,
            "api_key_set": bool(LLM_API_KEY)}


def mcp_tools_to_llm_tools(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert MCP tool descriptors ({name, description, input_schema}) to LLM function tools."""
    out = []
    for t in mcp_tools:
        schema = dict(t.get("input_schema") or {"type": "object", "properties": {}})
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        out.append({
            "type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""), "parameters": schema},
        })
    return out


def tool_result_message(tool_call: LLMToolCall, result: Any) -> dict[str, Any]:
    """Build the message that feeds a tool's output back to the model."""
    content = result if isinstance(result, str) else json.dumps(result, default=str)
    return {"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.name, "content": content}


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] = "auto",
    temperature: float | None = None,
) -> LLMResponse:
    """Single entry point for every LLM call (tool selection, reasoning, final report).

    `messages` uses the OpenAI chat format: the system prompt must be messages[0].
    Retries with backoff on rate limits (free tiers are RPM-limited).
    """
    client = _get_client()
    kwargs: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE if temperature is None else temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    delay = 2.0
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            completion = client.chat.completions.create(**kwargs)
            break
        except RateLimitError:
            if attempt == LLM_MAX_RETRIES:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except APIStatusError as e:
            if e.status_code >= 500 and attempt < LLM_MAX_RETRIES:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise

    choice = completion.choices[0]
    msg = choice.message
    tool_calls = []
    for tc in msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"_raw": tc.function.arguments}
        tool_calls.append(LLMToolCall(id=tc.id, name=tc.function.name, arguments=args))

    usage = {}
    if completion.usage:
        usage = {"prompt_tokens": completion.usage.prompt_tokens,
                 "completion_tokens": completion.usage.completion_tokens,
                 "total_tokens": completion.usage.total_tokens}
        cached = getattr(getattr(completion.usage, "prompt_tokens_details", None), "cached_tokens", None)
        if cached is not None:
            usage["cached_tokens"] = cached

    return LLMResponse(
        text=msg.content,
        tool_calls=tool_calls,
        finish_reason=choice.finish_reason,
        usage=usage,
        raw_message=msg.model_dump(exclude_none=True),
    )
