"""JSON golden tests for the entry Results.

Each golden file under ``tests/goldens/`` is the byte-exact JSON of
one representative Result produced by the REAL entry ``run(...)`` with
mocked externals (adapter classes / model client doubles -- the same
techniques as the entry tests, whose fixtures this module
reuses). The pinned equality mode is BYTE-IDENTICAL: a golden file
contains exactly ``result.to_json()`` plus one trailing newline, and
the test compares the raw file bytes against freshly built output.

Generation procedure (goldens are NEVER hand-written):

1. ``uv run python tests/goldens/regenerate.py`` (from the repo
   root) re-runs every builder in ``GOLDEN_CASES`` through the real
   entries and overwrites the ``.json`` files with ``to_json()``
   output.
2. The diff is reviewed once (values, key order, field sets), then the
   files are pinned. Later edits to entries/serialization show up as
   byte drift and must be a deliberate re-review + regeneration.

Determinism: every builder runs in a clean temporary working directory
(no ``./.env`` pickup) and freezes the two genuinely volatile sources
of the investigate entry -- the run-directory stamp/uuid inside
``gleam.core.storage.runstore`` (patched at that module boundary) and
the entry's ``utc_now`` (feeds observation records, hence observation
content-hash ids). udd/bts/check Results are deterministic by
construction (advisory adapter meta never enters ``to_data()``).

help/version are deliberately ABSENT from the goldens: they are
informational stdout-only shells with no Result, no ``run``/JSON
rendering (labels exist, no JSON kind does). That absence is
pinned by ``test_help_and_version_have_no_json_result_surface``.

Naming: ``<module>-<kind>-<status>.json`` -- one file per reachable
status x kind combo that serialization must pin.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from gleam.core.result import DiagnosticItem, GOLDEN_FIELD_ORDER
from gleam.entries import investigate as inv_entry
from gleam.entries.bts import KIND_BUG_RESULT
from gleam.entries.check import KIND_STORE_ERROR, KIND_STORE_INCOMPLETE
from gleam.entries.check import KIND_STORE_VALID
from gleam.entries.investigate import KIND_COMPLETED, KIND_NO_FINAL_REPORT
from gleam.entries.udd import KIND_LIST_DATASETS, KIND_QUERY_RESULT
from gleam.entries.repology import KIND_REPOLOGY_RESULT
from gleam.entries import bts as bts_entry
from gleam.entries import check as check_entry
from gleam.entries import repology as repology_entry
from gleam.entries import udd as udd_entry

# Fixture doubles reused wholesale from the entry tests.
from tests.test_entry_bts import fake_bts_result
from tests.test_entry_check import run_dir as make_check_run_dir
from tests.test_entry_check import write_run_json
from tests.test_entry_investigate import (
    ScriptedModel,
    finish_from_messages,
    make_udd_result,
    model_patch,
    prose_result,
    tool_call_turn,
    udd_patch,
)
from tests.test_entry_udd import fake_udd_result
from tests.test_entry_repology import fake_repology_result

UDD_WHERE = "gleam.core.sources.udd"
BTS_WHERE = "gleam.core.sources.bts"

GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"

#: udd.data field set (both udd kinds).
UDD_DATA_FIELDS = {
    "action", "package", "release", "datasets", "rows", "row_count",
    "truncated", "more_available", "provenance",
}
#: bts.data field set (7 fields; ``attempts`` rides inside
#: errors[i].details, not here).
BTS_DATA_FIELDS = {
    "bug_id", "source", "messages", "message_count", "truncated",
    "more_available", "provenance",
}
#: repology.data field set (the project digest; the search
#: digest shares action/slug/total_entries/note and adds its own
#: fields -- the ok golden pins the project shape, the failed golden
#: carries the empty digest).
REPOLOGY_DATA_FIELDS = {
    "action", "slug", "total_entries", "versions", "debian", "note",
}
#: check.data field ORDER (ordered, not a set).
CHECK_DATA_FIELDS = [
    "store", "run", "run_count", "valid_count", "incomplete_count",
    "error_count", "runs",
]
#: investigate Result data fields (shipped shape).
INVESTIGATE_DATA_FIELDS = {
    "run_id", "store", "package", "status_detail", "detail", "counts",
    "final_report", "final_report_validation", "fallback_final_prose",
    "fact_citation_index", "context_truncations", "errors",
}

#: Fixed store/run identifiers so check/investigate data echoes are
#: reproducible byte-for-byte.
CHECK_STORE = "golden-store"
CHECK_RUN = "20260101T000000Z-wizznic-11111111-1111-4111-8111-111111111111"
INV_STORE = "mystore"
INV_RUN_STAMP = "20260101T000000Z"
INV_RUN_UUID = "11111111-1111-4111-8111-111111111111"
INV_FROZEN_NOW = "2026-01-01T00:00:00Z"


@contextmanager
def _tmp_cwd():
    """Run inside an empty temporary working directory.

    Gives the binder no ``./.env`` to pick up and keeps every relative
    store path hermetic.
    """

    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            yield tmp
        finally:
            os.chdir(cwd)


def _udd_failed_adapter():
    return fake_udd_result(
        status="failed",
        rows=[],
        provenance=[],
        row_count=0,
        errors=[DiagnosticItem(
            code="udd-connection-error",
            message="connection failed: OSError: could not connect "
                    "to the mirror",
            where=UDD_WHERE,
        )],
    )


def _bts_failed_adapter():
    return fake_bts_result(
        status="failed",
        messages=[],
        message_count=0,
        provenance=[],
        errors=[DiagnosticItem(
            code="not-found",
            message="HTTP 404 from BTS",
            where=BTS_WHERE,
        )],
    )


def build_udd_list_datasets_ok():
    """(ok, list-datasets): the pure catalog path, no adapter at all."""
    with _tmp_cwd():
        return udd_entry.run(argv=["--list-datasets"])


def build_udd_query_ok():
    with _tmp_cwd():
        with patch("gleam.entries.udd.UddClient") as client_cls:
            client_cls.return_value.query.return_value = fake_udd_result()
            return udd_entry.run(
                argv=["--package", "wizznic", "--dataset", "sources"])


def build_udd_query_partial():
    with _tmp_cwd():
        with patch("gleam.entries.udd.UddClient") as client_cls:
            client_cls.return_value.query.return_value = fake_udd_result(
                truncated=True, more_available=True)
            return udd_entry.run(
                argv=["--package", "wizznic", "--dataset", "sources"])


def build_udd_query_failed():
    with _tmp_cwd():
        with patch("gleam.entries.udd.UddClient") as client_cls:
            client_cls.return_value.query.return_value = _udd_failed_adapter()
            return udd_entry.run(
                argv=["--package", "wizznic", "--dataset", "sources"])


def build_bts_ok():
    with _tmp_cwd():
        with patch("gleam.entries.bts.BtsClient") as client_cls:
            client_cls.return_value.fetch.return_value = fake_bts_result()
            return bts_entry.run(argv=["--bug-id", "999999"])


def build_bts_partial():
    with _tmp_cwd():
        with patch("gleam.entries.bts.BtsClient") as client_cls:
            client_cls.return_value.fetch.return_value = fake_bts_result(
                truncated=True, more_available=True)
            return bts_entry.run(argv=["--bug-id", "999999"])


def build_bts_failed():
    with _tmp_cwd():
        with patch("gleam.entries.bts.BtsClient") as client_cls:
            client_cls.return_value.fetch.return_value = _bts_failed_adapter()
            return bts_entry.run(argv=["--bug-id", "999999"])


def build_repology_ok():
    with _tmp_cwd():
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = \
                fake_repology_result()
            return repology_entry.run(argv=["--project", "ncdu"])


def build_repology_failed():
    with _tmp_cwd():
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = \
                fake_repology_result(
                    status="failed",
                    digest={},
                    errors=[DiagnosticItem(
                        code="fetch-connection-error",
                        message="repology.org unreachable in test",
                        where="gleam.core.sources.repology")])
            return repology_entry.run(argv=["--project", "ncdu"])


def _check_store_with(state):
    """Create ``golden-store/<fixed-run-id>`` in the given state.

    ``state`` is one of "complete" (usable run.json), "missing"
    (no run.json at all -> run-json-missing), or "recorded" (run.json
    records the storage-error status).
    """

    run_path = make_check_run_dir(CHECK_STORE, CHECK_RUN)
    if state == "complete":
        write_run_json(run_path, {
            "run_id": CHECK_RUN,
            "package": "wizznic",
            "status": "completed",
            "observation_ids": [],
        })
    elif state == "recorded":
        write_run_json(run_path, {
            "run_id": CHECK_RUN,
            "package": "wizznic",
            "status": "storage-error",
            "observation_ids": [],
        })
    # "missing": leave the directory without a run.json.
    return run_path


def build_check_ok():
    with _tmp_cwd():
        _check_store_with("complete")
        return check_entry.run(argv=["--store", CHECK_STORE])


def build_check_partial():
    with _tmp_cwd():
        _check_store_with("missing")
        return check_entry.run(argv=["--store", CHECK_STORE])


def build_check_storage_error():
    with _tmp_cwd():
        _check_store_with("recorded")
        return check_entry.run(argv=["--store", CHECK_STORE])


def _frozen_investigate_patches():
    """Freeze the run id stamp/uuid and the entry wall clock.

    ``runstore`` builds the run directory name from ``datetime.now``
    and ``uuid.uuid4`` inside its own namespace; patching those names
    at the runstore boundary is hermetic (no global module is touched).
    The entry's ``utc_now`` import feeds observation records, so it
    must be frozen for content-hash observation ids to be stable.
    """

    frozen_dt = Mock(name="frozen-datetime")
    frozen_dt.now.return_value.strftime.return_value = INV_RUN_STAMP
    frozen_uuid = Mock(name="frozen-uuid")
    frozen_uuid.uuid4.return_value = INV_RUN_UUID
    return [
        patch("gleam.core.storage.runstore.datetime", frozen_dt),
        patch("gleam.core.storage.runstore.uuid", frozen_uuid),
        patch("gleam.entries.investigate.utc_now",
              return_value=INV_FROZEN_NOW),
    ]


def build_investigate_completed():
    """(ok, completed): fetch_udd turn + a dynamic valid finish turn
    (observation ids are content hashes; the finish builder cites the
    evidence actually present in the conversation)."""

    with _tmp_cwd():
        with ExitStack() as stack:
            for frozen in _frozen_investigate_patches():
                stack.enter_context(frozen)
            udd_cls = stack.enter_context(udd_patch())
            udd_cls.return_value.query.return_value = make_udd_result()
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_udd",
                               {"action": "fetch", "datasets": ["sources"]}),
                finish_from_messages(),
            ])
            stack.enter_context(model_patch(script))
            return inv_entry.run(
                package="wizznic", store=INV_STORE,
                model_base="http://127.0.0.1:1", model="q")


def build_investigate_no_final_report():
    """(failed, no-final-report): the model answers with prose only."""

    with _tmp_cwd():
        with ExitStack() as stack:
            for frozen in _frozen_investigate_patches():
                stack.enter_context(frozen)
            stack.enter_context(udd_patch())  # never constructed (lazy)
            script = ScriptedModel([prose_result("looks dead to me")])
            stack.enter_context(model_patch(script))
            return inv_entry.run(
                package="wizznic", store=INV_STORE,
                model_base="http://127.0.0.1:1", model="q")


#: The pinned golden registry: one entry per serialized combo.
GOLDEN_CASES = [
    {
        "name": "udd-list-datasets-ok",
        "module": "udd", "status": "ok", "kind": KIND_LIST_DATASETS,
        "build": build_udd_list_datasets_ok,
        "data_fields": UDD_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "udd-query-result-ok",
        "module": "udd", "status": "ok", "kind": KIND_QUERY_RESULT,
        "build": build_udd_query_ok,
        "data_fields": UDD_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "udd-query-result-partial",
        "module": "udd", "status": "partial", "kind": KIND_QUERY_RESULT,
        "build": build_udd_query_partial,
        "data_fields": UDD_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "udd-query-result-failed",
        "module": "udd", "status": "failed", "kind": KIND_QUERY_RESULT,
        "build": build_udd_query_failed,
        "data_fields": UDD_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "bts-bug-result-ok",
        "module": "bts", "status": "ok", "kind": KIND_BUG_RESULT,
        "build": build_bts_ok,
        "data_fields": BTS_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "bts-bug-result-partial",
        "module": "bts", "status": "partial", "kind": KIND_BUG_RESULT,
        "build": build_bts_partial,
        "data_fields": BTS_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "bts-bug-result-failed",
        "module": "bts", "status": "failed", "kind": KIND_BUG_RESULT,
        "build": build_bts_failed,
        "data_fields": BTS_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "repology-repology-result-ok",
        "module": "repology", "status": "ok",
        "kind": KIND_REPOLOGY_RESULT,
        "build": build_repology_ok,
        "data_fields": REPOLOGY_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "repology-repology-result-failed",
        "module": "repology", "status": "failed",
        "kind": KIND_REPOLOGY_RESULT,
        "build": build_repology_failed,
        "data_fields": set(), "ordered_data": False,
    },
    {
        "name": "check-store-valid-ok",
        "module": "check", "status": "ok", "kind": KIND_STORE_VALID,
        "build": build_check_ok,
        "data_fields": CHECK_DATA_FIELDS, "ordered_data": True,
    },
    {
        "name": "check-store-incomplete-partial",
        "module": "check", "status": "partial",
        "kind": KIND_STORE_INCOMPLETE,
        "build": build_check_partial,
        "data_fields": CHECK_DATA_FIELDS, "ordered_data": True,
    },
    {
        "name": "check-store-error-storage-error",
        "module": "check", "status": "storage-error",
        "kind": KIND_STORE_ERROR,
        "build": build_check_storage_error,
        "data_fields": CHECK_DATA_FIELDS, "ordered_data": True,
    },
    {
        "name": "investigate-completed-ok",
        "module": "investigate", "status": "ok", "kind": KIND_COMPLETED,
        "build": build_investigate_completed,
        "data_fields": INVESTIGATE_DATA_FIELDS, "ordered_data": False,
    },
    {
        "name": "investigate-no-final-report-failed",
        "module": "investigate", "status": "failed",
        "kind": KIND_NO_FINAL_REPORT,
        "build": build_investigate_no_final_report,
        "data_fields": INVESTIGATE_DATA_FIELDS, "ordered_data": False,
    },
]

GOLDEN_NAMES = tuple(case["name"] for case in GOLDEN_CASES)


def golden_path(name: str) -> Path:
    return GOLDENS_DIR / f"{name}.json"


def write_goldens():
    """Regenerate every golden file (used by regenerate.py only)."""

    written = []
    for case in GOLDEN_CASES:
        result = case["build"]()
        path = golden_path(case["name"])
        path.write_bytes((result.to_json() + "\n").encode("utf-8"))
        written.append(path.name)
    return written


class GoldenJsonTests(unittest.TestCase):
    """One byte-identical comparison per pinned golden file."""

    maxDiff = None

    def assertGolden(self, case):
        result = case["build"]()
        actual = (result.to_json() + "\n").encode("utf-8")
        path = golden_path(case["name"])
        self.assertTrue(
            path.is_file(),
            f"golden {path} is missing; regenerate with "
            f"`uv run python tests/goldens/regenerate.py` and review")
        expected = path.read_bytes()
        self.assertEqual(
            actual, expected,
            f"golden drift for {case['name']}: the entry serialization "
            f"changed (or the golden was tampered with); review and "
            f"regenerate deliberately")
        payload = json.loads(expected)
        # Top-level key ORDER is part of the contract.
        self.assertEqual(list(payload), list(GOLDEN_FIELD_ORDER))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["status"], case["status"])
        self.assertEqual(payload["kind"], case["kind"])
        if case["ordered_data"]:
            self.assertEqual(list(payload["data"]), case["data_fields"])
        else:
            self.assertEqual(set(payload["data"]), case["data_fields"])
        # The three structured arrays always exist.
        for array in ("errors", "diagnostics", "warnings"):
            self.assertIn(array, payload)
            for item in payload[array]:
                self.assertEqual(
                    list(item), ["code", "message", "where", "details"])


def _make_golden_test(case):
    def test(self):
        self.assertGolden(case)
    test.__name__ = f"test_golden_{case['name'].replace('-', '_')}"
    test.__doc__ = (f"Golden {case['name']}: "
                    f"{case['status']}/{case['kind']}")
    return test


for _case in GOLDEN_CASES:
    setattr(GoldenJsonTests,
            f"test_golden_{_case['name'].replace('-', '_')}",
            _make_golden_test(_case))
del _case


class GoldenRegistryTests(unittest.TestCase):
    def test_golden_files_exactly_match_the_registry(self):
        on_disk = {path.name for path in GOLDENS_DIR.glob("*.json")}
        self.assertEqual(on_disk, {f"{name}.json" for name in GOLDEN_NAMES})

    def test_goldens_are_deterministic_across_rebuilds(self):
        """The frozen-clock patching must fully de-randomize the two
        riskiest builders; a stale byte here means the goldens would
        flap instead of pinning."""
        first = build_investigate_completed().to_json()
        second = build_investigate_completed().to_json()
        self.assertEqual(first, second)
        self.assertEqual(build_check_storage_error().to_json(),
                         build_check_storage_error().to_json())

    def test_help_and_version_have_no_json_result_surface(self):
        """help/version emit stdout text only -- there is no Result and
        no JSON kind to serialize, which is WHY they are absent from
        the goldens (informational shells)."""
        from gleam.entries import help as help_entry
        from gleam.entries import version as version_entry
        for module in (help_entry, version_entry):
            for symbol in ("run", "render_json", "render_human"):
                self.assertFalse(
                    hasattr(module, symbol),
                    f"{module.__name__} must not expose {symbol}")


if __name__ == "__main__":
    unittest.main()
