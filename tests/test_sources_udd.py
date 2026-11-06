"""Offline tests for the UDD source adapter.

Zero network, zero real database: every driver interaction patches
``gleam.core.sources.udd.pgdriver.pg8000`` with a fake module whose
``native.Connection`` is a connection double (the client deliberately
has no injection seam). The double consumes one
``(owner, columns, rows)`` script entry per query statement; session
setup (``SET``/``BEGIN``) returns no rows, mirroring the adapter
contract.
"""

from __future__ import annotations

import re
import types
import unittest
from unittest.mock import patch

from gleam.core.result import UsageError
from gleam.core.sources.udd import (
    DATASET_NAMES,
    DATASETS_TEMPLATE_VERSION,
    UddClient,
    UddPolicy,
    UddResult,
    list_datasets,
)
from gleam.core.sources.udd.catalog import (
    DATASETS,
    RELEASES,
    build_statements,
)
from gleam.core.sources.udd.client import WHERE
from gleam.core.sources.udd.pgdriver import TLS_FALLBACK_NOTE

PATCH_TARGET = "gleam.core.sources.udd.pgdriver.pg8000"
DSN = "postgresql://udd-mirror:secret@udd-mirror.debian.net/udd"

EXPECTED_DATASET_ORDER = (
    "sources", "upstream", "upstream_metadata", "vcswatch", "urls",
    "binaries", "bugs", "bugs_count", "archived_bugs", "wnpp",
    "upload_history", "upload_history_closes", "removals",
    "testing_autoremovals", "popcon_src", "ci", "reproducible",
    "lintian",
)


def _columns(name: str) -> tuple[str, ...]:
    from gleam.core.sources.udd.catalog import DATASETS
    return tuple(DATASETS[name]["columns"])


DATASETS_COLUMNS = {name: _columns(name) for name in EXPECTED_DATASET_ORDER}

META_COLUMNS = ("server_version", "server_now")
META_ROW = ("PostgreSQL 16.2", "2026-11-14 12:00:00+00")


def statements_script(dataset_rows: dict, meta: bool = True) -> list:
    """Driver script entries in execution order: the meta statement
    (owner ``None``) first, then one entry per dataset in insertion
    order. Row values are tuples aligned with the dataset's
    registered columns; an ``Exception`` instance in place of the
    rows makes the double raise when that statement runs."""

    script: list[tuple] = [
        (None, META_COLUMNS, [META_ROW] if meta else [])]
    for name, rows in dataset_rows.items():
        script.append((name, DATASETS_COLUMNS[name],
                       rows if isinstance(rows, Exception)
                       else list(rows)))
    return script


def make_driver(script=None, connect_error=None, refuse_ssl=False):
    """Build a fake pg8000 module double plus its logs.

    Returns ``(module, calls, runs)``: ``calls`` records one kwargs
    dict per ``Connection(...)`` construction, ``runs`` one
    ``(sql, params)`` pair per executed statement. Each new
    connection consumes a fresh copy of the script, so one double
    serves repeated queries.
    """

    calls: list[dict] = []
    runs: list[tuple[str, dict]] = []
    script = list(script or [])

    class _FakeConnection:
        def __init__(self, **kwargs):
            calls.append(dict(kwargs))
            if refuse_ssl and kwargs.get("ssl_context") is True:
                raise RuntimeError("server refuses SSL")
            if connect_error is not None:
                raise connect_error
            self.script = list(script)
            self.columns = None

        def run(self, sql, **params):
            runs.append((sql, params))
            lowered = sql.lower()
            if lowered.startswith("set ") \
                    or lowered.startswith("begin"):
                return []
            _, columns, rows = self.script.pop(0)
            if isinstance(rows, Exception):
                raise rows
            self.columns = [{"name": name} for name in columns]
            return rows

        def close(self):
            pass

    module = types.ModuleType("pg8000")
    module.native = types.SimpleNamespace(Connection=_FakeConnection)
    return module, calls, runs


def make_broken_driver():
    """A module double with a broken driver attribute chain."""

    return types.ModuleType("pg8000")  # no .native


