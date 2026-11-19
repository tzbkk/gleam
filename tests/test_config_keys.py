"""Key-closure and flag-surface pinning for gleam.core.config.

The closed ``GLEAM_*`` key table below is a verbatim transcription of
the authoritative key list. The tests assert set equality in both
directions: a missing key or an extra key both fail. The table holds
61 keys in six groups: 6 shared + 8 udd policy + 10 bts policy +
4 repology policy + 32 investigate + 1 check.

Also pins the eight modules' CLI flag surfaces, the object reuse of
udd query specs by investigate, the budget defaults and hard ranges,
and the rule that action switches never gain env keys.
"""

import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

from gleam.core.config import (
    ALL_ENV_KEYS,
    ENV_KEYS,
    MODULES,
    MODULE_SPECS,
    load_robots_exempt_hosts,
)
from gleam.core.result import UsageError
from gleam.entries.investigate import build_config

# --- The closed key table, transcribed verbatim, grouped ---

SHARED_KEYS = frozenset({
    "GLEAM_UDD_DSN",
    "GLEAM_MODEL",
    "GLEAM_MODEL_BASE",
    "GLEAM_MODEL_API_KEY",
    "GLEAM_SEARCH_BASE",
    "GLEAM_GITHUB_API_BASE",
})

UDD_POLICY_KEYS = frozenset({
    "GLEAM_UDD_STATEMENT_TIMEOUT",
    "GLEAM_UDD_MAX_CELL_CHARS",
    "GLEAM_UDD_MAX_TOTAL_ROW_CHARS",
    "GLEAM_UDD_MAX_QUERIES",
    "GLEAM_UDD_FIELD_CHAR_CAP",
    "GLEAM_UDD_MAX_DATASETS",
    "GLEAM_UDD_MAX_ROWS",
    "GLEAM_UDD_MAX_ROWS_PER_DATASET",
})

BTS_POLICY_KEYS = frozenset({
    "GLEAM_BTS_SOURCE",
    "GLEAM_BTS_TIMEOUT",
    "GLEAM_BTS_MAX_OUTPUT_BYTES",
    "GLEAM_BTS_MAX_REQUESTS",
    "GLEAM_BTS_MIN_INTERVAL",
    "GLEAM_BTS_MAX_MESSAGES",
    "GLEAM_BTS_MAX_MESSAGE_CHARS",
    "GLEAM_BTS_MAX_URLS_PER_MESSAGE",
    "GLEAM_BTS_POW_CPU_SECONDS",
    "GLEAM_BTS_ROBOTS_MAX_BYTES",
})

INVESTIGATE_KEYS = frozenset({
    "GLEAM_INVESTIGATE_STORE",
    "GLEAM_MODEL_MAX_TOKENS",
    "GLEAM_MODEL_TIMEOUT",
    "GLEAM_MODEL_TEMPERATURE",
    "GLEAM_MODEL_RESPONSE_MAX_BYTES",
    "GLEAM_MODEL_PROXY",
    "GLEAM_MAX_ROUNDS",
    "GLEAM_MAX_TOOL_CALLS",
    "GLEAM_MAX_BODY_CHARS",
    "GLEAM_MAX_LINKS",
    "GLEAM_MAX_CONTEXT_CHARS",
    "GLEAM_MAX_HTTP_REQUESTS",
    "GLEAM_MAX_BYTES",
    "GLEAM_TIMEOUT",
    "GLEAM_MAX_REDIRECTS",
    "GLEAM_MIN_INTERVAL",
    "GLEAM_ROBOTS_EXEMPT_FILE",
    "GLEAM_SEARCH_RESPONSE_MAX_BYTES",
    "GLEAM_GITHUB_README_TEXT_CAP",
    "GLEAM_GITHUB_PER_PAGE",
    "GLEAM_REPORT_CELL_MAX_CHARS",
    "GLEAM_REPORT_MAX_ERRORS",
    "GLEAM_REPORT_MAX_REPAIR_REASONS",
    "GLEAM_CONTEXT_KEEP_CHARS",
    "GLEAM_CONTEXT_OVERHEAD_CHARS",
    "GLEAM_CONTEXT_MAX_ITERATIONS",
    "GLEAM_MAILTO_MAX",
    "GLEAM_COPYRIGHT_MAX",
    "GLEAM_FOOTER_MAX",
    "GLEAM_MAX_URL_CHARS",
    "GLEAM_MAX_SEARCH_QUERY_CHARS",
    "GLEAM_MIN_QUOTE_CHARS",
})

