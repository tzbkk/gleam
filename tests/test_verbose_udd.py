"""--verbose narration for the udd module (plan verbose-flag §7.5).

Client-level transcript pins (connect / per-dataset / done lines,
exact wording behind the ``[udd HH:MM:SS]`` prefix with a patched
clock) and entry-level smoke runs: ``--list-datasets`` narrates
nothing even with ``--verbose`` (offline, instant), the quiet
default writes zero stderr bytes, and a connection failure narrates
the connect line then nothing further (narration never crashes or
pre-empts the failure classification).

Zero network: the driver seam is the established
``gleam.core.sources.udd.pgdriver.pg8000`` module double.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from gleam.core.sources.udd import UddClient
from gleam.core.verbose import make_verbose
from gleam.entries import udd as udd_entry
from tests.test_sources_udd import DSN, PATCH_TARGET, make_driver
from tests.test_sources_udd import statements_script
from tests.test_verbose import FIXED_TIME, LINE_SHAPE, run_main

#: Row fixtures aligned with the datasets' registered columns.
SRC_ROW = ("wizznic", "1.0-1", "sid", "QA", "https://wz.example", "",
           "git", "https://salsa.example/w/wizznic.git",
           "https://salsa.example/w/wizznic", "games", "extra")
UP_ROW = ("wizznic", "1.0-1", "sid", "ok", "1.0",
          "https://u.example", "", "", "2026-01-01 00:00:00+00")

QUERY_ARGV = ["--package", "wizznic", "--dataset", "sources"]


class ClientTranscriptTests(unittest.TestCase):
    """§7.5 udd — the UddClient transcript: the connect line carries
    host:port from the credential-free ``dsn_display`` form (the
    fixture DSN embeds userinfo and it must not leak), per-dataset
    rows lines pluralize by count and append the truncation suffix,
    and the done line sums the shown rows across datasets."""

    def _query(self, sink, script, **kwargs):
        module, _, _ = make_driver(script=script)
        client = UddClient(
            dsn=DSN, verbose=make_verbose("udd", True, stream=sink))
        defaults = {"datasets": ["sources"], "package": "wizznic"}
        defaults.update(kwargs)
        with patch(PATCH_TARGET, module):
            return client.query(**defaults)

    def test_transcript_truncated_dataset_and_totals(self):
        sink = io.StringIO()
        with patch("gleam.core.verbose.time.gmtime",
                   return_value=FIXED_TIME):
            result = self._query(
                sink,
                statements_script({"sources": [SRC_ROW] * 3,
                                   "upstream": [UP_ROW]}),
                datasets=["sources", "upstream"], limit=2)
        self.assertEqual(sink.getvalue().splitlines(), [
            "[udd 12:34:56] connect udd-mirror.debian.net:5432",
            "[udd 12:34:56] query sources: 2 rows "
            "(truncated, more available)",
            "[udd 12:34:56] query upstream: 1 row",
            "[udd 12:34:56] query done: 3 rows total",
        ])
        # the DSN's userinfo ("udd-mirror:secret@") never leaks
        self.assertNotIn("secret", sink.getvalue())
        # narration changed nothing about the result itself
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_count, 3)

    def test_explicit_dsn_port_and_zero_row_lines(self):
        sink = io.StringIO()
        module, _, _ = make_driver(
            script=statements_script({"sources": []}))
        client = UddClient(
            dsn="postgresql://udd-mirror.debian.net:9999/udd",
            verbose=make_verbose("udd", True, stream=sink))
        with patch(PATCH_TARGET, module), \
                patch("gleam.core.verbose.time.gmtime",
                      return_value=FIXED_TIME):
            client.query(["sources"], package="wizznic")
        self.assertEqual(sink.getvalue().splitlines(), [
            "[udd 12:34:56] connect udd-mirror.debian.net:9999",
            "[udd 12:34:56] query sources: 0 rows",
            "[udd 12:34:56] query done: 0 rows total",
        ])

    def test_singular_done_line_at_one_row(self):
        sink = io.StringIO()
        with patch("gleam.core.verbose.time.gmtime",
                   return_value=FIXED_TIME):
            self._query(sink,
                        statements_script({"sources": [SRC_ROW]}))
        self.assertEqual(sink.getvalue().splitlines(), [
            "[udd 12:34:56] connect udd-mirror.debian.net:5432",
            "[udd 12:34:56] query sources: 1 row",
            "[udd 12:34:56] query done: 1 row total",
        ])


class EntrySmokeTests(unittest.TestCase):
    """§7.5 udd — entry-level runs: line shape on stderr, the
    offline catalog mode silent even with --verbose, the quiet
    default fully silent, and the failure path narrating exactly the
    connect line."""

    def test_verbose_query_run_narrates(self):
        module, _, _ = make_driver(
            script=statements_script({"sources": [SRC_ROW]}))
        with patch(PATCH_TARGET, module):
            code, out, err = run_main(udd_entry.main,
                                      [*QUERY_ARGV, "--verbose"])
        self.assertEqual(code, 0)
        lines = err.splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, LINE_SHAPE)
        self.assertTrue(lines[0].endswith(
            "connect udd-mirror.debian.net:5432"))
        self.assertTrue(lines[1].endswith("query sources: 1 row"))
        self.assertTrue(lines[2].endswith("query done: 1 row total"))
        self.assertTrue(out.startswith("udd wizznic"))

    def test_list_datasets_verbose_writes_zero_stderr(self):
        code, out, err = run_main(udd_entry.main,
                                  ["--list-datasets", "--verbose"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("udd datasets"))

    def test_quiet_default_writes_zero_stderr(self):
        module, _, _ = make_driver(
            script=statements_script({"sources": [SRC_ROW]}))
        with patch(PATCH_TARGET, module):
            code, out, err = run_main(udd_entry.main, QUERY_ARGV)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("udd wizznic"))

    def test_connection_failure_connect_line_then_error_dump(self):
        module, _, _ = make_driver(
            script=statements_script({"sources": []}),
            connect_error=RuntimeError("boom"))
        with patch(PATCH_TARGET, module):
            code, out, err = run_main(udd_entry.main,
                                      [*QUERY_ARGV, "--verbose"])
        self.assertEqual(code, 1)
        lines = err.splitlines()
        # The client narrates the connect intent, then stops; the
        # main()-level diagnostics dump (§6) adds the structured error
        # item as one "! {json}" line.
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], LINE_SHAPE)
        self.assertTrue(lines[0].endswith(
            "connect udd-mirror.debian.net:5432"))
        self.assertRegex(lines[1], r"^\[udd \d{2}:\d{2}:\d{2}\] ! \{")
        self.assertIn('"code": "udd-connection-error"', lines[1])
        self.assertIn('"where": "gleam.core.sources.udd"', lines[1])
        self.assertIn('"details": {"dsn_display":', lines[1])
        self.assertIn("boom", lines[1])


if __name__ == "__main__":
    unittest.main()
