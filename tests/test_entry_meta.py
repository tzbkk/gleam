"""help/version entries + router -- pure informational commands.

No network, no storage: help and version are stdout-only on success,
usage errors take the exit-2 stderr path with no Result, and the
router is pure routing. The version
fallback path is exercised by mocking the installed-metadata lookup.
The flag ban is pinned: ``-h``/``--help``/``--version`` are
usage errors (exit 2) on the help entry, the version entry, and the
router; the help target is the positional ``target_module`` frozen in
``MODULE_SPECS["help"]``, so ``--target ...`` is an unknown
flag.
"""

import importlib.metadata
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from gleam.core.config import bind
from gleam.entries import help as help_entry
from gleam.entries import version as version_entry
from gleam.router import dispatch
from tests._helpers import capture_run as run_main

MODULES = ("udd", "bts", "repology", "check", "investigate", "searxng",
           "help", "version")
# Real flags from each module's parser surface; help (positional only)
# and version (no fields) define none.
PRIMARY_FLAGS = {
    "udd": "--list-datasets",
    "bts": "--bug-id",
    "repology": "--project",
    "check": "--store",
    "investigate": "--max-report-repairs",
    "searxng": "--start",
}
VERSION_SHAPE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def pyproject_version():
    """The pyproject.toml version field (the single source)."""
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


class HelpBindingTests(unittest.TestCase):
    """The declarative binding surface of the help entry."""

    def test_no_target_binds_none(self):
        self.assertIsNone(bind("help", argv=[]).target_module)

    def test_target_module_is_positional(self):
        self.assertEqual(bind("help", argv=["udd"]).target_module, "udd")
        self.assertEqual(
            bind("help", argv=["investigate"]).target_module, "investigate"
        )

