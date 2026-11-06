"""Tests for gleam.core.result: Result shell, diagnostics, exit mapping.

Covers the frozen contract: golden JSON field
order, fixed schema_version, construction-time validation of status and
kind, the DiagnosticItem required-field contract, the provisional
status-to-exit-code mapping, the usage-error path (exit 2, no Result),
and the warnings/diagnostics downgrade of ``ok`` to ``partial``.
"""

import json
import unittest
from dataclasses import replace

from gleam.core.result import (
    GOLDEN_FIELD_ORDER,
    SCHEMA_VERSION,
    DiagnosticItem,
    Result,
    UsageError,
    exit_code_for,
    finalize,
)


def make_item(**overrides):
    """Build a DiagnosticItem with valid defaults plus overrides."""
    base = {"code": "some-code", "message": "some message", "where": "some-module"}
    base.update(overrides)
    return DiagnosticItem(**base)


class GoldenSerializationTest(unittest.TestCase):
    """Stable JSON shape: field order and fixed schema version."""

    def test_top_level_key_order_matches_golden_contract(self):
        result = Result(
            status="failed",
            kind="bug-result",
            data={"bug_id": 7},
            errors=[make_item()],
            diagnostics=[make_item()],
            warnings=[make_item()],
        )
        payload = json.loads(result.to_json())
        self.assertEqual(list(payload.keys()), list(GOLDEN_FIELD_ORDER))
        self.assertEqual(
            list(payload.keys()),
            [
                "schema_version",
                "status",
                "kind",
                "data",
                "errors",
                "diagnostics",
                "warnings",
            ],
        )

    def test_schema_version_is_fixed_to_one(self):
        result = Result(status="ok", kind="help")
        self.assertEqual(result.schema_version, SCHEMA_VERSION)
        self.assertEqual(json.loads(result.to_json())["schema_version"], 1)

    def test_schema_version_cannot_be_overridden(self):
        with self.assertRaises(ValueError):
            Result(status="ok", kind="help", schema_version=2)

    def test_diagnostic_items_serialize_with_fixed_key_order(self):
        result = Result(
            status="ok", kind="help", warnings=[make_item(details={"k": [1]})]
        )
        payload = json.loads(result.to_json())
        self.assertEqual(
            list(payload["warnings"][0].keys()),
            ["code", "message", "where", "details"],
        )

    def test_to_json_is_deterministic_across_calls(self):
        result = Result(status="storage-error", kind="storage-error", data={"run": "r"})
        self.assertEqual(result.to_json(), result.to_json())


class StatusValidationTest(unittest.TestCase):
    """status is restricted to the four contract values."""

    def test_all_four_contract_statuses_are_accepted(self):
        for status in ("ok", "partial", "failed", "storage-error"):
            self.assertEqual(Result(status=status, kind="help").status, status)

    def test_invalid_status_is_rejected_at_construction(self):
        for bad in ("warning", "error", "OK", "ok ", "", None, 1):
            with self.assertRaises(ValueError):
                Result(status=bad, kind="help")


class KindValidationTest(unittest.TestCase):
    """kind must be kebab-case; vocabularies belong to modules."""

    def test_kebab_case_kinds_are_accepted(self):
        for kind in (
            "help",
            "version",
            "list-datasets",
            "store-incomplete",
            "no-final-report",
        ):
            Result(status="ok", kind=kind)

    def test_non_kebab_case_kind_is_rejected(self):
        for bad in ("Help", "help_text", "help--me", "-help", "help-", "", "h elp", None):
            with self.assertRaises(ValueError):
                Result(status="ok", kind=bad)


