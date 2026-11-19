"""--verbose foundation: spec binding, --json mutual exclusion,
stderr line format, inline-value rejection, and the pure helpers in
``gleam.core.verbose`` (plan verbose-flag §7.1/§7.2/§7.3/§7.8).

No network, no storage: the exclusion guard fires inside ``run()``
before any execution, and binding tests never execute. The clock is
patched (``gleam.core.verbose.time.gmtime``) for deterministic
stamps; stderr is captured through ``stream=io.StringIO()`` or
``contextlib.redirect_stderr`` (the ``run_main`` pattern from
test_entry_meta.py).
"""

import io
import re
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from gleam.core.config import bind
from gleam.core.result import DiagnosticItem, Result
from gleam.core.verbose import (
    _quiet,
    dump_diagnostics,
    format_count,
    format_kb,
    make_verbose,
    robots_line,
)
from gleam.entries import bts as bts_entry
from gleam.entries import check as check_entry
from gleam.entries import help as help_entry
from gleam.entries import investigate as investigate_entry
from gleam.entries import repology as repology_entry
from gleam.entries import searxng as searxng_entry
from gleam.entries import udd as udd_entry
from gleam.entries import version as version_entry
from tests._helpers import capture_run as run_main

WORKING_MODULES = ("udd", "bts", "repology", "check", "investigate",
                   "searxng")
ENTRIES = {
    "udd": udd_entry,
    "bts": bts_entry,
    "repology": repology_entry,
    "check": check_entry,
    "investigate": investigate_entry,
    "searxng": searxng_entry,
}
#: One bindable argv per module; the exclusion guard fires in run()
#: before any execution, so no backend is ever touched.
VALID_ARGV = {
    "udd": ["--package", "ncdu", "--dataset", "sources"],
    "bts": ["--bug-id", "1000000"],
    "repology": ["--project", "ncdu"],
    "check": [],
    "investigate": ["--package", "ncdu", "--model", "m",
                    "--model-base", "http://127.0.0.1:8000/v1"],
    "searxng": ["--validate"],
}
LINE_SHAPE = re.compile(
    r"^\[(udd|bts|repology|check|investigate|searxng) "
    r"\d{2}:\d{2}:\d{2}\] ")
FIXED_TIME = time.struct_time(
    (2026, 11, 19, 12, 34, 56, 3, 323, 0))


