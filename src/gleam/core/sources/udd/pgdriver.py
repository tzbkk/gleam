"""Bounded read-only UDD queries over the in-process pg8000 driver.

Frozen protocol contract: one connection per query, session setup
``SET client_encoding TO 'UTF8'`` then ``SET statement_timeout =
<ms>`` then ``BEGIN READ ONLY`` (the encoding pin is first and
unconditional: the server negotiates ``SQL_ASCII``, which pg8000
1.31.6 maps to the ascii codec that crashes decoding non-ASCII
row values; pinning UTF-8 restores psql-equivalent handling of
the mirror's UTF-8 data), every catalog statement executed one
per ``run()`` with named ``:params`` (pg8000 forbids
multi-statement runs that carry parameters), values passed
through unmodified (the catalog audit guarantees all-str
output), connection closed in a ``finally``.

Deadline semantics (honest, bounded overrun): the constructor
``timeout`` is the socket-level per-operation bound -- pg8000 1.31.6
hands it to ``socket.create_connection``/``settimeout``, so connect
and every read/write operation are individually bounded -- and a
remaining-budget check before each statement stops the run once the
overall ``policy.timeout_s`` budget is gone. This is NOT a
wall-clock kill: at most one statement is in flight when the budget
expires, and that statement is bounded per socket operation, not by
the overall deadline. Chosen over the per-statement private
``con._sock.settimeout(remaining)`` variant the work plan offered
because in 1.31.6 ``_sock`` is a ``socket.makefile`` object (the raw
socket is ``_usock``), so that variant cannot work as written; the
constructor-timeout branch is exact under the ``==1.31.6`` pin with
no private-attribute reach.

TLS-prefer (libpq ``sslmode=prefer`` equivalence): the first connect
attempt uses ``ssl_context=True`` (default SSLContext; pg8000 fails
the connection if SSL cannot be established). On connection failure
the retry uses ``ssl_context=False``. Plan text said retry with
``ssl_context=None``; installed-1.31.6 ground truth reconciled:
``None`` is NOT plaintext (it still negotiates SSL and merely
tolerates a server's refusal), ``False`` skips SSL entirely and is
the true plaintext retry. Both attempts count inside the
``policy.timeout_s`` budget: each attempt's constructor receives the
remaining budget, never a fresh one.

Failure classification (stable codes; the message keeps the driver
exception's type and tail, last 1000 chars):

===========  =========================================================
code         condition
===========  =========================================================
udd-driver-  the pg8000 attribute chain is broken (import or
unavailable  attribute errors resolving/invoking the driver)
udd-         DSN rejected by the parser or every ``Connection()``
connection-  attempt failed (TLS and plaintext)
error
udd-timeout  budget exhausted between statements, a socket
             ``TimeoutError`` in any phase, or the server-side
             statement_timeout cancel wording
udd-query-   a statement failed during session setup or execution
error        (everything that is not a timeout or connect failure)
udd-result-  cap hook: one cell over ``max_cell_chars`` or the
too-large     run's total row-chars over ``max_total_row_chars``
             (checked per statement after fetch; no content returned
             and no silent truncation -- todo 4 wires policy caps)
===========  =========================================================

Test seam: ``import pg8000`` at module top. Offline tests patch
``gleam.core.sources.udd.pgdriver.pg8000`` with a fake module whose
``native.Connection`` is a connection double; the driver class is
resolved lazily at call time. The DSN password never appears in any
message, note, or repr: ``DSNParts.password`` is repr-hidden and
malformed DSNs are reported without echoing the input. No process
environment is read; ``getpass.getuser()`` is the only OS fallback
(libpq-equivalent), used solely when the DSN carries no userinfo.
"""

from __future__ import annotations

import getpass
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import pg8000.native  # noqa: F401  (test seam; see module docstring)

__all__ = [
    "DEFAULT_PORT",
    "DriverFailure",
    "DriverPolicy",
    "DriverRequest",
    "DriverSuccess",
    "DSNParts",
    "ResultCaps",
    "dsn_display",
    "parse_dsn",
    "run_readonly",
]

DEFAULT_PORT = 5432

CODE_DRIVER_UNAVAILABLE = "udd-driver-unavailable"
CODE_CONNECTION_ERROR = "udd-connection-error"
CODE_TIMEOUT = "udd-timeout"
CODE_QUERY_ERROR = "udd-query-error"
CODE_RESULT_TOO_LARGE = "udd-result-too-large"