CHECK_KEYS = frozenset({"GLEAM_CHECK_STORE"})

REPOLOGY_POLICY_KEYS = frozenset({
    "GLEAM_REPOLOGY_MAX_REQUESTS",
    "GLEAM_REPOLOGY_MIN_INTERVAL",
    "GLEAM_REPOLOGY_TIMEOUT",
    "GLEAM_REPOLOGY_BASE",
})

CLOSED_KEY_TABLE = SHARED_KEYS | UDD_POLICY_KEYS | BTS_POLICY_KEYS | (
    INVESTIGATE_KEYS | CHECK_KEYS | REPOLOGY_POLICY_KEYS
)


def env_keys_of(module):
    """The env keys reachable through one module's param specs."""
    return {
        param.env_key
        for param in MODULE_SPECS[module]
        if param.env_key is not None
    }


class ClosedKeyTableTest(unittest.TestCase):
    """The ConfigSpec key set equals the closed key table EXACTLY."""

    def test_key_table_equality_in_both_directions(self):
        self.assertEqual(set(ENV_KEYS), CLOSED_KEY_TABLE)
        self.assertEqual(CLOSED_KEY_TABLE, set(ALL_ENV_KEYS))

    def test_table_size_is_61(self):
        # 6 shared + 8 udd + 10 bts + 4 repology + 32 investigate
        # + 1 check = 61 total.
        self.assertEqual(len(ENV_KEYS), 61)

    def test_groups_are_disjoint(self):
        groups = (SHARED_KEYS, UDD_POLICY_KEYS, BTS_POLICY_KEYS,
                  INVESTIGATE_KEYS, CHECK_KEYS, REPOLOGY_POLICY_KEYS)
        seen = set()
        for group in groups:
            self.assertFalse(seen & group, group & seen)
            seen |= group
        self.assertEqual(seen, CLOSED_KEY_TABLE)

    def test_required_model_configuration_has_no_default(self):
        # Model endpoint/name come only from operator configuration;
        # a missing value stays missing (exit 1 at start-up, not here).
        # The api key is optional: no default means unauthenticated.
        self.assertIsNone(ENV_KEYS["GLEAM_MODEL"].default)
        self.assertIsNone(ENV_KEYS["GLEAM_MODEL_BASE"].default)
        self.assertIsNone(ENV_KEYS["GLEAM_MODEL_API_KEY"].default)
        self.assertIsNone(ENV_KEYS["GLEAM_MODEL_PROXY"].default)

    def test_model_api_key_shape(self):
        # Optional bearer token for the model endpoint. Plain str,
        # default unset; bound on the investigate surface as
        # --model-api-key.
        spec = ENV_KEYS["GLEAM_MODEL_API_KEY"]
        self.assertEqual(spec.kind, "str")
        self.assertIsNone(spec.default)
        param = next(p for p in MODULE_SPECS["investigate"]
                     if p.name == "model_api_key")
        self.assertEqual(param.flag, "--model-api-key")
        self.assertEqual(param.env_key, "GLEAM_MODEL_API_KEY")
        self.assertIsNone(param.default)

    def test_robots_exempt_file_shape(self):
        # Operator-owned robots exemption list path. Plain str
        # (path-like, resolved by the consumer relative to the
        # process CWD), default robots-exempt.txt; bound on the
        # investigate surface as --robots-exempt-file.
        spec = ENV_KEYS["GLEAM_ROBOTS_EXEMPT_FILE"]
        self.assertEqual(spec.kind, "str")
        self.assertEqual(spec.default, "robots-exempt.txt")
        param = next(p for p in MODULE_SPECS["investigate"]
                     if p.name == "robots_exempt_file")
        self.assertEqual(param.flag, "--robots-exempt-file")
        self.assertEqual(param.env_key, "GLEAM_ROBOTS_EXEMPT_FILE")
        self.assertIsNone(param.default)

    def test_repology_policy_key_shapes(self):
        # Four Repology policy keys (BTS budget precedent; the API
        # terms require <=1 request/second). Three numeric
        # client-policy keys plus the trusted base URL, all bound on
        # BOTH the repology entry surface and investigate.
        expected = {
            "GLEAM_REPOLOGY_MAX_REQUESTS": ("int", 6, 1, None),
            "GLEAM_REPOLOGY_MIN_INTERVAL": ("float", 1.1, 0.0, None),
            "GLEAM_REPOLOGY_TIMEOUT": ("float", 15.0, 0.0, None),
            "GLEAM_REPOLOGY_BASE": ("url", "https://repology.org",
                                    None, None),
        }
        for key, (kind, default, low, high) in expected.items():
            spec = ENV_KEYS[key]
            self.assertEqual(spec.kind, kind, key)
            self.assertEqual(spec.default, default, key)
            self.assertEqual(spec.minimum, low, key)
            self.assertEqual(spec.maximum, high, key)
            self.assertEqual(spec.choices, (), key)
        # the timeout lower bound is exclusive (house style for
        # timeouts, same as GLEAM_BTS_TIMEOUT)
        self.assertTrue(ENV_KEYS["GLEAM_REPOLOGY_TIMEOUT"].exclusive_minimum)
        self.assertFalse(
            ENV_KEYS["GLEAM_REPOLOGY_MIN_INTERVAL"].exclusive_minimum)
        for module in ("repology", "investigate"):
            params = {p.name: p for p in MODULE_SPECS[module]}
            for name, flag, key in (
                    ("repology_base", "--repology-base",
                     "GLEAM_REPOLOGY_BASE"),
                    ("repology_max_requests", "--repology-max-requests",
                     "GLEAM_REPOLOGY_MAX_REQUESTS"),
                    ("repology_min_interval", "--repology-min-interval",
                     "GLEAM_REPOLOGY_MIN_INTERVAL"),
                    ("repology_timeout", "--repology-timeout",
                     "GLEAM_REPOLOGY_TIMEOUT")):
                param = params[name]
                self.assertEqual(param.flag, flag, (module, name))
                self.assertEqual(param.env_key, key, (module, name))