class HelpEntryTests(unittest.TestCase):
    """General overview, delegation, and usage errors."""

    def test_no_target_general_help_exit0(self):
        code, out, err = run_main(help_entry.main, [])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertGreaterEqual(len(out.splitlines()), 10)
        for name in MODULES:
            self.assertIn(name, out)
        self.assertIn('Run "gleam help <module>"', out)

    def test_general_help_matches_helper(self):
        code, out, _ = run_main(help_entry.main, [])
        self.assertEqual(code, 0)
        self.assertEqual(out, help_entry.general_help() + "\n")

    def test_delegates_to_each_module_doc(self):
        for name in MODULES:
            with self.subTest(module=name):
                code, out, err = run_main(help_entry.main, [name])
                self.assertEqual(code, 0)
                self.assertEqual(err, "")
                self.assertTrue(out.strip())
                self.assertNotEqual(out, help_entry.general_help() + "\n")
                # self-contained user help: its own usage line and its
                # own flag surface, not the overview.
                self.assertIn(f"Usage: gleam {name}", out)
                flag = PRIMARY_FLAGS.get(name)
                if flag is not None:
                    self.assertIn(flag, out)

    def test_module_help_never_mentions_psql(self):
        # The psql subprocess is gone from every surface; no module's
        # self-contained help may resurrect it.
        for name in MODULES:
            with self.subTest(module=name):
                code, out, err = run_main(help_entry.main, [name])
                self.assertEqual(code, 0)
                self.assertNotIn("psql", out.lower())

    def test_module_help_has_no_internal_jargon(self):
        # Module help is user-facing CLI documentation; internal
        # design vocabulary must not leak into it.
        banned = (
            "module_specs", "entry shell", "gleam.core", "precedent",
            "template", "precedence", "frozen",
        )
        for name in MODULES:
            with self.subTest(module=name):
                code, out, err = run_main(help_entry.main, [name])
                self.assertEqual(code, 0)
                lowered = out.lower()
                for token in banned:
                    self.assertNotIn(token, lowered)

    def test_unknown_target_exit2_stderr_no_stdout(self):
        code, out, err = run_main(help_entry.main, ["nosuch"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("nosuch", err)

    def test_target_flag_is_usage_error(self):
        # --target is an unknown flag (help takes a positional only),
        # so both spellings exit 2 with a stderr diagnostic.
        for argv in (["--target", "nosuch"], ["--target", "udd"]):
            with self.subTest(argv=argv):
                code, out, err = run_main(help_entry.main, argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--target", err)

    def test_stray_positional_after_target(self):
        code, out, err = run_main(help_entry.main, ["udd", "bts"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")

    def test_no_help_builtins_are_usage_errors(self):
        for flag in ("-h", "--help", "--version"):
            with self.subTest(flag=flag):
                code, out, err = run_main(help_entry.main, [flag])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(flag, err)

    def test_stage1_module_execution(self):
        proc = subprocess.run(
            [sys.executable, "-m", "gleam.entries.help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        for name in MODULES:
            self.assertIn(name, proc.stdout)
        self.assertEqual(proc.stderr, "")


class VersionEntryTests(unittest.TestCase):
    """Single-source version, both paths, and usage errors."""

    def test_metadata_path_prints_version_line(self):
        code, out, err = run_main(version_entry.main, [])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        line = out.rstrip("\n")
        self.assertRegex(line, VERSION_SHAPE)
        self.assertEqual(line, pyproject_version())

    def test_fallback_path_reads_pyproject(self):
        with patch(
            "gleam.core.package_version.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            code, out, err = run_main(version_entry.main, [])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, pyproject_version() + "\n")

    def test_version_string_uses_metadata_first(self):
        with patch(
            "gleam.core.package_version.importlib.metadata.version",
            return_value="9.9.9",
        ):
            self.assertEqual(version_entry.version_string(), "9.9.9")

    def test_no_flag_forms_are_usage_errors(self):
        for flag in ("-h", "--help", "--version"):
            with self.subTest(flag=flag):
                code, out, err = run_main(version_entry.main, [flag])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(flag, err)

    def test_stray_positional_is_usage_error(self):
        code, out, err = run_main(version_entry.main, ["9.9.9"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")

    def test_stage1_module_execution(self):
        proc = subprocess.run(
            [sys.executable, "-m", "gleam.entries.version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertRegex(proc.stdout.rstrip("\n"), VERSION_SHAPE)


class RouterTests(unittest.TestCase):
    """Pure routing: empty/known/unknown argv."""

    def patch_all_mains(self):
        mocks = {}
        for name in MODULES:
            patcher = patch(f"gleam.entries.{name}.main", return_value=7)
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        return mocks

    def test_route_matrix(self):
        for token in MODULES:
            with self.subTest(token=token):
                mocks = self.patch_all_mains()
                self.assertEqual(dispatch([token, "--flag", "value"]), 7)
                for name, mock in mocks.items():
                    if name == token:
                        mock.assert_called_once_with(["--flag", "value"])
                    else:
                        mock.assert_not_called()

    def test_empty_argv_routes_to_help(self):
        mocks = self.patch_all_mains()
        self.assertEqual(dispatch([]), 7)
        mocks["help"].assert_called_once_with([])
        for name, mock in mocks.items():
            if name != "help":
                mock.assert_not_called()

    def test_unknown_token_routes_to_help_diagnostic(self):
        mocks = self.patch_all_mains()
        self.assertEqual(dispatch(["nosuch"]), 7)
        mocks["help"].assert_called_once_with(["nosuch"])
        for name, mock in mocks.items():
            if name != "help":
                mock.assert_not_called()

    def test_router_banned_flag_forms_route_to_exit2(self):
        for argv in (["-h"], ["--help"], ["--version"]):
            with self.subTest(argv=argv):
                code, out, err = run_main(dispatch, argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(argv[0], err)

    def test_router_import_has_no_side_effects(self):
        proc = subprocess.run(
            [sys.executable, "-c", "from gleam.router import dispatch"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")


class ZeroStorageTests(unittest.TestCase):
    """Informational entries never write files (storage-free shells)."""

    def test_help_and_version_leave_cwd_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                before = sorted(os.listdir(tmp))
                for argv, entry_main in (
                    ([], help_entry.main),
                    (["udd"], help_entry.main),
                    ([], version_entry.main),
                ):
                    code, _, _ = run_main(entry_main, argv)
                    self.assertEqual(code, 0)
                self.assertEqual(sorted(os.listdir(tmp)), before)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
