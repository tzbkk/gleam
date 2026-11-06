"""End-to-end ``.env`` behavior matrix against the real CLI.

The unit suite (``tests/test_env_behavior.py``) pins the parser
and binder internals; this module drives the SAME behavior
matrix through real entry subprocesses (``[sys.executable, "-m",
"gleam.entries.<module>", ...]``) so the exit-code mapping (config
error -> 2 with NO Result on stdout) and the rendered outputs are
proven at the surface users actually invoke. Fields a CLI run cannot
observe (e.g. ``investigate.model`` without a model endpoint) are pinned
by the ``bind``/``build_config`` rows in ``test_env_behavior.py``.

Hermeticity: every env fixture lives in a per-test temporary
directory; default-env rows run from a fresh cwd that contains no
``.env`` at all; no row depends on a running SearXNG or on docker; and
no test ever creates or depends on a ``.env`` at the repository root.
A root ``.env`` is legal local operator config; the final invariant
test pins its exclusion from version control instead of forbidding its
existence.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class CliSandbox(unittest.TestCase):
    """Temporary env fixtures plus the subprocess CLI runner."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_env(self, content, name="test.env"):
        """Write one env fixture into the sandbox; return its path."""
        path = self.tmp / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return str(path)

    def fresh_cwd(self, name="cwd"):
        """A working directory guaranteed to hold no ``.env`` file."""
        path = self.tmp / name
        path.mkdir()
        return path

    def run_cli(self, module, args=(), cwd=None):
        """Run one entry module as a real subprocess.

        The source tree is prepended to PYTHONPATH so the child always
        imports the repository's gleam package regardless of install
        state. Default cwd is the repository root (explicit ``--env-file``
        rows never read a default env file); default-env rows pass a
        fresh cwd explicitly.
        """
        env = dict(os.environ)
        env["PYTHONPATH"] = (
            str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""))
        workdir = str(cwd) if cwd is not None else str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", f"gleam.entries.{module}", *args],
            capture_output=True,
            text=True,
            cwd=workdir,
            env=env,
            timeout=120,
        )

    def assert_config_error(self, proc, *stderr_needles):
        """Exit 2, no Result on stdout, config diagnostic on stderr."""
        self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
        self.assertEqual(
            proc.stdout.strip(), "", "config errors must not produce a Result")
        self.assertNotEqual(proc.stderr.strip(), "")
        for needle in stderr_needles:
            self.assertIn(needle, proc.stderr)


class DefaultEnvRow(CliSandbox):
    """Row 1: no ``.env`` anywhere -> the module runs on defaults."""

    def test_missing_default_env_proceeds_with_defaults(self):
        proc = self.run_cli(
            "udd", ["--list-datasets", "--json"], cwd=self.fresh_cwd())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], "list-datasets")
        self.assertEqual(len(payload["data"]["datasets"]), 18)


class ExplicitEnvFileFailureRows(CliSandbox):
    """Row 2: an explicit env file that cannot load -> exit 2."""

    def test_missing_explicit_env_file_is_config_error(self):
        missing = str(self.tmp / "nope.env")
        proc = self.run_cli(
            "udd", ["--list-datasets", "--json", "--env-file", missing])
        self.assert_config_error(proc, "env file not found", missing)

class BenignContentRows(CliSandbox):
    """Rows 3-4: empty/comment-only files and non-GLEAM keys are fine."""

    def test_empty_env_file_is_an_empty_config(self):
        path = self.write_env("")
        proc = self.run_cli(
            "check", ["--json", "--env-file", path], cwd=self.fresh_cwd())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["store"], "runs")

class EmptyValueRows(CliSandbox):
    """Row 5: ``KEY=`` equals unset at the bound config."""

    def test_empty_check_store_value_falls_back_to_default(self):
        path = self.write_env("GLEAM_CHECK_STORE=\n")
        proc = self.run_cli(
            "check", ["--json", "--env-file", path], cwd=self.fresh_cwd())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Empty value never becomes an empty-string store path.
        self.assertEqual(json.loads(proc.stdout)["data"]["store"], "runs")

class PrecedenceRows(CliSandbox):
    """Row 6: explicit CLI > ``.env`` > default."""

    def test_store_resolution_three_way(self):
        store_a = self.tmp / "store-a"
        store_a.mkdir()
        store_b = self.tmp / "store-b"
        store_b.mkdir()
        env_path = self.write_env(f"GLEAM_CHECK_STORE={store_a}\n")
        # .env beats the closed-table default ...
        proc = self.run_cli(
            "check", ["--json", "--env-file", env_path],
            cwd=self.fresh_cwd())
        self.assertEqual(
            json.loads(proc.stdout)["data"]["store"], str(store_a))
        # ... the CLI flag beats .env ...
        proc = self.run_cli(
            "check", ["--json", "--store", str(store_b),
                      "--env-file", env_path],
            cwd=self.fresh_cwd("cwd2"))
        self.assertEqual(
            json.loads(proc.stdout)["data"]["store"], str(store_b))
        # ... and with neither, the default applies.
        empty = self.write_env("", name="empty.env")
        proc = self.run_cli(
            "check", ["--json", "--env-file", empty], cwd=self.fresh_cwd("cwd3"))
        self.assertEqual(json.loads(proc.stdout)["data"]["store"], "runs")

class IllegalValueRows(CliSandbox):
    """Row 7: illegal values -> exit 2 with no Result."""

    def assert_env_rejected(self, env_content, needle):
        path = self.write_env(env_content)
        proc = self.run_cli("investigate", ["--json", "--env-file", path])
        self.assert_config_error(proc, needle)

    def test_out_of_range_budget_rejected(self):
        for bad in ("0", "65", "99999"):
            with self.subTest(bad=bad):
                self.assert_env_rejected(
                    f"GLEAM_MAX_ROUNDS={bad}\n", "GLEAM_MAX_ROUNDS")

class SyntaxErrorRows(CliSandbox):
    """Row 8: malformed .env content -> exit 2 (EnvSyntaxError surface)."""

    def assert_syntax_rejected(self, content):
        path = self.write_env(content)
        proc = self.run_cli(
            "udd", ["--list-datasets", "--json", "--env-file", path])
        self.assert_config_error(proc)

    def test_line_without_equals_is_rejected(self):
        self.assert_syntax_rejected("GLEAM_MODEL oops\n")

class RepoRootInvariant(unittest.TestCase):
    """A repo-root ``.env`` is legal local operator config
    (model/DSN/api-key live there) and must never be
    committable. Tests create no root fixture and depend on no root
    contents; the invariant protecting the repo is gitignore coverage.
    """

    def test_repo_root_env_file_is_gitignored(self):
        lines = (REPO_ROOT / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        self.assertIn(".env", {line.strip() for line in lines})


if __name__ == "__main__":
    unittest.main()