class BudgetDefaultsAndRangesTest(unittest.TestCase):
    """Budget defaults and hard ranges, transcribed exactly."""

    def test_budget_table(self):
        expected = {
            "GLEAM_MAX_ROUNDS": (48, 1, 64),
            "GLEAM_MAX_TOOL_CALLS": (48, 1, 512),
            "GLEAM_MAX_BODY_CHARS": (40000, 200, 200000),
            "GLEAM_MAX_LINKS": (120, 1, 200),
            "GLEAM_MAX_CONTEXT_CHARS": (320000, 4000, 2000000),
        }
        for key, (default, low, high) in expected.items():
            spec = ENV_KEYS[key]
            self.assertEqual(spec.default, default, key)
            self.assertEqual(spec.minimum, low, key)
            self.assertEqual(spec.maximum, high, key)

    def test_github_hard_bound_is_baked_in(self):
        spec = ENV_KEYS["GLEAM_GITHUB_PER_PAGE"]
        self.assertEqual(spec.default, 10)
        self.assertEqual(spec.minimum, 1)
        self.assertEqual(spec.maximum, 100)

    def test_model_token_and_timeout_caps(self):
        tokens = ENV_KEYS["GLEAM_MODEL_MAX_TOKENS"]
        self.assertEqual(tokens.default, 8192)
        self.assertEqual(tokens.maximum, 65536)
        timeout = ENV_KEYS["GLEAM_MODEL_TIMEOUT"]
        self.assertEqual(timeout.default, 120.0)
        self.assertEqual(timeout.maximum, 600.0)

    def test_policy_defaults(self):
        expected = {
            "GLEAM_UDD_STATEMENT_TIMEOUT": 15000,
            "GLEAM_UDD_MAX_CELL_CHARS": 16384,
            "GLEAM_UDD_MAX_TOTAL_ROW_CHARS": 262144,
            "GLEAM_UDD_MAX_QUERIES": 10,
            "GLEAM_UDD_FIELD_CHAR_CAP": 600,
            "GLEAM_UDD_MAX_DATASETS": 6,
            "GLEAM_UDD_MAX_ROWS": 120,
            "GLEAM_UDD_MAX_ROWS_PER_DATASET": 20,
            "GLEAM_BTS_TIMEOUT": 20.0,
            "GLEAM_BTS_MAX_OUTPUT_BYTES": 8 * 1024 * 1024,
            "GLEAM_BTS_MAX_REQUESTS": 6,
            "GLEAM_BTS_MIN_INTERVAL": 2.0,
            "GLEAM_BTS_MAX_MESSAGES": 200,
            "GLEAM_BTS_MAX_MESSAGE_CHARS": 20000,
            "GLEAM_BTS_MAX_URLS_PER_MESSAGE": 20,
            "GLEAM_BTS_POW_CPU_SECONDS": 20.0,
            "GLEAM_BTS_ROBOTS_MAX_BYTES": 65536,
        }
        for key, default in expected.items():
            self.assertEqual(ENV_KEYS[key].default, default, key)

    def test_udd_row_cap_keys_match_policy_defaults(self):
        # The two result-honesty caps mirror UddPolicy's own defaults
        # (16384 per cell / 262144 total row characters), so an
        # unconfigured run and a default-constructed policy agree.
        for key, default in (("GLEAM_UDD_MAX_CELL_CHARS", 16384),
                             ("GLEAM_UDD_MAX_TOTAL_ROW_CHARS", 262144)):
            spec = ENV_KEYS[key]
            self.assertEqual(spec.kind, "int", key)
            self.assertEqual(spec.default, default, key)
            self.assertEqual(spec.minimum, 1, key)
            self.assertIsNone(spec.maximum, key)


