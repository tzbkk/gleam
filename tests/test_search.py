"""SearXNG 受信任适配器测试：全部合成响应（patch _request），不触网。"""

import http.client
import json
import unittest
from unittest import mock

from gleam import models as models_mod
from gleam.search import (SearxngSearch, SearchResponse, canonical_url,
                          render_results_text, strip_html,
                          validate_search_base)

BASE = "http://127.0.0.1:8888"


def client():
    return SearxngSearch(BASE, timeout_s=2.0)


def ok_body(results, unresponsive=None):
    return (json.dumps({"results": results,
                        "unresponsive_engines": unresponsive or [],
                        "number_of_results": len(results)})
            .encode())


class StripHtmlTest(unittest.TestCase):
    def test_strips_highlight_tags_and_entities(self):
        self.assertEqual(
            strip_html("<b>librcd</b> &amp; friends &#39;quoted&#39;"),
            "librcd & friends 'quoted'")

    def test_non_string_returns_empty(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(strip_html(42), "")


class CanonicalUrlTest(unittest.TestCase):
    def test_accepts_http_https_and_drops_fragment(self):
        self.assertEqual(canonical_url("HTTPS://Example.COM/a?b=1#frag"),
                         "https://example.com/a?b=1")
        self.assertEqual(canonical_url(" http://x.org/p "), "http://x.org/p")

    def test_rejects_non_web_schemes_credentials_and_bad_ports(self):
        self.assertIsNone(canonical_url("javascript:alert(1)"))
        self.assertIsNone(canonical_url("mailto:a@b.c"))
        self.assertIsNone(canonical_url("http://u:p@example.com/"))
        self.assertIsNone(canonical_url("http://example.com:99999/"))
        self.assertIsNone(canonical_url("not a url"))
        self.assertIsNone(canonical_url(""))
        self.assertIsNone(canonical_url(None))


class BaseValidationTest(unittest.TestCase):
    def test_rejects_bad_schemes_credentials_and_hosts(self):
        for bad in ("ftp://127.0.0.1:8888", "http://user:pass@127.0.0.1",
                    "http://", "http://[::1", "http://127.0.0.1:99999"):
            self.assertNotEqual(validate_search_base(bad), "", bad)
        self.assertEqual(validate_search_base(BASE), "")
        self.assertEqual(validate_search_base("https://search.example/"), "")

    def test_constructor_rejects_credentials(self):
        with self.assertRaises(ValueError):
            SearxngSearch("http://a:b@127.0.0.1:8888")


class RequestUrlTest(unittest.TestCase):
    def test_request_url_is_built_by_adapter_only(self):
        url = client().request_url("librcd debian & upstream")
        self.assertTrue(url.startswith(BASE + "/search?"))
        self.assertIn("format=json", url)
        self.assertIn("q=librcd", url)


class SearchResponseGradingTest(unittest.TestCase):
    def _search(self, status, headers, body):
        c = client()
        with mock.patch.object(c, "_request",
                               return_value=(status, headers, body)):
            return c.search("q")

    def test_success_normalizes_dedups_and_keeps_rank(self):
        body = ok_body([
            {"title": "<b>librcd</b> RusXMMS", "url": "https://a.io/x#frag",
             "content": "a <i>snippet</i>", "engines": ["duckduckgo"]},
            {"title": "Other", "url": "https://b.io/y",
             "content": "s2", "engines": ["bing"]},
            {"title": "dup of first", "url": "https://a.io/x",
             "content": "", "engines": ["wikipedia"]},
            {"title": "bad", "url": "javascript:x", "content": "",
             "engines": []},
        ], unresponsive=[["mojeek", "timeout"]])
        resp = self._search(200, {}, body)
        self.assertTrue(resp.ok)
        self.assertIsNone(resp.error)
        self.assertEqual([r["url"] for r in resp.results],
                         ["https://a.io/x", "https://b.io/y"])
        self.assertEqual(resp.results[0]["title"], "librcd RusXMMS")
        self.assertEqual(resp.results[0]["snippet"], "a snippet")
        self.assertEqual(resp.results[0]["engines"],
                         ["duckduckgo", "wikipedia"])
        self.assertEqual(resp.deduped_count, 2)
        self.assertEqual(resp.unresponsive_engines, [["mojeek", "timeout"]])
        self.assertIn("https://a.io/x", resp.rendered_text)
        self.assertIn("duckduckgo, wikipedia", resp.rendered_text)
        self.assertIn("weaker evidence", resp.rendered_text)

    def test_engine_string_field_supported(self):
        body = ok_body([{"title": "t", "url": "https://a.io/",
                         "content": "s", "engine": "mojeek"}])
        resp = self._search(200, {}, body)
        self.assertEqual(resp.results[0]["engines"], ["mojeek"])

    def test_empty_with_no_engine_failures_is_honest_no_results(self):
        resp = self._search(200, {}, ok_body([]))
        self.assertEqual(resp.results, [])
        self.assertEqual(resp.error["kind"], "no-results")

    def test_empty_with_failures_is_ambiguous_not_empty(self):
        body = ok_body([], unresponsive=[["duckduckgo", "error"],
                                         ["bing", "captcha"]])
        resp = self._search(200, {}, body)
        self.assertEqual(resp.error["kind"], "all-engines-failed-or-empty")
        self.assertIn("ambiguous", resp.error["message"])

    def test_403_reports_format_forbidden(self):
        resp = self._search(403, {}, b"forbidden")
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error["kind"], "format-forbidden")

    def test_429_reports_rate_limited_with_retry_after(self):
        resp = self._search(429, {"retry-after": ["30"]}, b"")
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error["kind"], "rate-limited")
        self.assertEqual(resp.error["retry_after"], "30")

    def test_500_reports_http_error(self):
        resp = self._search(500, {}, b"boom")
        self.assertEqual(resp.error["kind"], "http-5xx")

    def test_redirect_is_never_followed(self):
        calls = []

        def fake_request(query):
            calls.append(query)
            return 302, {"location": ["http://evil.example/steal"]}, b""

        c = client()
        with mock.patch.object(c, "_request", side_effect=fake_request):
            resp = c.search("q")
        self.assertEqual(len(calls), 1, "redirect must not be followed")
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error["kind"], "unexpected-redirect")
        self.assertIn("evil.example", resp.error["message"])

    def test_malformed_json_is_explicit(self):
        resp = self._search(200, {}, b"{not json")
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error["kind"], "malformed-json")

    def test_results_not_a_list_is_malformed(self):
        resp = self._search(200, {}, b'{"results": "nope"}')
        self.assertEqual(resp.error["kind"], "malformed-json")

    def test_nonfinite_json_rejected(self):
        resp = self._search(200, {},
                            b'{"results": [], "number_of_results": NaN}')
        self.assertEqual(resp.error["kind"], "malformed-json")

    def test_body_cap_yields_explicit_malformed_not_silence(self):
        resp = self._search(200, {}, b'{"results": [' + b"x" * (1 << 21))
        self.assertEqual(resp.error["kind"], "malformed-json")

    def test_connection_error_and_timeout_graded(self):
        c = client()
        with mock.patch.object(c, "_request",
                               side_effect=OSError("refused")):
            resp = c.search("q")
        self.assertEqual(resp.error["kind"], "connection-error")
        with mock.patch.object(c, "_request",
                               side_effect=TimeoutError("late")):
            resp = c.search("q")
        self.assertEqual(resp.error["kind"], "timeout")

    def test_http_exception_graded_as_connection_error(self):
        # BadStatusLine/IncompleteRead 是 http.client.HTTPException 而非
        # OSError：必须显式按 connection-error 分级，绝不外溢
        c = client()
        with mock.patch.object(c, "_request",
                               side_effect=http.client.BadStatusLine("")):
            resp = c.search("q")
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error["kind"], "connection-error")
        self.assertIn("BadStatusLine", resp.error["message"])
        with mock.patch.object(c, "_request",
                               side_effect=http.client.IncompleteRead(b"x")):
            resp = c.search("q")
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error["kind"], "connection-error")
        self.assertIn("IncompleteRead", resp.error["message"])

    def test_captcha_engines_surfaced_in_unresponsive(self):
        body = ok_body(
            [{"title": "t", "url": "https://ok.io/", "content": "s",
              "engines": ["duckduckgo"]}],
            unresponsive=[["brave", "captcha"]])
        resp = self._search(200, {}, body)
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.unresponsive_engines, [["brave", "captcha"]])

    def test_number_of_results_nonfinite_sanitized_to_none(self):
        # 提供商给 "NaN" 字符串：number_of_results 归 None，渲染文本无 NaN
        body = json.dumps({"results": [], "unresponsive_engines": [],
                           "number_of_results": "NaN"}).encode()
        resp = self._search(200, {}, body)
        self.assertTrue(resp.ok)
        self.assertIsNone(resp.number_of_results)
        self.assertNotIn("NaN", resp.rendered_text)
        # 非有限浮点（宽松解析混入的 NaN 字面量）同样归 None，不得外泄
        with mock.patch.object(models_mod, "_loads_strict", json.loads):
            resp2 = self._search(
                200, {}, b'{"results": [], "unresponsive_engines": [], '
                         b'"number_of_results": NaN}')
        self.assertTrue(resp2.ok)
        self.assertIsNone(resp2.number_of_results)


class RenderTextTest(unittest.TestCase):
    def test_render_contains_rank_url_snippet_and_engines(self):
        text = render_results_text(
            "q", [{"title": "T", "url": "https://a.io/", "snippet": "S",
                   "engines": ["ddg"]}], [["x", "err"]])
        self.assertIn("1. T", text)
        self.assertIn("url: https://a.io/", text)
        self.assertIn("snippet: S", text)
        self.assertIn("engines: ddg", text)
        self.assertIn("unresponsive engines:", text)
        self.assertIn("weaker evidence", text)


if __name__ == "__main__":
    unittest.main()
