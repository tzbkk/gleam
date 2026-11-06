"""Model domain: model client, tools protocol and raw per-turn
request/response auditing for the investigation loop.

``import http.client`` below is load-bearing: tests and QA patch
``gleam.core.model.http.client`` to fake the transport with zero network,
so the client must resolve ``http.client`` through this package namespace
at call time.
"""

from __future__ import annotations

import http.client  # noqa: F401  (patch seam: gleam.core.model.http.client)

from .client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_RESPONSE_MAX_BYTES,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_S,
    ChatResult,
    ModelClient,
    ModelConfigError,
    http_json,
    validate_base,
)
from .parse import (
    SAFETY_KINDS,
    SAFETY_KIND_EMPTY_CONTENT,
    SAFETY_KIND_MALFORMED_TOOL_CALLS,
    SAFETY_KIND_TRUNCATED,
    AssistantTurn,
    ToolCall,
    parse_assistant,
    parse_completion,
    parse_tool_arguments,
    tool_result_message,
    unknown_tool_error,
)
from .protocol import TOOL_NAMES, TOOL_SPECS
from .strictjson import loads_strict, prompt_hash

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_RESPONSE_MAX_BYTES",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_S",
    "SAFETY_KINDS",
    "SAFETY_KIND_EMPTY_CONTENT",
    "SAFETY_KIND_MALFORMED_TOOL_CALLS",
    "SAFETY_KIND_TRUNCATED",
    "AssistantTurn",
    "ChatResult",
    "ModelClient",
    "ModelConfigError",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "ToolCall",
    "http_json",
    "loads_strict",
    "parse_assistant",
    "parse_completion",
    "parse_tool_arguments",
    "prompt_hash",
    "tool_result_message",
    "unknown_tool_error",
    "validate_base",
]
