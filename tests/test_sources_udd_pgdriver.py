"""Offline tests for the pg8000 driver adapter.

Zero network, zero real database: every driver interaction patches
``gleam.core.sources.udd.pgdriver.pg8000`` with a fake module whose
``native.Connection`` is a connection double, and every budget test
patches the adapter's ``time`` with a controllable clock (no real
sleeps anywhere). DSN parsing tests patch ``getpass`` so the bare-DSN
OS-user fallback stays deterministic.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from gleam.core.sources.udd.catalog import DATASETS, build_statements
from gleam.core.sources.udd.pgdriver import (
    TLS_FALLBACK_NOTE,
    DriverFailure,
    DriverPolicy,
    DriverRequest,
    DriverSuccess,
    ResultCaps,
    dsn_display,
    parse_dsn,
    run_readonly,
)

PATCH_TARGET = "gleam.core.sources.udd.pgdriver.pg8000"
GETPASS_TARGET = "gleam.core.sources.udd.pgdriver.getpass"
TIME_TARGET = "gleam.core.sources.udd.pgdriver.time"

#: Run-phase DSN (userinfo present, so getpass is never consulted).
DSN = "postgresql://udd:SEKRIT@udd-mirror.debian.net/udd"

BARE_DSN = "postgresql://udd-mirror.debian.net/udd"

STATEMENTS = [
    (None, "SELECT version() AS server_version, now()::text AS server_now"),
    ("sources", "SELECT version FROM sources WHERE source = :source "
                "LIMIT :limit1"),
]
PARAMS = {"source": "ncdu", "release": "", "limit1": 21}


class FakeClock:
    """Deterministic monotonic clock; tests advance it explicitly."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


def make_connection_cls(behavior: dict):
    """Build a Connection double plus its constructor-call log.

    Behavior keys (all optional): ``script`` (list of
    ``(columns, rows)`` consumed per query statement), ``refuse_ssl``
    (constructor raises when ``ssl_context is True``),
    ``connect_error`` (exception raised by every constructor call),
    ``connect_burn`` (seconds the failing TLS constructor consumes),
    ``errors`` (exception per run index; ``None`` = succeed),
    ``statement_burn`` (seconds each query-statement run consumes),
    ``clock`` (the FakeClock the double advances).
    """

    calls: list[dict] = []
    instances: list = []

    class _FakeConnection:
        def __init__(self, **kwargs):
            calls.append(dict(kwargs))
            if behavior.get("refuse_ssl") and kwargs.get("ssl_context") is True:
                if behavior.get("connect_burn") and behavior.get("clock"):
                    behavior["clock"].now += behavior["connect_burn"]
                raise RuntimeError("server refuses SSL")
            if behavior.get("connect_error") is not None:
                raise behavior["connect_error"]
            self.script = list(behavior.get("script") or [])
            self.errors = list(behavior.get("errors") or [])
            self.burn = behavior.get("statement_burn", 0.0)
            self.clock = behavior.get("clock")
            self.runs: list[tuple[str, dict]] = []
            self.columns = None
            self.closed = False
            instances.append(self)

        def run(self, sql, **params):
            self.runs.append((sql, params))
            index = len(self.runs) - 1
            if index < len(self.errors) and self.errors[index] is not None:
                raise self.errors[index]
            lowered = sql.lower()
            if lowered.startswith("set ") \
                    or lowered.startswith("begin"):
                return []
            columns, rows = self.script.pop(0)
            if self.burn and self.clock is not None:
                self.clock.now += self.burn
            self.columns = [{"name": name} for name in columns]
            return rows

        def close(self):
            self.closed = True

    return _FakeConnection, calls, instances


def make_module(connection_cls) -> types.ModuleType:
    """A bare pg8000 stand-in exposing ``native.Connection``."""

    module = types.ModuleType("pg8000")
    module.native = types.SimpleNamespace(Connection=connection_cls)
    return module


def run_with(behavior: dict, *, dsn: str = DSN,
             statements=STATEMENTS, params=PARAMS,
             policy=None, caps=None):
    """Wire the fake driver and run one request; returns the outcome
    plus ``(cls, calls, instances)`` for assertions."""

    clock = behavior.setdefault("clock", FakeClock())
    cls, calls, instances = make_connection_cls(behavior)
    with patch(PATCH_TARGET, make_module(cls)), \
            patch(TIME_TARGET,
                  types.SimpleNamespace(monotonic=clock.monotonic)):
        outcome = run_readonly(DriverRequest(
            dsn=dsn, policy=policy or DriverPolicy(),
            statements=list(statements), params=params, caps=caps))
    return outcome, (cls, calls, instances)


