"""GLEAM_MODEL_API_KEY end-to-end tests.

The model client must send exactly one ``Authorization: Bearer`` header
when an api key is configured, send byte-identical headers when none is,
treat a whitespace-only key as unset, and keep the key out of the audit
record, the ChatResult, endpoint strings, error messages, and reprs.

Transport is faked by patching ``gleam.core.model.http.client`` — the
same zero-network seam as tests/test_model.py (the fakes are mirrored
here, not imported).
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from gleam.core.model import ModelClient, ModelConfigError
from gleam.core.model.client import http_json

SECRET = "sk-test-0123456789abcdef"

# The exact header mapping of an unauthenticated POST with a payload
# (byte-identical to the pre-amendment client).
_PLAIN_HEADERS = {"Accept": "application/json",
                  "Content-Type": "application/json"}


class _FakeResponse:
    def __init__(self, status, body_bytes):
        self.status = status
        self._body = body_bytes

    def read(self, limit=-1):
        if isinstance(limit, int) and limit > 0:
            return self._body[:limit]
        return self._body


class _FakeHTTPConnection:
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
    pass


def _patched_http(status=200, body=b"{}"):
    _FakeHTTPConnection.next_response = _FakeResponse(status, body)
    _FakeHTTPConnection.request_error = None
    _FakeHTTPConnection.created = []
    return patch("gleam.core.model.http.client")


def _install_fakes(hc):
    hc.HTTPConnection = _FakeHTTPConnection
    hc.HTTPSConnection = _FakeHTTPSConnection


def _ok_body():
    return json.dumps({"choices": [{"message": {"content": "ok"},
                                    "finish_reason": "stop"}]}).encode()


def _client(audit=None, **kw):
    kw.setdefault("base", "http://127.0.0.1:1")
    kw.setdefault("model", "q")
    if audit is not None:
        kw["audit"] = audit
    return ModelClient(**kw)


def _sent_headers(**client_kw):
    """Chat once; return (headers of the single request, connections)."""
    with _patched_http(200, _ok_body()) as hc:
        _install_fakes(hc)
        _client(**client_kw).chat(
            messages=[{"role": "user", "content": "hi"}])
    return _FakeHTTPConnection.created[0].calls[0]["headers"], \
        _FakeHTTPConnection.created


class AuthorizationHeaderTests(unittest.TestCase):
    def test_key_set_sends_exactly_one_bearer_header(self):
        headers, conns = _sent_headers(api_key=SECRET)
        self.assertEqual(len(conns), 1)
        self.assertEqual(
            headers,
            {**_PLAIN_HEADERS, "Authorization": f"Bearer {SECRET}"})

    def test_key_unset_sends_no_authorization_header(self):
        headers, _ = _sent_headers()
        self.assertEqual(headers, _PLAIN_HEADERS)

    def test_empty_string_key_treated_as_unset(self):
        headers, _ = _sent_headers(api_key="")
        self.assertEqual(headers, _PLAIN_HEADERS)

    def test_whitespace_only_key_treated_as_unset(self):
        headers, _ = _sent_headers(api_key="   \t ")
        self.assertEqual(headers, _PLAIN_HEADERS)

    def test_key_surrounding_whitespace_stripped_on_wire(self):
        headers, _ = _sent_headers(api_key=f"  {SECRET}\n")
        self.assertEqual(headers["Authorization"], f"Bearer {SECRET}")

    def test_https_base_also_carries_authorization(self):
        with _patched_http(200, _ok_body()) as hc:
            _install_fakes(hc)
            _client(base="https://example.invalid",
                    api_key=SECRET).chat(
                messages=[{"role": "user", "content": "hi"}])
        conn = _FakeHTTPConnection.created[0]
        self.assertIsInstance(conn, _FakeHTTPSConnection)
        self.assertEqual(conn.calls[0]["headers"]["Authorization"],
                         f"Bearer {SECRET}")

    def test_non_string_key_rejected(self):
        for bad in (123, b"key", True, object()):
            with self.assertRaises(ModelConfigError):
                _client(api_key=bad)

    def test_http_json_direct_call_with_and_without_key(self):
        with _patched_http(200, b'{"ok": true}') as hc:
            _install_fakes(hc)
            status, body = http_json("http://127.0.0.1:1", "/x", "GET",
                                     None, 1.0, api_key=SECRET)
        self.assertEqual((status, body), (200, '{"ok": true}'))
        headers = _FakeHTTPConnection.created[0].calls[0]["headers"]
        # GET without payload: no Content-Type; the key adds exactly one
        # header on top of the bare Accept. The no-key half of the wire
        # contract is pinned by test_model.py::test_http_json_direct_call.
        self.assertEqual(headers, {"Accept": "application/json",
                                   "Authorization": f"Bearer {SECRET}"})


class AuditSecrecyTests(unittest.TestCase):
    def test_key_never_in_audit_record_or_chatresult(self):
        audit = []
        with _patched_http(200, _ok_body()) as hc:
            _install_fakes(hc)
            client = _client(audit=audit.append, api_key=SECRET)
            result = client.chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(len(audit), 1)
        dumped = json.dumps(audit)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("api_key", dumped)
        self.assertNotIn("Authorization", dumped)
        self.assertNotIn(SECRET, result.endpoint)
        self.assertNotIn(SECRET, json.dumps(result.body))
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, repr(client))

    def test_key_not_leaked_through_error_paths(self):
        for setup in (
            {"status": 500, "body": b"server exploded"},
            {"status": 401, "body": b'{"error": "bad token"}'},
        ):
            audit = []
            with _patched_http(**setup) as hc:
                _install_fakes(hc)
                result = _client(audit=audit.append,
                                 api_key=SECRET).chat(
                    messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(result.error["kind"], "http-error")
            self.assertNotIn(SECRET, json.dumps(result.error))
            self.assertNotIn(SECRET, json.dumps(audit))
        audit = []
        _FakeHTTPConnection.next_response = None
        _FakeHTTPConnection.request_error = OSError("boom")
        _FakeHTTPConnection.created = []
        with patch("gleam.core.model.http.client") as hc:
            _install_fakes(hc)
            result = _client(audit=audit.append, api_key=SECRET).chat(
                messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(result.error["kind"], "connection-error")
        self.assertNotIn(SECRET, json.dumps(result.error))
        self.assertNotIn(SECRET, json.dumps(audit))


if __name__ == "__main__":
    unittest.main()
