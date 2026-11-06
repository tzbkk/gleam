"""OpenAI-compatible native-tools chat client.

Trust model: the base URL and model name are operator-configured explicit
values — there is NO default endpoint or model. Missing or invalid config
is rejected at construction (:class:`ModelConfigError`, a
``gleam.core.result.UsageError`` so the entry maps it to the exit-2 config
path). The base is a trusted destination: it is parsed but does not go
through the public-web SSRF checks.

Every turn's full request payload (messages AND tools array), response
status/body, errors and safety outcomes are handed to the ``audit``
callback as one structured record. Writing that record to disk is the
entry's job (through the storage domain); this module never touches files
or the process environment. Assistant replay (``AssistantTurn.replay``)
carries content + tool_calls only — ``reasoning_content`` is never sent
back.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse
from dataclasses import dataclass

from gleam.core.result import UsageError

from .parse import AssistantTurn, parse_assistant, parse_completion
from .protocol import TOOL_SPECS
from .strictjson import prompt_hash

# Defaults mirror the config-domain key defaults
# (GLEAM_MODEL_MAX_TOKENS / GLEAM_MODEL_TIMEOUT /
# GLEAM_MODEL_TEMPERATURE / GLEAM_MODEL_RESPONSE_MAX_BYTES); range
# authority lives in the config domain, this side only sanity-checks.
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_TEMPERATURE = 0.0
DEFAULT_RESPONSE_MAX_BYTES = 1 << 20  # 1 MiB read cap

# Built at import: when the http.client test seam is swapped, re-resolving
# HTTPException from it at raise time yields a non-class (TypeError).
_TRANSPORT_ERRORS = (OSError, http.client.HTTPException)


class ModelConfigError(UsageError, ValueError):
    """Model client construction problem (missing/invalid explicit config
    or bad numeric parameters). Dual-rooted so both the config-domain
    exit-2 convention and plain value-error handling hold."""


def _split_base(base_url: str) -> tuple[str, str, int]:
    """Split a base URL into (scheme, host, port); trusted configuration,
    no SSRF validation."""

    parsed = urllib.parse.urlsplit(base_url)
    scheme = parsed.scheme or "http"
    if scheme not in ("http", "https"):
        raise ValueError(f"model base scheme must be http(s): {base_url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"model base has no host: {base_url!r}")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def validate_base(base_url: str) -> str:
    """Pre-check a model base; returns an error message or ``''`` (for
    entry-side checks before any run dir)."""

    try:
        _split_base(base_url)
    except ValueError as exc:
        return str(exc)
    return ""


def http_json(base_url: str, rel_path: str, method: str,
              payload: dict | None, timeout: float,
              max_response_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
              api_key: str | None = None
              ) -> tuple[int, str]:
    """One JSON request against the configured model service; returns
    ``(status, body_text)``. Direct ``http.client`` connection (no proxy
    variables are consulted); the URL comes only from operator config.
    ``api_key``, when a non-empty string after stripping, adds exactly
    one ``Authorization: Bearer`` header; unset/blank adds none."""

    scheme, host, port = _split_base(base_url)
    base_path = urllib.parse.urlsplit(base_url).path.rstrip("/")
    path = base_path + rel_path
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        headers = {"Accept": "application/json"}
        if api_key is not None and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read(max_response_bytes)  # beyond the cap the body
        # is treated as malformed downstream (strict JSON parse)
        return response.status, data.decode("utf-8", "replace")
    finally:
        try:
            conn.close()
        except OSError:
            pass


@dataclass(frozen=True)
class ChatResult:
    """Outcome of one ``chat()`` call.

    ``error`` is ``None`` or ``{"kind", "message", ...}`` with stable
    codes: ``timeout`` / ``connection-error`` (transport),
    ``http-error`` (non-200; carries ``http_status``) and
    ``malformed-json`` (unparseable body). A parsed-but-unsafe turn keeps
    ``error=None`` and surfaces through ``turn.safety_failure``.
    """

    model: str
    endpoint: str
    status: int | None
    body: str
    turn: AssistantTurn | None
    error: dict | None
    duration_s: float

    @property
    def ok(self) -> bool:
        return (self.error is None and self.turn is not None
                and not self.turn.safety_failure)

    @property
    def safety_failure(self) -> bool:
        return bool(self.turn is not None and self.turn.safety_failure)


class ModelClient:
    """Native-tools chat client for one explicitly configured model.

    ``audit`` (constructor param by contract, not a test seam) receives one
    record dict per ``chat()`` call — success, HTTP error, malformed body
    or transport failure alike — with the full request payload under
    ``request`` (messages AND tools array).

    ``api_key`` is an optional bearer token: set and
    non-blank, every request carries exactly one ``Authorization: Bearer``
    header. It is a secret — it is never placed in the audit record, the
    :class:`ChatResult`, endpoint strings, error messages, or any repr.
    """

    def __init__(self, base: str, model: str, audit=None, *,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 temperature: float = DEFAULT_TEMPERATURE,
                 response_max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
                 api_key: str | None = None):
        for name, value in (("base", base), ("model", model)):
            if not isinstance(value, str) or not value.strip():
                raise ModelConfigError(
                    f"{name} must be an explicit non-empty string; there "
                    f"is no default model endpoint or name (operator "
                    f"configuration only)")
        try:
            _split_base(base)
        except ValueError as exc:
            raise ModelConfigError(str(exc)) from None
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
                or max_tokens <= 0:
            raise ModelConfigError("max_tokens must be a positive integer")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) \
                or not timeout_s > 0:
            raise ModelConfigError("timeout_s must be a positive number")
        if isinstance(temperature, bool) \
                or not isinstance(temperature, (int, float)) \
                or not temperature >= 0:
            raise ModelConfigError("temperature must be a non-negative "
                                   "number")
        if isinstance(response_max_bytes, bool) \
                or not isinstance(response_max_bytes, int) \
                or response_max_bytes <= 0:
            raise ModelConfigError("response_max_bytes must be a positive "
                                   "integer")
        if api_key is not None and not isinstance(api_key, str):
            raise ModelConfigError("api_key must be a string or None")
        self._base = base
        self._model = model
        self._audit = audit
        self._max_tokens = max_tokens
        self._timeout_s = float(timeout_s)
        self._temperature = float(temperature)
        self._response_max_bytes = response_max_bytes
        # Blank/whitespace-only means no authentication; the stripped
        # value is what goes on the wire.
        self._api_key: str | None = None
        if api_key is not None:
            self._api_key = api_key.strip() or None
        self._endpoint = base.rstrip("/") + "/chat/completions"

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def chat(self, messages: list, tools: list | None = None) -> ChatResult:
        """One chat/completions turn. ``tools`` defaults to the frozen
        investigation tool protocol (the same array is offered every round);
        pass an explicit list to override. No ``response_format`` — tool
        selection must not stack with structured output."""

        if tools is None:
            tools = TOOL_SPECS
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": list(messages),
            "tools": tools,
        }
        started = time.monotonic()
        status: int | None = None
        body = ""
        error: dict | None = None
        try:
            status, body = http_json(self._base, "/chat/completions",
                                     "POST", payload, self._timeout_s,
                                     self._response_max_bytes,
                                     api_key=self._api_key)
        except TimeoutError as exc:  # includes socket timeouts
            error = {"kind": "timeout",
                     "message": f"{type(exc).__name__}: {exc}"}
        except _TRANSPORT_ERRORS as exc:
            error = {"kind": "connection-error",
                     "message": f"{type(exc).__name__}: {exc}"}
        duration = round(time.monotonic() - started, 3)
        message: dict | None = None
        turn: AssistantTurn | None = None
        finish_reason: str | None = None
        if error is None:
            if status != 200:
                error = {"kind": "http-error",
                         "message": f"status {status}",
                         "http_status": status}
            else:
                message, finish_reason, parse_error = parse_completion(body)
                if parse_error is not None:
                    error = parse_error
                else:
                    turn = parse_assistant(message,
                                           finish_reason=finish_reason)
        self._emit_audit(payload=payload, status=status, body=body,
                         error=error, duration_s=duration,
                         finish_reason=finish_reason, message=message,
                         turn=turn)
        return ChatResult(model=self._model, endpoint=self._endpoint,
                          status=status, body=body, turn=turn, error=error,
                          duration_s=duration)

    def _emit_audit(self, *, payload, status, body, error, duration_s,
                    finish_reason, message, turn) -> None:
        if self._audit is None:
            return
        record = {
            "model": self._model,
            "endpoint": self._endpoint,
            "request": payload,
            "prompt_sha256": prompt_hash(payload),
            "response_status": status,
            "response_body_raw": body,
            "error": error,
            "validation": "error" if error is not None else "ok",
            "duration_s": duration_s,
            "finish_reason": finish_reason,
            "reasoning_content_present": bool(
                (message or {}).get("reasoning_content")),
            "safety_failure": bool(turn is not None
                                   and turn.safety_failure),
            "safety_error": (turn.safety_error
                             if turn is not None and turn.safety_failure
                             else None),
        }
        self._audit(record)
