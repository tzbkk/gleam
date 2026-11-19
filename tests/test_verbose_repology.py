"""--verbose narration for the repology module (plan verbose-flag §7.5).

The client itself narrates nothing: every line comes from its
internal Fetcher (task 2), reached un-patched through the transport
stub harness (DNS at safety.socket.getaddrinfo, connections at
net._open_connection -- zero network). The project-query transcript
pins the four approved lines exactly -- exempt (tool-intrinsic, API
terms) -> GET -> dns -> response -- with budget numbers derived from
the configured cap (a non-default client cap proves the derivation;
the entry smoke derives the default 6/6 from its own bound policy).
The search path narrates the same shape for its own GET, and the
quiet default writes zero stderr bytes.

A constructor-level pin (the test_sources_repology transport-patch
pattern) guards the threading itself: the Fetcher receives the
caller's verbose callable and the exact exempt-note string.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from gleam.core.config import bind_keys
from gleam.core.sources.repology import (
    RepologyClient,
    RepologyPolicy,
    build_project_url,
    build_search_url,
)
from gleam.core.verbose import make_verbose
from gleam.entries import repology as repology_entry
from tests.test_sources_repology import NCDU_ENTRIES, PATCH_TARGET
from tests.test_transport import FakeResponse, patch_env
from tests.test_verbose import FIXED_TIME, LINE_SHAPE, run_main
from tests.test_verbose_fetcher import PUBLIC_IP, route_map

PREFIX = "[repology 12:34:56] "
PROJECT_URL = build_project_url("https://repology.org", "ncdu")
SEARCH_URL = build_search_url("https://repology.org", "ncdu")
EXEMPT_LINE = ("robots repology.org: exempt "
               "(tool-intrinsic, API terms)")

#: A one-project search payload (the exact-match shape).
SEARCH_BODY = json.dumps({
    "ncdu": [{"repo": "aur", "version": "1.21", "status": "newest",
              "visiblename": "ncdu"}],
}).encode("utf-8")


def stub_route(body: bytes):
    """Every path on repology.org answers 200 with ``body`` (the
    exempt host never asks for robots.txt)."""
    return route_map({}, default=FakeResponse(200, body=body))


class ClientTranscriptTests(unittest.TestCase):
    """§7.5 repology — the real-Fetcher transcript through the
    client's own internal fetcher: exempt host, first request, so
    the budget reads cap/cap with no robots consumption."""

    def _query(self, action, body: bytes, **policy_kwargs):
        patch_env(self, {"repology.org": PUBLIC_IP}, stub_route(body))
        sink = io.StringIO()
        policy = RepologyPolicy(min_interval_s=0.0, timeout_s=1.0,
                                **policy_kwargs)
        client = RepologyClient(policy=policy,
                                verbose=make_verbose("repology", True,
                                                     stream=sink))
        with patch("gleam.core.verbose.time.gmtime",
                   return_value=FIXED_TIME):
            result = client.query(action,
                                  **({"query": "ncdu"}
                                     if action == "search"
                                     else {"slug": "ncdu"}))
        return client, result, sink.getvalue().splitlines()

    def test_project_query_transcript_pins_all_four_lines(self):
        # non-default cap 7: the 7/7 budget proves the number derives
        # from the configured cap, not the sample's 6
        body = json.dumps(NCDU_ENTRIES).encode("utf-8")
        client, result, lines = self._query(
            "project", body, max_requests=7)
        cap = client.policy["max_requests"]
        self.assertEqual(result.status, "ok")
        self.assertEqual(lines, [
            f"{PREFIX}{EXEMPT_LINE}",
            f"{PREFIX}GET {PROJECT_URL} (budget {cap}/{cap})",
            f"{PREFIX}dns repology.org -> {PUBLIC_IP} (pinned)",
            f"{PREFIX}200 OK, {len(body)} bytes",
        ])
        self.assertEqual(client.request_count, 1)

    def test_exempt_line_carries_tool_intrinsic_note(self):
        _, result, lines = self._query("project", b"[]")
        exempt = [line for line in lines if "exempt" in line]
        self.assertEqual(len(exempt), 1)
        self.assertTrue(exempt[0].endswith(EXEMPT_LINE))
        self.assertNotIn("operator file", "".join(lines))
        self.assertEqual(result.status, "ok")

    def test_search_query_narrates_same_shape(self):
        client, result, lines = self._query("search", SEARCH_BODY)
        cap = client.policy["max_requests"]
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.digest["exact_match"])
        self.assertEqual(lines, [
            f"{PREFIX}{EXEMPT_LINE}",
            f"{PREFIX}GET {SEARCH_URL} (budget {cap}/{cap})",
            f"{PREFIX}dns repology.org -> {PUBLIC_IP} (pinned)",
            f"{PREFIX}200 OK, {len(SEARCH_BODY)} bytes",
        ])

    def test_quiet_client_writes_nothing(self):
        body = json.dumps(NCDU_ENTRIES).encode("utf-8")
        patch_env(self, {"repology.org": PUBLIC_IP}, stub_route(body))
        err = io.StringIO()
        client = RepologyClient(
            policy=RepologyPolicy(min_interval_s=0.0))
        with redirect_stderr(err):
            result = client.query("project", slug="ncdu")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(result.status, "ok")


class FetcherThreadingTests(unittest.TestCase):
    """The constructor hands its verbose callable and the exact
    tool-intrinsic exempt note to its OWN Fetcher (the client stores
    neither; narration and the label live in the fetcher)."""

    def test_fetcher_receives_verbose_and_exempt_note(self):
        sink = io.StringIO()
        verbose = make_verbose("repology", True, stream=sink)
        with patch(PATCH_TARGET) as mocked:
            RepologyClient(policy=RepologyPolicy(min_interval_s=0.0),
                           verbose=verbose)
            kwargs = mocked.Fetcher.call_args.kwargs
        self.assertIs(kwargs["verbose"], verbose)
        self.assertEqual(kwargs["exempt_note"],
                         "tool-intrinsic, API terms")

    def test_verbose_defaults_to_none(self):
        with patch(PATCH_TARGET) as mocked:
            RepologyClient(policy=RepologyPolicy(min_interval_s=0.0))
            self.assertIsNone(
                mocked.Fetcher.call_args.kwargs["verbose"])


class EntrySmokeTests(unittest.TestCase):
    """§7.5 repology — main() runs on the same transport stub
    harness; the temp env file isolates policy from any ./.env and
    pins the throttle to zero. The default cap (6) is derived via
    bind_keys, so the GET line reads the plan's sample 6/6."""

    def setUp(self):
        env = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False)
        env.write("GLEAM_REPOLOGY_MIN_INTERVAL=0\n")
        env.close()
        self.addCleanup(os.unlink, env.name)
        self.env_file = env.name

    def _run(self, argv):
        body = json.dumps(NCDU_ENTRIES).encode("utf-8")
        patch_env(self, {"repology.org": PUBLIC_IP}, stub_route(body))
        return run_main(repology_entry.main,
                        [*argv, "--env-file", self.env_file])

    def _cap(self):
        return bind_keys(
            ("GLEAM_REPOLOGY_MAX_REQUESTS",),
            env_file=self.env_file)["GLEAM_REPOLOGY_MAX_REQUESTS"]

    def test_verbose_project_pins_the_four_lines(self):
        body = json.dumps(NCDU_ENTRIES).encode("utf-8")
        code, out, err = self._run(["--project", "ncdu", "--verbose"])
        self.assertEqual(code, 0)
        cap = self._cap()
        lines = err.splitlines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertRegex(line, LINE_SHAPE)
        self.assertTrue(lines[0].endswith(EXEMPT_LINE))
        self.assertTrue(lines[1].endswith(
            f"GET {PROJECT_URL} (budget {cap}/{cap})"))
        self.assertTrue(lines[2].endswith(
            f"dns repology.org -> {PUBLIC_IP} (pinned)"))
        self.assertTrue(lines[3].endswith(
            f"200 OK, {len(body)} bytes"))
        self.assertTrue(out.startswith("repology ncdu:"))

    def test_verbose_search_same_shape(self):
        patch_env(self, {"repology.org": PUBLIC_IP},
                  stub_route(SEARCH_BODY))
        code, out, err = run_main(
            repology_entry.main,
            ["--search", "ncdu", "--verbose",
             "--env-file", self.env_file])
        self.assertEqual(code, 0)
        cap = self._cap()
        lines = err.splitlines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertRegex(line, LINE_SHAPE)
        self.assertTrue(lines[0].endswith(EXEMPT_LINE))
        self.assertTrue(lines[1].endswith(
            f"GET {SEARCH_URL} (budget {cap}/{cap})"))
        self.assertTrue(lines[2].endswith(
            f"dns repology.org -> {PUBLIC_IP} (pinned)"))
        self.assertTrue(lines[3].endswith(
            f"200 OK, {len(SEARCH_BODY)} bytes"))
        self.assertTrue(out.startswith("repology search 'ncdu':"))

    def test_quiet_default_zero_stderr(self):
        code, out, err = self._run(["--project", "ncdu"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("repology ncdu:"))


if __name__ == "__main__":
    unittest.main()
