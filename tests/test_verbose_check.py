"""--verbose narration for the check entry (plan verbose-flag §7.5,
check bullet): scanning / per-run / scan-done stderr lines whose
reason vocabulary is the renderer's own ``_REASON_TEXT`` (one shared
join, so the two surfaces cannot drift).

Fake stores are real directory trees under temp dirs (the
test_entry_check fixture builders); the repo's real ``./runs`` store
is never touched. The clock is patched
(``gleam.core.verbose.time.gmtime``) for deterministic stamps; stderr
is captured through the ``run_main`` redirect pattern.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from gleam.entries.check import main as check_main
from tests.test_entry_check import (
    RUN_A,
    RUN_B,
    RUN_C,
    RUN_D,
    RUN_F,
    RUN_G,
    complete_run,
    run_dir,
    snapshot,
    write_run_json,
)
from tests.test_verbose import FIXED_TIME, run_main

#: Every narrated line under the patched clock carries this prefix.
PREFIX = "[check 12:34:56] "


def mixed_store(tmp):
    """One store covering every reason code plus one intact run.

    Sorted-by-name scan order: A intact, B run-json-missing, C
    tmp-residue, D run-json-unreadable, F run-status-missing, G
    recorded storage-error.
    """
    complete_run(tmp, RUN_A)
    run_dir(tmp, RUN_B)
    residue = complete_run(tmp, RUN_C)
    with open(
        os.path.join(residue, ".tmp-json-x"), "w", encoding="utf-8"
    ) as fh:
        fh.write("{}")
    unreadable = run_dir(tmp, RUN_D)
    with open(
        os.path.join(unreadable, "run.json"), "w", encoding="utf-8"
    ) as fh:
        fh.write("{not json")
    statusless = run_dir(tmp, RUN_F)
    write_run_json(statusless, {"run_id": RUN_F})
    complete_run(tmp, RUN_G, status="storage-error")


def expected_mixed_lines(store):
    """The exact §7.5 transcript for :func:`mixed_store`."""
    return [
        f"{PREFIX}scanning {store}/: 6 run directories",
        f"{PREFIX}run {RUN_A}: intact",
        f"{PREFIX}run {RUN_B}: incomplete (no run.json (interrupted "
        f"before the run record was written))",
        f"{PREFIX}run {RUN_C}: incomplete (temp-file residue from an "
        f"interrupted write)",
        f"{PREFIX}run {RUN_D}: incomplete (run.json is unreadable "
        f"(corrupt or half-written))",
        f"{PREFIX}run {RUN_F}: incomplete (run.json has no completion "
        f"status)",
        f"{PREFIX}run {RUN_G}: broken (run recorded a storage error)",
        f"{PREFIX}scan done: 6 runs, 4 incomplete, 1 broken",
    ]


class StoreScanTranscriptTests(unittest.TestCase):
    """§7.5 check (a): scanning + per-run + scan-done lines pinned
    exactly; reason parentheticals come straight from the renderer
    vocabulary."""

    def test_mixed_store_transcript_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixed_store(tmp)
            before = snapshot(tmp)
            with patch("gleam.core.verbose.time.gmtime",
                       return_value=FIXED_TIME):
                code, out, err = run_main(
                    check_main, ["--store", tmp, "--verbose"])
            after = snapshot(tmp)
        self.assertEqual(code, 1)  # broken + incomplete runs present
        self.assertEqual(err.splitlines(), expected_mixed_lines(tmp))
        self.assertTrue(out)  # human stdout stays the normal surface
        # Narration is purely additive: the read-only scan stays
        # byte- and mtime-identical.
        self.assertEqual(after, before)

    def test_single_run_store_singular_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            complete_run(tmp, RUN_A)
            with patch("gleam.core.verbose.time.gmtime",
                       return_value=FIXED_TIME):
                code, _, err = run_main(
                    check_main, ["--store", tmp, "--verbose"])
        self.assertEqual(code, 0)
        self.assertEqual(err.splitlines(), [
            f"{PREFIX}scanning {tmp}/: 1 run directory",
            f"{PREFIX}run {RUN_A}: intact",
            f"{PREFIX}scan done: 1 run, 0 incomplete, 0 broken",
        ])

    def test_empty_store_zero_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            with patch("gleam.core.verbose.time.gmtime",
                       return_value=FIXED_TIME):
                code, _, err = run_main(
                    check_main, ["--store", empty, "--verbose"])
        self.assertEqual(code, 0)
        self.assertEqual(err.splitlines(), [
            f"{PREFIX}scanning {empty}/: 0 run directories",
            f"{PREFIX}scan done: 0 runs, 0 incomplete, 0 broken",
        ])


class RunModeTranscriptTests(unittest.TestCase):
    """§7.5 check (b): --run narrates exactly the one per-run line;
    no directory enumeration happens, so no scanning/scan-done
    lines."""

    def test_intact_run_single_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = complete_run(tmp, RUN_A)
            with patch("gleam.core.verbose.time.gmtime",
                       return_value=FIXED_TIME):
                code, _, err = run_main(
                    check_main, ["--run", path, "--verbose"])
        self.assertEqual(code, 0)
        self.assertEqual(err.splitlines(),
                         [f"{PREFIX}run {RUN_A}: intact"])

    def test_incomplete_run_single_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_dir(tmp, RUN_B)
            os.makedirs(os.path.join(path, "rounds"))
            with patch("gleam.core.verbose.time.gmtime",
                       return_value=FIXED_TIME):
                code, _, err = run_main(
                    check_main, ["--run", path, "--verbose"])
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), [
            f"{PREFIX}run {RUN_B}: incomplete (no run.json (interrupted "
            f"before the run record was written))",
        ])

    def test_usage_error_never_narrates(self):
        with tempfile.TemporaryDirectory() as tmp:
            stray = os.path.join(tmp, "not-a-run")
            os.makedirs(os.path.join(stray, "src"))
            with patch("gleam.core.verbose.time.gmtime",
                       return_value=FIXED_TIME):
                code, _, err = run_main(
                    check_main, ["--run", stray, "--verbose"])
        self.assertEqual(code, 2)
        # Only the usage message: no [check HH:MM:SS] lines.
        self.assertNotIn("[check ", err)
        self.assertIn("not a run directory", err)


class QuietDefaultTests(unittest.TestCase):
    """§7.5 check (c): without --verbose the scan writes zero stderr
    bytes and behaves identically (same exit code, same stdout)."""

    def test_quiet_zero_stderr_identical_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixed_store(tmp)
            with patch("gleam.core.verbose.time.gmtime",
                       return_value=FIXED_TIME):
                code_v, out_v, err_v = run_main(
                    check_main, ["--store", tmp, "--verbose"])
                code_q, out_q, err_q = run_main(
                    check_main, ["--store", tmp])
        self.assertEqual(err_v.splitlines(), expected_mixed_lines(tmp))
        self.assertEqual(err_q, "")
        self.assertEqual(code_q, code_v)
        self.assertEqual(out_q, out_v)


if __name__ == "__main__":
    unittest.main()
