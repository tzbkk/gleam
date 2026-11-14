"""udd entry shell -- shapes, honest statuses, usage errors, zero storage.

No network and no database anywhere: the query path is exercised through
``patch("gleam.entries.udd.UddClient")`` and the one real-adapter usage
case fails in ``validate_datasets`` before any connection could be made.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gleam.core.result import DiagnosticItem, GOLDEN_FIELD_ORDER, exit_code_for
from gleam.core.sources.udd import UddResult
from gleam.entries.udd import KIND_LIST_DATASETS, KIND_QUERY_RESULT, main, run

UDD_WHERE = "gleam.core.sources.udd"

UDD_DATA_FIELDS = {
    "action",
    "package",
    "release",
    "datasets",
    "rows",
    "row_count",
    "truncated",
    "more_available",
    "provenance",
}


def fake_udd_result(**overrides):
    """A minimal successful adapter result (columnar rows + provenance)."""
    fields = dict(
        status="ok",
        package="wizznic",
        release=None,
        datasets=("sources",),
        rows=[{"source": "wizznic", "homepage": "https://example.test"}],
        provenance=["udd:sources#row=1"],
        row_count=1,
        dataset_info={
            "sources": {
                "row_count": 1,
                "limit": 20,
                "more_available": False,
                "hidden_row_count": 0,
            }
        },
    )
    fields.update(overrides)
    return UddResult(**fields)


def run_main(argv):
    """Run main(argv) capturing stdout/stderr; returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class ListDatasetsPath(unittest.TestCase):
    """The pure catalog path: no DB, no network, no side effects."""

    def test_json_shape_is_frozen(self):
        code, out, err = run_main(["--list-datasets", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertNotIn("\n", out.strip())  # single-line JSON
        payload = json.loads(out)
        self.assertEqual(list(payload), list(GOLDEN_FIELD_ORDER))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], KIND_LIST_DATASETS)
        self.assertEqual(len(payload["data"]["datasets"]), 18)
        self.assertEqual(set(payload["data"]), UDD_DATA_FIELDS)
        self.assertEqual(payload["data"]["action"], "list-datasets")
        self.assertIsNone(payload["data"]["package"])
        self.assertEqual(payload["data"]["rows"], [])
        self.assertEqual(payload["data"]["provenance"], [])
        self.assertFalse(payload["data"]["truncated"])

    def test_exit_code_comes_from_the_mapper(self):
        code, out, _ = run_main(["--list-datasets", "--json"])
        payload = json.loads(out)
        self.assertEqual(code, exit_code_for(payload["status"]))

    def test_human_render_lists_all_names(self):
        code, out, err = run_main(["--list-datasets"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.strip())
        for name in json.loads(run_main(["--list-datasets", "--json"])[1])[
            "data"
        ]["datasets"]:
            self.assertIn(name, out)

    def test_query_inputs_conflict_with_list_mode(self):
        code, out, err = run_main(["--list-datasets", "--package", "wizznic"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")  # usage errors never emit a Result
        self.assertTrue(err.strip())


class QueryPath(unittest.TestCase):
    """Query path over a mocked ``gleam.entries.udd.UddClient``."""

    LIST_ARGV = ["--package", "wizznic", "--dataset", "sources"]

    def _patched_client(self, result):
        patcher = patch("gleam.entries.udd.UddClient")
        client_cls = patcher.start()
        client_cls.return_value.query.return_value = result
        self.addCleanup(patcher.stop)
        return client_cls

    def test_ok_result_json_shape(self):
        client_cls = self._patched_client(fake_udd_result())
        # Fresh cwd: the default dsn below is the closed-table default,
        # not whatever an operator's repo-root .env happens to set.
        with tempfile.TemporaryDirectory() as cwd, chdir(cwd):
            code, out, err = run_main([*self.LIST_ARGV, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["kind"], KIND_QUERY_RESULT)
        self.assertEqual(payload["status"], "ok")
        for field in UDD_DATA_FIELDS:
            self.assertIn(field, payload["data"])
        self.assertEqual(payload["data"]["action"], "query")
        self.assertEqual(payload["data"]["package"], "wizznic")
        self.assertEqual(payload["data"]["row_count"], 1)
        self.assertEqual(payload["data"]["provenance"], ["udd:sources#row=1"])
        self.assertFalse(payload["data"]["truncated"])
        # client wiring: default dsn from the closed table + policy
        client_cls.assert_called_once()
        kwargs = client_cls.call_args.kwargs
        self.assertEqual(
            kwargs["dsn"], "postgresql://udd-mirror.debian.net/udd"
        )
        self.assertNotIn("psql_path", kwargs)
        client_cls.return_value.query.assert_called_once_with(
            ("sources",), "wizznic", release=None, limit=None
        )

    def test_row_cap_keys_flow_into_the_policy(self):
        # GLEAM_UDD_MAX_CELL_CHARS / GLEAM_UDD_MAX_TOTAL_ROW_CHARS
        # ride the closed table into UddPolicy; a tempdir .env is the
        # env source (never the repo root).
        client_cls = self._patched_client(fake_udd_result())
        with tempfile.TemporaryDirectory() as cwd, chdir(cwd):
            Path(".env").write_text(
                "GLEAM_UDD_MAX_CELL_CHARS=200\n"
                "GLEAM_UDD_MAX_TOTAL_ROW_CHARS=4000\n",
                encoding="utf-8")
            code, out, err = run_main([*self.LIST_ARGV, "--json"])
        self.assertEqual(code, 0)
        policy = client_cls.call_args.kwargs["policy"]
        self.assertEqual(policy.max_cell_chars, 200)
        self.assertEqual(policy.max_total_row_chars, 4000)

    def test_truncation_maps_to_partial(self):
        self._patched_client(fake_udd_result(truncated=True, more_available=True))
        code, out, _ = run_main([*self.LIST_ARGV, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["status"], "partial")
        self.assertTrue(payload["data"]["truncated"])
        self.assertTrue(payload["data"]["more_available"])
        self.assertEqual(code, exit_code_for("partial"))

    def test_adapter_failure_maps_to_failed_with_lifted_errors(self):
        result = fake_udd_result(
            status="failed",
            rows=[],
            provenance=[],
            row_count=0,
            errors=[
                DiagnosticItem(
                    code="udd-connection-error",
                    message="driver failed",
                    where=UDD_WHERE,
                )
            ],
        )
        self._patched_client(result)
        code, out, _ = run_main([*self.LIST_ARGV, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["errors"][0]["code"], "udd-connection-error")
        self.assertEqual(payload["errors"][0]["where"], UDD_WHERE)
        self.assertEqual(code, exit_code_for("failed"))

    def test_function_api_run_returns_the_same_result(self):
        client_cls = self._patched_client(fake_udd_result())
        result = run(package="wizznic", datasets=("sources",))
        self.assertEqual(result.kind, KIND_QUERY_RESULT)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["action"], "query")
        client_cls.return_value.query.assert_called_once_with(
            ("sources",), "wizznic", release=None, limit=None
        )

    def test_human_render_is_nonempty_and_lists_rows(self):
        self._patched_client(fake_udd_result())
        code, out, _ = run_main(self.LIST_ARGV)
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        self.assertIn("row 1", out)
        self.assertNotIn("udd:sources#row=1", out)  # provenance: --json


class UsageErrors(unittest.TestCase):
    """Usage/config failures: exit 2, stderr message, stdout untouched."""

    def test_unknown_dataset_name_no_result_on_stdout(self):
        # Real adapter (no patch): validate_datasets raises before any
        # subprocess could spawn, so this stays offline by construction.
        code, out, err = run_main(
            ["--package", "wizznic", "--dataset", "BAD!", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_invalid_package_name_from_the_binder(self):
        code, out, err = run_main(
            ["--package", "WIZZNIC", "--dataset", "sources", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_query_mode_requires_package(self):
        code, out, err = run_main(["--dataset", "sources", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_query_mode_requires_a_dataset(self):
        code, out, err = run_main(["--package", "wizznic", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_unknown_flag(self):
        code, out, err = run_main(["--bogus"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_psql_flag_is_gone(self):
        # The psql subprocess flag left the surface with the pg8000
        # driver; a stale invocation must be an unknown-flag error.
        code, out, err = run_main(["--psql", "x", "--list-datasets"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--psql", err)


class ZeroStorage(unittest.TestCase):
    """Neither path may create anything under the working dir."""

    def test_no_files_created_under_the_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(os.listdir(tmp))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                run_main(["--list-datasets", "--json"])
                with patch("gleam.entries.udd.UddClient") as client_cls:
                    client_cls.return_value.query.return_value = (
                        fake_udd_result()
                    )
                    run_main(
                        ["--package", "wizznic", "--dataset", "sources",
                         "--json"]
                    )
            finally:
                os.chdir(cwd)
            self.assertEqual(sorted(os.listdir(tmp)), before)
            self.assertEqual(before, [])


if __name__ == "__main__":
    unittest.main()
