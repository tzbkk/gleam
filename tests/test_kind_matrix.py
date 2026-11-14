"""Status x kind reachability matrix for all eight entries.

Every REACHABLE combo below is produced by the REAL entry -- ``main()``
with ``--json`` (adapter classes / model client doubled exactly like
the entry tests, whose fixtures this module reuses) -- and each
test pins three things at once:

1. the Result ``status`` and ``kind`` observed on the wire (kind is
   compared against the entry's own KIND_ constant, tying the matrix
   to the module's kind vocabulary),
2. the process exit code as an exact integer, and
3. that the exit code equals ``exit_code_for(status)`` -- exits come
   only from the frozen mapper table: ok -> 0, and each of
   partial / failed / storage-error -> 1.

Combo census (26 reachable + 2 asserted-unreachable = 28 minimum):

- udd: (ok, list-datasets), (ok, query-result), (partial,
  query-result), (failed, query-result)
- bts: (ok, bug-result), (partial, bug-result), (failed, bug-result)
- repology: (ok, repology-result), (failed, repology-result)
- check: (ok, store-valid), (partial, store-incomplete),
  (storage-error, store-error)
- investigate: (ok, completed), (partial, partial), (failed,
  no-final-report), (failed, invalid-report), (failed, model-error),
  (storage-error, storage-error)
- searxng: (ok, searxng-validated) via the faked PASS probe chain,
  (failed, searxng-validated) via a faked BLOCKER; (ok,
  searxng-started) via the faked pre-validate PASS reuse, (failed,
  searxng-started) via a faked bring-up guard refusal; (ok,
  searxng-stopped) via the faked absent no-op, (failed,
  searxng-stopped) via a faked daemon-unreachable refusal
- help: (ok, help); version: (ok, version) -- informational
  stdout-only shells: no Result, no JSON kind exists, so they
  are pinned as exit 0 == ``exit_code_for("ok")`` plus non-empty stdout
  text; this is also why they carry no JSON golden.

UNREACHABLE pair proof (udd/bts/repology x storage-error,
zero-storage entries) uses two independent layers:

- contract level: the entry module source never mentions
  ``storage-error`` at all -- its status vocabulary is built from
  the literals ok/partial/failed only (read via ``Path(__file__).read_text``
  on the entry module, no import-time magic);
- behavioral level: a transport-level adapter failure surfaces as
  status ``failed`` with lifted structured errors, and an adapter CRASH
  propagates the exception un-laundered -- neither path can mint an
  entry-level storage-error Result.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gleam.core.result import DiagnosticItem, exit_code_for
from gleam.core.sources.searxng.lifecycle import (
    CODE_DAEMON_UNREACHABLE,
    CODE_PORT_IN_USE,
    GuardFailure,
    TearDownOutcome,
)
from gleam.entries import bts as bts_module
from gleam.entries import check as check_module
from gleam.entries import investigate as inv_module
from gleam.entries import udd as udd_module
from gleam.entries.bts import KIND_BUG_RESULT, main as bts_main
from gleam.entries.check import (
    KIND_STORE_ERROR,
    KIND_STORE_INCOMPLETE,
    KIND_STORE_VALID,
)
from gleam.entries.check import main as check_main
from gleam.entries.help import main as help_main
from gleam.entries.investigate import KIND_COMPLETED, KIND_INVALID_REPORT
from gleam.entries.investigate import KIND_MODEL_ERROR, KIND_NO_FINAL_REPORT
from gleam.entries.investigate import KIND_PARTIAL
from gleam.entries.investigate import main as inv_main
from gleam.entries.udd import KIND_LIST_DATASETS, KIND_QUERY_RESULT
from gleam.entries.udd import main as udd_main
from gleam.entries.repology import KIND_REPOLOGY_RESULT
from gleam.entries.repology import main as repology_main
from gleam.entries.searxng import (
    KIND_SEARXNG_STARTED,
    KIND_SEARXNG_STOPPED,
    KIND_SEARXNG_VALIDATED,
)
from gleam.entries.searxng import main as searxng_main
from gleam.entries.version import main as version_main, version_string

# Fixture doubles reused wholesale from the entry tests.
from tests.test_entry_bts import fake_bts_result
from tests.test_entry_check import run_dir as make_check_run_dir
from tests.test_entry_check import write_run_json
from tests.test_entry_investigate import (
    ScriptedModel,
    finish_from_messages,
    forged_finish_turn,
    make_udd_result,
    model_patch,
    prose_result,
    tool_call_turn,
    transport_error_result,
    udd_patch,
)
from tests.test_entry_udd import fake_udd_result
from tests.test_entry_repology import fake_repology_result
from tests.test_searxng_validate import blocker_outcome, pass_outcome

UDD_WHERE = "gleam.core.sources.udd"
BTS_WHERE = "gleam.core.sources.bts"

INV_ARGV = ["--package", "wizznic",
            "--model-base", "http://127.0.0.1:1", "--model", "q"]

#: The frozen exit table, pinned here in one place (each combo
#: test also cross-checks its own status through ``exit_code_for``).
EXIT_TABLE = {"ok": 0, "partial": 1, "failed": 1, "storage-error": 1}


def _failed_udd_adapter():
    return fake_udd_result(
        status="failed", rows=[], provenance=[], row_count=0,
        errors=[DiagnosticItem(code="connection-error",
                               message="driver could not connect",
                               where=UDD_WHERE)])


def _failed_bts_adapter():
    return fake_bts_result(
        status="failed", messages=[], message_count=0, provenance=[],
        errors=[DiagnosticItem(code="not-found",
                               message="HTTP 404 from BTS",
                               where=BTS_WHERE)])


def _udd_main_json(adapter=None, argv=None):
    """Run the real udd main with a doubled adapter; returns
    (code, payload)."""

    args = list(argv or ["--package", "wizznic", "--dataset", "sources"])
    with patch("gleam.entries.udd.UddClient") as client_cls:
        client_cls.return_value.query.return_value = adapter
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = udd_main([*args, "--json"])
    assert err.getvalue() == "", err.getvalue()
    return code, json.loads(out.getvalue())


def _bts_main_json(adapter):
    with patch("gleam.entries.bts.BtsClient") as client_cls:
        client_cls.return_value.fetch.return_value = adapter
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bts_main(["--bug-id", "999999", "--json"])
    assert err.getvalue() == "", err.getvalue()
    return code, json.loads(out.getvalue())


def _check_store_with(state, store):
    """Create one run directory in the given state under ``store``."""

    run_path = make_check_run_dir(store, "20260101T000000Z-wizznic-1" * 4)
    if state == "complete":
        write_run_json(run_path, {"run_id": "wizznic", "package": "wizznic",
                                  "status": "completed",
                                  "observation_ids": []})
    elif state == "recorded":
        write_run_json(run_path, {"run_id": "wizznic", "package": "wizznic",
                                  "status": "storage-error",
                                  "observation_ids": []})
    # "missing": the bare directory stays without a run.json.


def _check_main_json(state):
    with tempfile.TemporaryDirectory() as store:
        _check_store_with(state, store)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check_main(["--store", store, "--json"])
    assert err.getvalue() == "", err.getvalue()
    return code, json.loads(out.getvalue())


def _inv_main_json(script, extra_argv=()):
    """Run the real investigate main with a doubled model + adapter in
    an isolated temporary store; returns (code, payload)."""

    with tempfile.TemporaryDirectory() as store:
        with udd_patch() as udd_cls:
            udd_cls.return_value.query.return_value = make_udd_result()
            with model_patch(script):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = inv_main([*INV_ARGV, "--store", store,
                                     "--json", *extra_argv])
    assert err.getvalue() == "", err.getvalue()
    return code, json.loads(out.getvalue())


class MatrixTestCase(unittest.TestCase):
    """Shared combo assertion: status + kind + exact exit + mapper."""

    def assertCombo(self, code, payload, status, kind):
        self.assertEqual(payload["status"], status)
        self.assertEqual(payload["kind"], kind)
        self.assertEqual(code, EXIT_TABLE[status])
        self.assertEqual(code, exit_code_for(payload["status"]))


class UddMatrix(MatrixTestCase):
    def test_udd_ok_list_datasets(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = udd_main(["--list-datasets", "--json"])
        payload = json.loads(out.getvalue())
        self.assertCombo(code, payload, "ok", KIND_LIST_DATASETS)

    def test_udd_ok_query_result(self):
        code, payload = _udd_main_json(fake_udd_result())
        self.assertCombo(code, payload, "ok", KIND_QUERY_RESULT)

    def test_udd_partial_query_result(self):
        code, payload = _udd_main_json(fake_udd_result(
            truncated=True, more_available=True))
        self.assertCombo(code, payload, "partial", KIND_QUERY_RESULT)

    def test_udd_failed_query_result(self):
        code, payload = _udd_main_json(_failed_udd_adapter())
        self.assertCombo(code, payload, "failed", KIND_QUERY_RESULT)


class BtsMatrix(MatrixTestCase):
    def test_bts_ok_bug_result(self):
        code, payload = _bts_main_json(fake_bts_result())
        self.assertCombo(code, payload, "ok", KIND_BUG_RESULT)

    def test_bts_partial_bug_result(self):
        code, payload = _bts_main_json(fake_bts_result(
            truncated=True, more_available=True))
        self.assertCombo(code, payload, "partial", KIND_BUG_RESULT)

    def test_bts_failed_bug_result(self):
        code, payload = _bts_main_json(_failed_bts_adapter())
        self.assertCombo(code, payload, "failed", KIND_BUG_RESULT)


class CheckMatrix(MatrixTestCase):
    def test_check_ok_store_valid(self):
        code, payload = _check_main_json("complete")
        self.assertCombo(code, payload, "ok", KIND_STORE_VALID)

    def test_check_partial_store_incomplete(self):
        code, payload = _check_main_json("missing")
        self.assertCombo(code, payload, "partial", KIND_STORE_INCOMPLETE)

    def test_check_storage_error_store_error(self):
        code, payload = _check_main_json("recorded")
        self.assertCombo(code, payload, "storage-error", KIND_STORE_ERROR)


def _repology_main_json(adapter):
    with patch("gleam.entries.repology.RepologyClient") as client_cls:
        client_cls.return_value.query.return_value = adapter
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = repology_main(["--project", "ncdu", "--json"])
    assert err.getvalue() == "", err.getvalue()
    return code, json.loads(out.getvalue())


class RepologyMatrix(MatrixTestCase):
    def test_repology_ok_repology_result(self):
        code, payload = _repology_main_json(fake_repology_result())
        self.assertCombo(code, payload, "ok", KIND_REPOLOGY_RESULT)

    def test_repology_failed_repology_result(self):
        code, payload = _repology_main_json(fake_repology_result(
            status="failed",
            errors=[DiagnosticItem(
                code="fetch-timeout", message="deadline in test",
                where="gleam.core.sources.repology")]))
        self.assertCombo(code, payload, "failed", KIND_REPOLOGY_RESULT)


class SearxngMatrix(MatrixTestCase):
    """--validate faked at the entry's run_validate seam; --start and
    --stop through the entry's lifecycle/validate seams in their ok
    and failed grades."""

    def _searxng_main_json(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = searxng_main([*argv, "--json"])
        assert err.getvalue() == "", err.getvalue()
        return code, json.loads(out.getvalue())

    def test_searxng_ok_started(self):
        with patch("gleam.entries.searxng.run_validate",
                   return_value=pass_outcome()):
            code, payload = self._searxng_main_json(["--start"])
        self.assertCombo(code, payload, "ok", KIND_SEARXNG_STARTED)

    def test_searxng_failed_started(self):
        guard = GuardFailure(CODE_PORT_IN_USE, "port in use", {})
        with patch("gleam.entries.searxng.run_validate",
                   return_value=blocker_outcome()), \
                patch("gleam.entries.searxng.bring_up",
                      return_value=guard):
            code, payload = self._searxng_main_json(["--start"])
        self.assertCombo(code, payload, "failed", KIND_SEARXNG_STARTED)

    def test_searxng_ok_stopped(self):
        with patch("gleam.entries.searxng.tear_down",
                   return_value=TearDownOutcome(
                       container="gleam-searxng",
                       action="absent-noop")):
            code, payload = self._searxng_main_json(["--stop"])
        self.assertCombo(code, payload, "ok", KIND_SEARXNG_STOPPED)

    def test_searxng_failed_stopped(self):
        guard = GuardFailure(CODE_DAEMON_UNREACHABLE,
                             "Docker daemon is not reachable.",
                             {"stderr_tail": []})
        with patch("gleam.entries.searxng.tear_down",
                   return_value=guard):
            code, payload = self._searxng_main_json(["--stop"])
        self.assertCombo(code, payload, "failed", KIND_SEARXNG_STOPPED)

    def test_searxng_ok_validated(self):
        with patch("gleam.entries.searxng.run_validate",
                   return_value=pass_outcome()):
            code, payload = self._searxng_main_json(["--validate"])
        self.assertCombo(code, payload, "ok", KIND_SEARXNG_VALIDATED)

    def test_searxng_failed_validated(self):
        with patch("gleam.entries.searxng.run_validate",
                   return_value=blocker_outcome()):
            code, payload = self._searxng_main_json(["--validate"])
        self.assertCombo(code, payload, "failed", KIND_SEARXNG_VALIDATED)


class InvestigateMatrix(MatrixTestCase):
    def test_investigate_ok_completed(self):
        script = ScriptedModel([
            tool_call_turn("c-1", "fetch_udd",
                           {"action": "fetch", "datasets": ["sources"]}),
            finish_from_messages(),
        ])
        code, payload = _inv_main_json(script)
        self.assertCombo(code, payload, "ok", KIND_COMPLETED)

    def test_investigate_partial_partial(self):
        # max rounds reached with only tool turns -> budget-exhausted.
        script = ScriptedModel([
            tool_call_turn("c-1", "fetch_udd",
                           {"action": "fetch", "datasets": ["sources"]}),
            tool_call_turn("c-2", "fetch_udd",
                           {"action": "fetch", "datasets": ["sources"]}),
        ])
        code, payload = _inv_main_json(script, ["--max-rounds", "2"])
        self.assertCombo(code, payload, "partial", KIND_PARTIAL)

    def test_investigate_failed_no_final_report(self):
        script = ScriptedModel([prose_result("looks dead to me")])
        code, payload = _inv_main_json(script)
        self.assertCombo(code, payload, "failed", KIND_NO_FINAL_REPORT)

    def test_investigate_failed_invalid_report(self):
        # Three forged finishes exceed the default repair budget (2).
        script = ScriptedModel([
            tool_call_turn("c-1", "fetch_udd",
                           {"action": "fetch", "datasets": ["sources"]}),
            forged_finish_turn("c-2"),
            forged_finish_turn("c-3"),
            forged_finish_turn("c-4"),
        ])
        code, payload = _inv_main_json(script)
        self.assertCombo(code, payload, "failed", KIND_INVALID_REPORT)

    def test_investigate_failed_model_error(self):
        script = ScriptedModel([transport_error_result()])
        code, payload = _inv_main_json(script)
        self.assertCombo(code, payload, "failed", KIND_MODEL_ERROR)

class HelpVersionMatrix(unittest.TestCase):
    """help/version: (ok, help) and (ok, version) -- no Result exists;
    the pinned surface is exit 0 (= exit_code_for("ok")) plus text."""

    def test_help_ok_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = help_main([])
        self.assertEqual(code, EXIT_TABLE["ok"])
        self.assertEqual(code, exit_code_for("ok"))
        self.assertTrue(out.getvalue().strip())

    def test_version_ok_version(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = version_main([])
        self.assertEqual(code, EXIT_TABLE["ok"])
        self.assertEqual(code, exit_code_for("ok"))
        self.assertEqual(out.getvalue(), version_string() + "\n")


class UnreachableCombos(unittest.TestCase):
    """udd/bts x storage-error is unreachable BY DESIGN (zero-storage
    entries). Proven at two layers; see module docstring."""

    def _assert_entry_never_mints_storage_error(self, module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "storage-error", source,
            f"{module.__name__} must never reference the storage-error "
            f"status (zero-storage entry)")

    def test_udd_storage_error_is_unreachable(self):
        self._assert_entry_never_mints_storage_error(udd_module)
        # Behavioral: transport-level adapter failure -> failed.
        with patch("gleam.entries.udd.UddClient") as client_cls:
            client_cls.return_value.query.return_value = _failed_udd_adapter()
            result = udd_module.run(
                argv=["--package", "wizznic", "--dataset", "sources"])
        self.assertEqual(result.status, "failed")
        self.assertNotEqual(result.status, "storage-error")
        # Behavioral: an adapter crash propagates un-laundered -- it is
        # never converted into an entry Result of any status.
        with patch("gleam.entries.udd.UddClient") as client_cls:
            client_cls.return_value.query.side_effect = OSError("boom")
            with self.assertRaises(OSError):
                udd_module.run(
                    argv=["--package", "wizznic", "--dataset", "sources"])

    def test_bts_storage_error_is_unreachable(self):
        self._assert_entry_never_mints_storage_error(bts_module)
        # Behavioral: not-found adapter failure -> failed.
        with patch("gleam.entries.bts.BtsClient") as client_cls:
            client_cls.return_value.fetch.return_value = _failed_bts_adapter()
            result = bts_module.run(argv=["--bug-id", "999999"])
        self.assertEqual(result.status, "failed")
        self.assertNotEqual(result.status, "storage-error")
        # Behavioral: an adapter crash propagates un-laundered.
        with patch("gleam.entries.bts.BtsClient") as client_cls:
            client_cls.return_value.fetch.side_effect = OSError("boom")
            with self.assertRaises(OSError):
                bts_module.run(argv=["--bug-id", "999999"])

    def test_repology_storage_error_is_unreachable(self):
        from gleam.entries import repology as repology_module
        self._assert_entry_never_mints_storage_error(repology_module)
        # Behavioral: adapter failure -> failed.
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = \
                fake_repology_result(
                    status="failed",
                    errors=[DiagnosticItem(
                        code="fetch-timeout", message="deadline",
                        where="gleam.core.sources.repology")])
            result = repology_module.run(argv=["--project", "ncdu"])
        self.assertEqual(result.status, "failed")
        # Behavioral: an adapter crash propagates un-laundered.
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.side_effect = OSError("boom")
            with self.assertRaises(OSError):
                repology_module.run(argv=["--project", "ncdu"])


if __name__ == "__main__":
    unittest.main()