# ---- Static template audit helpers -------------------------------------------
#
# The audit parses the shipped template SQL directly (registry ->
# SELECT list -> per-column classification); it pins nothing by
# copying template text, so any template edit is re-audited.
# Assumption (holds for every shipped template): string literals never
# contain parens or commas, so depth-based splitting is exact.

#: UDD columns that are plain text in the live schema: text-safe with
#: no explicit cast. Deliberately excludes debversion/integer/timestamp
#: columns (version, id, arrival, last_check, ...) so a stripped
#: ``::text`` fails the audit.
TEXT_SAFE_COLUMNS = frozenset({
    "arch", "architecture", "bin", "browser", "changed_by",
    "changelog_distribution", "checkmethod", "ci_status", "commit_id",
    "component", "distribution", "done", "error", "error_message",
    "errors", "file", "filename", "ftpmaster", "homepage", "information",
    "key", "maintainer", "message", "name", "package", "package_arch",
    "package_type", "previous_status", "priority", "reasons",
    "redirect_url", "release", "requestor", "section", "severity",
    "source", "status", "suite", "tag", "tag_type", "title", "type",
    "upstream_url", "upstream_version", "url", "value", "vcs",
    "vcs_browser", "vcs_type", "vcs_url", "watch_file",
})

#: SQL functions whose return type is text regardless of input.
TEXT_RETURNING_CALLS = re.compile(
    r"\b(string_agg|array_to_string|version)\s*\(", re.IGNORECASE)


