"""--verbose diagnostics dump at the main() seam (plan verbose-flag
§6 / §7.7) plus the §7.4 quiet-default sweep over all six entries.

Each main() argv-sniffs ``--verbose`` without ``--json`` and dumps
one ``[module HH:MM:SS] ! {single-line sorted JSON}`` line per item
in errors + diagnostics + warnings, for every Result status, before
rendering. No real module path populates all three lists in one
Result, so the ordering pin fakes ``run`` at the udd module seam;
the real failure surfaces ride the established per-module fixtures
(udd connection failure in test_verbose_udd, investigate prose-stop
below on the ScriptedModel double). ``--verbose --json`` never
reaches the dump: the §7.2 exclusion guard exits 2 inside run()
(tested in test_verbose.py) — not retested here by design.

Zero network, zero docker, zero model.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import chdir
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gleam.core.result import DiagnosticItem, Result
from gleam.entries import bts as bts_entry
from gleam.entries import check as check_entry
from gleam.entries import investigate as investigate_entry
from gleam.entries import repology as repology_entry
from gleam.entries import searxng as searxng_entry
from gleam.entries import udd as udd_entry
from tests.test_entry_check import RUN_A, complete_run
from tests.test_entry_investigate import (
    ScriptedModel,
    finish_citing_udd,
    make_udd_result,
    model_patch,
    prose_result,
    tool_call_turn,
    udd_patch,
)
from tests.test_sources_bts import bts_route, patch_transport
from tests.test_sources_repology import NCDU_ENTRIES
from tests.test_sources_udd import (
    PATCH_TARGET,
    make_driver,
    statements_script,
)
from tests.test_searxng_lifecycle import (
    NAME,
    FakeDocker,
    default_script,
    lifecycle_env,
)
from tests.test_searxng_validate import (
    FakeHealthzOpener,
    FakeSearxngClient,
    search_result,
)
from tests.test_verbose import FIXED_TIME, run_main
from tests.test_verbose_fetcher import PUBLIC_IP, patch_env
from tests.test_verbose_investigate import entry_argv
from tests.test_verbose_repology import stub_route
from tests.test_verbose_udd import QUERY_ARGV, SRC_ROW

VALIDATE_MODULE = "gleam.core.sources.searxng.validate"
BTS_BUG = 620025
REPOLOGY_BODY = json.dumps(NCDU_ENTRIES).encode("utf-8")


def _zero_interval_env(testcase, line):
    """One temp env file isolating policy from any ./.env and pinning
    a module's throttle interval to zero (the task-4/5 entry-test
    pattern; real-client runs otherwise sleep the default)."""
    env = tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False)
    env.write(line + "\n")
    env.close()
    testcase.addCleanup(os.unlink, env.name)
    return env.name


class MainDumpTests(unittest.TestCase):
    """§7.7 — the main()-level dump: presence, ordering, shape, and
    the quiet default's absence on the same argv."""

    def _three_list_result(self):
        return Result(
            status="failed",
            kind="query-result",
            data={"action": "query", "package": "wizznic",
                  "release": None, "datasets": ["sources"], "rows": [],
                  "row_count": 0, "truncated": False},
            errors=[DiagnosticItem(
                code="udd-query-error", message="boom",
                where="gleam.core.sources.udd",
                details={"host": "udd-mirror.debian.net",
                         "port": 5432})],
            diagnostics=[DiagnosticItem(
                code="udd-slow", message="slow",
                where="gleam.core.udd",
                details={"duration_s": 9.5})],
            warnings=[DiagnosticItem(
                code="udd-truncated", message="truncated",
                where="gleam.core.udd", details={"rows": 3})],
        )

    def test_three_lists_dumped_in_order(self):
        result = self._three_list_result()
        with patch.object(udd_entry, "run", return_value=result), \
                patch("gleam.core.verbose.time.gmtime",
                      return_value=FIXED_TIME):
            code_v, out_v, err_v = run_main(
                udd_entry.main, [*QUERY_ARGV, "--verbose"])
            code_q, out_q, err_q = run_main(udd_entry.main, QUERY_ARGV)
        # errors -> diagnostics -> warnings, one sorted-JSON line each
        # with the full where/details, before rendering
        self.assertEqual(code_v, 1)
        self.assertEqual(err_v.splitlines(), [
            '[udd 12:34:56] ! {"code": "udd-query-error", '
            '"details": {"host": "udd-mirror.debian.net", '
            '"port": 5432}, "message": "boom", '
            '"where": "gleam.core.sources.udd"}',
            '[udd 12:34:56] ! {"code": "udd-slow", '
            '"details": {"duration_s": 9.5}, "message": "slow", '
            '"where": "gleam.core.udd"}',
            '[udd 12:34:56] ! {"code": "udd-truncated", '
            '"details": {"rows": 3}, "message": "truncated", '
            '"where": "gleam.core.udd"}',
        ])
        self.assertTrue(out_v.startswith("udd wizznic"))
        # the SAME run without --verbose: no dump, identical stdout,
        # identical exit code — the dump is additive only
        self.assertEqual(err_q, "")
        self.assertEqual(out_q, out_v)
        self.assertEqual(code_q, code_v)

    def test_investigate_storage_run_dumps_terminal_error(self):
        def prose_script():
            return ScriptedModel([prose_result("stopping short")])

        with tempfile.TemporaryDirectory() as store:
            with model_patch(prose_script()), \
                    patch("gleam.core.verbose.time.gmtime",
                          return_value=FIXED_TIME):
                code_v, out_v, err_v = run_main(
                    investigate_entry.main, entry_argv(store))
            with model_patch(prose_script()):
                code_q, _, err_q = run_main(
                    investigate_entry.main,
                    entry_argv(store, verbose=False))
        self.assertEqual(code_v, 1)
        lines = err_v.splitlines()
        # narration first (run dir + round lines), dump LAST; the
        # terminal error item is the run's only structured item
        self.assertEqual(lines[-1], (
            '[investigate 12:34:56] ! {"code": "no-final-report", '
            '"details": {"model_errors": 0, "rounds": 1, '
            '"tool_calls": 0}, "message": "model stopped with prose '
            'instead of calling finish_investigation; prose preserved '
            'but not accepted as a discovered report", '
            '"where": "investigate"}'))
        self.assertEqual(
            sum(1 for line in lines if "] ! {" in line), 1)
        self.assertTrue(out_v)  # render_human reached stdout
        # the same script without --verbose: zero stderr bytes
        self.assertEqual(code_q, 1)
        self.assertEqual(err_q, "")