class DsnParsingTests(unittest.TestCase):
    """Both DSN shapes, redaction, and rejection without echo."""

    def test_full_shape(self):
        parts = parse_dsn(
            "postgresql://udd:p%40ss@udd-mirror.debian.net:5433/udd")
        self.assertEqual(parts.user, "udd")
        self.assertEqual(parts.password, "p@ss")  # percent-decoded
        self.assertEqual(parts.host, "udd-mirror.debian.net")
        self.assertEqual(parts.port, 5433)
        self.assertEqual(parts.database, "udd")

    def test_bare_default_dsn_uses_os_user(self):
        with patch(GETPASS_TARGET,
                   types.SimpleNamespace(getuser=lambda: "os-user")):
            parts = parse_dsn(BARE_DSN)
        self.assertEqual(
            (parts.user, parts.password, parts.host, parts.port,
             parts.database),
            ("os-user", None, "udd-mirror.debian.net", 5432, "udd"))

    def test_password_hidden_from_repr(self):
        parts = parse_dsn(DSN)
        self.assertNotIn("SEKRIT", repr(parts))
        self.assertNotIn("SEKRIT", str(parts))

    def test_invalid_dsns_raise_without_echoing_input(self):
        for bad in ("not-a-dsn", "postgresql://",
                    "postgresql://udd:SEKRIT@host:99999/db"):
            with self.assertRaises(ValueError) as ctx:
                parse_dsn(bad)
            self.assertNotIn("SEKRIT", str(ctx.exception))

    def test_dsn_display_drops_credentials(self):
        self.assertEqual(dsn_display(DSN),
                         "postgresql://udd-mirror.debian.net/udd")
        self.assertNotIn("SEKRIT", dsn_display(DSN))


class SessionContractTests(unittest.TestCase):
    """Happy path: ordering, params, column names, passthrough."""

    META_COLUMNS = ("server_version", "server_now")
    SOURCE_COLUMNS = tuple(DATASETS["sources"]["columns"])
    META_ROWS = [("PostgreSQL 16.2", "2026-11-18 12:00:00+00")]
    SOURCE_ROWS = [("1.19-1",)]

    def test_session_setup_and_per_statement_execution(self):
        outcome, (cls, calls, instances) = run_with({
            "script": [(self.META_COLUMNS, self.META_ROWS),
                       (self.SOURCE_COLUMNS, self.SOURCE_ROWS)],
        })
        self.assertIsInstance(outcome, DriverSuccess)
        # one TLS attempt, full parsed DSN, bounded constructor timeout
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {
            "user": "udd", "host": "udd-mirror.debian.net", "port": 5432,
            "database": "udd", "password": "SEKRIT",
            "ssl_context": True, "timeout": 45.0})
        con = instances[0]
        # The sequence IS the offline teeth for the encoding pin:
        # the SQL_ASCII decode crash happens inside pg8000 itself,
        # which offline fakes cannot reproduce; the live proof is
        # F3's re-run against the real mirror. Here we pin the exact
        # session setup -- encoding first, then timeout, then BEGIN
        # -- before any statement executes.
        self.assertEqual(
            con.runs,
            [("SET client_encoding TO 'UTF8'", {}),
             ("SET statement_timeout = 15000", {}),
             ("BEGIN READ ONLY", {}),
             (STATEMENTS[0][1], PARAMS),
             (STATEMENTS[1][1], PARAMS)])
        # per-statement results: owner, column names, rows passthrough
        self.assertEqual(outcome.statements[0],
                         (None, self.META_COLUMNS, self.META_ROWS))
        self.assertEqual(outcome.statements[1][0], "sources")
        self.assertEqual(outcome.statements[1][1], self.SOURCE_COLUMNS)
        self.assertIs(outcome.statements[1][2], self.SOURCE_ROWS)
        self.assertEqual(outcome.notes, ())
        self.assertEqual(outcome.dsn_display,
                         "postgresql://udd-mirror.debian.net/udd")
        self.assertIsInstance(outcome.duration_s, float)
        self.assertTrue(instances[0].closed)

    def test_catalog_statements_and_params_interop(self):
        statements = build_statements(("sources",), {})
        outcome, _ = run_with({
            "script": [(self.META_COLUMNS, self.META_ROWS),
                       (self.SOURCE_COLUMNS, self.SOURCE_ROWS)],
        }, statements=statements)
        self.assertIsInstance(outcome, DriverSuccess)
        self.assertEqual([owner for owner, _, _ in outcome.statements],
                         [None, "sources"])


class TlsPreferTests(unittest.TestCase):
    """First attempt encrypted; on failure, plaintext retry + note."""

    def test_fallback_retries_plaintext_with_note(self):
        outcome, (cls, calls, instances) = run_with({
            "refuse_ssl": True,
            "script": [(("server_version", "server_now"), [])],
        }, statements=STATEMENTS[:1])
        self.assertIsInstance(outcome, DriverSuccess)
        self.assertEqual([c["ssl_context"] for c in calls],
                         [True, False])
        self.assertIn(TLS_FALLBACK_NOTE, outcome.notes)
        # the plaintext retry also runs inside the budget, not fresh
        self.assertLessEqual(calls[1]["timeout"], 45.0)
        self.assertGreater(calls[1]["timeout"], 0.0)
        self.assertTrue(instances[0].closed)

    def test_encrypted_success_has_no_retry_and_no_note(self):
        outcome, (cls, calls, _) = run_with({
            "script": [(("server_version", "server_now"), [])],
        }, statements=STATEMENTS[:1])
        self.assertIsInstance(outcome, DriverSuccess)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["ssl_context"], True)
        self.assertEqual(outcome.notes, ())

    def test_budget_exhausted_by_tls_attempt_is_timeout(self):
        clock = FakeClock(now=0.0)
        outcome, (cls, calls, _) = run_with({
            "refuse_ssl": True, "connect_burn": 50.0, "clock": clock,
        }, policy=DriverPolicy(timeout_s=10.0))
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-timeout")
        self.assertIn("encrypted connection attempt", outcome.message)
        self.assertEqual(len(calls), 1)  # no budget left for a retry