class ModuleFlagSurfaceTest(unittest.TestCase):
    """The eight modules' flag surfaces."""

    def setUp(self):
        self.flags = {
            module: {param.flag for param in params if param.flag}
            for module, params in MODULE_SPECS.items()
        }

    def test_module_registry_is_the_eight_entries(self):
        self.assertEqual(set(MODULE_SPECS), set(MODULES))

    def test_udd_flags(self):
        self.assertEqual(self.flags["udd"], {
            "--package", "--dataset", "--release", "--limit", "--dsn",
            "--timeout", "--list-datasets", "--json", "--verbose",
            "--env-file",
        })

    def test_bts_flags(self):
        self.assertEqual(self.flags["bts"], {
            "--bug-id", "--source", "--json", "--verbose", "--env-file",
        })

    def test_repology_flags(self):
        self.assertEqual(self.flags["repology"], {
            "--project", "--search", "--repology-base",
            "--repology-max-requests", "--repology-min-interval",
            "--repology-timeout", "--json", "--verbose", "--env-file",
        })

    def test_check_flags(self):
        self.assertEqual(self.flags["check"], {
            "--store", "--run", "--json", "--verbose", "--env-file",
        })

    def test_searxng_flags(self):
        self.assertEqual(self.flags["searxng"], {
            "--start", "--stop", "--validate", "--image", "--name",
            "--port", "--bind", "--pull", "--search-base", "--json",
            "--verbose", "--env-file",
        })

    def test_investigate_uses_fetch_prefixed_flags(self):
        investigate = self.flags["investigate"]
        for flag in ("--fetch-max-bytes", "--fetch-timeout",
                     "--fetch-max-redirects", "--fetch-min-interval",
                     "--max-http-requests"):
            self.assertIn(flag, investigate)
        # The unprefixed fetch flag names do not exist; --timeout stays
        # because it is the udd-inherited query deadline, not the fetch
        # one.
        for removed in ("--max-bytes", "--max-redirects",
                        "--max-requests", "--min-interval"):
            self.assertNotIn(removed, investigate)
        self.assertIn("--package", investigate)
        self.assertIn("--dataset", investigate)
        self.assertIn("--json", investigate)
        self.assertIn("--env-file", investigate)

    def test_help_and_version_have_no_option_flags(self):
        # Pure subcommands; no -h/--help/--version aliases exist.
        self.assertEqual(self.flags["help"], set())
        self.assertEqual(self.flags["version"], set())
        for module in MODULES:
            self.assertNotIn("--help", self.flags[module])
            self.assertNotIn("--version", self.flags[module])

    def test_help_takes_one_positional_module_choice(self):
        (param,) = MODULE_SPECS["help"]
        self.assertTrue(param.positional)
        self.assertEqual(param.choices, MODULES)

    def test_version_has_no_fields(self):
        self.assertEqual(MODULE_SPECS["version"], ())


