"""Assistant-turn parsing and tool-call rejection helpers.

Split of responsibilities (the multi-round loop itself lives in the
investigate entry, not here):

- :func:`parse_assistant` validates one assistant message (plus its
  ``finish_reason``) and returns an :class:`AssistantTurn`. Structural
  safety failures — malformed ``tool_calls`` envelopes, truncated output
  (``finish_reason=length``) and empty content without tool calls — set
  ``safety_failure=True`` with a stable machine code in ``safety_error``
  and leave ``tool_calls`` EMPTY: that turn must execute ZERO tools.
- Unknown tool names, malformed JSON arguments and NaN/Infinity arguments
  are NOT safety failures; the helpers here produce the error payload that
  the loop feeds back to the model as the tool result for that call id.
- :meth:`AssistantTurn.replay` rebuilds the assistant message for the next
  request with ``content`` and ``tool_calls`` ONLY — ``reasoning_content``
  is never replayed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .protocol import TOOL_NAMES
from .strictjson import loads_strict

# Stable machine codes for the safety-failure vocabulary.
SAFETY_KIND_MALFORMED_TOOL_CALLS = "malformed-tool-calls"
SAFETY_KIND_TRUNCATED = "truncated"
SAFETY_KIND_EMPTY_CONTENT = "empty-content"

SAFETY_KINDS = frozenset({
    SAFETY_KIND_MALFORMED_TOOL_CALLS,
    SAFETY_KIND_TRUNCATED,
    SAFETY_KIND_EMPTY_CONTENT,
})


@dataclass(frozen=True)
class ToolCall:
    """One structurally validated tool call from an assistant message."""

    id: str
    name: str
    arguments_raw: object  # exactly as received: JSON string or object


@dataclass(frozen=True)
class AssistantTurn:
    """Parsed assistant turn.

    ``safety_failure`` turns carry ``tool_calls == ()`` (zero execution)
    and a ``safety_error`` dict ``{kind, message}`` with a stable code.
    """

    content: str | None
    finish_reason: str | None
    tool_calls: tuple[ToolCall, ...]
    safety_failure: bool
    safety_error: dict | None = None

    def replay(self) -> dict:
        """Assistant message for the next request: content + tool_calls
        ONLY (``reasoning_content`` is never replayed); non-string content
        replays as ``""`` and non-string arguments as their JSON text."""

        echo = []
        for tc in self.tool_calls:
            raw = tc.arguments_raw
            if not isinstance(raw, str):
                raw = json.dumps(raw, ensure_ascii=False)
            echo.append({"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": raw}})
        content = self.content if isinstance(self.content, str) else ""
        return {"role": "assistant", "content": content, "tool_calls": echo}


def _envelope_problem(tool_calls) -> str | None:
    """Structural ``tool_calls`` envelope check (``arguments`` is
    REQUIRED and must be a string or object — a missing key rejects the
    whole envelope). Returns the problem text or None."""

    if not isinstance(tool_calls, list) or not tool_calls:
        return "message.tool_calls must be a non-empty array"
    seen_ids: set[str] = set()
    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            return f"tool_calls[{i}] is not an object"
        tc_id = tc.get("id")
        if not isinstance(tc_id, str) or not tc_id.strip():
            return f"tool_calls[{i}].id must be a non-empty string"
        if tc_id in seen_ids:
            return f"duplicate tool call id {tc_id!r}"
        seen_ids.add(tc_id)
        fn = tc.get("function")
        if not isinstance(fn, dict):
            return f"tool_calls[{i}].function is not an object"
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            return (f"tool_calls[{i}].function.name must be a non-empty "
                    f"string")
        args = fn.get("arguments")
        if not isinstance(args, (str, dict)):
            return (f"tool_calls[{i}].function.arguments must be a string "
                    f"or object")
    return None


def parse_assistant(message, finish_reason: str | None = None) -> AssistantTurn:
    """Validate one assistant message into an :class:`AssistantTurn`.

    Safety failures (zero tool execution): truncated output
    (``finish_reason=length``), malformed ``tool_calls`` envelope, or an
    empty/absent content with no tool calls at all. Unknown tool names and
    bad argument JSON are deliberately NOT judged here — the loop rejects
    those per-call via :func:`parse_tool_arguments` /
    :func:`unknown_tool_error`.
    """

    if not isinstance(message, dict):
        raise ValueError("assistant message must be a dict")
    content = message.get("content")
    if not isinstance(content, str):
        content = None
    tool_calls_raw = message.get("tool_calls")

    def _turn(**kw) -> AssistantTurn:
        return AssistantTurn(content=content, finish_reason=finish_reason,
                             **kw)

    if finish_reason == "length":
        # Truncation = safety failure: possibly incomplete tool calls are
        # never executed.
        return _turn(tool_calls=(), safety_failure=True,
                     safety_error={"kind": SAFETY_KIND_TRUNCATED,
                                   "message": "finish_reason=length; "
                                              "output truncated by token "
                                              "budget; partial tool calls "
                                              "NOT executed"})
    if tool_calls_raw:
        problem = _envelope_problem(tool_calls_raw)
        if problem is not None:
            return _turn(tool_calls=(), safety_failure=True,
                         safety_error={
                             "kind": SAFETY_KIND_MALFORMED_TOOL_CALLS,
                             "message": "invalid tool_calls envelope; "
                                        "nothing executed: " + problem})
        calls = tuple(
            ToolCall(id=tc["id"], name=tc["function"]["name"],
                     arguments_raw=tc["function"]["arguments"])
            for tc in tool_calls_raw)
        return _turn(tool_calls=calls, safety_failure=False,
                     safety_error=None)
    # No tool calls: the turn must at least say something.
    if content is None or not content.strip():
        return _turn(tool_calls=(), safety_failure=True,
                     safety_error={"kind": SAFETY_KIND_EMPTY_CONTENT,
                                   "message": "assistant message has "
                                              "neither usable content nor "
                                              "tool calls; nothing to act "
                                              "on and nothing executed"})
    return _turn(tool_calls=(), safety_failure=False, safety_error=None)


def parse_completion(body: str):
    """Parse a chat/completions response body (strict JSON).

    Returns ``(message, finish_reason, error)``; ``error`` is ``None`` or
    ``{"kind": "malformed-json", "message": ...}``. Tool-call envelope
    validation is NOT done here — run :func:`parse_assistant` on the
    returned message.
    """

    try:
        outer = loads_strict(body)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, None, {"kind": "malformed-json",
                            "message": f"non-JSON or non-finite numbers in "
                                       f"completion body: {exc}"}
    if not isinstance(outer, dict):
        return None, None, {"kind": "malformed-json",
                            "message": "completion body is not an object"}
    choices = outer.get("choices")
    if not isinstance(choices, list) or not choices \
            or not isinstance(choices[0], dict):
        return None, None, {"kind": "malformed-json",
                            "message": "completion body missing non-empty "
                                       "choices array"}
    first = choices[0]
    message = first.get("message")
    if not isinstance(message, dict):
        return None, first.get("finish_reason"), {
            "kind": "malformed-json",
            "message": "choices[0].message is not an object"}
    return message, first.get("finish_reason"), None


def parse_tool_arguments(raw_arguments) -> tuple[dict | None, dict | None]:
    """Returns ``(args, error)`` for one tool call. Strict JSON (rejects
    NaN/Infinity and overflow floats); the decoded value must be an
    object. The error is meant to be fed back to the model as that call's
    tool result."""

    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    if not isinstance(raw_arguments, str):
        return None, {"kind": "malformed-arguments",
                      "message": "tool call arguments must be a JSON string"}
    try:
        parsed = loads_strict(raw_arguments)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, {"kind": "malformed-arguments",
                      "message": f"arguments are not valid strict JSON: "
                                 f"{exc} (non-finite numbers rejected)"}
    if not isinstance(parsed, dict):
        return None, {"kind": "malformed-arguments",
                      "message": "tool call arguments must decode to an "
                                 "object"}
    return parsed, None


def unknown_tool_error(name, available=None) -> dict:
    """Error payload for a structurally valid call to a tool that does not
    exist; the loop feeds it back as the tool result for that call id."""

    known = sorted(available) if available is not None \
        else sorted(TOOL_NAMES)
    return {"kind": "unknown-tool",
            "message": f"tool {name!r} does not exist; available: {known}"}


def tool_result_message(tool_call_id: str, result: dict) -> dict:
    """Tool result message keyed by ``tool_call_id`` for the next request."""

    return {"role": "tool", "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False)}