def _split_top_level(text: str) -> list[str]:
    """Comma-split at paren depth 0, whitespace-stripped, empties dropped."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _strip_alias(expr: str) -> str:
    """Drop a trailing depth-0 ``AS alias`` from a column expression."""
    depth = 0
    for m in re.finditer(r"[()]|\bAS\b", expr, re.IGNORECASE):
        token = m.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            return expr[:m.start()].strip()
    return expr.strip()


def _select_column_expressions(sql: str) -> list[str]:
    """Column expressions of a statement's top-level SELECT list; the
    list ends at the first depth-0 FROM or at the trailing ``;``."""
    depth = 0
    start = sql.upper().index("SELECT") + len("SELECT")
    end = len(sql)
    for i in range(start, len(sql)):
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif (depth == 0 and sql[i:i + 4].upper() == "FROM"
              and sql[i - 1].isspace() and i + 4 < len(sql)
              and sql[i + 4].isspace()):
            end = i
            break
    return _split_top_level(sql[start:end].rstrip().rstrip(";"))


def _is_text_safe(expr: str) -> bool:
    """True when the column expression yields text: explicit ``::text``
    anywhere, ``coalesce(x, '')`` where x is text-safe, a bare
    (possibly qualified) TEXT_SAFE_COLUMNS member, or a text-returning
    function call."""
    stripped = _strip_alias(expr)
    if "::text" in stripped.lower():
        return True
    low = stripped.lower()
    if low.startswith("coalesce(") and stripped.endswith(")"):
        args = _split_top_level(stripped[len("coalesce("):-1])
        if len(args) == 2 and args[1] == "''":
            return _is_text_safe(args[0])
    if re.fullmatch(r"(?:\w+\.)?\w+", stripped):
        return stripped.rsplit(".", 1)[-1].lower() in TEXT_SAFE_COLUMNS
    return bool(TEXT_RETURNING_CALLS.search(low))


class CatalogTests(unittest.TestCase):
    """The 18-dataset registry, transcribed verbatim."""

    def test_eighteen_datasets_in_frozen_order(self):
        catalog = list_datasets()
        self.assertEqual(len(catalog), 18)
        self.assertEqual(tuple(entry["name"] for entry in catalog),
                         EXPECTED_DATASET_ORDER)
        self.assertEqual(DATASET_NAMES, EXPECTED_DATASET_ORDER)

    def test_all_max_limit_fifty_and_defaults(self):
        for entry in list_datasets():
            self.assertEqual(entry["max_limit"], 50, entry["name"])
            if entry["name"] in ("archived_bugs", "lintian"):
                self.assertEqual(entry["default_limit"], 10)
            else:
                self.assertEqual(entry["default_limit"], 20)

    def test_heavy_row_caps(self):
        from gleam.core.sources.udd.catalog import DATASETS
        self.assertEqual(DATASETS["archived_bugs"]["row_cap"], 10)
        self.assertEqual(DATASETS["lintian"]["row_cap"], 10)
        uncapped = {name for name in DATASET_NAMES
                    if DATASETS[name]["row_cap"] is not None}
        self.assertEqual(uncapped, {"archived_bugs", "lintian"})

    def test_release_scoped_set(self):
        scoped = {entry["name"] for entry in list_datasets()
                  if entry["release_scoped"]}
        self.assertEqual(scoped,
                         {"sources", "upstream", "binaries", "reproducible"})

    def test_list_datasets_is_pure_no_database_contact(self):
        module, calls, _ = make_driver()
        with patch(PATCH_TARGET, module):
            catalog = list_datasets()
        self.assertEqual(len(catalog), 18)
        self.assertEqual(calls, [])

    def test_list_datasets_returns_fresh_copies(self):
        first = list_datasets()
        first[0]["columns"].append("tampered")
        first[0]["name"] = "tampered"
        second = list_datasets()
        self.assertEqual(second[0]["name"], "sources")
        self.assertNotIn("tampered", second[0]["columns"])

    def test_build_statements_attribution_and_params(self):
        statements = build_statements(("sources", "upstream"), {})
        self.assertEqual([owner for owner, _ in statements],
                         [None, "sources", "upstream"])
        meta_sql = statements[0][1]
        self.assertEqual(meta_sql,
                         "SELECT version() AS server_version, "
                         "now()::text AS server_now;")
        for _, sql in statements:
            self.assertNotIn("grp", sql)
            self.assertNotIn(":'", sql)
            self.assertNotIn("BEGIN", sql)
            self.assertNotIn("COMMIT", sql)
        sources_sql = statements[1][1]
        self.assertIn("WHERE source = :source", sources_sql)
        self.assertIn(":release = ''", sources_sql)
        self.assertTrue(sources_sql.endswith("LIMIT :limit1;"))

    def test_build_statements_heavy_caps_use_least_clause(self):
        statements = build_statements(("sources", "lintian"),
                                      {"lintian": 10})
        self.assertIn("LIMIT :limit1;", statements[1][1])
        self.assertIn(f"LIMIT LEAST(:limit1, {10 + 1});",
                      statements[2][1])

    def test_templates_use_pg8000_named_params_only(self):
        for name in DATASET_NAMES:
            sql = DATASETS[name]["template"]
            self.assertNotIn(":'", sql, name)
            self.assertNotIn("AS grp", sql, name)
            self.assertIn(":source", sql, name)
            self.assertEqual(":release" in sql,
                             DATASETS[name]["release_scoped"], name)
        meta_sql = build_statements((), {})[0][1]
        self.assertNotIn(":'", meta_sql)
        self.assertNotIn("AS grp", meta_sql)
        self.assertNotIn(":source", meta_sql)
        self.assertNotIn(":release", meta_sql)

    def test_every_template_column_is_text_safe(self):
        # Static audit over the shipped templates: every output column
        # is explicit ::text, coalesce(...,'') over a text column, or a
        # known text column, so driver rows are all-str (NULL ≡ '').
        for name in DATASET_NAMES:
            for expr in _select_column_expressions(
                    DATASETS[name]["template"]):
                self.assertTrue(
                    _is_text_safe(expr),
                    f"{name}: column not text-safe: {expr!r}")
        meta_sql = build_statements((), {})[0][1]
        for expr in _select_column_expressions(meta_sql):
            self.assertTrue(
                _is_text_safe(expr),
                f"meta: column not text-safe: {expr!r}")

    def test_release_allowlist_content(self):
        for release in ("sid", "trixie", "oldstable", "bookworm-security"):
            self.assertIn(release, RELEASES)
        self.assertNotIn("hamm", RELEASES)


class ValidationTests(unittest.TestCase):
    """Deterministic caller errors raise UsageError before any
    connection is made."""

    def setUp(self):
        self.module, self.calls, _ = make_driver(
            script=statements_script({"sources": []}))

    def run_query(self, **kwargs):
        client = UddClient(dsn=DSN)
        defaults = {"datasets": ["sources"], "package": "wizznic"}
        defaults.update(kwargs)
        return client.query(**defaults)

    def assert_usage_error(self, **kwargs):
        with patch(PATCH_TARGET, self.module):
            with self.assertRaises(UsageError):
                self.run_query(**kwargs)
        self.assertEqual(self.calls, [])  # no connection was ever made

    def test_bad_package_names_rejected(self):
        for bad in ("Wizznic", "bad_name", "-leading", ".dot", "",
                    "a" * 65, "-"):
            self.assert_usage_error(package=bad)
        self.assert_usage_error(package=123)
        self.assert_usage_error(package=True)

    def test_good_package_names_accepted_shape(self):
        # Only connects with the fake driver in place; valid policy names.
        for good in ("wizznic", "a1", "libx11-6", "trailing-", "0ad",
                     "python3.+foo"):
            with patch(PATCH_TARGET, self.module):
                result = self.run_query(package=good)
            self.assertEqual(result.status, "ok", good)

    def test_unknown_dataset_rejected(self):
        self.assert_usage_error(datasets=["nope"])
        self.assert_usage_error(datasets=["sources", "nope"])

    def test_datasets_type_rules(self):
        self.assert_usage_error(datasets="sources")   # bare string
        self.assert_usage_error(datasets=[])          # empty selection
        self.assert_usage_error(datasets=[42])
        self.assert_usage_error(datasets=None)

    def test_more_than_six_datasets_rejected(self):
        seven = ["sources", "upstream", "vcswatch", "bugs_count", "wnpp",
                 "popcon_src", "ci"]
        self.assert_usage_error(datasets=seven)

    def test_release_allowlist(self):
        for bad in ("hamm", "SID", 7):
            self.assert_usage_error(release=bad)
        # empty string and None mean "no filter"
        with patch(PATCH_TARGET, self.module):
            self.assertEqual(self.run_query(release="").release, None)
            self.assertEqual(self.run_query(release=None).release, None)

    def test_limit_bounds(self):
        for bad in (0, 51, -1, "20", True):
            self.assert_usage_error(limit=bad)
        with patch(PATCH_TARGET, self.module):
            self.run_query(limit=1)
            self.run_query(limit=50)

    def test_total_effective_rows_cap(self):
        # 6 datasets x limit 21 = 126 effective rows > 120.
        datasets = ["sources", "upstream", "vcswatch", "bugs_count",
                    "wnpp", "popcon_src"]
        self.assert_usage_error(datasets=datasets, limit=21)
        # exactly 120 at the default limit 20 is allowed
        with patch(PATCH_TARGET, self.module):
            self.run_query(datasets=datasets, limit=20)

    def test_statement_count_cap(self):
        policy = UddPolicy(max_datasets=10, max_queries=8)
        client = UddClient(dsn=DSN, policy=policy)
        eight = list(EXPECTED_DATASET_ORDER[:8])
        with patch(PATCH_TARGET, self.module):
            with self.assertRaises(UsageError):
                client.query(eight, package="wizznic", limit=1)
        self.assertEqual(self.calls, [])

    def test_dsn_must_be_nonempty_string(self):
        for bad in ("", None, 5):
            with self.assertRaises(UsageError):
                UddClient(dsn=bad)


class DriverSessionTests(unittest.TestCase):
    """What the client hands to the driver: DSN components, session
    setup, and the shared named params (the adapter's own tests pin
    the deeper protocol)."""

    def test_dsn_components_reach_the_connection(self):
        module, calls, _ = make_driver(
            script=statements_script({"sources": []}))
        with patch(PATCH_TARGET, module):
            UddClient(dsn=DSN).query(["sources"], package="wizznic")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["user"], "udd-mirror")
        self.assertEqual(calls[0]["password"], "secret")
        self.assertEqual(calls[0]["host"], "udd-mirror.debian.net")
        self.assertEqual(calls[0]["port"], 5432)
        self.assertEqual(calls[0]["database"], "udd")
        self.assertIs(calls[0]["ssl_context"], True)  # TLS preferred

    def test_statement_timeout_flows_into_session_setup(self):
        module, _, runs = make_driver(
            script=statements_script({"sources": []}))
        with patch(PATCH_TARGET, module):
            UddClient(dsn=DSN,
                      policy=UddPolicy(statement_timeout_ms=2500)
                      ).query(["sources"], package="wizznic")
        self.assertEqual(runs[0],
                         ("SET client_encoding TO 'UTF8'", {}))
        self.assertEqual(runs[1],
                         ("SET statement_timeout = 2500", {}))
        self.assertEqual(runs[2], ("BEGIN READ ONLY", {}))

    def test_named_params_flow_to_every_statement(self):
        module, _, runs = make_driver(
            script=statements_script({"sources": [], "bugs_count": []}))
        with patch(PATCH_TARGET, module):
            UddClient(dsn=DSN).query(["sources", "bugs_count"],
                                      package="wizznic",
                                      release="sid", limit=7)
        expected = {"source": "wizznic", "release": "sid", "limit1": 8}
        for sql, params in runs[3:]:
            self.assertEqual(params, expected, sql)
        # no filter means the release param is the empty string
        module, _, runs = make_driver(
            script=statements_script({"sources": []}))
        with patch(PATCH_TARGET, module):
            UddClient(dsn=DSN).query(["sources"], package="wizznic")
        self.assertEqual(runs[3][1],
                         {"source": "wizznic", "release": "",
                          "limit1": 21})


class SuccessPathTests(unittest.TestCase):
    """Columnar rows from statement attribution, parallel
    provenance."""

    SRC_ROW = ("wizznic", "1.0-1", "sid", "QA", "https://wz.example", "",
               "git", "https://salsa.example/w/wizznic.git",
               "https://salsa.example/w/wizznic", "games", "extra")
    SRC_ROW2 = ("wizznic", "1.0-1", "trixie", "QA", "https://wz.example",
                "", "git", "https://salsa.example/w/wizznic.git",
                "https://salsa.example/w/wizznic", "games", "extra")
    BC_ROW = ("wizznic", "2", "9")

    def _run(self, script, **kwargs):
        module, _, _ = make_driver(script=script)
        defaults = {"datasets": ["sources"], "package": "wizznic"}
        defaults.update(kwargs)
        with patch(PATCH_TARGET, module):
            return UddClient(dsn=DSN).query(**defaults)

    def test_rows_with_provenance(self):
        result = self._run(
            statements_script({"sources": [self.SRC_ROW, self.SRC_ROW2]}))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.row_count, 2)
        self.assertEqual(len(result.provenance), 2)
        self.assertEqual(result.provenance,
                         ["udd:sources#row=1", "udd:sources#row=2"])
        self.assertEqual(list(result.rows[0]),
                         list(DATASETS_COLUMNS["sources"]))
        self.assertEqual(result.rows[0]["homepage"], "https://wz.example")
        self.assertEqual(result.rows[1]["release"], "trixie")
        self.assertFalse(result.truncated)
        self.assertEqual(result.server["server_version"],
                         "PostgreSQL 16.2")
        self.assertEqual(result.template_version,
                         DATASETS_TEMPLATE_VERSION)

    def test_multi_dataset_row_and_provenance_order(self):
        # statements execute in requested order; rows of each dataset
        # stay grouped under their owning statement.
        result = self._run(
            statements_script({"sources": [self.SRC_ROW],
                               "bugs_count": [self.BC_ROW]}),
            datasets=["sources", "bugs_count"])
        self.assertEqual(result.provenance,
                         ["udd:sources#row=1", "udd:bugs_count#row=1"])
        self.assertEqual(result.rows[1]["rc_bugs"], "2")
        self.assertEqual(set(result.dataset_info),
                         {"sources", "bugs_count"})

    def test_dataset_input_dedup_preserving_order(self):
        result = self._run(
            statements_script({"bugs_count": [], "sources": [self.SRC_ROW]}),
            datasets=["bugs_count", "sources", "sources", "bugs_count"])
        self.assertEqual(result.datasets, ("bugs_count", "sources"))
        self.assertEqual(result.provenance, ["udd:sources#row=1"])
        self.assertEqual(result.dataset_info["bugs_count"]["row_count"], 0)

    def test_real_empty_result_is_ok_even_for_unknown_source(self):
        result = self._run(
            statements_script({"removals": [], "testing_autoremovals": []}),
            datasets=["removals", "testing_autoremovals"],
            package="zzzzunkn0wn")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.row_count, 0)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.provenance, [])
        self.assertFalse(result.truncated)
        self.assertFalse(result.more_available)
        self.assertEqual(result.errors, [])
        # the meta statement still ran, so server facts are present
        self.assertEqual(result.server["server_version"],
                         "PostgreSQL 16.2")

    def test_meta_statement_without_rows_means_server_none(self):
        result = self._run(
            statements_script({"sources": []}, meta=False))
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.server)

    def test_to_data_field_set(self):
        result = self._run(
            statements_script({"sources": [self.SRC_ROW]}))
        data = result.to_data()
        self.assertEqual(set(data), {
            "package", "release", "datasets", "rows", "row_count",
            "truncated", "more_available", "provenance"})
        self.assertEqual(data["package"], "wizznic")
        self.assertEqual(data["datasets"], ["sources"])

    def test_notes_disclose_semantics(self):
        result = self._run(
            statements_script({"sources": [self.SRC_ROW]}))
        self.assertTrue(result.notes)
        self.assertTrue(any("no speculative filling" in note
                            for note in result.notes))
        self.assertTrue(any("0 rows is a valid honest result" in note
                            for note in result.notes))


class TruncationTests(unittest.TestCase):
    """Truncation is flagged, never masked; adapter stays ok
    and the entry maps ``truncated`` to ``partial``."""

    SRC_ROW = ("wizznic", "1.0-1", "sid", "QA", "https://wz", "", "git",
               "https://v", "https://b", "games", "extra")
    LINT_ROW = ("wizznic", "warning", "source", "1.0-1", "all", "tag-a",
                "info")

    def _run(self, script, **kwargs):
        module, _, _ = make_driver(script=script)
        with patch(PATCH_TARGET, module):
            return UddClient(dsn=DSN).query(**kwargs)

    def test_limit_plus_one_probe_marks_truncation(self):
        result = self._run(
            statements_script({"sources": [self.SRC_ROW] * 3}),
            datasets=["sources"], package="wizznic", limit=2)
        self.assertEqual(result.status, "ok")  # entry derives partial
        self.assertTrue(result.truncated)
        self.assertTrue(result.more_available)
        self.assertEqual(result.row_count, 2)
        self.assertEqual(result.provenance,
                         ["udd:sources#row=1", "udd:sources#row=2"])
        info = result.dataset_info["sources"]
        self.assertEqual(info["hidden_row_count"], 1)
        self.assertTrue(info["more_available"])

    def test_heavy_dataset_effective_cap_ten(self):
        result = self._run(
            statements_script({"lintian": [self.LINT_ROW] * 11}),
            datasets=["lintian"], package="wizznic", limit=50)
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_count, 10)
        self.assertEqual(result.dataset_info["lintian"]["limit"], 10)
        self.assertEqual(result.dataset_info["lintian"]["hidden_row_count"],
                         1)


class FailureTests(unittest.TestCase):
    """Driver failures map straight through to failed results with
    the stable ``udd-*`` codes."""

    SRC_COLUMNS = DATASETS_COLUMNS["sources"]

    def _run(self, **driver_kwargs):
        module = driver_kwargs.pop("module", None)
        if module is None:
            module, _, _ = make_driver(**driver_kwargs)
        with patch(PATCH_TARGET, module):
            return UddClient(dsn=DSN).query(["sources"],
                                            package="wizznic")

    def test_driver_unavailable_is_failed_with_stable_code(self):
        result = self._run(module=make_broken_driver())
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.errors)
        self.assertEqual(result.errors[0].code, "udd-driver-unavailable")
        self.assertEqual(result.errors[0].where, WHERE)
        self.assertEqual(result.row_count, 0)
        self.assertEqual(result.rows, [])

    def test_connection_failure_is_connection_error(self):
        result = self._run(connect_error=RuntimeError("mirror down"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "udd-connection-error")
        self.assertIn("mirror down", result.errors[0].message)
        self.assertEqual(result.errors[0].details["dsn_display"],
                         "postgresql://udd-mirror.debian.net/udd")

    def test_connect_timeout_is_timeout(self):
        result = self._run(connect_error=TimeoutError("timed out"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "udd-timeout")

    def test_statement_failure_is_query_error(self):
        result = self._run(script=statements_script({
            "sources": RuntimeError(
                'relation "nope" does not exist')}))
        self.assertEqual(result.status, "failed")
        error = result.errors[0]
        self.assertEqual(error.code, "udd-query-error")
        self.assertIn("relation", error.message)

    def test_server_statement_timeout_cancel_is_timeout(self):
        result = self._run(script=statements_script({
            "sources": RuntimeError(
                "canceling statement due to statement timeout")}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "udd-timeout")

    def test_tls_fallback_note_surfaces_in_failure_details(self):
        # encrypted connect refused, plaintext retry succeeds, then a
        # statement fails: the failure still discloses the fallback.
        result = self._run(
            refuse_ssl=True,
            script=statements_script(
                {"sources": RuntimeError("boom")}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "udd-query-error")
        self.assertEqual(result.errors[0].details["driver_notes"],
                         [TLS_FALLBACK_NOTE])

    def test_failed_result_keeps_meta_fields(self):
        result = self._run(module=make_broken_driver())
        self.assertEqual(result.package, "wizznic")
        self.assertEqual(result.datasets, ("sources",))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.provenance, [])
        self.assertFalse(result.truncated)


class ResultCapTests(unittest.TestCase):
    """The policy char caps wire into the driver's honesty hook:
    oversized content fails the query, never truncates silently."""

    SRC_ROW = ("wizznic", "1.0-1", "sid", "QA", "https://wz", "", "git",
               "https://v", "https://b", "games", "extra")

    def _run(self, policy):
        module, _, _ = make_driver(
            script=statements_script({"sources": [self.SRC_ROW]}))
        with patch(PATCH_TARGET, module):
            return UddClient(dsn=DSN, policy=policy).query(
                ["sources"], package="wizznic")

    def test_oversized_cell_fails_honestly(self):
        result = self._run(UddPolicy(max_cell_chars=8))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "udd-result-too-large")
        self.assertIn("max_cell_chars=8", result.errors[0].message)
        self.assertEqual(result.row_count, 0)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.provenance, [])

    def test_oversized_total_fails_honestly(self):
        result = self._run(UddPolicy(max_total_row_chars=10))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "udd-result-too-large")
        self.assertIn("max_total_row_chars=10", result.errors[0].message)
        self.assertEqual(result.row_count, 0)
        self.assertEqual(result.rows, [])


class ResultTypeTests(unittest.TestCase):

    def test_ok_result_without_warnings_has_empty_arrays(self):
        module, _, _ = make_driver(
            script=statements_script({"sources": []}))
        with patch(PATCH_TARGET, module):
            result = UddClient(dsn=DSN).query(["sources"],
                                              package="wizznic")
        self.assertEqual(result.errors, [])
        self.assertEqual(result.warnings, [])
        self.assertIsInstance(result, UddResult)
        self.assertTrue(result.notes)


if __name__ == "__main__":
    unittest.main()
