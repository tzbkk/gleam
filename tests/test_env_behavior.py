"""``.env`` behavior matrix and binder validation for gleam.core.config.

Covers ``.env`` syntax, file loading, empty-value semantics and the
exit-2 error family, precedence, and the binder's type/range/
choice/URL/input-contract validation. All env files are created in
per-test temporary directories; the repository's own directory is never
used as an env source. Tests that exercise the default ``./.env`` path
chdir into a temporary directory and restore the original afterwards.
"""

import os
import tempfile
import unittest
from pathlib import Path

from gleam.core.config import (
    EnvFileError,
    EnvSyntaxError,
    bind,
    bind_keys,
    load_default_env,
    parse_env_file,
    parse_env_text,
)
from gleam.core.result import UsageError

DEFAULT_DSN = "postgresql://udd-mirror.debian.net/udd"


class EnvFileSandbox(unittest.TestCase):
    """Base fixture: temporary env files, chdir isolation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._original_cwd = None

    def write_env(self, content, name="test.env"):
        """Write an env file into the sandbox and return its path."""
        path = self.tmp / name
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return str(path)

    def chdir_tmp(self):
        """Run the rest of this test inside the sandbox directory."""
        self._original_cwd = Path.cwd()
        os.chdir(self.tmp)
        self.addCleanup(self._restore_cwd)

    def _restore_cwd(self):
        if self._original_cwd is not None:
            os.chdir(self._original_cwd)


class PrecedenceTest(EnvFileSandbox):
    """Explicit args > CLI > ``.env`` > defaults."""

    def test_cli_beats_env(self):
        env_path = self.write_env("GLEAM_UDD_DSN=postgresql://env-host/udd\n")
        config = bind("udd", argv=["--dsn", "postgresql://cli-host/udd"],
                      env_file=env_path)
        self.assertEqual(config.dsn, "postgresql://cli-host/udd")

    def test_env_beats_default_when_no_cli(self):
        env_path = self.write_env("GLEAM_UDD_DSN=postgresql://env-host/udd\n")
        config = bind("udd", argv=[], env_file=env_path)
        self.assertEqual(config.dsn, "postgresql://env-host/udd")

    def test_default_applies_when_nothing_explicit(self):
        env_path = self.write_env("GLEAM_MODEL_TEMPERATURE=0.5\n")
        config = bind("udd", argv=[], env_file=env_path)
        self.assertEqual(config.dsn, DEFAULT_DSN)

    def test_function_override_beats_cli_and_env(self):
        env_path = self.write_env("GLEAM_MAX_ROUNDS=3\n")
        config = bind("investigate", argv=["--max-rounds", "5"],
                      env_file=env_path, max_rounds=7)
        self.assertEqual(config.max_rounds, 7)

    def test_cli_beats_env_for_budget_keys(self):
        env_path = self.write_env("GLEAM_MAX_ROUNDS=3\n")
        config = bind("investigate", argv=["--max-rounds", "5"],
                      env_file=env_path)
        self.assertEqual(config.max_rounds, 5)

    def test_env_only_policy_keys_bind_on_investigate(self):
        env_path = self.write_env(
            "GLEAM_MODEL_TEMPERATURE=0.25\n"
            "GLEAM_CONTEXT_KEEP_CHARS=128\n"
            "GLEAM_REPORT_MAX_ERRORS=12\n"
        )
        config = bind("investigate", env_file=env_path)
        self.assertEqual(config.temperature, 0.25)
        self.assertEqual(config.context_keep_chars, 128)
        self.assertEqual(config.report_max_errors, 12)

    def test_shared_udd_dsn_reaches_both_udd_and_investigate(self):
        env_path = self.write_env("GLEAM_UDD_DSN=postgresql://shared/udd\n")
        self.assertEqual(
            bind("udd", env_file=env_path).dsn, "postgresql://shared/udd")
        self.assertEqual(
            bind("investigate", env_file=env_path).dsn,
            "postgresql://shared/udd")

    def test_known_but_irrelevant_keys_are_not_errors(self):
        # GLEAM_CHECK_STORE is a legal closed-table key; it simply does
        # not bind onto the udd request surface.
        env_path = self.write_env("GLEAM_CHECK_STORE=elsewhere\n")
        config = bind("udd", env_file=env_path)
        self.assertEqual(config.dsn, DEFAULT_DSN)

    def test_env_file_flag_selects_the_explicit_file(self):
        self.write_env("GLEAM_UDD_DSN=postgresql://flagged/udd\n",
                       name="chosen.env")
        config = bind("udd", argv=["--env-file", str(self.tmp / "chosen.env")])
        self.assertEqual(config.dsn, "postgresql://flagged/udd")

    def test_bind_keys_uses_the_same_precedence(self):
        env_path = self.write_env("GLEAM_UDD_MAX_QUERIES=4\n")
        self.assertEqual(
            bind_keys(["GLEAM_UDD_MAX_QUERIES", "GLEAM_BTS_MAX_MESSAGES"],
                      env_file=env_path),
            {"GLEAM_UDD_MAX_QUERIES": 4, "GLEAM_BTS_MAX_MESSAGES": 200},
        )
        self.assertEqual(
            bind_keys(["GLEAM_UDD_MAX_QUERIES"], env_file=env_path,
                      GLEAM_UDD_MAX_QUERIES=9),
            {"GLEAM_UDD_MAX_QUERIES": 9},
        )


class EnvSyntaxMatrixTest(EnvFileSandbox):
    """Syntax rules and empty-value semantics."""

    def test_duplicate_key_is_an_error_even_with_equal_values(self):
        env_path = self.write_env(
            "GLEAM_MODEL=a\nGLEAM_MODEL=a\n")
        with self.assertRaises(EnvSyntaxError):
            parse_env_file(env_path)

    def test_unknown_gleam_key_is_an_error(self):
        env_path = self.write_env("GLEAM_NOT_A_REAL_KEY=1\n")
        with self.assertRaises(EnvSyntaxError):
            parse_env_file(env_path)

    def test_request_budget_key_is_unknown(self):
        # ``GLEAM_MAX_REQUESTS`` is not in the closed key table; it
        # must error, not silently apply.
        env_path = self.write_env("GLEAM_MAX_REQUESTS=9\n")
        with self.assertRaises(EnvSyntaxError):
            parse_env_file(env_path)

    def test_retired_udd_output_bytes_key_is_unknown(self):
        # ``GLEAM_UDD_MAX_OUTPUT_BYTES`` left the closed table with
        # the psql subprocess; an operator ``.env`` still carrying it
        # must error, not silently apply.
        env_path = self.write_env("GLEAM_UDD_MAX_OUTPUT_BYTES=1024\n")
        with self.assertRaises(EnvSyntaxError):
            parse_env_file(env_path)

    def test_non_gleam_keys_are_ignored(self):
        env_path = self.write_env(
            "SOME_TOOL_HOME=/opt\nOTHER=value\n"
            "GLEAM_UDD_DSN=postgresql://env-host/udd\n")
        values = parse_env_file(env_path)
        self.assertEqual(values, {"GLEAM_UDD_DSN": "postgresql://env-host/udd"})

    def test_non_gleam_duplicates_are_ignored(self):
        env_path = self.write_env("FOO=1\nFOO=2\n")
        self.assertEqual(parse_env_file(env_path), {})

    def test_empty_value_is_equivalent_to_unset(self):
        env_path = self.write_env(
            "GLEAM_UDD_DSN=\nGLEAM_MODEL=\n")
        values = parse_env_file(env_path)
        self.assertEqual(values, {})
        config = bind("udd", env_file=env_path)
        self.assertEqual(config.dsn, DEFAULT_DSN)
        investigate = bind("investigate", env_file=env_path)
        self.assertIsNone(investigate.model)

    def test_utf8_bom_is_a_syntax_error(self):
        env_path = self.write_env(b"\xef\xbb\xbfGLEAM_UDD_DSN=x\n")
        with self.assertRaises(EnvSyntaxError):
            parse_env_file(env_path)

    def test_crlf_line_endings_are_stripped(self):
        env_path = self.write_env(
            "GLEAM_UDD_DSN=postgresql://crlf/udd\r\n"
            "GLEAM_MODEL=q\r\n")
        values = parse_env_file(env_path)
        self.assertEqual(values["GLEAM_UDD_DSN"], "postgresql://crlf/udd")
        self.assertEqual(values["GLEAM_MODEL"], "q")

    def test_line_without_equals_is_a_syntax_error(self):
        env_path = self.write_env("GLEAM_UDD_DSN oops\n")
        with self.assertRaises(EnvSyntaxError):
            parse_env_file(env_path)

    def test_export_prefix_is_unsupported_and_rejected(self):
        env_path = self.write_env("export GLEAM_MODEL=a\n")
        with self.assertRaises(EnvSyntaxError):
            parse_env_file(env_path)

    def test_hash_belongs_to_the_value(self):
        env_path = self.write_env("GLEAM_MODEL=name # not a comment\n")
        values = parse_env_file(env_path)
        self.assertEqual(values["GLEAM_MODEL"], "name # not a comment")

    def test_whitespace_around_key_and_value_is_stripped(self):
        env_path = self.write_env("  GLEAM_MODEL =  qwen  \n")
        self.assertEqual(parse_env_file(env_path), {"GLEAM_MODEL": "qwen"})

    def test_empty_or_comment_only_file_is_an_empty_config(self):
        env_path = self.write_env("\n   \n# only comments\n\n")
        self.assertEqual(parse_env_file(env_path), {})
        config = bind("udd", env_file=env_path)
        self.assertEqual(config.dsn, DEFAULT_DSN)

    def test_key_with_inner_space_is_rejected(self):
        with self.assertRaises(EnvSyntaxError):
            parse_env_text("BAD KEY=1\n")

    def test_parse_env_text_duplicate_line_numbers_in_message(self):
        with self.assertRaises(EnvSyntaxError) as caught:
            parse_env_text("GLEAM_MODEL=a\nGLEAM_MODEL=b\n")
        self.assertIn("2", str(caught.exception))


class EnvFileFailureTest(EnvFileSandbox):
    """Explicit-file failures and default-file behavior."""

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaises(EnvFileError):
            parse_env_file(self.tmp / "absent.env")

    def test_explicit_directory_is_an_error(self):
        with self.assertRaises(EnvFileError):
            parse_env_file(self.tmp)

    def test_invalid_utf8_is_an_error(self):
        env_path = self.write_env(b"GLEAM_MODEL=\xff\xfe\n")
        with self.assertRaises(EnvFileError):
            parse_env_file(env_path)

    def test_default_env_missing_proceeds_with_defaults(self):
        self.chdir_tmp()
        self.assertEqual(load_default_env(), {})
        config = bind("udd")
        self.assertEqual(config.dsn, DEFAULT_DSN)

    def test_default_env_present_is_loaded_from_cwd(self):
        self.chdir_tmp()
        self.write_env("GLEAM_UDD_DSN=postgresql://cwd/udd\n", name=".env")
        config = bind("udd")
        self.assertEqual(config.dsn, "postgresql://cwd/udd")

    def test_default_env_without_parent_search(self):
        # A .env in the sandbox parent must not leak into the child
        # working directory (no parent-directory search).
        parent = tempfile.mkdtemp(dir=self.tmp)
        child = Path(parent) / "child"
        child.mkdir()
        (Path(parent) / ".env").write_text(
            "GLEAM_UDD_DSN=postgresql://parent/udd\n", encoding="utf-8")
        original = Path.cwd()
        os.chdir(child)
        try:
            self.assertEqual(load_default_env(), {})
        finally:
            os.chdir(original)

    def test_broken_default_env_still_errors(self):
        self.chdir_tmp()
        self.write_env("GLEAM_MODEL=a\nGLEAM_MODEL=b\n", name=".env")
        with self.assertRaises(EnvSyntaxError):
            bind("udd")

    def test_error_classes_are_usage_errors_with_exit_code_2(self):
        for error in (EnvSyntaxError, EnvFileError):
            self.assertTrue(issubclass(error, UsageError))
            self.assertEqual(error.exit_code, 2)


class BinderValidationTest(EnvFileSandbox):
    """Type/range/choice/URL/input-contract validation (exit-2)."""

    def test_budget_upper_bounds_rejected(self):
        env_path = self.write_env("GLEAM_MAX_ROUNDS=65\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)

    def test_budget_boundaries_accepted(self):
        env_path = self.write_env(
            "GLEAM_MAX_ROUNDS=1\nGLEAM_MAX_TOOL_CALLS=512\n"
            "GLEAM_MAX_BODY_CHARS=200\nGLEAM_MAX_LINKS=200\n"
            "GLEAM_MAX_CONTEXT_CHARS=4000\n")
        config = bind("investigate", env_file=env_path)
        self.assertEqual(config.max_rounds, 1)
        self.assertEqual(config.max_tool_calls, 512)
        self.assertEqual(config.max_body_chars, 200)
        self.assertEqual(config.max_links, 200)
        self.assertEqual(config.max_context_chars, 4000)

    def test_budget_lower_bounds_rejected(self):
        env_path = self.write_env("GLEAM_MAX_BODY_CHARS=199\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)

    def test_non_integer_integer_key_rejected(self):
        env_path = self.write_env("GLEAM_MAX_ROUNDS=many\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)
        float_path = self.write_env("GLEAM_MAX_ROUNDS=1.5\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=float_path)

    def test_underscored_number_literal_rejected(self):
        env_path = self.write_env("GLEAM_MAX_TOOL_CALLS=1_000\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)

    def test_udd_row_cap_keys_validate_and_flow(self):
        # The two result-honesty caps bind through bind_keys (no CLI
        # flag): positive integers only, custom values resolved from
        # the env file.
        keys = ["GLEAM_UDD_MAX_CELL_CHARS",
                "GLEAM_UDD_MAX_TOTAL_ROW_CHARS"]
        happy = self.write_env(
            "GLEAM_UDD_MAX_CELL_CHARS=200\n"
            "GLEAM_UDD_MAX_TOTAL_ROW_CHARS=4000\n")
        self.assertEqual(
            bind_keys(keys, env_file=happy),
            {"GLEAM_UDD_MAX_CELL_CHARS": 200,
             "GLEAM_UDD_MAX_TOTAL_ROW_CHARS": 4000},
        )
        for bad in ("0", "-5", "many", "1.5"):
            with self.subTest(value=bad):
                env_path = self.write_env(
                    f"GLEAM_UDD_MAX_CELL_CHARS={bad}\n")
                with self.assertRaises(UsageError):
                    bind_keys(keys, env_file=env_path)

    def test_float_keys_accept_decimal_text(self):
        env_path = self.write_env("GLEAM_TIMEOUT=2.5\n")
        config = bind("investigate", env_file=env_path)
        self.assertEqual(config.fetch_timeout, 2.5)

    def test_non_finite_float_rejected(self):
        env_path = self.write_env("GLEAM_TIMEOUT=nan\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)

    def test_zero_timeout_rejected_but_zero_min_interval_accepted(self):
        env_path = self.write_env("GLEAM_MIN_INTERVAL=0\n")
        config = bind("investigate", env_file=env_path)
        self.assertEqual(config.fetch_min_interval, 0.0)
        zero_timeout = self.write_env("GLEAM_TIMEOUT=0\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=zero_timeout)

    def test_url_key_rejects_non_http_scheme(self):
        env_path = self.write_env("GLEAM_SEARCH_BASE=ftp://127.0.0.1:8888\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)

    def test_url_key_rejects_embedded_credentials(self):
        env_path = self.write_env(
            "GLEAM_SEARCH_BASE=http://user:secret@127.0.0.1:8888\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)

    def test_url_key_rejects_missing_host(self):
        env_path = self.write_env("GLEAM_GITHUB_API_BASE=http:///path\n")
        with self.assertRaises(UsageError):
            bind("investigate", env_file=env_path)

    def test_url_key_accepts_plain_http_urls(self):
        env_path = self.write_env(
            "GLEAM_SEARCH_BASE=http://127.0.0.1:8888\n"
            "GLEAM_MODEL_PROXY=http://127.0.0.1:7890\n")
        config = bind("investigate", env_file=env_path)
        self.assertEqual(config.search_base, "http://127.0.0.1:8888")
        self.assertEqual(config.model_proxy, "http://127.0.0.1:7890")

    def test_url_flag_rejects_out_of_range_port(self):
        with self.assertRaises(UsageError):
            bind("investigate",
                 argv=["--search-base", "http://127.0.0.1:99999"])

    def test_choice_key_accepts_documented_values_only(self):
        ok = self.write_env("GLEAM_BTS_SOURCE=soap\n")
        self.assertEqual(bind("bts", env_file=ok).source, "soap")
        bad = self.write_env("GLEAM_BTS_SOURCE=carrier-pigeon\n")
        with self.assertRaises(UsageError):
            bind("bts", env_file=bad)

    def test_bug_id_contract(self):
        self.assertEqual(bind("bts", argv=["--bug-id", "9999999"]).bug_id,
                         9999999)
        for bad in ("007", "0", "10000000", "abc", "-5"):
            with self.assertRaises(UsageError):
                bind("bts", argv=["--bug-id", bad])

    def test_source_name_contract(self):
        self.assertEqual(bind("udd", argv=["--package", "wizznic"]).package,
                         "wizznic")
        too_long = "a" * 65
        # Trailing hyphens/dots stay legal: the frozen regex only
        # constrains the first character and the character class.
        self.assertEqual(bind("udd", argv=["--package", "a-b.c+d"]).package,
                         "a-b.c+d")
        for bad in ("Bad_Name", "-leading", "with space", too_long, ""):
            with self.assertRaises(UsageError):
                bind("udd", argv=["--package", bad])

    def test_udd_limit_range(self):
        self.assertEqual(bind("udd", argv=["--limit", "50"]).limit, 50)
        for bad in ("0", "51"):
            with self.assertRaises(UsageError):
                bind("udd", argv=["--limit", bad])

    def test_help_target_module_choice(self):
        self.assertEqual(bind("help", argv=["udd"]).target_module, "udd")
        with self.assertRaises(UsageError):
            bind("help", argv=["nosuch"])

    def test_unknown_override_field_is_rejected(self):
        with self.assertRaises(UsageError):
            bind("udd", nonexistent="x")


class ArgvParsingTest(EnvFileSandbox):
    """The spec-driven argv parser behind bind()."""

    def test_inline_equals_syntax(self):
        config = bind("udd", argv=["--dsn=postgresql://inline/udd"])
        self.assertEqual(config.dsn, "postgresql://inline/udd")

    def test_boolean_flag_takes_no_value(self):
        with self.assertRaises(UsageError):
            bind("udd", argv=["--json=1"])

    def test_boolean_flags_set_true(self):
        config = bind("udd", argv=["--json", "--list-datasets"])
        self.assertTrue(config.json)
        self.assertTrue(config.list_datasets)

    def test_unknown_flag_rejected(self):
        with self.assertRaises(UsageError):
            bind("udd", argv=["--frobnicate", "1"])
        with self.assertRaises(UsageError):
            bind("version", argv=["--json"])

    def test_value_flag_requires_a_value(self):
        with self.assertRaises(UsageError):
            bind("udd", argv=["--dsn"])

    def test_stray_positional_rejected_for_flag_only_modules(self):
        with self.assertRaises(UsageError):
            bind("udd", argv=["stray"])

    def test_repeatable_flag_accumulates_in_order(self):
        config = bind("udd", argv=["--dataset", "sources",
                                   "--dataset=upstream"])
        self.assertEqual(config.datasets, ("sources", "upstream"))

    def test_plain_flag_last_occurrence_wins(self):
        config = bind("udd", argv=["--release", "trixie",
                                   "--release", "bookworm"])
        self.assertEqual(config.release, "bookworm")

    def test_double_dash_feeds_positionals(self):
        self.assertEqual(bind("help", argv=["--", "bts"]).target_module, "bts")


class BoundConfigShapeTest(EnvFileSandbox):
    """The object entries consume: attribute access and mapping view."""

    def test_attribute_and_mapping_access(self):
        config = bind("check")
        self.assertEqual(config.store, "runs")
        self.assertEqual(config.as_dict()["store"], "runs")
        self.assertEqual(
            config.field_names(), ("store", "run", "json", "verbose", "env_file")
        )
        self.assertIsNone(config.run)
        with self.assertRaises(AttributeError):
            _ = config.nonexistent

    def test_repr_names_the_module(self):
        self.assertIn("udd", repr(bind("udd")))

    def test_unknown_module_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            bind("nosuch")

    def test_bind_keys_rejects_out_of_table_keys(self):
        with self.assertRaises(ValueError):
            bind_keys(["GLEAM_NOT_A_REAL_KEY"])


if __name__ == "__main__":
    unittest.main()
