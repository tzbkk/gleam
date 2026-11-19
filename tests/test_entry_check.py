"""Check entry -- read-only scan, kind/status mapping, data shape.

Real directory fixtures built in tmp dirs (no storage mocks): the
structure-inference primitives run against actual trees. Fixture
classes: complete run, run.json missing, temp-file residue,
unparseable run.json, recorded storage-error, empty store, store path
is a regular file, missing store. Every scan asserts the store tree is
byte- and mtime-identical before and after (pure read).
"""

import hashlib
import json
import os
import tempfile
import unittest

from gleam.core.result import GOLDEN_FIELD_ORDER, UsageError, exit_code_for
from gleam.core.storage import LAYOUT_DIRS
from gleam.entries.check import (
    KIND_STORE_ERROR,
    KIND_STORE_INCOMPLETE,
    KIND_STORE_VALID,
    main,
    parser,
    render_human,
    render_json,
    run,
)
from tests._helpers import capture_run

CHECK_DATA_FIELDS = [
    "store",
    "run",
    "run_count",
    "valid_count",
    "incomplete_count",
    "error_count",
    "runs",
]
RUN_ENTRY_FIELDS = ["run_id", "status", "diagnostics"]
KIND_VOCABULARY = (KIND_STORE_VALID, KIND_STORE_INCOMPLETE, KIND_STORE_ERROR)

RUN_A = "20260101T000000Z-alpha-11111111-1111-1111-1111-111111111111"
RUN_B = "20260102T000000Z-beta-22222222-2222-2222-2222-222222222222"
RUN_C = "20260103T000000Z-gamma-33333333-3333-3333-3333-333333333333"
RUN_D = "20260104T000000Z-delta-44444444-4444-4444-4444-444444444444"
RUN_F = "20260106T000000Z-zeta-66666666-6666-6666-6666-666666666666"
RUN_G = "20260107T000000Z-eta-77777777-7777-7777-7777-777777777777"


def run_dir(store, run_id):
    """Create (and return) one run directory directly under ``<store>/``."""
    path = os.path.join(store, run_id)
    os.makedirs(path, exist_ok=True)
    return path