class SpecReuseAndBoundariesTest(unittest.TestCase):
    """Query-spec reuse-by-reference and key reachability."""

    def test_investigate_reuses_udd_param_objects(self):
        # Reuse: each shared query field carries the same
        # flag/env key/kind/default on both surfaces. The params are
        # frozen dataclasses, so object identity is unobservable and
        # value equality is the observable contract.
        udd_by_name = {param.name: param for param in MODULE_SPECS["udd"]}
        for name in ("package", "datasets", "release", "limit", "dsn",
                      "timeout"):
            investigate_param = next(
                param for param in MODULE_SPECS["investigate"]
                if param.name == name
            )
            shared = udd_by_name[name]
            self.assertEqual(
                (investigate_param.flag, investigate_param.env_key,
                 investigate_param.kind, investigate_param.default),
                (shared.flag, shared.env_key,
                 shared.kind, shared.default),
                name,
            )

    def test_investigate_does_not_inherit_udd_action_flags(self):
        investigate_names = {
            param.name for param in MODULE_SPECS["investigate"]
        }
        for action_only in ("list_datasets",):
            self.assertNotIn(action_only, investigate_names)

    def test_action_switches_have_no_env_keys(self):
        for module, params in MODULE_SPECS.items():
            for param in params:
                if param.boolean:
                    self.assertIsNone(param.env_key, (module, param.name))

    def test_env_key_reachability_per_module(self):
        self.assertEqual(env_keys_of("udd"), {"GLEAM_UDD_DSN"})
        self.assertEqual(env_keys_of("bts"), {"GLEAM_BTS_SOURCE"})
        self.assertEqual(env_keys_of("check"), {"GLEAM_CHECK_STORE"})
        self.assertEqual(env_keys_of("searxng"), {"GLEAM_SEARCH_BASE"})
        self.assertEqual(env_keys_of("repology"), REPOLOGY_POLICY_KEYS)
        self.assertEqual(env_keys_of("help"), set())
        self.assertEqual(env_keys_of("version"), set())
        # investigate reaches every shared key plus every investigate
        # domain key plus the repology domain keys; the policy-only
        # udd/bts keys are NOT request fields of any module
        # (capability-domain consumers use bind_keys).
        self.assertEqual(
            env_keys_of("investigate"),
            SHARED_KEYS | INVESTIGATE_KEYS | REPOLOGY_POLICY_KEYS
        )

    def test_fixed_input_contracts_are_params_not_keys(self):
        # Package and bug-id validation exist as fixed
        # param kinds, and neither is configurable through an env key.
        for module in ("udd", "investigate"):
            package = next(
                param for param in MODULE_SPECS[module]
                if param.name == "package"
            )
            self.assertEqual(package.kind, "source_name")
            self.assertIsNone(package.env_key)
        bug_id = next(
            param for param in MODULE_SPECS["bts"]
            if param.name == "bug_id"
        )
        self.assertEqual(bug_id.kind, "bug_id")
        self.assertIsNone(bug_id.env_key)

