"""--verbose narration for the bts module (plan verbose-flag §7.5).

Client-level transcript pins: the source-choice line (auto tries soap;
an explicit source states itself), the soap ok line with count-driven
plural, the soap fail line carrying kind + message, the robots verdict
through the shared ``robots_line`` shapes (including the status-shaped
unknown compression), and the mbox GET budget / response lines -- exact
wording behind the ``[bts HH:MM:SS]`` prefix with a patched clock,
budget numbers derived from the configured request cap. Entry-level
smoke runs drive the real client through ``main()`` on the same
module-boundary fakes: both paths' line shapes, the quiet default's
zero stderr, and an explicit ``--source mbox``.

Zero network: the transport seams are the established
``gleam.core.sources.bts.client`` connection doubles.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from gleam.core.config import bind_keys
from gleam.core.sources.bts import BtsClient, build_mbox_url
from gleam.core.verbose import make_verbose
from gleam.entries import bts as bts_entry
from tests.test_sources_bts import (
    MBOX_FIXTURE,
    FakeResponse,
    bts_route,
    fast_policy,
    patch_transport,
    soap_envelope,
)
from tests.test_verbose import FIXED_TIME, LINE_SHAPE, run_main

BUG = 620025
MBOX_URL = build_mbox_url(BUG)
SOAP_FAIL_LINE = ("soap failed (transport-error: "
                  "ConnectionResetError: reset by peer)")


def fallback_route():
    """robots absent (permits fallback), soap transport failure, and a
    healthy mbox response -- the auto fallback shape."""

    def route(host, path, headers):
        if path == "/robots.txt":
            return FakeResponse(404, {}, b"")
        if path.startswith("/cgi-bin/soap.cgi"):
            raise ConnectionResetError("reset by peer")
        if path.startswith("/cgi-bin/bugreport.cgi"):
            return FakeResponse(
                200, {"content-type": ["application/mbox"]},
                MBOX_FIXTURE)
        return FakeResponse(404, {}, b"")
    return route


def robots_absent_mbox_route():
    """robots absent plus a healthy mbox response (explicit-mbox path)."""

    def route(host, path, headers):
        if path == "/robots.txt":
            return FakeResponse(404, {}, b"")
        if path.startswith("/cgi-bin/bugreport.cgi"):
            return FakeResponse(
                200, {"content-type": ["application/mbox"]},
                MBOX_FIXTURE)
        return FakeResponse(404, {}, b"")
    return route


class ClientTranscriptTests(unittest.TestCase):
    """§7.5 bts — the BtsClient transcript pins."""

    def _fetch(self, route, *, source="auto", policy=None):
        patch_transport(self, route)
        sink = io.StringIO()
        client = BtsClient(policy=policy or fast_policy(),
                           verbose=make_verbose("bts", True, stream=sink))
        with patch("gleam.core.verbose.time.gmtime",
                   return_value=FIXED_TIME):
            result = client.fetch(BUG, source=source)
        return client, result, sink.getvalue().splitlines()

    def test_soap_path_source_and_ok_lines(self):
        client, result, lines = self._fetch(bts_route())
        self.assertEqual(lines, [
            "[bts 12:34:56] source auto: trying soap",
            "[bts 12:34:56] soap get_bug: 2 messages",
        ])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source, "soap")

    def test_soap_ok_singular_at_one_message(self):
        client, result, lines = self._fetch(
            bts_route(soap_body=soap_envelope(n_messages=1).encode()))
        self.assertEqual(lines[-1], "[bts 12:34:56] soap get_bug: 1 message")

    def test_fallback_path_full_transcript(self):
        # a non-default cap proves the budget numbers derive from the
        # configured cap: robots + soap consumed 2 of 9 before the
        # mbox GET, read pre-reserve (remaining-including-this)
        client, result, lines = self._fetch(
            fallback_route(), policy=fast_policy(max_requests=9))
        cap = client.policy["max_requests"]
        self.assertEqual(lines, [
            "[bts 12:34:56] source auto: trying soap",
            f"[bts 12:34:56] {SOAP_FAIL_LINE}",
            "[bts 12:34:56] robots https://bugs.debian.org/: allow",
            f"[bts 12:34:56] GET {MBOX_URL} (budget {cap - 2}/{cap})",
            f"[bts 12:34:56] 200 OK, {len(MBOX_FIXTURE)} bytes",
            "[bts 12:34:56] mbox fetch: 3 messages",
        ])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source, "mbox")
        self.assertEqual(client.request_count, 3)

    def test_explicit_mbox_disallow_line_then_refusal(self):
        client, result, lines = self._fetch(
            bts_route(pages={"/cgi-bin/bugreport.cgi": FakeResponse(
                200, {"content-type": ["application/mbox"]},
                MBOX_FIXTURE)}),
            source="mbox")
        self.assertEqual(lines, [
            "[bts 12:34:56] source mbox",
            "[bts 12:34:56] robots https://bugs.debian.org/: disallow "
            "(path /cgi-bin/bugreport.cgi)",
        ])
        # narration only: the refusal itself is unchanged
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "robots-disallow")

    def test_auto_refusal_narrates_unknown_status_compressed(self):
        # robots 403 -> unknown; the shared helper compresses the
        # status-shaped detail ("robots status 403; recorded") to
        # "robots 403, abstain"
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(403, {}, b"")
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset by peer")
            return FakeResponse(404, {}, b"")

        client, result, lines = self._fetch(route)
        self.assertEqual(lines, [
            "[bts 12:34:56] source auto: trying soap",
            f"[bts 12:34:56] {SOAP_FAIL_LINE}",
            "[bts 12:34:56] robots https://bugs.debian.org/: unknown "
            "(robots 403, abstain)",
        ])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "transport-error")

    def test_quiet_client_writes_nothing(self):
        patch_transport(self, fallback_route())
        err = io.StringIO()
        client = BtsClient(policy=fast_policy())
        with redirect_stderr(err):
            result = client.fetch(BUG)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source, "mbox")


class EntrySmokeTests(unittest.TestCase):
    """§7.5 bts — entry-level runs through the real client on the
    module-boundary fakes; budget numbers derive from the entry's own
    configured cap (GLEAM_BTS_MAX_REQUESTS default). The env file
    pins the throttle interval to zero so the runs never sleep."""

    def setUp(self):
        env = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False)
        env.write("GLEAM_BTS_MIN_INTERVAL=0\n")
        env.close()
        self.addCleanup(os.unlink, env.name)
        self.env_file = env.name

    def _argv(self, *flags):
        return ["--bug-id", str(BUG), "--env-file", self.env_file, *flags]

    def _cap(self):
        return bind_keys(("GLEAM_BTS_MAX_REQUESTS",),
                         env_file=self.env_file)["GLEAM_BTS_MAX_REQUESTS"]

    def test_verbose_soap_path(self):
        patch_transport(self, bts_route())
        code, out, err = run_main(bts_entry.main,
                                  self._argv("--verbose"))
        self.assertEqual(code, 0)
        lines = err.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(line, LINE_SHAPE)
        self.assertTrue(lines[0].endswith("source auto: trying soap"))
        self.assertTrue(lines[1].endswith("soap get_bug: 2 messages"))
        self.assertTrue(out.startswith("bts #620025"))

    def test_verbose_fallback_path(self):
        patch_transport(self, fallback_route())
        code, out, err = run_main(bts_entry.main,
                                  self._argv("--verbose"))
        self.assertEqual(code, 0)
        cap = self._cap()
        lines = err.splitlines()
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertRegex(line, LINE_SHAPE)
        self.assertTrue(lines[0].endswith("source auto: trying soap"))
        self.assertTrue(lines[1].endswith(SOAP_FAIL_LINE))
        self.assertTrue(lines[2].endswith(
            "robots https://bugs.debian.org/: allow"))
        self.assertTrue(lines[3].endswith(
            f"GET {MBOX_URL} (budget {cap - 2}/{cap})"))
        self.assertTrue(lines[4].endswith(
            f"200 OK, {len(MBOX_FIXTURE)} bytes"))
        self.assertTrue(lines[5].endswith("mbox fetch: 3 messages"))
        self.assertIn("3 messages", out)

    def test_quiet_default_zero_stderr(self):
        patch_transport(self, fallback_route())
        code, out, err = run_main(bts_entry.main, self._argv())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("bts #620025"))

    def test_explicit_source_mbox_narrates_source_line(self):
        patch_transport(self, robots_absent_mbox_route())
        code, out, err = run_main(
            bts_entry.main, self._argv("--source", "mbox", "--verbose"))
        self.assertEqual(code, 0)
        cap = self._cap()
        lines = err.splitlines()
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertRegex(line, LINE_SHAPE)
        self.assertTrue(lines[0].endswith("source mbox"))
        self.assertTrue(lines[1].endswith(
            "robots https://bugs.debian.org/: allow"))
        # robots consumed the only prior request: 5/6 on the default
        # cap is the plan's sample number, derived not hardcoded
        self.assertTrue(lines[2].endswith(
            f"GET {MBOX_URL} (budget {cap - 1}/{cap})"))
        self.assertTrue(lines[3].endswith(
            f"200 OK, {len(MBOX_FIXTURE)} bytes"))
        self.assertTrue(lines[4].endswith("mbox fetch: 3 messages"))


if __name__ == "__main__":
    unittest.main()