#: Diagnostic note added when the plaintext retry succeeded.
TLS_FALLBACK_NOTE = (
    "tls-prefer: encrypted connection failed, retried plaintext "
    "(libpq sslmode=prefer equivalence)")


@dataclass(frozen=True)
class DSNParts:
    """Connection components extracted from a DSN URI.

    ``password`` is repr-hidden: it must never surface in logs,
    messages, or test output.
    """

    user: str
    password: str | None = field(default=None, repr=False)
    host: str = "localhost"
    port: int = DEFAULT_PORT
    database: str | None = None


@dataclass(frozen=True)
class DriverPolicy:
    """The two bounds this adapter enforces.

    ``UddPolicy`` instances are accepted structurally: the attribute
    names match one-to-one so the client passes its own policy
    through unchanged.
    """

    timeout_s: float = 45.0
    statement_timeout_ms: int = 15000


@dataclass(frozen=True)
class ResultCaps:
    """Char caps for the udd-result-too-large hook (todo 4 wiring)."""

    max_cell_chars: int
    max_total_row_chars: int


@dataclass(frozen=True)
class DriverFailure:
    """Typed failure: stable ``code``, credential-free ``message``,
    diagnostic ``notes`` (e.g. the TLS fallback)."""

    code: str
    message: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DriverSuccess:
    """Per-statement outcomes in execution order.

    ``statements`` holds ``(owner, columns, rows)`` per statement:
    ``owner`` is the dataset that owns the rows (``None`` for the
    meta statement), ``columns`` the names from ``con.columns``, and
    ``rows`` the driver's row tuples passed through unmodified.
    """

    statements: list[tuple[str | None, tuple[str, ...], list]]
    notes: tuple[str, ...] = ()
    duration_s: float = 0.0
    dsn_display: str = ""


@dataclass(frozen=True)
class DriverRequest:
    """One bounded read-only query: DSN, policy, the catalog's
    ``(owner, sql)`` statements, the shared named ``params``, and
    optional caps."""

    dsn: str
    policy: DriverPolicy
    statements: list[tuple[str | None, str]]
    params: dict | None = None
    caps: ResultCaps | None = None


def dsn_display(dsn: str) -> str:
    """Credential-free display form: ``postgresql://host[:port]/db``."""

    parts = urlsplit(dsn)
    netloc = parts.netloc.split("@", 1)[1] if "@" in parts.netloc \
        else parts.netloc
    return f"postgresql://{netloc}{parts.path}"


def parse_dsn(dsn: str) -> DSNParts:
    """Parse a ``postgresql://`` URI into connection components.

    Absent userinfo → ``getpass.getuser()`` (libpq-equivalent OS-user
    fallback; the only OS access in this module), absent port → 5432,
    absent password → ``None``, absent database → ``None``. Query
    parameters (``?sslmode=...``) are ignored: TLS-prefer is the
    fixed policy here. Raises ``ValueError`` for non-PostgreSQL or
    port-invalid DSNs without echoing the input.
    """

    parts = urlsplit(dsn)
    if parts.scheme not in ("postgresql", "postgres") or not parts.netloc:
        raise ValueError(
            "dsn must be postgresql://[user[:password]@]host[:port]/db")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("dsn port must be an integer in 1..65535") from exc
    return DSNParts(
        user=(unquote(parts.username) if parts.username
              else getpass.getuser()),
        password=(unquote(parts.password)
                  if parts.password is not None else None),
        host=parts.hostname or "localhost",
        port=DEFAULT_PORT if port is None else port,
        database=parts.path.lstrip("/") or None,
    )


def _fmt_exc(exc: BaseException) -> str:
    """Driver exception as ``Type: message``, tail-preserved."""

    return f"{type(exc).__name__}: {exc}"[-1000:]


def _classify(exc: BaseException, phase: str, timeout_s: float,
              notes: tuple[str, ...]) -> DriverFailure:
    """Map one driver exception to a stable code (see module table)."""

    if isinstance(exc, TimeoutError):  # socket.timeout is TimeoutError
        return DriverFailure(
            CODE_TIMEOUT,
            f"{phase} timed out inside the {timeout_s:g}s budget: "
            f"{_fmt_exc(exc)}", notes)
    if isinstance(exc, (ImportError, AttributeError)):
        return DriverFailure(
            CODE_DRIVER_UNAVAILABLE,
            f"pg8000 driver unavailable: {_fmt_exc(exc)}", notes)
    text = str(exc)
    if "statement timeout" in text or "canceling statement" in text:
        return DriverFailure(
            CODE_TIMEOUT,
            f"server canceled a statement (statement_timeout): "
            f"{text[-1000:]}", notes)
    if phase == "connect":
        return DriverFailure(
            CODE_CONNECTION_ERROR, f"connection failed: {_fmt_exc(exc)}",
            notes)
    return DriverFailure(
        CODE_QUERY_ERROR, f"{phase} failed: {_fmt_exc(exc)}", notes)