class DiagnosticItemValidationTest(unittest.TestCase):
    """DiagnosticItem fixed keys and required fields."""

    def test_missing_or_empty_code_is_rejected(self):
        with self.assertRaises(ValueError):
            DiagnosticItem(message="m", where="w")
        with self.assertRaises(ValueError):
            make_item(code="")

    def test_missing_or_empty_where_is_rejected(self):
        with self.assertRaises(ValueError):
            DiagnosticItem(code="c", message="m")
        with self.assertRaises(ValueError):
            make_item(where="")

    def test_missing_or_empty_message_is_rejected(self):
        with self.assertRaises(ValueError):
            DiagnosticItem(code="c", where="w")
        with self.assertRaises(ValueError):
            make_item(message="")

    def test_non_string_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            make_item(code=42)
        with self.assertRaises(ValueError):
            make_item(message=42)
        with self.assertRaises(ValueError):
            make_item(where=42)

    def test_details_defaults_to_empty_dict(self):
        item = DiagnosticItem(code="c", message="m", where="w")
        self.assertEqual(item.details, {})

    def test_details_accepts_arbitrary_json_values(self):
        for value in ({"a": [1, {"b": None}]}, [1, "two", None, True], "flat", 7, None):
            self.assertEqual(make_item(details=value).details, value)

    def test_non_serializable_details_are_rejected(self):
        with self.assertRaises(ValueError):
            make_item(details={"bad": object()})
        with self.assertRaises(ValueError):
            make_item(details={"nan": float("nan")})
        with self.assertRaises(ValueError):
            make_item(details={1, 2})


class ResultArrayValidationTest(unittest.TestCase):
    """The three diagnostic arrays only accept DiagnosticItem entries."""

    def test_string_entries_in_diagnostic_arrays_are_rejected(self):
        with self.assertRaises(ValueError):
            Result(status="failed", kind="bug-result", errors=["boom"])
        with self.assertRaises(ValueError):
            Result(status="ok", kind="help", diagnostics=["note"])
        with self.assertRaises(ValueError):
            Result(status="ok", kind="help", warnings=["careful"])


class ExitMappingTest(unittest.TestCase):
    """Provisional status-to-exit mapping."""

    def test_mapper_covers_all_four_statuses(self):
        self.assertEqual(exit_code_for("ok"), 0)
        self.assertEqual(exit_code_for("partial"), 1)
        self.assertEqual(exit_code_for("failed"), 1)
        self.assertEqual(exit_code_for("storage-error"), 1)

    def test_mapper_rejects_unknown_statuses(self):
        for bad in ("warning", "", None):
            with self.assertRaises(ValueError):
                exit_code_for(bad)


class UsageErrorPathTest(unittest.TestCase):
    """Usage/config errors exit 2 without producing a Result."""

    def test_usage_error_is_a_distinct_type_outside_the_result_flow(self):
        # the real contract: not a ValueError (exit-2 mapping distinguishes
        # usage errors from value-validation errors)
        self.assertFalse(issubclass(UsageError, ValueError))

    def test_usage_error_carries_exit_code_two_and_stderr_message(self):
        err = UsageError("unknown dataset 'nope'")
        self.assertEqual(err.exit_code, 2)
        self.assertEqual(str(err), "unknown dataset 'nope'")

class FinalizeDowngradeTest(unittest.TestCase):
    """warnings/diagnostics downgrade a usable ok Result."""

    def test_ok_with_warnings_is_downgraded_to_partial(self):
        result = Result(
            status="ok",
            kind="query-result",
            data={"rows": [1]},
            warnings=[make_item()],
        )
        final = finalize(result)
        self.assertEqual(final.status, "partial")
        self.assertEqual(final.warnings, result.warnings)
        self.assertEqual(final.data, result.data)

    def test_ok_with_diagnostics_is_downgraded_to_partial(self):
        result = Result(
            status="ok",
            kind="bug-result",
            data={"bug_id": 5},
            diagnostics=[make_item()],
        )
        self.assertEqual(finalize(result).status, "partial")

    def test_failed_with_warnings_stays_failed(self):
        result = Result(
            status="failed",
            kind="bug-result",
            errors=[make_item()],
            warnings=[make_item()],
        )
        self.assertEqual(finalize(result).status, "failed")

    def test_ok_with_empty_arrays_stays_ok(self):
        result = Result(status="ok", kind="help", data={})
        self.assertEqual(finalize(result).status, "ok")

    def test_finalize_downgrade_never_mutates_its_input(self):
        result = Result(
            status="ok",
            kind="query-result",
            data={"rows": [1]},
            warnings=[make_item()],
        )
        final = finalize(result)
        self.assertEqual(result.status, "ok")
        self.assertEqual(final, replace(result, status="partial"))


if __name__ == "__main__":
    unittest.main()