class VerboseBindingTests(unittest.TestCase):
    """§7.1 — --verbose binds as a boolean flag in the six working
    modules; help and version keep rejecting it (exit 2, existing
    unknown-argument messages, no new code there)."""

    def test_default_is_false_in_six_modules(self):
        for module in WORKING_MODULES:
            with self.subTest(module=module):
                config = bind(module, argv=VALID_ARGV[module])
                self.assertIs(config.verbose, False)

    def test_flag_binds_true(self):
        for module in WORKING_MODULES:
            with self.subTest(module=module):
                config = bind(module, argv=[*VALID_ARGV[module],
                                            "--verbose"])
                self.assertIs(config.verbose, True)

    def test_help_rejects_verbose(self):
        code, out, err = run_main(help_entry.main, ["--verbose"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("unknown flag '--verbose'", err)

    def test_version_rejects_verbose(self):
        code, out, err = run_main(version_entry.main, ["--verbose"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("unknown flag '--verbose'", err)


class VerboseJsonExclusionTests(unittest.TestCase):
    """§7.2 — --verbose --json together is a UsageError on every
    working module: exit 2, both flags named, stdout untouched."""

    def test_mutually_exclusive_exit2_both_flags_named(self):
        for module in WORKING_MODULES:
            with self.subTest(module=module):
                code, out, err = run_main(
                    ENTRIES[module].main,
                    [*VALID_ARGV[module], "--verbose", "--json"])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--verbose", err)
                self.assertIn("--json", err)
                self.assertIn("mutually exclusive", err)


class InlineValueTests(unittest.TestCase):
    """§7.8 — --verbose=true is a usage error on the binder's boolean
    "takes no value" path, the same rejection --json=1 gets."""

    def test_verbose_equals_true_rejected_everywhere(self):
        for module in WORKING_MODULES:
            with self.subTest(module=module):
                code, out, err = run_main(
                    ENTRIES[module].main,
                    [*VALID_ARGV[module], "--verbose=true"])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("flag --verbose takes no value", err)

    def test_json_equals_one_is_the_same_path(self):
        code, out, err = run_main(udd_entry.main,
                                  ["--list-datasets", "--json=1"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("flag --json takes no value", err)


class MakeVerboseTests(unittest.TestCase):
    """§7.3 — the [module HH:MM:SS] stderr line format."""

    def test_line_shape_all_modules(self):
        with patch("gleam.core.verbose.time.gmtime",
                   return_value=FIXED_TIME):
            for module in WORKING_MODULES:
                with self.subTest(module=module):
                    sink = io.StringIO()
                    verbose = make_verbose(module, True, stream=sink)
                    verbose("hello")
                    verbose("two words -> 200")
                    lines = sink.getvalue().splitlines()
                    self.assertEqual(len(lines), 2)
                    for line in lines:
                        self.assertRegex(line, LINE_SHAPE)

    def test_exact_line_with_patched_clock(self):
        sink = io.StringIO()
        with patch("gleam.core.verbose.time.gmtime",
                   return_value=FIXED_TIME):
            make_verbose("udd", True, stream=sink)("connect host:5432")
        self.assertEqual(sink.getvalue(),
                         "[udd 12:34:56] connect host:5432\n")

    def test_disabled_returns_quiet_with_zero_output(self):
        sink = io.StringIO()
        verbose = make_verbose("bts", False, stream=sink)
        self.assertIs(verbose, _quiet)
        verbose("anything")
        self.assertEqual(sink.getvalue(), "")

    def test_stream_parameter_honored(self):
        sink = io.StringIO()
        make_verbose("check", True, stream=sink)("scanning runs/")
        self.assertEqual(sink.getvalue().count("] scanning runs/\n"), 1)

    def test_default_stream_resolved_at_call_time(self):
        # stream=None -> sys.stderr at CALL time, so redirect_stderr
        # captures lines written after construction.
        verbose = make_verbose("repology", True)
        err = io.StringIO()
        with redirect_stderr(err):
            verbose("exempt")
        self.assertRegex(err.getvalue(), LINE_SHAPE)


class RobotsLineTests(unittest.TestCase):
    """The shared robots narration helper: plan §5 pinned wording on
    every branch (frozen signature: robots_line(url, verdict, detail)
    — detail is the request path for disallow, the exempt note label
    for exempt, and the verdict's detail string for unknown)."""

    def test_allow(self):
        self.assertEqual(
            robots_line("https://site/x", "allow", None),
            "robots https://site/x: allow")

    def test_disallow_shows_request_path(self):
        self.assertEqual(
            robots_line("https://site/x", "disallow", "/private"),
            "robots https://site/x: disallow (path /private)")

    def test_exempt_operator_file_label(self):
        self.assertEqual(
            robots_line("site.org", "exempt", "operator file"),
            "robots site.org: exempt (operator file)")

    def test_exempt_caller_note_label(self):
        self.assertEqual(
            robots_line("repology.org", "exempt",
                        "tool-intrinsic, API terms"),
            "robots repology.org: exempt (tool-intrinsic, API terms)")

    def test_unknown_status_details_compress(self):
        # The Fetcher shapes "...; conservative abstain", bts shapes
        # "...; recorded" — both compress to "robots <status>, abstain".
        self.assertEqual(
            robots_line("https://site/x", "unknown",
                        "robots status 503; conservative abstain"),
            "robots https://site/x: unknown (robots 503, abstain)")
        self.assertEqual(
            robots_line("https://site/x", "unknown",
                        "robots status 503; recorded"),
            "robots https://site/x: unknown (robots 503, abstain)")

    def test_unknown_raw_detail_verbatim_fallback(self):
        detail = ("robots fetch failed (timeout: stalled); "
                  "conservative abstain")
        self.assertEqual(
            robots_line("https://site/x", "unknown", detail),
            f"robots https://site/x: unknown ({detail})")


class CountFormatterTests(unittest.TestCase):
    def test_edges(self):
        self.assertEqual(format_count(0), "0")
        self.assertEqual(format_count(999), "999")
        self.assertEqual(format_count(1000), "1.0k")
        self.assertEqual(format_count(1200), "1.2k")
        self.assertEqual(format_count(12453), "12.4k")
        # floored to tenths, never rounded up
        self.assertEqual(format_count(12499), "12.4k")


class KbFormatterTests(unittest.TestCase):
    def test_edges(self):
        self.assertEqual(format_kb(4096), "4.0 KB")
        self.assertEqual(format_kb(4100), "4.1 KB")
        self.assertEqual(format_kb(4199), "4.1 KB")
        self.assertEqual(format_kb(99), "0.0 KB")
        self.assertEqual(format_kb(0), "0.0 KB")


class DumpDiagnosticsTests(unittest.TestCase):
    """One line per item, errors -> diagnostics -> warnings, ``! ``
    plus single-line sorted JSON carrying the full where/details."""

    @staticmethod
    def _result():
        return Result(
            status="failed",
            kind="query-result",
            errors=[DiagnosticItem(
                code="e1", message="boom",
                where="gleam.core.sources.udd.client",
                details={"host": "udd-mirror"})],
            diagnostics=[DiagnosticItem(
                code="d1", message="slow", where="gleam.core.udd")],
            warnings=[DiagnosticItem(
                code="w1", message="truncated", where="gleam.core.udd",
                details={"rows": 3})],
        )

    def test_ordering_prefix_and_sorted_json(self):
        sink = io.StringIO()
        dump_diagnostics(self._result(),
                         make_verbose("udd", True, stream=sink))
        lines = sink.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, LINE_SHAPE)
            self.assertIn("] ! {", line)
        self.assertIn('"code": "e1"', lines[0])
        self.assertIn('"code": "d1"', lines[1])
        self.assertIn('"code": "w1"', lines[2])

    def test_full_where_and_details_single_line(self):
        sink = io.StringIO()
        dump_diagnostics(self._result(),
                         make_verbose("udd", True, stream=sink))
        first = sink.getvalue().splitlines()[0]
        expected_payload = ('{"code": "e1", '
                            '"details": {"host": "udd-mirror"}, '
                            '"message": "boom", '
                            '"where": "gleam.core.sources.udd.client"}')
        self.assertTrue(first.endswith("! " + expected_payload))

    def test_empty_lists_zero_lines(self):
        result = Result(status="ok", kind="query-result")
        sink = io.StringIO()
        dump_diagnostics(result, make_verbose("check", True,
                                              stream=sink))
        self.assertEqual(sink.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