def _close_quietly(con) -> None:
    """Best-effort close; never masks the primary outcome."""

    if con is None:
        return
    try:
        con.close()
    except Exception:
        pass


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _connect(conn_cls, parts: DSNParts, *, ssl_context, timeout: float):
    """One connection attempt with the given TLS mode and budget."""

    return conn_cls(user=parts.user, host=parts.host, port=parts.port,
                    database=parts.database, password=parts.password,
                    ssl_context=ssl_context, timeout=timeout)


def _caps_check(rows: list, total_chars: int,
                caps: ResultCaps) -> tuple[bool, int]:
    """Fold one statement's rows into the running char total.

    Returns ``(too_large, new_total)``: a cell over the per-cell cap
    or a total over the run cap trips the hook.
    """

    for row in rows:
        for value in row:
            chars = len(str(value))
            if chars > caps.max_cell_chars:
                return True, total_chars
            total_chars += chars
    return total_chars > caps.max_total_row_chars, total_chars


def run_readonly(request: DriverRequest) -> DriverSuccess | DriverFailure:
    """Execute one bounded read-only query; never raises driver errors.

    Deterministic caller problems (malformed DSN) and every driver
    failure come back as :class:`DriverFailure`; rows arrive as
    :class:`DriverSuccess.statements` in execution order.
    """

    policy = request.policy
    started = time.monotonic()
    deadline = started + policy.timeout_s
    notes: list[str] = []
    con = None
    phase = "connect"
    try:
        conn_cls = pg8000.native.Connection  # resolved per call (seam)
        parts = parse_dsn(request.dsn)

        budget = _remaining(deadline)
        if budget <= 0.0:
            return DriverFailure(
                CODE_TIMEOUT,
                f"budget of {policy.timeout_s:g}s exhausted before "
                f"connecting", tuple(notes))
        try:
            con = _connect(conn_cls, parts, ssl_context=True,
                           timeout=budget)
        except (ImportError, AttributeError):
            raise
        except Exception:
            budget = _remaining(deadline)
            if budget <= 0.0:
                return DriverFailure(
                    CODE_TIMEOUT,
                    f"budget of {policy.timeout_s:g}s exhausted during "
                    f"the encrypted connection attempt", tuple(notes))
            con = _connect(conn_cls, parts, ssl_context=False,
                           timeout=budget)
            notes.append(TLS_FALLBACK_NOTE)

        phase = "session"
        con.run("SET client_encoding TO 'UTF8'")
        con.run(f"SET statement_timeout = {int(policy.statement_timeout_ms)}")
        con.run("BEGIN READ ONLY")

        phase = "query"
        params = request.params or {}
        results: list[tuple[str | None, tuple[str, ...], list]] = []
        total_chars = 0
        for index, (owner, sql) in enumerate(request.statements):
            budget = _remaining(deadline)
            if budget <= 0.0:
                return DriverFailure(
                    CODE_TIMEOUT,
                    f"budget of {policy.timeout_s:g}s exhausted after "
                    f"{index} of {len(request.statements)} statements; "
                    f"at most one statement was in flight at the "
                    f"deadline (socket-level per-operation bound, not "
                    f"a wall-clock kill)", tuple(notes))
            rows = con.run(sql, **params)
            columns = tuple(c["name"] for c in con.columns)
            if request.caps is not None:
                too_large, total_chars = _caps_check(
                    rows, total_chars, request.caps)
                if too_large:
                    return DriverFailure(
                        CODE_RESULT_TOO_LARGE,
                        f"result exceeds caps (max_cell_chars="
                        f"{request.caps.max_cell_chars}, "
                        f"max_total_row_chars="
                        f"{request.caps.max_total_row_chars}); no "
                        f"content returned, no silent truncation",
                        tuple(notes))
            results.append((owner, columns, rows))
        return DriverSuccess(
            statements=results, notes=tuple(notes),
            duration_s=round(time.monotonic() - started, 3),
            dsn_display=dsn_display(request.dsn))
    except Exception as exc:
        return _classify(exc, phase, policy.timeout_s, tuple(notes))
    finally:
        _close_quietly(con)