class FailureClassificationTests(unittest.TestCase):
    """Every stable code, on the path that produces it."""

    def test_connection_error(self):
        outcome, _ = run_with({"connect_error": RuntimeError("boom")})
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-connection-error")
        self.assertIn("RuntimeError: boom", outcome.message)

    def test_connect_timeout_exception(self):
        outcome, _ = run_with(
            {"connect_error": TimeoutError("timed out")})
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-timeout")

    def test_driver_unavailable_on_broken_module(self):
        module = types.ModuleType("pg8000")  # no native attribute
        with patch(PATCH_TARGET, module):
            outcome = run_readonly(DriverRequest(
                dsn=DSN, policy=DriverPolicy(),
                statements=list(STATEMENTS)))
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-driver-unavailable")

    def test_driver_unavailable_on_import_error(self):
        outcome, _ = run_with(
            {"connect_error": ImportError("pg8000 missing")})
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-driver-unavailable")

    def test_session_failure_is_query_error(self):
        outcome, _ = run_with({
            "errors": [RuntimeError("SET rejected")],
        })
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-query-error")

    def test_statement_failure_is_query_error(self):
        outcome, (cls, calls, instances) = run_with({
            "script": [(("server_version", "server_now"), [])],
            "errors": [None, None, None, RuntimeError("syntax error")],
        })
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-query-error")
        self.assertIn("RuntimeError: syntax error", outcome.message)
        self.assertTrue(instances[0].closed)  # closed on failure too

    def test_server_statement_timeout_cancel_is_timeout(self):
        outcome, _ = run_with({
            "script": [(("server_version", "server_now"), [])],
            "errors": [None, None, None,
                       RuntimeError("canceling statement due to "
                                    "statement timeout")],
        })
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-timeout")
        self.assertIn("statement_timeout", outcome.message)

    def test_budget_expiry_between_statements_is_timeout(self):
        clock = FakeClock(now=0.0)
        outcome, (cls, calls, instances) = run_with({
            "script": [(("server_version", "server_now"), []),
                       (("version",), [])],
            "errors": [None, None, None, None],
            "statement_burn": 12.0,
            "clock": clock,
        }, policy=DriverPolicy(timeout_s=10.0))
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-timeout")
        # bounded overrun declared honestly: one statement ran, the
        # message refuses wall-clock-kill equivalence
        self.assertEqual(len(instances[0].runs), 4)  # 2 SETs, BEGIN, meta
        self.assertIn("exhausted after 1 of 2 statements",
                      outcome.message)
        self.assertIn("at most one statement was in flight",
                      outcome.message)
        self.assertIn("not a wall-clock kill", outcome.message)
        self.assertTrue(instances[0].closed)

    def test_password_never_in_failure_outputs(self):
        outcome, _ = run_with({"connect_error": RuntimeError("boom")})
        self.assertIsInstance(outcome, DriverFailure)
        self.assertNotIn("SEKRIT", outcome.message)
        self.assertNotIn("SEKRIT", repr(outcome))
        self.assertNotIn("SEKRIT", "".join(outcome.notes))


class ResultCapHookTests(unittest.TestCase):
    """The udd-result-too-large seam todo 4 wires policy caps into."""

    ROWS = [("PostgreSQL 16.2", "2026-11-18 12:00:00+00")]

    def _request(self, caps):
        outcome, _ = run_with({
            "script": [(("server_version", "server_now"), self.ROWS)],
            "statements": STATEMENTS[:1],
        }, statements=STATEMENTS[:1], caps=caps)
        return outcome

    def test_cell_cap_trips(self):
        outcome = self._request(ResultCaps(max_cell_chars=5,
                                           max_total_row_chars=4096))
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-result-too-large")
        self.assertIn("max_cell_chars=5", outcome.message)

    def test_total_cap_trips(self):
        outcome = self._request(ResultCaps(max_cell_chars=4096,
                                           max_total_row_chars=5))
        self.assertIsInstance(outcome, DriverFailure)
        self.assertEqual(outcome.code, "udd-result-too-large")
        self.assertIn("max_total_row_chars=5", outcome.message)

    def test_no_caps_means_passthrough(self):
        outcome = self._request(None)
        self.assertIsInstance(outcome, DriverSuccess)
        self.assertEqual(outcome.statements[0][2], self.ROWS)


if __name__ == "__main__":
    unittest.main()
