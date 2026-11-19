"""Tests for gleam.core.storage: run directories, atomic writes, dedup.

Covers exclusive creation (stamp + uuid4 naming, atomic writes),
content-hash observation IDs and within-run dedup, half-run
preservation with best-effort run.json close-out, read-only structure
classification (no marker files), the pre-creation failure semantics,
and the inside-run layout (nine evidence directories,
rounds/NNNN.json, content-addressed per-source documents, the label ->
source-directory table).
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from gleam.core.storage import (
    LAYOUT_DIRS,
    REASON_RUN_JSON_MISSING,
    REASON_RUN_JSON_UNREADABLE,
    REASON_RUN_STATUS_MISSING,
    REASON_STORAGE_ERROR_RECORDED,
    REASON_TMP_RESIDUE,
    SOURCE_DIRS,
    STATE_ERROR,
    STATE_INCOMPLETE,
    STATE_VALID,
    RunStore,
    StorageError,
    infer_run,
    infer_store,
    is_run_dir,
    observation_id_for,
    source_dir_for_label,
)


class CreationTest(unittest.TestCase):
    """Naming pattern, layout, exclusivity, pre-creation failures."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_create_dir_name_pattern_and_subdirs(self):
        store = RunStore.create(self.base, package="wizznic")
        name = os.path.basename(store.run_dir)
        self.assertRegex(name, r"^\d{8}T\d{6}Z-wizznic-[0-9a-f-]{36}$")
        self.assertEqual(store.run_id, name)
        self.assertEqual(
            os.path.dirname(store.run_dir),
            self.base,  # run dirs live directly under the store
        )
        # Exactly the nine evidence directories exist.
        self.assertEqual(LAYOUT_DIRS,
                         ("rounds", "observations", "fetch_url",
                          "search_web", "github", "fetch_bug", "fetch_udd",
                          "repology", "robots"))
        self.assertEqual(set(SOURCE_DIRS) | {"rounds", "observations"},
                         set(LAYOUT_DIRS))
        self.assertEqual(sorted(os.listdir(store.run_dir)),
                         sorted(LAYOUT_DIRS))

    def test_two_consecutive_creates_never_collide_and_stay_independent(self):
        first = RunStore.create(self.base, package="wizznic")
        second = RunStore.create(self.base, package="wizznic")
        self.assertNotEqual(first.run_dir, second.run_dir)
        self.assertTrue(os.path.isdir(first.run_dir))
        self.assertTrue(os.path.isdir(second.run_dir))
        record = {"outcome": "OK", "endpoint": {"url": "https://example.org/"}}
        # Dedup is per-run: the same content is stored once in EACH run.
        self.assertEqual(first.write_observation(record),
                         second.write_observation(record))
        first.close_run(status="completed")
        second.close_run(status="budget-exhausted")
        with open(os.path.join(first.run_dir, "run.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "completed")
        with open(os.path.join(second.run_dir, "run.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "budget-exhausted")

    def test_precreation_failure_raises_storage_error_and_leaves_nothing(self):
        with patch("gleam.core.storage.runstore.os.makedirs",
                   side_effect=OSError("disk full")):
            with self.assertRaises(StorageError):
                RunStore.create(self.base, package="wizznic")
        self.assertEqual(os.listdir(self.base), [])

    def test_precreation_failure_at_subdir_cleans_partial_run_dir(self):
        real_mkdir = os.mkdir

        def fail_on_robots(path, *args, **kwargs):
            if os.path.basename(path) == "robots":
                raise OSError("cannot create robots")
            return real_mkdir(path, *args, **kwargs)

        with patch("gleam.core.storage.runstore.os.mkdir",
                   side_effect=fail_on_robots):
            with self.assertRaises(StorageError):
                RunStore.create(self.base, package="wizznic")
        self.assertEqual(os.listdir(self.base), [])

    def test_create_on_file_store_path_raises_storage_error(self):
        filepath = os.path.join(self.base, "not-a-store")
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write("occupied")
        with self.assertRaises(StorageError):
            RunStore.create(filepath, package="wizznic")
        self.assertTrue(os.path.isfile(filepath))

    def test_create_on_existing_store_keeps_prior_runs(self):
        RunStore.create(self.base, package="wizznic").close_run()
        with patch("gleam.core.storage.runstore.os.mkdir",
                   side_effect=OSError("boom")):
            with self.assertRaises(StorageError):
                RunStore.create(self.base, package="wizznic")
        runs = os.listdir(self.base)
        self.assertEqual(len(runs), 1)


class _CRLFTranslatingWriter:
    """Mimics a Windows text-mode file object: with ``newline=None``
    (the default) every LF the writer emits is translated to CRLF."""

    def __init__(self, fh):
        self._fh = fh

    def write(self, text):
        self._fh.write(text.replace("\n", "\r\n"))

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._fh.__exit__(*exc_info)


class WriteTest(unittest.TestCase):
    """Atomic writes, dedup, close-out semantics."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.store = RunStore.create(self.base, package="wizznic")

    def record(self, **overrides):
        base = {"run_id": self.store.run_id, "package": "wizznic",
                "outcome": "OK", "endpoint": {"url": "https://example.org/"},
                "observed_at": "2026-11-14T02:13:01Z"}
        base.update(overrides)
        return base

    def test_atomic_writes_leave_no_tmp_and_no_marker_files(self):
        obs_id = self.store.write_observation(self.record())
        round_path = self.store.write_round(
            1, {"kind": "investigate-round"})
        doc_ref = self.store.write_source_document(
            "fetch_url", {"url": "https://example.org/", "body": "x"})
        self.store.close_run(status="completed")
        found = set()
        for root, dirs, files in os.walk(self.store.run_dir):
            for name in files:
                found.add(os.path.relpath(os.path.join(root, name),
                                          self.store.run_dir)
                          .replace(os.sep, "/"))
        expected = {
            "run.json",
            f"observations/{obs_id}.json",
            round_path,
            doc_ref["path"],
        }
        self.assertEqual(found, expected)
        # Top level holds exactly the nine evidence subdirs plus
        # run.json: no marker/sentinel file of any kind.
        self.assertEqual(set(os.listdir(self.store.run_dir)),
                         set(LAYOUT_DIRS) | {"run.json"})

    def test_content_hash_dedup_same_content_one_file_one_id(self):
        first = self.store.write_observation(self.record())
        second = self.store.write_observation(self.record())
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        obs_dir = os.path.join(self.store.run_dir, "observations")
        # The ID is the file stem: same 64 hex characters, no prefix.
        self.assertEqual(os.listdir(obs_dir), [first + ".json"])
        with open(os.path.join(obs_dir, first + ".json"),
                  encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["observation_id"], first)

    def test_different_content_gets_different_observation_id(self):
        first = self.store.write_observation(self.record())
        other = self.store.write_observation(
            self.record(outcome="HTTP_ERROR"))
        self.assertNotEqual(first, other)
        self.assertEqual(len(os.listdir(
            os.path.join(self.store.run_dir, "observations"))), 2)

    def test_observation_id_hash_ignores_stale_id_key(self):
        self.assertEqual(observation_id_for({"a": 1, "observation_id": "x"}),
                         observation_id_for({"a": 1}))
        stale = self.store.write_observation(
            self.record(observation_id="0001"))
        path = os.path.join(self.store.run_dir, "observations",
                            stale + ".json")
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["observation_id"], stale)

    def test_run_json_carries_required_summary_fields(self):
        obs_id = self.store.write_observation(self.record())
        self.store.close_run(status="completed", counts={"rounds": 3})
        with open(os.path.join(self.store.run_dir, "run.json"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["run_id"], self.store.run_id)
        self.assertEqual(doc["package"], "wizznic")
        self.assertEqual(doc["status"], "completed")
        self.assertEqual(doc["observation_ids"], [obs_id])
        self.assertEqual(doc["counts"], {"rounds": 3})
        self.assertRegex(doc["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(doc["finished_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_json_writes_stay_lf_under_windows_text_mode_simulation(self):
        """The ``newline="\\n"`` guard keeps stored JSON LF-only even if
        the text layer would translate LF to CRLF (Windows text-mode
        default), and the file still reads back as JSON."""
        real_fdopen = os.fdopen

        def windows_text_mode_fdopen(fd, *args, **kwargs):
            fh = real_fdopen(fd, *args, **kwargs)
            if ("w" in args and "b" not in args
                    and kwargs.get("newline") is None):
                return _CRLFTranslatingWriter(fh)
            return fh

        with patch("gleam.core.storage.runstore.os.fdopen",
                   side_effect=windows_text_mode_fdopen):
            obs_id = self.store.write_observation(
                self.record(note="héllo"))
            self.store.close_run(status="completed")

        for rel_path in (f"observations/{obs_id}.json", "run.json"):
            with open(os.path.join(self.store.run_dir, rel_path),
                      "rb") as fh:
                raw = fh.read()
            self.assertNotIn(b"\r", raw,
                             f"{rel_path} contains CR bytes")
            with open(os.path.join(self.store.run_dir, rel_path),
                      encoding="utf-8") as fh:
                json.load(fh)
        with open(os.path.join(self.store.run_dir, "observations",
                               obs_id + ".json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["note"], "héllo")

    def test_postcreation_write_failure_closes_with_storage_error(self):
        real_replace = os.replace

        def fail_observation_replaces(src, dst):
            if os.path.sep + "observations" + os.path.sep in dst:
                raise OSError("replace failed")
            return real_replace(src, dst)

        with patch("gleam.core.storage.runstore.os.replace",
                   side_effect=fail_observation_replaces):
            with self.assertRaises(StorageError):
                self.store.write_observation(self.record())
        # Half-written directory preserved; best-effort close-out landed.
        self.assertTrue(os.path.isdir(self.store.run_dir))
        with open(os.path.join(self.store.run_dir, "run.json"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["status"], "storage-error")
        self.assertIn("observation write failed", doc["storage_error"]["operation"])
        info = infer_run(self.store.run_dir)
        self.assertEqual(info.state, STATE_ERROR)
        self.assertEqual(info.status, "storage-error")
        # Further writes are refused after the failure.
        with self.assertRaises(StorageError):
            self.store.write_source_document("fetch_url", {"body": "x"})
        with self.assertRaises(StorageError):
            self.store.write_round(2, {"kind": "investigate-round"})

    def test_postcreation_failure_when_closeout_also_fails(self):
        with patch("gleam.core.storage.runstore.os.replace",
                   side_effect=OSError("disk gone")):
            with self.assertRaises(StorageError) as ctx:
                self.store.write_observation(self.record())
        self.assertIn("close-out also failed", str(ctx.exception))
        self.assertTrue(os.path.isdir(self.store.run_dir))
        self.assertFalse(os.path.exists(
            os.path.join(self.store.run_dir, "run.json")))
        info = infer_run(self.store.run_dir)
        self.assertEqual(info.state, STATE_INCOMPLETE)
        self.assertIn(REASON_RUN_JSON_MISSING, info.reasons)
        self.assertIn(REASON_TMP_RESIDUE, info.reasons)

    def test_close_run_failure_raises_and_keep_directory(self):
        with patch("gleam.core.storage.runstore.os.replace",
                   side_effect=OSError("replace boom")):
            with self.assertRaises(StorageError):
                self.store.close_run(status="completed")
        self.assertTrue(os.path.isdir(self.store.run_dir))
        with self.assertRaises(StorageError):
            self.store.close_run(status="completed")

    def test_write_after_close_rejected(self):
        self.store.close_run(status="completed")
        with self.assertRaises(StorageError):
            self.store.write_observation(self.record())
        with self.assertRaises(StorageError):
            self.store.write_round(1, {"kind": "investigate-round"})
        with self.assertRaises(StorageError):
            self.store.write_source_document("fetch_url", {"body": "x"})
        with self.assertRaises(StorageError):
            self.store.close_run(status="completed")
        # Argument validation precedes the closed-state check: a bogus
        # input is ValueError even on a closed store.
        with self.assertRaises(ValueError):
            self.store.write_source_document("bogus", {})
        with self.assertRaises(ValueError):
            self.store.write_round(0, {"kind": "investigate-round"})

    def test_unsafe_identifiers_and_wrong_usage_rejected(self):
        with self.assertRaises(ValueError):
            self.store.write_source_document("bogus", {})
        with self.assertRaises(ValueError):
            self.store.write_source_document("../escape", {})
        with self.assertRaises(ValueError):
            self.store.write_round(0, {"kind": "investigate-round"})
        with self.assertRaises(ValueError):
            self.store.write_round("0001", {"kind": "investigate-round"})
        fresh = RunStore(self.base, package="wizznic")
        with self.assertRaises(RuntimeError):
            fresh.write_observation(self.record())
        with self.assertRaises(ValueError):
            RunStore(self.base, package="")
        for unsafe_package in ("../evil", ".", "..", "a/b"):
            with self.assertRaises(ValueError):
                RunStore(self.base, package=unsafe_package)


class RoundAndSourceDocumentTest(unittest.TestCase):
    """Store layout: rounds/NNNN.json write-once rounds, content-addressed
    per-source documents, and the label -> source-directory table."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.store = RunStore.create(self.base, package="wizznic")

    def test_write_round_paths_are_four_digit_padded(self):
        self.assertEqual(self.store.write_round(1, {"round": 1}),
                         "rounds/0001.json")
        self.assertEqual(self.store.write_round(7, {"round": 7}),
                         "rounds/0007.json")
        self.assertEqual(self.store.write_round(12, {"round": 12}),
                         "rounds/0012.json")
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.store.run_dir, "rounds"))),
            ["0001.json", "0007.json", "0012.json"])
        with open(os.path.join(self.store.run_dir, "rounds", "0007.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"round": 7})

    def test_write_round_is_write_once(self):
        self.store.write_round(1, {"round": 1})
        with self.assertRaises(ValueError):
            self.store.write_round(1, {"round": 1, "again": True})
        with open(os.path.join(self.store.run_dir, "rounds", "0001.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"round": 1})

    def test_source_document_canonical_bytes_name_and_content(self):
        document = {"url": "https://example.org/", "body": "x",
                    "nested": {"b": 2, "a": 1}}
        ref = self.store.write_source_document("fetch_url", document)
        blob = json.dumps(document, ensure_ascii=False, sort_keys=True,
                          allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        self.assertEqual(ref["source"], "fetch_url")
        self.assertEqual(ref["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(ref["path"],
                         f"fetch_url/{ref['sha256']}.json")
        with open(os.path.join(self.store.run_dir, ref["path"]),
                  "rb") as fh:
            self.assertEqual(fh.read(), blob)

    def test_source_document_identical_document_is_idempotent(self):
        document = {"url": "https://example.org/", "body": "x"}
        first = self.store.write_source_document("fetch_url", document)
        second = self.store.write_source_document("fetch_url", document)
        self.assertEqual(first, second)
        self.assertEqual(
            os.listdir(os.path.join(self.store.run_dir, "fetch_url")),
            [f"{first['sha256']}.json"])

    def test_source_document_same_content_different_source_both_dirs(self):
        document = {"url": "https://example.org/", "body": "x"}
        fetch_ref = self.store.write_source_document("fetch_url", document)
        github_ref = self.store.write_source_document("github", document)
        self.assertEqual(fetch_ref["sha256"], github_ref["sha256"])
        self.assertNotEqual(fetch_ref["path"], github_ref["path"])
        self.assertTrue(os.path.isfile(os.path.join(
            self.store.run_dir, fetch_ref["path"])))
        self.assertTrue(os.path.isfile(os.path.join(
            self.store.run_dir, github_ref["path"])))

    def test_source_document_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            self.store.write_source_document("bogus", {})
        with self.assertRaises(ValueError):
            self.store.write_source_document("rounds", {})
        with self.assertRaises(ValueError):
            self.store.write_source_document("observations", {})

    def test_source_dir_for_label_table(self):
        self.assertEqual(source_dir_for_label("agent-fetch"), "fetch_url")
        self.assertEqual(source_dir_for_label("agent-search"), "search_web")
        self.assertEqual(source_dir_for_label("agent-github"), "github")
        self.assertEqual(source_dir_for_label("agent-bts"), "fetch_bug")
        self.assertEqual(source_dir_for_label("agent-udd"), "fetch_udd")
        self.assertEqual(source_dir_for_label("agent-repology"),
                         "repology")
        with self.assertRaises(ValueError):
            source_dir_for_label("unknown")
        with self.assertRaises(ValueError):
            source_dir_for_label("robots")


class InferenceTest(unittest.TestCase):
    """Pure structure classification over hand-built fixtures."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.runs_root = self.base  # store root IS the runs root

    def make_run(self, run_id, run_json=None, raw_json=None,
                 tmp_residue=False):
        run_dir = os.path.join(self.runs_root, run_id)
        os.makedirs(os.path.join(run_dir, "observations"))
        if run_json is not None:
            with open(os.path.join(run_dir, "run.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(run_json, fh)
        elif raw_json is not None:
            with open(os.path.join(run_dir, "run.json"), "w",
                      encoding="utf-8") as fh:
                fh.write(raw_json)
        if tmp_residue:
            with open(os.path.join(run_dir, ".tmp-json-leftover"),
                      "w", encoding="utf-8") as fh:
                fh.write("partial")
        return run_dir

    def test_valid_run(self):
        run_dir = self.make_run(
            "20261114T021301Z-1" + "0" * 35,
            run_json={"run_id": "20261114T021301Z-1" + "0" * 35,
                      "status": "completed"})
        info = infer_run(run_dir)
        self.assertEqual(info.state, STATE_VALID)
        self.assertEqual(info.status, "completed")
        self.assertEqual(info.reasons, ())

    def test_incomplete_missing_run_json(self):
        run_dir = self.make_run("20261114T021302Z-2" + "0" * 35)
        info = infer_run(run_dir)
        self.assertEqual(info.state, STATE_INCOMPLETE)
        self.assertIsNone(info.status)
        self.assertIn(REASON_RUN_JSON_MISSING, info.reasons)

    def test_incomplete_unreadable_run_json(self):
        run_dir = self.make_run("20261114T021303Z-3" + "0" * 35,
                                raw_json="{not json")
        info = infer_run(run_dir)
        self.assertEqual(info.state, STATE_INCOMPLETE)
        self.assertIn(REASON_RUN_JSON_UNREADABLE, info.reasons)

    def test_incomplete_status_field_missing(self):
        run_dir = self.make_run("20261114T021304Z-4" + "0" * 35,
                                run_json={"package": "wizznic"})
        info = infer_run(run_dir)
        self.assertEqual(info.state, STATE_INCOMPLETE)
        self.assertIn(REASON_RUN_STATUS_MISSING, info.reasons)

    def test_incomplete_tmp_residue_despite_completed_json(self):
        run_dir = self.make_run("20261114T021305Z-5" + "0" * 35,
                                run_json={"status": "completed"},
                                tmp_residue=True)
        info = infer_run(run_dir)
        self.assertEqual(info.state, STATE_INCOMPLETE)
        self.assertEqual(info.status, "completed")
        self.assertIn(REASON_TMP_RESIDUE, info.reasons)

    def test_error_run_records_storage_error(self):
        run_dir = self.make_run(
            "20261114T021306Z-6" + "0" * 35,
            run_json={"status": "storage-error",
                      "storage_error": {"operation": "o", "error": "e"}})
        info = infer_run(run_dir)
        self.assertEqual(info.state, STATE_ERROR)
        self.assertEqual(info.status, "storage-error")
        self.assertIn(REASON_STORAGE_ERROR_RECORDED, info.reasons)

    def test_store_scan_sorts_and_ignores_stray_files(self):
        self.make_run("20261114T021301Z-a" + "0" * 35,
                      run_json={"status": "completed"})
        self.make_run("20261114T021302Z-b" + "0" * 35)
        with open(os.path.join(self.runs_root, "stray.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("ignored")
        infos = infer_store(self.base)
        self.assertEqual(
            [info.run_id for info in infos],
            ["20261114T021301Z-a" + "0" * 35,
             "20261114T021302Z-b" + "0" * 35],
        )
        self.assertEqual([info.state for info in infos],
                         [STATE_VALID, STATE_INCOMPLETE])

    def test_empty_missing_or_file_store_yields_no_runs(self):
        self.assertEqual(infer_store(self.base), [])
        self.assertEqual(infer_store(
            os.path.join(self.base, "nope")), [])
        filepath = os.path.join(self.base, "plain-file")
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.assertEqual(infer_store(filepath), [])

    def test_full_lifecycle_run_classifies_valid(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        store = RunStore.create(base, package="wizznic")
        store.write_observation({"outcome": "OK"})
        store.close_run(status="completed")
        infos = infer_store(base)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].state, STATE_VALID)
        self.assertEqual(infos[0].status, "completed")


class RunDirShapeTests(unittest.TestCase):
    """`is_run_dir`: structural run signature, never a name guess."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_run_json_is_a_signal(self):
        path = os.path.join(self.base, "a-run")
        os.makedirs(path)
        with open(os.path.join(path, "run.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertTrue(is_run_dir(path))

    def test_evidence_child_is_a_signal(self):
        for name in ("rounds", "observations", "fetch_udd"):
            with self.subTest(name=name):
                self.assertIn(name, LAYOUT_DIRS)
                path = os.path.join(self.base, f"run-{name}")
                os.makedirs(os.path.join(path, name))
                self.assertTrue(is_run_dir(path))

    def test_unrelated_layout_is_not_a_run(self):
        path = os.path.join(self.base, "not-a-run")
        os.makedirs(os.path.join(path, "src"))
        with open(os.path.join(path, "README.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")
        self.assertFalse(is_run_dir(path))

    def test_empty_directory_is_not_a_run(self):
        path = os.path.join(self.base, "empty")
        os.makedirs(path)
        self.assertFalse(is_run_dir(path))

    def test_missing_and_file_paths_are_not_runs(self):
        self.assertFalse(is_run_dir(os.path.join(self.base, "nope")))
        filepath = os.path.join(self.base, "plain-file")
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write("x")
        self.assertFalse(is_run_dir(filepath))


if __name__ == "__main__":
    unittest.main()