def write_run_json(run_path, doc):
    with open(os.path.join(run_path, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def complete_run(store, run_id=RUN_A, status="completed"):
    """Fixture class 1: a complete run with a usable run.json."""
    path = run_dir(store, run_id)
    write_run_json(
        path,
        {
            "run_id": run_id,
            "package": "alpha",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:00+00:00",
            "status": status,
            "observation_ids": [],
        },
    )
    return path


def layout_run(store, run_id):
    """Complete run in the nine-directory inside-run layout.

    The nine ``LAYOUT_DIRS`` plus a hand-written run.json (classification
    rides on ``status`` and the recursive ``.tmp`` walk alone).
    """
    path = run_dir(store, run_id)
    for sub in LAYOUT_DIRS:
        os.makedirs(os.path.join(path, sub), exist_ok=True)
    write_run_json(
        path,
        {
            "run_id": run_id,
            "package": "beta",
            "started_at": "2026-01-06T00:00:00+00:00",
            "finished_at": "2026-01-06T00:01:00+00:00",
            "status": "completed",
            "observation_ids": [],
        },
    )
    return path


def run_main(argv):
    """Run main(argv) capturing stdout/stderr; returns (code, out, err)."""
    return capture_run(main, argv)


def snapshot(root):
    """Path -> (kind, content digest, size, mtime_ns) for a whole tree."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            entries[rel] = ("dir",)
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            stat = os.stat(path)
            entries[rel] = ("file", digest, stat.st_size, stat.st_mtime_ns)
    return entries


class StoreMappingTests(unittest.TestCase):
    """kind -> status mapping over the four fixture classes."""

    def test_complete_run_is_store_valid_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp)
            code, out, err = run_main(["--store", tmp, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertNotIn("\n", out.strip())  # single-line JSON
        payload = json.loads(out)
        self.assertEqual(list(payload), list(GOLDEN_FIELD_ORDER))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["kind"], KIND_STORE_VALID)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(list(payload["data"]), CHECK_DATA_FIELDS)
        self.assertEqual(payload["data"]["run_count"], 1)
        self.assertEqual(payload["data"]["valid_count"], 1)
        self.assertEqual(payload["data"]["incomplete_count"], 0)
        self.assertEqual(payload["data"]["error_count"], 0)
        entry = payload["data"]["runs"][0]
        self.assertEqual(list(entry), RUN_ENTRY_FIELDS)
        self.assertEqual(entry["run_id"], RUN_A)
        self.assertEqual(entry["status"], "completed")  # raw run status
        self.assertEqual(entry["diagnostics"], [])

    def test_dir_without_run_json_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir(tmp, RUN_B)
            code, out, _ = run_main(["--store", tmp, "--json"])
        payload = json.loads(out)
        self.assertEqual(code, exit_code_for("partial"))
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["data"]["incomplete_count"], 1)
        entry = payload["data"]["runs"][0]
        self.assertIsNone(entry["status"])
        codes = [item["code"] for item in entry["diagnostics"]]
        self.assertIn("run-json-missing", codes)

    def test_tmp_residue_is_incomplete_even_with_completed_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = complete_run(tmp, RUN_C)
            with open(
                os.path.join(path, ".tmp-json-partial"), "w", encoding="utf-8"
            ) as fh:
                fh.write('{"status": "comp')
            code, out, _ = run_main(["--store", tmp, "--json"])
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        entry = payload["data"]["runs"][0]
        # Raw status survives (only kind values are banned).
        self.assertEqual(entry["status"], "completed")
        codes = [item["code"] for item in entry["diagnostics"]]
        self.assertIn("tmp-residue", codes)

    def test_unparseable_run_json_is_incomplete_not_error(self):
        # Semantics: an unreadable summary is incomplete evidence;
        # only a RECORDED storage-error status reaches the error state.
        with tempfile.TemporaryDirectory() as tmp:
            path = run_dir(tmp, RUN_D)
            with open(
                os.path.join(path, "run.json"), "w", encoding="utf-8"
            ) as fh:
                fh.write("{not json")
            code, out, _ = run_main(["--store", tmp, "--json"])
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        self.assertEqual(payload["data"]["incomplete_count"], 1)
        self.assertEqual(payload["data"]["error_count"], 0)
        entry = payload["data"]["runs"][0]
        self.assertIsNone(entry["status"])
        codes = [item["code"] for item in entry["diagnostics"]]
        self.assertIn("run-json-unreadable", codes)

    def test_recorded_storage_error_is_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp, RUN_A, status="storage-error")
            code, out, _ = run_main(["--store", tmp, "--json"])
        payload = json.loads(out)
        self.assertEqual(code, exit_code_for("storage-error"))
        self.assertEqual(payload["kind"], KIND_STORE_ERROR)
        self.assertEqual(payload["status"], "storage-error")
        self.assertEqual(payload["data"]["error_count"], 1)
        entry = payload["data"]["runs"][0]
        self.assertEqual(entry["status"], "storage-error")
        codes = [item["code"] for item in entry["diagnostics"]]
        self.assertIn("storage-error-recorded", codes)

    def test_error_outranks_incomplete_in_mixed_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp, RUN_A)
            run_dir(tmp, RUN_B)  # incomplete
            complete_run(tmp, RUN_C, status="storage-error")
            code, out, _ = run_main(["--store", tmp, "--json"])
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], KIND_STORE_ERROR)
        self.assertEqual(payload["status"], "storage-error")
        self.assertEqual(payload["data"]["run_count"], 3)
        self.assertEqual(payload["data"]["valid_count"], 1)
        self.assertEqual(payload["data"]["incomplete_count"], 1)
        self.assertEqual(payload["data"]["error_count"], 1)


class QAScenarioTests(unittest.TestCase):
    """A frozen mixed-store scenario, verbatim semantics."""

    def test_mixed_store_partial_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp, RUN_A)  # run A: complete
            run_dir(tmp, RUN_B)  # run B: no run.json
            path = run_dir(tmp, RUN_C)  # run C: leftover .tmp file
            with open(
                os.path.join(path, ".tmp"), "w", encoding="utf-8"
            ) as fh:
                fh.write("interrupted write")
            code, out, err = run_main(["--store", tmp, "--json"])
            payload = json.loads(out)
            data = payload["data"]
            statuses = {r["run_id"]: r["status"] for r in data["runs"]}
        self.assertEqual(err, "")
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(data["run_count"], 3)
        self.assertEqual(data["valid_count"], 1)
        self.assertEqual(data["incomplete_count"], 2)
        self.assertEqual(data["error_count"], 0)
        # Sorted, stable run order (infer_store sorts by directory name).
        self.assertEqual(
            [r["run_id"] for r in data["runs"]], [RUN_A, RUN_B, RUN_C]
        )
        self.assertEqual(statuses[RUN_A], "completed")
        self.assertIsNone(statuses[RUN_B])
        self.assertIsNone(statuses[RUN_C])
        # Evidence lives in data.runs[].diagnostics, never at the top
        # level for check (the kind/status pair IS the global summary).
        self.assertEqual(payload["diagnostics"], [])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["warnings"], [])

    def test_runs_status_never_carries_kind_values(self):
        # Dedicated vocabulary ban: across a store holding every
        # fixture class, runs[].status must never contain a kind value.
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp, RUN_A)
            run_dir(tmp, RUN_B)
            run_dir(tmp, RUN_C)
            complete_run(tmp, RUN_D, status="storage-error")
            _, out, _ = run_main(["--store", tmp, "--json"])
            payload = json.loads(out)
        for entry in payload["data"]["runs"]:
            self.assertIn(
                entry["status"],
                (None, "completed", "storage-error"),
                msg=f"raw status expected, got {entry['status']!r}",
            )
            self.assertNotIn(entry["status"], KIND_VOCABULARY)


class StoreLayoutTests(unittest.TestCase):
    """Runs in the nine-directory layout through the real check entry."""

    def test_tmp_residue_in_rounds_is_incomplete_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout_run(tmp, RUN_F)
            broken = layout_run(tmp, RUN_G)
            with open(
                os.path.join(broken, "rounds", ".tmp-json-abc"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write('{"status": "comp')
            code, out, err = run_main(["--store", tmp, "--json"])
            payload = json.loads(out)
            data = payload["data"]
            entries = {r["run_id"]: r for r in data["runs"]}
        self.assertEqual(err, "")
        self.assertEqual(code, exit_code_for("partial"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(data["run_count"], 2)
        self.assertEqual(data["valid_count"], 1)
        self.assertEqual(data["incomplete_count"], 1)
        self.assertEqual(data["error_count"], 0)
        self.assertEqual(sorted(entries), [RUN_F, RUN_G])
        # The intact run stays valid; only the residue carrier flips
        # to incomplete with the tmp-residue reason.
        self.assertEqual(entries[RUN_F]["diagnostics"], [])
        broken_entry = entries[RUN_G]
        self.assertEqual(broken_entry["status"], "completed")
        codes = [item["code"] for item in broken_entry["diagnostics"]]
        self.assertEqual(codes, ["tmp-residue"])


class EmptyUnusableStoreTests(unittest.TestCase):
    """Empty/missing/unusable store -> ok with zero counts."""

    def test_empty_store_directory_is_ok_zero_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_main(["--store", tmp, "--json"])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], KIND_STORE_VALID)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["run_count"], 0)
        self.assertEqual(payload["data"]["valid_count"], 0)
        self.assertEqual(payload["data"]["incomplete_count"], 0)
        self.assertEqual(payload["data"]["error_count"], 0)
        self.assertEqual(payload["data"]["runs"], [])

class ReadOnlyScanTests(unittest.TestCase):
    """The scan modifies nothing (content, size, mtime, tree)."""

    def _rich_store(self, tmp):
        """One store touching every fixture class plus nested files."""
        path = complete_run(tmp, RUN_A)
        for sub in ("rounds", "observations", "fetch_url"):
            os.makedirs(os.path.join(path, sub), exist_ok=True)
        with open(
            os.path.join(path, "observations", "aa.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write('{"observation_id": "aa", "outcome": "OK"}')
        with open(
            os.path.join(path, "fetch_url", "body.txt"), "wb"
        ) as fh:
            fh.write(b"raw evidence bytes")
        incomplete = run_dir(tmp, RUN_B)
        with open(
            os.path.join(incomplete, ".tmp-bytes-partial"), "wb"
        ) as fh:
            fh.write(b"half-written")
        os.makedirs(
            os.path.join(incomplete, "observations"), exist_ok=True
        )
        with open(
            os.path.join(incomplete, "observations", ".tmp-json-x"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("{}")
        unparseable = run_dir(tmp, RUN_D)
        with open(
            os.path.join(unparseable, "run.json"), "w", encoding="utf-8"
        ) as fh:
            fh.write("{broken")
        complete_run(tmp, RUN_C, status="storage-error")

    def test_scan_leaves_the_store_byte_and_mtime_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._rich_store(tmp)
            before = snapshot(tmp)
            code, out, err = run_main(["--store", tmp, "--json"])
            after = snapshot(tmp)
        self.assertEqual(err, "")
        self.assertEqual(code, 1)  # error run present -> storage-error
        self.assertEqual(after, before)  # no content/size/mtime drift
        self.assertEqual(sorted(after), sorted(before))  # no new files

    def test_scan_creates_nothing_under_the_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._rich_store(tmp)
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                before = sorted(os.listdir(tmp))
                run_main(["--store", tmp, "--json"])
                self.assertEqual(sorted(os.listdir(tmp)), before)
            finally:
                os.chdir(cwd)


class BindingSurfaceTests(unittest.TestCase):
    """Flag surface: defaults and env-file resolution via the binder."""

    def test_store_defaults_from_the_closed_table(self):
        # The closed config table (spec.py) owns the default; the
        # --store flag is optional despite the surface prose.
        config = parser(argv=[])
        self.assertEqual(config.store, "runs")

    def test_store_from_env_file_selects_the_scanned_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir(tmp, RUN_B)  # incomplete
            env_path = os.path.join(tmp, "check.env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write(f"GLEAM_CHECK_STORE={tmp}\n")
            code, out, _ = run_main(["--env-file", env_path, "--json"])
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        self.assertEqual(payload["data"]["store"], tmp)

    def test_function_api_run_uses_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp)
            result = run(store=tmp)
        self.assertEqual(result.kind, KIND_STORE_VALID)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["run_count"], 1)


class UsageErrorTests(unittest.TestCase):
    """Usage failures: exit 2, stderr message, stdout untouched."""

    def test_unknown_flag(self):
        code, out, err = run_main(["--store", "x", "--nope", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

class RenderTests(unittest.TestCase):
    def test_render_json_is_single_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run(store=tmp)
        self.assertEqual(result.status, "ok")
        self.assertNotIn("\n", render_json(result))

    def test_render_human_lists_counts_and_broken_run_signs(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp, RUN_A)
            run_dir(tmp, RUN_B)
            result = run(store=tmp)
        text = render_human(result)
        # the summary counts are present; their JSON counterparts are
        # pinned in the data tests above
        self.assertRegex(text, r"\d+ runs — \d+ intact, \d+ incomplete")
        self.assertIn(RUN_B, text)
        self.assertIn("run.json", text)
        self.assertIn("interrupted before the run record", text)
        self.assertNotIn(RUN_A, text)  # complete runs stay silent


class StoreRootShapeTests(unittest.TestCase):
    """A run directory, or a directory holding no runs, is a usage error."""

    def test_run_directory_as_store_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_path = layout_run(tmp, RUN_A)
            for label, extra, err_parts in (
                    ("human", [], ("run directory", "store root")),
                    ("json", ["--json"], ())):
                with self.subTest(label):
                    code, out, err = run_main(
                        ["--store", run_path, *extra])
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertTrue(err.strip())
                    for part in err_parts:
                        self.assertIn(part, err)

    def test_function_api_raises_usage_error_for_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_path = layout_run(tmp, RUN_A)
            with self.assertRaises(UsageError):
                run(store=run_path)

    def test_directory_without_any_run_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("core", "entries", "docs"):
                child = os.path.join(tmp, name)
                os.makedirs(child)
                with open(os.path.join(child, "file.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write("x")
            code, out, err = run_main(["--store", tmp])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("does not look like an evidence store", err)

    def test_missing_empty_and_file_stores_keep_zero_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope")
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            plain = os.path.join(tmp, "plain-file")
            with open(plain, "w", encoding="utf-8") as fh:
                fh.write("x")
            for store in (missing, empty, plain):
                with self.subTest(store=store):
                    code, out, err = run_main(["--store", store, "--json"])
                    self.assertEqual(code, 0)
                    self.assertEqual(err, "")
                    payload = json.loads(out)
                    self.assertEqual(payload["kind"], KIND_STORE_VALID)
                    self.assertEqual(payload["data"]["run_count"], 0)

    def test_store_with_runs_and_stray_child_still_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp, RUN_A)
            interrupted = run_dir(tmp, RUN_B)
            os.makedirs(os.path.join(interrupted, "rounds"))
            stray = os.path.join(tmp, "archive")
            os.makedirs(stray)
            with open(os.path.join(stray, "old.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("x")
            code, out, err = run_main(["--store", tmp, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        self.assertEqual(payload["data"]["run_count"], 3)
        by_run = {entry["run_id"]: entry for entry in payload["data"]["runs"]}
        self.assertEqual(by_run[RUN_A]["diagnostics"], [])
        self.assertIn(
            "run-json-missing",
            [item["code"] for item in by_run[RUN_B]["diagnostics"]],
        )


class RunModeTests(unittest.TestCase):
    """`--run`: validate exactly one run directory."""

    def test_complete_run_is_store_valid_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = layout_run(tmp, RUN_A)
            code, out, err = run_main(["--run", path, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(list(payload), list(GOLDEN_FIELD_ORDER))
        self.assertEqual(list(payload["data"]), CHECK_DATA_FIELDS)
        self.assertEqual(payload["kind"], KIND_STORE_VALID)
        self.assertEqual(payload["data"]["store"], None)
        self.assertEqual(payload["data"]["run"], path)
        self.assertEqual(payload["data"]["run_count"], 1)
        self.assertEqual(payload["data"]["valid_count"], 1)
        self.assertEqual(
            payload["data"]["runs"][0]["run_id"], RUN_A
        )
        self.assertEqual(payload["data"]["runs"][0]["diagnostics"], [])

    def test_interrupted_run_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_dir(tmp, RUN_B)
            os.makedirs(os.path.join(path, "rounds"))
            code, out, err = run_main(["--run", path, "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["kind"], KIND_STORE_INCOMPLETE)
        self.assertEqual(payload["data"]["incomplete_count"], 1)
        self.assertIn(
            "run-json-missing",
            [item["code"] for item in payload["data"]["runs"][0]["diagnostics"]],
        )

    def test_recorded_storage_error_run_is_store_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = complete_run(tmp, RUN_C, status="storage-error")
            code, out, err = run_main(["--run", path, "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], KIND_STORE_ERROR)
        self.assertEqual(payload["data"]["error_count"], 1)

    def test_non_run_directory_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "not-a-run")
            os.makedirs(os.path.join(path, "src"))
            code, out, err = run_main(["--run", path])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not a run directory", err)

    def test_missing_and_file_paths_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "plain-file")
            with open(plain, "w", encoding="utf-8") as fh:
                fh.write("x")
            for path in (os.path.join(tmp, "nope"), plain):
                with self.subTest(path=path):
                    code, out, err = run_main(["--run", path])
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertIn("not a directory", err)

    def test_run_with_non_default_store_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = layout_run(tmp, RUN_A)
            code, out, err = run_main(["--run", path, "--store", "elsewhere"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot be combined", err)
        self.assertIn("name one scan target", err)

    def test_run_with_explicit_default_store_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = layout_run(tmp, RUN_A)
            code, out, err = run_main(["--run", path, "--store", "runs"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot be combined", err)

    def test_run_with_inline_store_form_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = layout_run(tmp, RUN_A)
            code, out, err = run_main(["--run", path, "--store=runs"])
        self.assertEqual(code, 2)
        self.assertIn("cannot be combined", err)

    def test_human_output_names_the_run_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = complete_run(tmp, RUN_A)
            code, out, err = run_main(["--run", path])
        self.assertEqual(code, 0)
        self.assertIn(f"check {RUN_A}: intact", out)
        self.assertNotIn("store=", out)

    def test_function_api_accepts_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = complete_run(tmp, RUN_A)
            result = run(run=path)
        self.assertEqual(result.kind, KIND_STORE_VALID)
        self.assertEqual(result.data["run"], path)
        self.assertEqual(result.data["run_count"], 1)


if __name__ == "__main__":
    unittest.main()
