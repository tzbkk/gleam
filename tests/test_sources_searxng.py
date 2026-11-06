"""Offline tests for the typed SearXNG source adapter.

Zero network: the package's transport module boundary is patched with
``unittest.mock.patch``; normalization rules are additionally
exercised as pure functions.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from gleam.core.result import UsageError
from gleam.core.sources.searxng import (
    SearxngClient,
    canonical_url,
    normalize_results,
    strip_html,
)

PATCH_TARGET = "gleam.core.sources.searxng.client.transport"


def search_returning(body: bytes, status: int = 200,
                     headers: dict | None = None):
    """Context manager: a client whose transport returns the given
    ``(status, headers, body)``."""

    patcher = patch(PATCH_TARGET)
    mocked = patcher.start()

    class _Ctx:
        def __enter__(self):
            mocked.request.return_value = (status, headers or {}, body)
            return SearxngClient()

        def __exit__(self, *exc):
            patcher.stop()
            return False

    return _Ctx()


class NormalizationTests(unittest.TestCase):
    def test_relative_and_non_http_urls_dropped(self):
        raw = [
            {"url": "/relative/path", "title": "a", "content": "c",
             "engines": ["e1"]},
            {"url": "ftp://files.example.org/x", "title": "b",
             "content": "c", "engines": ["e1"]},
            {"url": "javascript:alert(1)", "title": "c", "content": "c",
             "engines": ["e1"]},
            {"url": "https://user:pw@host.example.org/p", "title": "d",
             "content": "c", "engines": ["e1"]},
            {"url": "https://good.example.org/p", "title": "e",
             "content": "c", "engines": ["e1"]},
            "not a dict",
        ]
        results, deduped = normalize_results(raw)
        self.assertEqual([r["url"] for r in results],
                         ["https://good.example.org/p"])
        self.assertEqual(deduped, 5)

    def test_fragments_stripped(self):
        self.assertEqual(canonical_url("https://x.example.org/a#frag"),
                         "https://x.example.org/a")
        self.assertEqual(canonical_url("https://x.example.org/a?b=1#f"),
                         "https://x.example.org/a?b=1")
        self.assertIsNone(canonical_url("https://x.example.org:bad/a"))

    def test_dedup_preserves_rank_and_merges_engines(self):
        raw = [
            {"url": "https://first.example.org/", "title": "first",
             "content": "s1", "engines": ["google"]},
            {"url": "https://second.example.org/", "title": "second",
             "content": "s2", "engines": ["bing"]},
            {"url": "https://first.example.org/", "title": "",
             "content": "s1-alt", "engines": ["ddg", "google"]},
        ]
        results, deduped = normalize_results(raw)
        self.assertEqual([r["url"] for r in results],
                         ["https://first.example.org/",
                          "https://second.example.org/"])
        self.assertEqual(deduped, 1)
        self.assertEqual(results[0]["engines"], ["google", "ddg"])
        # first non-empty title/snippet kept from first occurrence
        self.assertEqual(results[0]["title"], "first")
        self.assertEqual(results[0]["snippet"], "s1")

    def test_tags_stripped_and_entities_decoded(self):
        self.assertEqual(strip_html("<b>Hello</b> &amp; goodbye"),
                         "Hello & goodbye")
        self.assertEqual(strip_html("  multi\nline  text "), 
                         "multi line text")
        self.assertEqual(strip_html(None), "")
        self.assertEqual(strip_html(42), "")

    def test_engine_attribution_per_result(self):
        raw = [
            {"url": "https://a.example.org/", "title": "a",
             "content": "", "engine": "startpage"},
            {"url": "https://b.example.org/", "title": "b",
             "content": "", "engines": "duckduckgo"},
        ]
        results, _ = normalize_results(raw)
        self.assertEqual(results[0]["engines"], ["startpage"])
        self.assertEqual(results[1]["engines"], ["duckduckgo"])


class GradingTests(unittest.TestCase):
    def test_clean_zero_results_is_ok_not_ambiguous(self):
        body = json.dumps({"results": [], "unresponsive_engines": [],
                           "number_of_results": 0}).encode()
        with search_returning(body) as client:
            result = client.search("wizznic upstream")
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.results, [])
        self.assertEqual(result.errors, [])
        self.assertIn("no results", result.note)

    def test_zero_results_with_engine_failures_is_explicit_ambiguity(self):
        body = json.dumps({
            "results": [],
            "unresponsive_engines": [["duckduckgo",
                                      "connection timed out"]],
        }).encode()
        with search_returning(body) as client:
            result = client.search("wizznic upstream")
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.errors[0].code,
                         "all-engines-failed-or-empty")
        self.assertEqual(result.errors[0].details["unresponsive_engines"],
                         [["duckduckgo", "connection timed out"]])
        # the ambiguity is announced, never silently read as empty
        self.assertIn("unknown", result.note)
        self.assertIn("ambiguous", result.errors[0].message)

    def test_captcha_with_usable_results_is_warning_not_failure(self):
        body = json.dumps({
            "results": [{"url": "https://r.example.org/", "title": "r",
                         "content": "s", "engines": ["google"]}],
            "unresponsive_engines": [["bing", "captcha error"]],
        }).encode()
        with search_returning(body) as client:
            result = client.search("q")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.results), 1)
        self.assertEqual([w.code for w in result.warnings],
                         ["captcha-reported"])
        self.assertEqual(result.warnings[0].details["captcha_engines"],
                         [["bing", "captcha error"]])

    def test_403_format_forbidden_grade(self):
        with search_returning(b"", status=403) as client:
            result = client.search("q")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "format-forbidden")
        self.assertIn("json", result.errors[0].message.lower())

    def test_429_rate_limited_grade(self):
        with search_returning(b"", status=429,
                              headers={"retry-after": ["30"]}) as client:
            result = client.search("q")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "rate-limited")
        self.assertEqual(result.errors[0].details["retry_after"], "30")

    def test_redirect_never_followed(self):
        with patch(PATCH_TARGET) as mocked:
            mocked.request.return_value = (
                302, {"location": ["https://evil.example.org/"]}, b"")
            client = SearxngClient()
            result = client.search("q")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.errors[0].code, "unexpected-redirect")
            self.assertEqual(result.errors[0].details["location"],
                             "https://evil.example.org/")
            self.assertEqual(mocked.request.call_count, 1)

    def test_malformed_json_grade(self):
        with search_returning(b"<html>not json") as client:
            result = client.search("wizznic upstream")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "malformed-json")
        # a failure is never read as a no-results answer
        self.assertNotEqual(result.status, "ok")
        self.assertIn("unknown", result.note)

    def test_json_missing_results_array_is_malformed(self):
        with search_returning(b'{"answer": 42}') as client:
            result = client.search("q")
        self.assertEqual(result.errors[0].code, "malformed-json")
        self.assertIn("results array", result.errors[0].message)

    def test_non_finite_number_of_results_dropped(self):
        body = (b'{"results": [], "unresponsive_engines": [], '
                b'"number_of_results": NaN}')
        with search_returning(body) as client:
            result = client.search("q")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "malformed-json")

    def test_timeout_grade(self):
        with patch(PATCH_TARGET) as mocked:
            mocked.request.side_effect = TimeoutError("deadline exceeded")
            client = SearxngClient()
            result = client.search("q")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.errors[0].code, "timeout")
            # the surfaced message names the effective (default)
            # timeout without pinning its exact formatting
            self.assertIn(str(client.policy.timeout_s),
                          result.errors[0].message)

    def test_connection_error_grade(self):
        import http.client
        for exc in (ConnectionRefusedError("refused"),
                    http.client.BadStatusLine("garbage")):
            with self.subTest(exc=type(exc).__name__):
                with patch(PATCH_TARGET) as mocked:
                    mocked.request.side_effect = exc
                    client = SearxngClient()
                    result = client.search("q")
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.errors[0].code,
                                     "connection-error")
                    self.assertIn(type(exc).__name__,
                                  result.errors[0].message)

    def test_http_5xx_grade(self):
        with search_returning(b"boom", status=503) as client:
            result = client.search("q")
        self.assertEqual(result.errors[0].code, "http-5xx")


class ContractTests(unittest.TestCase):
    def test_query_validation_usage_errors(self):
        with search_returning(b"{}") as client:
            for bad in ("", "   ", None, 42):
                with self.subTest(query=bad):
                    with self.assertRaises(UsageError):
                        client.search(bad)
            with self.assertRaises(UsageError):
                client.search("x" * 401)

    def test_base_validation_usage_errors(self):
        for bad in ("ftp://127.0.0.1:8888",
                    "https://user:pw@127.0.0.1:8888",
                    "http://", 42):
            with self.subTest(base=bad):
                with self.assertRaises(UsageError):
                    SearxngClient(base=bad)

    def test_timeout_constructor_override(self):
        client = SearxngClient(timeout=3)
        self.assertEqual(client.policy.timeout_s, 3.0)
        with self.assertRaises(UsageError):
            SearxngClient(timeout=0)
        with self.assertRaises(UsageError):
            SearxngClient(timeout=-1)

    def test_request_path_built_by_application(self):
        body = json.dumps({"results": []}).encode()
        with patch(PATCH_TARGET) as mocked:
            mocked.request.return_value = (200, {}, body)
            client = SearxngClient(base="http://127.0.0.1:8888")
            client.search("wizznic upstream")
            args, kwargs = mocked.request.call_args
            scheme, host, port, path = args
            self.assertEqual((scheme, host, port),
                             ("http", "127.0.0.1", 8888))
            self.assertTrue(path.startswith("/search?"))
            self.assertIn("format=json", path)
            self.assertIn("q=wizznic+upstream", path)
            self.assertEqual(kwargs["timeout_s"], 10.0)

    def test_raw_body_retained_and_data_provenance(self):
        payload = {"results": [
            {"url": "https://r.example.org/a", "title": "<b>R</b>",
             "content": "third-party summary", "engines": ["google"]}],
            "unresponsive_engines": []}
        body = json.dumps(payload).encode()
        with search_returning(body) as client:
            result = client.search("q")
        self.assertEqual(result.raw_body, body)
        self.assertIn("third-party search summaries", result.note)
        self.assertIn("third-party search summaries",
                      result.rendered_text)
        data = result.to_data()
        self.assertNotIn("raw_body", data)
        self.assertNotIn("rendered_text", data)
        for key in ("query", "final_url", "http_status", "results",
                    "result_count", "deduped_count",
                    "unresponsive_engines", "number_of_results",
                    "ambiguous", "note"):
            self.assertIn(key, data)
        self.assertEqual(data["result_count"], 1)
        self.assertEqual(result.results[0]["title"], "R")


if __name__ == "__main__":
    unittest.main()
