"""Offline tests for the model domain (gleam.core.model).

All transport is faked by patching ``gleam.core.model.http.client`` —
zero network. Covers: normal turns, malformed tool_calls envelopes
(safety failure, zero execution), per-call rejections (unknown tool /
malformed JSON / NaN arguments) that feed back as tool results,
finish_reason=length, empty content, timeout / rate-limit surfaces,
tools-array serialization into the audited request, full audit records,
and the reasoning_content replay ban.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from gleam.core.model import (
    TOOL_SPECS,
    ModelClient,
    ModelConfigError,
    parse_assistant,
    parse_completion,
    parse_tool_arguments,
    tool_result_message,
    unknown_tool_error,
)
from gleam.core.model.client import http_json
from gleam.core.result import UsageError


class _FakeResponse:
    def __init__(self, status, body_bytes):
        self.status = status
        self._body = body_bytes

    def read(self, limit=-1):
        if isinstance(limit, int) and limit > 0:
            return self._body[:limit]
        return self._body


class _FakeHTTPConnection:
    scheme = "http"
    next_response = None
    request_error = None
    created = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        _FakeHTTPConnection.created.append(self)

    def request(self, method, path, body=None, headers=None):
        self.calls.append({"method": method, "path": path, "body": body,
                           "headers": dict(headers or {})})
        if _FakeHTTPConnection.request_error is not None:
            raise _FakeHTTPConnection.request_error

    def getresponse(self):
        return _FakeHTTPConnection.next_response

    def close(self):
        pass


class _FakeHTTPSConnection(_FakeHTTPConnection):
    scheme = "https"


def _choices_body(message, finish_reason=None):
    choice = {"message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return json.dumps({"choices": [choice]}).encode()


def _patched_http(status=200, body=b"{}"):
    """Reset fakes and return a patcher for the package http seam."""
    _FakeHTTPConnection.next_response = _FakeResponse(status, body)
    _FakeHTTPConnection.request_error = None
    _FakeHTTPConnection.created = []
    return patch("gleam.core.model.http.client")


def _install_fakes(hc):
    hc.HTTPConnection = _FakeHTTPConnection
    hc.HTTPSConnection = _FakeHTTPSConnection


def _client(audit=None, **kw):
    kw.setdefault("base", "http://127.0.0.1:1")
    kw.setdefault("model", "q")
    if audit is not None:
        kw["audit"] = audit
    return ModelClient(**kw)


class ConstructionTests(unittest.TestCase):
    def test_missing_base_rejected(self):
        for bad in (None, "", "   "):
            with self.assertRaises(ModelConfigError):
                ModelClient(base=bad, model="q")

    def test_missing_model_rejected(self):
        for bad in (None, "", "   "):
            with self.assertRaises(ModelConfigError):
                ModelClient(base="http://h", model=bad)

    def test_invalid_base_rejected(self):
        for bad in ("ftp://h", "http:///nohost", "not a url"):
            with self.assertRaises(ModelConfigError):
                ModelClient(base=bad, model="q")

    def test_config_error_is_usage_error_and_value_error(self):
        # Dual root: config-domain exit-2 convention AND value-error
        # handling both hold.
        with self.assertRaises(UsageError):
            ModelClient(base="", model="q")
        with self.assertRaises(ValueError):
            ModelClient(base="", model="q")

    def test_bad_numeric_params_rejected(self):
        with self.assertRaises(ModelConfigError):
            _client(max_tokens=0)
        with self.assertRaises(ModelConfigError):
            _client(timeout_s=0)
        with self.assertRaises(ModelConfigError):
            _client(temperature=-1)
        with self.assertRaises(ModelConfigError):
            _client(response_max_bytes=0)

    def test_valid_construction_exposes_endpoint(self):
        client = _client(base="http://127.0.0.1:1/v1")
        self.assertEqual(client.endpoint,
                         "http://127.0.0.1:1/v1/chat/completions")


class ChatTransportTests(unittest.TestCase):
    def test_normal_turn_content(self):
        audit = []
        with _patched_http(200, _choices_body({"content": "ok"})) as hc:
            _install_fakes(hc)
            result = _client(audit=audit.append).chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertIsNone(result.error)
        self.assertTrue(result.ok)
        self.assertFalse(result.safety_failure)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.turn.content, "ok")
        self.assertEqual(result.turn.tool_calls, ())
        self.assertIsNone(result.turn.safety_error)

    def test_request_shape_on_the_wire(self):
        with _patched_http(200, _choices_body({"content": "x"})) as hc:
            _install_fakes(hc)
            _client(max_tokens=512, timeout_s=5.0,
                    temperature=0.0).chat(
                messages=[{"role": "user", "content": "hi"}])
        conn = _FakeHTTPConnection.created[0]
        self.assertEqual(len(_FakeHTTPConnection.created), 1)
        call = conn.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], "/chat/completions")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["headers"]["Accept"], "application/json")
        sent = json.loads(call["body"])
        self.assertEqual(sent["model"], "q")
        self.assertEqual(sent["max_tokens"], 512)
        self.assertEqual(sent["temperature"], 0.0)
        self.assertEqual(sent["messages"], [{"role": "user",
                                             "content": "hi"}])
        # No response_format: tool selection must not stack with it.
        self.assertNotIn("response_format", sent)

    def test_base_prefix_path_joins_endpoint(self):
        with _patched_http(200, _choices_body({"content": "x"})) as hc:
            _install_fakes(hc)
            _client(base="http://127.0.0.1:1/v1").chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(_FakeHTTPConnection.created[0].calls[0]["path"],
                         "/v1/chat/completions")

    def test_https_base_uses_https_connection(self):
        with _patched_http(200, _choices_body({"content": "x"})) as hc:
            _install_fakes(hc)
            _client(base="https://example.invalid").chat(
                messages=[{"role": "user", "content": "hi"}])
        conn = _FakeHTTPConnection.created[0]
        self.assertIsInstance(conn, _FakeHTTPSConnection)
        self.assertEqual(conn.host, "example.invalid")
        self.assertEqual(conn.port, 443)

    def test_timeout_surface(self):
        _FakeHTTPConnection.next_response = None
        _FakeHTTPConnection.request_error = TimeoutError("timed out")
        _FakeHTTPConnection.created = []
        with patch("gleam.core.model.http.client") as hc:
            _install_fakes(hc)
            result = _client().chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertIsNone(result.turn)
        self.assertEqual(result.error["kind"], "timeout")
        self.assertIn("timed out", result.error["message"])

    def test_connection_error_surface(self):
        _FakeHTTPConnection.next_response = None
        _FakeHTTPConnection.request_error = ConnectionRefusedError(
            "refused")
        _FakeHTTPConnection.created = []
        with patch("gleam.core.model.http.client") as hc:
            _install_fakes(hc)
            result = _client().chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(result.error["kind"], "connection-error")
        self.assertIsNone(result.status)

    def test_http_error_and_rate_limit_surface(self):
        for status in (500, 429):
            with _patched_http(status, b"nope") as hc:
                _install_fakes(hc)
                result = _client().chat(
                    messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(result.error["kind"], "http-error")
            self.assertEqual(result.error["http_status"], status)
            self.assertEqual(result.status, status)
            self.assertIsNone(result.turn)

    def test_malformed_body_grades(self):
        for body in (b"not json", b'{"choices": []}',
                     b'{"choices": [{"message": "not-an-object"}]}',
                     b'{"x": NaN}'):
            with _patched_http(200, body) as hc:
                _install_fakes(hc)
                result = _client().chat(
                    messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(result.error["kind"], "malformed-json",
                             body)
            self.assertIsNone(result.turn)


class EnvelopeSafetyTests(unittest.TestCase):
    def test_malformed_tool_calls_shapes_are_safety_failures(self):
        # Plan QA scenario (a), verbatim shapes: missing arguments,
        # missing id, duplicate id, function not an object, name not
        # a string. A None column means the case never pinned that
        # field.
        cases = (
            ("missing-arguments",
             {"content": "",
              "tool_calls": [{"id": "a",
                              "function": {"name": "fetch_url"}}]},
             "malformed-tool-calls", None),
            ("missing-id",
             {"content": "", "tool_calls": [
                 {"function": {"name": "fetch_url", "arguments": "{}"}}]},
             None, None),
            ("duplicate-id",
             {"content": "", "tool_calls": [
                 {"id": "a", "function": {"name": "fetch_url",
                                          "arguments": "{}"}},
                 {"id": "a", "function": {"name": "fetch_url",
                                          "arguments": "{}"}}]},
             None, "duplicate"),
            ("function-not-an-object",
             {"content": "", "tool_calls": [
                 {"id": "a", "function": "fetch_url"}]},
             "malformed-tool-calls", None),
            ("name-not-a-string",
             {"content": "", "tool_calls": [
                 {"id": "a", "function": {"name": 7, "arguments": "{}"}}]},
             None, None),
        )
        for label, msg, kind, message_part in cases:
            with self.subTest(label):
                out = parse_assistant(msg)
                self.assertTrue(out.safety_failure)
                self.assertFalse(out.tool_calls)
                if kind is not None:
                    self.assertEqual(out.safety_error["kind"], kind)
                if message_part is not None:
                    self.assertIn(message_part,
                                  out.safety_error["message"])

    def test_malformed_envelope_via_chat_executes_zero_tools(self):
        body = _choices_body(
            {"content": "",
             "tool_calls": [{"id": "a", "function": {"name": "fetch_url"}}]},
            finish_reason="tool_calls")
        with _patched_http(200, body) as hc:
            _install_fakes(hc)
            result = _client().chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertTrue(result.safety_failure)
        self.assertEqual(result.turn.tool_calls, ())
        self.assertIsNone(result.error)  # safety, not transport/parse error

    def test_finish_reason_length_is_safety_failure(self):
        msg = {"content": "partial",
               "tool_calls": [{"id": "a", "function": {
                   "name": "fetch_url", "arguments": "{}"}}]}
        out = parse_assistant(msg, finish_reason="length")
        self.assertTrue(out.safety_failure)
        self.assertEqual(out.safety_error["kind"], "truncated")
        self.assertFalse(out.tool_calls)

    def test_empty_content_without_tool_calls_is_safety_failure(self):
        for content in ("", None, "   ", []):
            out = parse_assistant({"content": content})
            self.assertTrue(out.safety_failure, content)
            self.assertEqual(out.safety_error["kind"], "empty-content")
            self.assertFalse(out.tool_calls)

    def test_prose_turn_is_not_a_safety_failure(self):
        out = parse_assistant({"content": "just words"})
        self.assertFalse(out.safety_failure)
        self.assertEqual(out.content, "just words")
        self.assertEqual(out.tool_calls, ())

    def test_valid_tool_turn_parses(self):
        msg = {"content": None,
               "tool_calls": [
                   {"id": "a", "function": {"name": "fetch_url",
                                            "arguments": '{"url": "x"}'}},
                   {"id": "b", "function": {"name": "search_web",
                                            "arguments": {"query": "y"}}}]}
        out = parse_assistant(msg, finish_reason="tool_calls")
        self.assertFalse(out.safety_failure)
        self.assertEqual(len(out.tool_calls), 2)
        self.assertEqual(out.tool_calls[0].id, "a")
        self.assertEqual(out.tool_calls[0].name, "fetch_url")
        self.assertEqual(out.tool_calls[0].arguments_raw,
                         '{"url": "x"}')
        # dict arguments tolerated verbatim (string form on replay)
        self.assertEqual(out.tool_calls[1].arguments_raw,
                         {"query": "y"})


class RejectionFeedbackTests(unittest.TestCase):
    """Unknown tool / malformed JSON / NaN arguments are NOT safety
    failures: they are per-call rejections the loop feeds back as the
    tool result for that call id."""

    def test_unknown_tool_is_not_a_safety_failure(self):
        msg = {"content": "", "tool_calls": [
            {"id": "a", "function": {"name": "does_not_exist",
                                     "arguments": "{}"}}]}
        out = parse_assistant(msg)
        self.assertFalse(out.safety_failure)
        self.assertEqual(len(out.tool_calls), 1)

    def test_unknown_tool_error_payload_feeds_back(self):
        err = unknown_tool_error("does_not_exist")
        self.assertEqual(err["kind"], "unknown-tool")
        self.assertIn("does_not_exist", err["message"])
        message = tool_result_message("a", {"error": err})
        self.assertEqual(message["role"], "tool")
        self.assertEqual(message["tool_call_id"], "a")
        self.assertIn("unknown-tool", message["content"])

    def test_rejected_argument_shapes(self):
        for label, raw in (
                ("malformed-json", '{"url": '),
                ("non-string", 17),
                ("non-object", "[1, 2]")):
            with self.subTest(label):
                args, err = parse_tool_arguments(raw)
                self.assertIsNone(args)
                self.assertEqual(err["kind"], "malformed-arguments")

    def test_nan_infinity_overflow_arguments_rejected(self):
        for raw in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}',
                    '{"x": 1e999}'):
            args, err = parse_tool_arguments(raw)
            self.assertIsNone(args, raw)
            self.assertEqual(err["kind"], "malformed-arguments", raw)
            self.assertIn("non-finite", err["message"])

    def test_dict_arguments_pass_through(self):
        args, err = parse_tool_arguments({"url": "x"})
        self.assertIsNone(err)
        self.assertEqual(args, {"url": "x"})


class AuditTests(unittest.TestCase):
    def test_audit_record_shape_on_success(self):
        audit = []
        body = _choices_body({"content": "ok"}, finish_reason="stop")
        with _patched_http(200, body) as hc:
            _install_fakes(hc)
            _client(audit=audit.append).chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(len(audit), 1)
        rec = audit[0]
        self.assertEqual(rec["model"], "q")
        self.assertEqual(rec["endpoint"],
                         "http://127.0.0.1:1/chat/completions")
        self.assertEqual(rec["response_status"], 200)
        self.assertEqual(json.loads(rec["response_body_raw"])
                         ["choices"][0]["message"]["content"], "ok")
        self.assertIsNone(rec["error"])
        self.assertEqual(rec["validation"], "ok")
        self.assertEqual(rec["finish_reason"], "stop")
        self.assertFalse(rec["safety_failure"])
        self.assertIsNone(rec["safety_error"])
        self.assertIsInstance(rec["duration_s"], float)
        self.assertEqual(len(rec["prompt_sha256"]), 64)
        int(rec["prompt_sha256"], 16)  # hex sha256
        # request carries messages AND tools
        self.assertEqual(rec["request"]["messages"],
                         [{"role": "user", "content": "hi"}])
        self.assertIn("tools", rec["request"])

    def test_audit_error_record_on_transport_failure(self):
        audit = []
        _FakeHTTPConnection.next_response = None
        _FakeHTTPConnection.request_error = OSError("boom")
        _FakeHTTPConnection.created = []
        with patch("gleam.core.model.http.client") as hc:
            _install_fakes(hc)
            _client(audit=audit.append).chat(
                messages=[{"role": "user", "content": "hi"}])
        rec = audit[0]
        self.assertEqual(rec["error"]["kind"], "connection-error")
        self.assertIsNone(rec["response_status"])
        self.assertEqual(rec["response_body_raw"], "")
        self.assertEqual(rec["validation"], "error")
        # the request side is still fully archived
        self.assertIn("tools", rec["request"])
        self.assertTrue(rec["request"]["messages"])

    def test_audit_safety_fields_on_truncation(self):
        audit = []
        body = _choices_body(
            {"content": "", "tool_calls": [{"id": "a", "function": {
                "name": "fetch_url", "arguments": "{}"}}]},
            finish_reason="length")
        with _patched_http(200, body) as hc:
            _install_fakes(hc)
            _client(audit=audit.append).chat(
                messages=[{"role": "user", "content": "hi"}])
        rec = audit[0]
        self.assertTrue(rec["safety_failure"])
        self.assertEqual(rec["safety_error"]["kind"], "truncated")

class ToolsProtocolTests(unittest.TestCase):
    def test_frozen_tools_include_report_inspection_and_preview(self):
        self.assertEqual(
            {t["function"]["name"] for t in TOOL_SPECS},
            {"fetch_url", "search_web", "github", "read_evidence",
             "fetch_bug", "fetch_udd", "repology",
             "inspect_report_state", "validate_report_draft",
             "finish_investigation"})

    def test_repology_action_enum_matches_live_catalog(self):
        # The frozen schema enum must track the sources-domain catalog.
        from gleam.core.sources.repology import REPOLOGY_ACTIONS
        enum = None
        for spec in TOOL_SPECS:
            if spec["function"]["name"] == "repology":
                enum = spec["function"]["parameters"]["properties"][
                    "action"]["enum"]
        self.assertEqual(enum, list(REPOLOGY_ACTIONS))

    def test_every_spec_is_wellformed_function_schema(self):
        for spec in TOOL_SPECS:
            self.assertEqual(spec["type"], "function")
            fn = spec["function"]
            self.assertIsInstance(fn["name"], str)
            self.assertIsInstance(fn["description"], str)
            params = fn["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("required", params)

    def test_fetch_udd_enum_matches_live_catalog(self):
        # The frozen schema enum must track the sources-domain catalog.
        from gleam.core.sources.udd import DATASET_NAMES
        enum = None
        for spec in TOOL_SPECS:
            if spec["function"]["name"] == "fetch_udd":
                enum = spec["function"]["parameters"]["properties"][
                    "datasets"]["items"]["enum"]
        self.assertEqual(enum, list(DATASET_NAMES))
        self.assertEqual(len(enum), 18)

    def test_github_action_enum_matches_live_catalog(self):
        # The frozen schema enum must track the sources-domain catalog.
        from gleam.core.sources.github import GITHUB_ACTIONS
        enum = None
        for spec in TOOL_SPECS:
            if spec["function"]["name"] == "github":
                enum = spec["function"]["parameters"]["properties"][
                    "action"]["enum"]
        self.assertEqual(enum, list(GITHUB_ACTIONS))

    def test_chat_default_tools_is_frozen_protocol(self):
        audit = []
        with _patched_http(200, _choices_body({"content": "x"})) as hc:
            _install_fakes(hc)
            _client(audit=audit.append).chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(audit[0]["request"]["tools"], TOOL_SPECS)

    def test_chat_explicit_tools_passed_through(self):
        audit = []
        custom = [{"type": "function", "function": {
            "name": "solo", "parameters": {"type": "object",
                                           "properties": {}}}}]
        with _patched_http(200, _choices_body({"content": "x"})) as hc:
            _install_fakes(hc)
            _client(audit=audit.append).chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=custom)
        self.assertEqual(audit[0]["request"]["tools"], custom)


class ReplayTests(unittest.TestCase):
    def test_reasoning_content_never_replayed(self):
        msg = {"content": "thinking out loud",
               "reasoning_content": "chain of thought",
               "tool_calls": [{"id": "a", "type": "function",
                               "function": {"name": "fetch_url",
                                            "arguments": '{"url": "x"}'}}]}
        out = parse_assistant(msg)
        replay = out.replay()
        self.assertEqual(replay["role"], "assistant")
        self.assertEqual(replay["content"], "thinking out loud")
        self.assertNotIn("reasoning_content", replay)
        dumped = json.dumps(replay)
        self.assertNotIn("reasoning_content", dumped)
        self.assertNotIn("chain of thought", dumped)

    def test_replay_normalizes_non_string_content_and_dict_arguments(self):
        msg = {"content": ["not", "a", "string"],
               "tool_calls": [{"id": "a", "function": {
                   "name": "search_web",
                   "arguments": {"query": "y"}}}]}
        out = parse_assistant(msg)
        replay = out.replay()
        self.assertEqual(replay["content"], "")
        self.assertEqual(replay["tool_calls"][0]["function"]["arguments"],
                         '{"query": "y"}')
        self.assertEqual(replay["tool_calls"][0]["type"], "function")

    def test_reasoning_content_flagged_in_audit_only(self):
        audit = []
        body = _choices_body({"content": "ok",
                              "reasoning_content": "hidden chain"})
        with _patched_http(200, body) as hc:
            _install_fakes(hc)
            _client(audit=audit.append).chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertTrue(audit[0]["reasoning_content_present"])
        # and the raw body (with reasoning) is archived verbatim
        self.assertIn("hidden chain", audit[0]["response_body_raw"])


class HelperUnitTests(unittest.TestCase):
    def test_parse_completion_returns_message_and_finish_reason(self):
        msg = {"content": "hi"}
        message, finish_reason, err = parse_completion(
            json.dumps({"choices": [{"message": msg,
                                     "finish_reason": "stop"}]}))
        self.assertIsNone(err)
        self.assertEqual(message, msg)
        self.assertEqual(finish_reason, "stop")

    def test_http_json_direct_call(self):
        with _patched_http(201, b'{"odd": true}') as hc:
            _install_fakes(hc)
            status, body = http_json("http://127.0.0.1:1", "/x", "GET",
                                     None, 1.0)
        self.assertEqual((status, body), (201, '{"odd": true}'))
        call = _FakeHTTPConnection.created[0].calls[0]
        self.assertEqual((call["method"], call["path"]), ("GET", "/x"))
        self.assertNotIn("Content-Type", call["headers"])


if __name__ == "__main__":
    unittest.main()