class RobotsExemptFileTest(unittest.TestCase):
    """The operator exemption list file -> frozenset[str].

    Every test runs in a fresh temporary working directory so a
    repo-root operator file can never leak into the resolution of the
    relative default path (same hermetic pattern as the entry-shell
    tests).
    """

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(chdir(tmp))
        self.dir = Path(tmp)

    def test_valid_file_comments_blanks_and_mixed_case(self):
        (self.dir / "robots-exempt.txt").write_text(
            "# comment line\n"
            "\n"
            "Repology.ORG\n"
            "example.org\n"
            "# another comment\n"
            "a-b.c-d.example\n",
            encoding="utf-8")
        hosts = load_robots_exempt_hosts("robots-exempt.txt")
        self.assertEqual(
            hosts, frozenset({"repology.org", "example.org",
                              "a-b.c-d.example"}))

    def test_absent_file_is_empty_set(self):
        self.assertEqual(load_robots_exempt_hosts("robots-exempt.txt"),
                         frozenset())

    def test_invalid_entries_are_usage_errors_naming_file_line_text(self):
        cases = [
            "https://x.y",
            "a b",
            "no-dot",
            "*.wild.example",
            "host.example:8080",
            "repology.org/x",
            "repology.org ",
            " leading.space",
            ".leading.dot",
            "double..dot",
            "under_score.example",
        ]
        for line_no, entry in enumerate(cases, start=1):
            with self.subTest(entry=entry):
                (self.dir / "robots-exempt.txt").write_text(
                    "ok.example\n" * (line_no - 1) + f"{entry}\n"
                    + "also-ok.example\n", encoding="utf-8")
                with self.assertRaises(UsageError) as caught:
                    load_robots_exempt_hosts("robots-exempt.txt")
                message = str(caught.exception)
                self.assertIn("robots-exempt.txt", message)
                self.assertIn(f"line {line_no}", message)
                self.assertIn(repr(entry), message)
                self.assertEqual(caught.exception.exit_code, 2)

    def test_investigate_config_derives_exempt_hosts_frozenset(self):
        (self.dir / "robots-exempt.txt").write_text(
            "# operator list\nRepology.ORG\n\nexample.org\n",
            encoding="utf-8")
        config = build_config(package="wizznic", model="m",
                              model_base="http://127.0.0.1:9",
                              store=str(self.dir / "s"))
        self.assertEqual(config.robots_exempt_hosts,
                         frozenset({"repology.org", "example.org"}))
        self.assertEqual(config.robots_exempt_file, "robots-exempt.txt")

    def test_investigate_config_absent_file_derives_empty_frozenset(self):
        config = build_config(package="wizznic", model="m",
                              model_base="http://127.0.0.1:9",
                              store=str(self.dir / "s"))
        self.assertEqual(config.robots_exempt_hosts, frozenset())

    def test_investigate_config_invalid_file_is_startup_usage_error(self):
        (self.dir / "my-exempt.txt").write_text(
            "fine.example\nbad line here\n", encoding="utf-8")
        with self.assertRaises(UsageError) as caught:
            build_config(package="wizznic", model="m",
                         model_base="http://127.0.0.1:9",
                         store=str(self.dir / "s"),
                         robots_exempt_file="my-exempt.txt")
        message = str(caught.exception)
        self.assertIn("my-exempt.txt", message)
        self.assertIn("line 2", message)
        self.assertIn("bad line here", message)
        self.assertEqual(caught.exception.exit_code, 2)

    def test_flag_and_env_resolution_reach_the_exemption_file(self):
        (self.dir / "flag-list.txt").write_text("flag.example\n",
                                                encoding="utf-8")
        config = build_config(argv=["--robots-exempt-file", "flag-list.txt"])
        self.assertEqual(config.robots_exempt_hosts,
                         frozenset({"flag.example"}))
        (self.dir / ".env").write_text(
            "GLEAM_ROBOTS_EXEMPT_FILE=env-list.txt\n", encoding="utf-8")
        (self.dir / "env-list.txt").write_text("env.example\n",
                                               encoding="utf-8")
        config = build_config(argv=[])
        self.assertEqual(config.robots_exempt_hosts,
                         frozenset({"env.example"}))

    def test_config_identifiers_keep_approved_spellings(self):
        # Configuration identifiers stay independent of the tool
        # surface names (github, fetch_bug): the endpoint base flag
        # and the capability toggles parse with their own spellings.
        config = build_config(argv=["--github-api-base",
                                    "https://gh.example",
                                    "--no-bts", "--no-github"])
        self.assertEqual(config.github_api_base, "https://gh.example")
        self.assertTrue(config.no_bts)
        self.assertTrue(config.no_github)


if __name__ == "__main__":
    unittest.main()