class QuietDefaultSweepTests(unittest.TestCase):
    """§7.4 — one main()-level sweep: a full fake-transport run of
    each of the six entries with verbose unset writes zero stderr
    bytes. The per-module files pin their own smoke shapes; this is
    the cross-entry guarantee (and the main()-only guard that a
    miswired sniff cannot dump on quiet runs)."""

    def test_six_entries_quiet_zero_stderr(self):
        for module, runner in self.RUNNERS.items():
            with self.subTest(module=module):
                code, _, err = runner(self)
                self.assertEqual(err, "")
                self.assertEqual(code, 0)

    def _quiet_udd(self):
        module, _, _ = make_driver(
            script=statements_script({"sources": [SRC_ROW]}))
        with patch(PATCH_TARGET, module):
            return run_main(udd_entry.main, QUERY_ARGV)

    def _quiet_bts(self):
        env_file = _zero_interval_env(self, "GLEAM_BTS_MIN_INTERVAL=0")
        patch_transport(self, bts_route())
        return run_main(bts_entry.main,
                        ["--bug-id", str(BTS_BUG),
                         "--env-file", env_file])

    def _quiet_repology(self):
        env_file = _zero_interval_env(
            self, "GLEAM_REPOLOGY_MIN_INTERVAL=0")
        patch_env(self, {"repology.org": PUBLIC_IP},
                  stub_route(REPOLOGY_BODY))
        return run_main(repology_entry.main,
                        ["--project", "ncdu", "--env-file", env_file])

    def _quiet_check(self):
        with TemporaryDirectory() as store:
            complete_run(store, RUN_A)
            return run_main(check_entry.main, ["--store", store])

    def _quiet_investigate(self):
        script = ScriptedModel([
            tool_call_turn("q-1", "fetch_udd",
                           {"action": "fetch", "datasets": ["sources"]}),
            finish_citing_udd("q-2"),
        ])
        with TemporaryDirectory() as store:
            with udd_patch() as udd_cls, model_patch(script):
                udd_cls.return_value.query.return_value = \
                    make_udd_result()
                return run_main(investigate_entry.main,
                                entry_argv(store, verbose=False))

    def _quiet_searxng(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(chdir(tmp))
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        FakeSearxngClient.reset(search_result())
        with lifecycle_env(docker=docker), \
                patch(f"{VALIDATE_MODULE}._HEALTHZ_OPENER",
                      FakeHealthzOpener()), \
                patch(f"{VALIDATE_MODULE}.SearxngClient",
                      FakeSearxngClient):
            return run_main(searxng_entry.main, ["--validate"])

    RUNNERS = {
        "udd": _quiet_udd,
        "bts": _quiet_bts,
        "repology": _quiet_repology,
        "check": _quiet_check,
        "investigate": _quiet_investigate,
        "searxng": _quiet_searxng,
    }


if __name__ == "__main__":
    unittest.main()
