"""Typed UDD client: the datasets-only query surface.

One ``query`` call = one bounded in-process driver session = one
read-only transaction over at most ``policy.max_datasets`` fixed
dataset templates. The surface exposes exactly two capabilities:

- ``list_datasets()`` -- pure catalog (imported from the catalog
  module; no database, no budget consumption);
- ``UddClient.query(datasets, package, ...)`` -- bounded query
  returning a typed :class:`UddResult`.

Honest three-way semantics: a real empty result (including an
unknown source package name) is ``status="ok"`` with zero rows; a
failure is ``status="failed"`` with a stable machine error code from
the ``udd-*`` family (the driver's classification passes straight
through); truncation is never masked -- it sets
``truncated``/``more_available`` and drops the overflow rows, and the
entry layer maps it to ``partial``. Every returned row has a parallel
provenance entry ``udd:<dataset>#row=N`` (1-based within that
dataset's rows of this fetch).

Result-size honesty: one cell over ``policy.max_cell_chars`` or the
run's total row characters over ``policy.max_total_row_chars`` fails
the whole query with ``udd-result-too-large`` -- no content is
returned and nothing is silently truncated.

Deterministic caller mistakes raise :class:`UsageError` (config-domain
style, exit 2 at the entry): invalid package name, unknown
dataset name, invalid release, limit out of range, and budget
violations (dataset count / total effective rows / statement count).
The DSN is an opaque explicit parameter -- never read from the process
environment here and never validated beyond being a non-empty string
(connection-form authority belongs to the config domain).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from gleam.core.result import DiagnosticItem, UsageError
from gleam.core.verbose import _quiet

from .catalog import (
    DATASETS,
    DATASETS_TEMPLATE_VERSION,
    DATASET_NAMES,
    DEBIAN_SOURCE_NAME,
    MAX_DATASET_LIMIT,
    RELEASES,
    build_statements,
    list_datasets,  # re-exported for the public surface
)
from .pgdriver import (
    DEFAULT_PORT,
    DriverFailure,
    DriverRequest,
    ResultCaps,
    dsn_display,
    run_readonly,
)

__all__ = [
    "UddClient",
    "UddPolicy",
    "UddResult",
    "list_datasets",
]

#: Stable ``where`` marker for every diagnostic this adapter emits.
WHERE = "gleam.core.sources.udd"


@dataclass(frozen=True)
class UddPolicy:
    """Numeric bounds for one query (defaults; the live values
    come from the config domain at call time -- these defaults exist
    only for standalone testing).

    ``max_rows_per_dataset`` is the default per-dataset limit used
    when ``limit=None``; the registry ``max_limit`` (50) stays the hard
    bound. ``max_cell_chars``/``max_total_row_chars`` are the honesty
    caps: exceeding either fails the query (``udd-result-too-large``)
    instead of truncating. ``field_char_cap`` is deliberately absent:
    display-time field truncation belongs to the entry/tool layer,
    not the source.
    """

    timeout_s: float = 45.0
    statement_timeout_ms: int = 15000
    max_cell_chars: int = 16384
    max_total_row_chars: int = 262144
    max_queries: int = 10
    max_datasets: int = 6
    max_rows_total: int = 120
    max_rows_per_dataset: int = 20


@dataclass(frozen=True)
class UddResult:
    """Typed adapter result for one query.

    ``status`` is adapter-level: ``ok`` (rows usable, possibly empty)
    or ``failed`` (errors non-empty). Truncation keeps ``ok`` and sets
    ``truncated``/``more_available`` -- the ENTRY derives the
    ``partial`` Result status from that flag. ``rows`` and
    ``provenance`` are parallel arrays (row i carries provenance[i]);
    rows hold exactly the dataset's registered columns, keyed by the
    column names the driver reported for the owning statement.
    ``to_data()`` returns the ``udd.data`` field set; the entry adds
    ``action``.
    """

    status: str
    package: str
    release: str | None
    datasets: tuple[str, ...]
    rows: list[dict] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    more_available: bool = False
    dataset_info: dict[str, dict] = field(default_factory=dict)
    errors: list[DiagnosticItem] = field(default_factory=list)
    diagnostics: list[DiagnosticItem] = field(default_factory=list)
    warnings: list[DiagnosticItem] = field(default_factory=list)
    # provenance meta (advisory; entries/tools may surface it)
    server: dict | None = None
    fetched_at: str = ""
    duration_s: float = 0.0
    template_version: str = DATASETS_TEMPLATE_VERSION
    dsn_display: str = ""
    response_sha256: str = ""
    notes: list[str] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        """The ``udd.data`` fields (the entry adds ``action``)."""
        return {
            "package": self.package,
            "release": self.release,
            "datasets": list(self.datasets),
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "more_available": self.more_available,
            "provenance": list(self.provenance),
        }


def validate_package(value: Any) -> str:
    """Source package name per the frozen Debian policy."""

    if isinstance(value, bool) or not isinstance(value, str):
        raise UsageError(
            f"package must be a string, got {type(value).__name__}")
    if not DEBIAN_SOURCE_NAME.match(value) or len(value) > 64:
        raise UsageError(
            "package must match the Debian source-name policy "
            "(^[a-z0-9][a-z0-9+.-]+$, max 64 chars); "
            f"got {value!r}")
    return value


def validate_datasets(datasets: Any, policy: UddPolicy) -> tuple[str, ...]:
    """Strict catalog allowlist; dedupe preserving order; non-empty;
    bounded by the per-query dataset cap."""

    if isinstance(datasets, str) or not isinstance(datasets, (list, tuple)):
        raise UsageError(
            "datasets must be a non-empty list of dataset names "
            "(see list_datasets())")
    seen: list[str] = []
    for item in datasets:
        if not isinstance(item, str) or item not in DATASETS:
            raise UsageError(
                f"unknown dataset {item!r}; allowed datasets: "
                f"{list(DATASET_NAMES)}")
        if item not in seen:
            seen.append(item)
    if not seen:
        raise UsageError(
            "datasets must be a non-empty list of dataset names "
            "(see list_datasets())")
    if len(seen) > policy.max_datasets:
        raise UsageError(
            f"{len(seen)} datasets exceed the per-query dataset cap "
            f"{policy.max_datasets}")
    return tuple(seen)


def validate_release(release: Any) -> str | None:
    """None/empty means no filter; otherwise the fixed allowlist."""

    if release is None or release == "":
        return None
    if not isinstance(release, str) or release not in RELEASES:
        raise UsageError(
            f"unknown release {release!r}; allowed: "
            f"{sorted(RELEASES)[:8]}... ({len(RELEASES)} total)")
    return release


def validate_limit(value: Any, policy: UddPolicy) -> int:
    """Resolve the per-dataset limit: explicit value or the policy
    default, always bounded by the registry hard limit (1..50)."""

    if value is None:
        value = policy.max_rows_per_dataset
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError(f"limit must be an integer, got {value!r}")
    if not (1 <= value <= MAX_DATASET_LIMIT):
        raise UsageError(
            f"limit must be in [1, {MAX_DATASET_LIMIT}]; got {value}")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _response_digest(outcome) -> str:
    """Content hash of the raw driver answer (advisory meta; empty
    for failures, which return no content)."""

    if isinstance(outcome, DriverFailure):
        return hashlib.sha256(b"").hexdigest()
    payload = json.dumps(outcome.statements, ensure_ascii=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connect_target(dsn: str) -> str:
    """``host[:port]`` for the connect narration line, derived from
    the credential-free ``dsn_display`` form -- never the raw DSN
    (userinfo is already stripped there). A netloc without a port
    gets the driver default appended; a malformed port stays
    verbatim (the driver classifies the DSN failure right after, and
    narration must not crash or pre-empt that)."""

    netloc = urlsplit(dsn_display(dsn)).netloc
    tail = netloc.rsplit("]", 1)[-1]  # after any IPv6 bracket
    return netloc if ":" in tail else f"{netloc}:{DEFAULT_PORT}"


class UddClient:
    """Bounded read-only UDD client over the datasets surface.

    The DSN is an explicit opaque parameter (the config domain owns
    defaults and connection-form authority); no test-only injection
    seams exist -- tests patch ``pg8000`` at the driver module
    boundary (``gleam.core.sources.udd.pgdriver.pg8000``).
    """

    def __init__(self, dsn: str, policy: UddPolicy | None = None,
                 verbose: Callable[[str], None] | None = None):
        if not isinstance(dsn, str) or not dsn:
            raise UsageError(
                f"dsn must be a non-empty string, got {dsn!r}")
        self.dsn = dsn
        self.policy = policy if policy is not None else UddPolicy()
        # Narration sink (``make_verbose`` callable or _quiet when
        # unset); process-stream only, never a Result payload.
        self._verbose = verbose or _quiet

    def query(self, datasets, package: str, release: str | None = None,
              limit: int | None = None) -> UddResult:
        """Run one bounded read-only query for one source package.

        Deterministic caller errors (validation, budgets) raise
        :class:`UsageError` before any connection is made; driver
        failures come back as ``status="failed"`` results with the
        stable ``udd-*`` error codes.
        """

        pkg = validate_package(package)
        ds_list = validate_datasets(datasets, self.policy)
        rel = validate_release(release)
        lim = validate_limit(limit, self.policy)

        caps = {
            name: (min(lim, DATASETS[name]["row_cap"])
                   if DATASETS[name]["row_cap"] is not None else lim)
            for name in ds_list}
        total_rows = sum(caps.values())
        if total_rows > self.policy.max_rows_total:
            raise UsageError(
                f"requested effective rows {total_rows} exceed the "
                f"per-query total row cap {self.policy.max_rows_total} "
                f"(datasets={list(ds_list)}, limit={lim})")
        statement_count = 1 + len(ds_list)
        if statement_count > self.policy.max_queries:
            raise UsageError(
                f"{statement_count} statements exceed the per-query "
                f"statement cap {self.policy.max_queries}")
        heavy_caps = {name: caps[name] for name in ds_list
                      if DATASETS[name]["row_cap"] is not None}

        statements = build_statements(ds_list, heavy_caps)
        params = {"source": pkg, "release": rel or "",
                  "limit1": lim + 1}

        def _base(**extra: Any) -> dict[str, Any]:
            return dict(
                package=pkg, release=rel, datasets=tuple(ds_list),
                dsn_display=dsn_display(self.dsn),
                template_version=DATASETS_TEMPLATE_VERSION,
                fetched_at=_utc_now(), **extra)

        request = DriverRequest(
            dsn=self.dsn, policy=self.policy, statements=statements,
            params=params,
            caps=ResultCaps(
                max_cell_chars=self.policy.max_cell_chars,
                max_total_row_chars=self.policy.max_total_row_chars))
        self._verbose(f"connect {_connect_target(self.dsn)}")
        started = time.monotonic()
        outcome = run_readonly(request)
        duration = round(time.monotonic() - started, 3)
        sha = _response_digest(outcome)

        if isinstance(outcome, DriverFailure):
            details: dict[str, Any] = {
                "dsn_display": dsn_display(self.dsn),
                "duration_s": duration,
            }
            if outcome.notes:
                details["driver_notes"] = list(outcome.notes)
            return UddResult(
                status="failed",
                errors=[DiagnosticItem(
                    code=outcome.code, message=outcome.message,
                    where=WHERE, details=details)],
                **_base(duration_s=duration, response_sha256=sha))

        # Statement attribution: the meta statement (owner None)
        # feeds ``server``; each dataset statement owns its rows.
        meta: dict = {}
        by_dataset: dict[str, tuple[tuple[str, ...], list]] = {}
        for owner, columns, rows in outcome.statements:
            if owner is None:
                if rows:
                    meta = dict(zip(columns, rows[0]))
            else:
                by_dataset[owner] = (columns, rows)

        rows_out: list[dict] = []
        provenance: list[str] = []
        dataset_info: dict[str, dict] = {}
        truncated = False
        for name in ds_list:
            columns, raw_rows = by_dataset[name]
            cap = caps[name]
            more = len(raw_rows) > cap
            shown = raw_rows[:cap]
            for idx, raw in enumerate(shown, start=1):
                rows_out.append(dict(zip(columns, raw)))
                provenance.append(f"udd:{name}#row={idx}")
            dataset_info[name] = {
                "row_count": len(shown),
                "limit": cap,
                "more_available": more,
                "hidden_row_count": max(0, len(raw_rows) - len(shown)),
            }
            count = len(shown)
            line = f"query {name}: {count} {'row' if count == 1 else 'rows'}"
            if more:
                line = f"{line} (truncated, more available)"
            self._verbose(line)
            truncated = truncated or more

        total = len(rows_out)
        self._verbose(
            f"query done: {total} "
            f"{'row' if total == 1 else 'rows'} total")

        notes = [
            "rows are live UDD facts fetched via bounded read-only "
            "queries over the built-in pg8000 driver; source-side "
            "timestamps are server-rendered verbatim and are "
            "distinct from fetched_at",
            "each row carries provenance "
            '"udd:<dataset>#row=N" -- the 1-based row number within '
            "that dataset as ordered by its fixed ORDER BY, valid "
            "only for rows of THIS fetch",
            "templates coalesce NULL to the empty string, so NULL "
            "and the empty string are indistinguishable in rows; "
            "no speculative filling",
            "the release filter is applied only to release-scoped "
            "datasets (sources, upstream, binaries, reproducible); "
            "other datasets ignore the release parameter",
            "0 rows is a valid honest result (common for removals, "
            "testing_autoremovals, upstream_metadata, or an unknown "
            "source package name); errors mean unknown, never absence",
        ]

        return UddResult(
            status="ok",
            rows=rows_out,
            provenance=provenance,
            row_count=len(rows_out),
            truncated=truncated,
            more_available=truncated,
            dataset_info=dataset_info,
            diagnostics=[],
            server={"server_version": meta.get("server_version"),
                    "server_now": meta.get("server_now")}
            if meta else None,
            notes=notes,
            **_base(duration_s=duration, response_sha256=sha))
