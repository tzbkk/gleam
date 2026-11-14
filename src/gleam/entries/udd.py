"""udd - query live UDD package facts for Debian QA packages

Fetch what the Ultimate Debian Database knows about one Debian source
package: pick datasets, optionally filter by release, and get rows back
from the public read-only UDD mirror over a pure-Python driver with
fixed read-only queries per dataset. Runs are read-only.

Usage: gleam udd [flags]

Modes:
  --list-datasets   print the 18-dataset catalog and exit; no database
                    contact, works offline
  query mode        fetch rows; requires --package and at least one
                    --dataset (combining them with --list-datasets is
                    an error). An honest empty result (zero rows) is a
                    success, truncation is reported as partial, and
                    failures are reported as failures, never masked.

Flags:
  --package NAME     Debian source package name, lowercase, max 64
                     characters (required in query mode)
  --dataset NAME     dataset to fetch; repeat the flag for several
                     (run --list-datasets for the valid names)
  --release NAME     release filter, e.g. sid or trixie (default: any)
  --limit N          per-dataset row selection cap, 1..50
  --dsn URL          PostgreSQL DSN (default
                     postgresql://udd-mirror.debian.net/udd; .env key
                     GLEAM_UDD_DSN)
  --timeout SECONDS  query deadline: socket deadline plus a
                     per-statement budget (default 45)
  --list-datasets    catalog mode switch (see Modes)
   --json             print the machine-readable Result envelope
   --verbose          narrate progress to stderr (mutually exclusive
                      with --json)
   --env-file PATH    explicit .env path (default: ./.env when present)

Value flags also read from their GLEAM_* .env keys; a flag beats the
.env value, which beats the default.

Output and exit codes: human-readable text by default; --json switches
stdout to the Result envelope. ok exits 0; any other result status
exits 1; usage errors exit 2.

Storage: never writes to disk.

Examples:
  python -m gleam udd --list-datasets
  python -m gleam udd --package wizznic --dataset sources --json
  python -m gleam udd --package wizznic --dataset sources \\
      --dataset upstream --release sid --limit 10
"""

# Dev notes: all capability lives in the core domains; this module only
# wires it. parser -> bind over the declarative flag surface; run ->
# finalized Result (kinds: list-datasets / query-result; non-applicable
# data fields empty/null/false); render_human/render_json -> text;
# main -> exit mapping (usage/config errors exit 2 with no Result; every
# other status via the unified mapper). Honest statuses: adapter failure
# -> failed; truncation -> partial (never masked); a real empty result
# -> ok. Zero storage: no run directory, no file writes. Structure
# shared by every entry module: parser / run / render_human /
# render_json / main.

from __future__ import annotations

import sys
from typing import Sequence

from gleam.core.config import BoundConfig, bind, bind_keys
from gleam.core.result import Result, UsageError, exit_code_for, finalize
from gleam.core.sources.udd import UddClient, UddPolicy, list_datasets
from gleam.core.verbose import dump_diagnostics, make_verbose
from gleam.entries._human import problem_lines

__all__ = [
    "KIND_LIST_DATASETS",
    "KIND_QUERY_RESULT",
    "main",
    "parser",
    "render_human",
    "render_json",
    "run",
]

_MODULE = "udd"

#: Stable kind vocabulary of the udd entry.
KIND_LIST_DATASETS = "list-datasets"
KIND_QUERY_RESULT = "query-result"

#: UDD client policy keys of the closed config table; resolved
#: per call and mapped onto ``UddPolicy`` (numeric defaults live there,
#: not here). ``GLEAM_UDD_FIELD_CHAR_CAP`` is display-time tool-layer
#: policy and has no consumer in this shell.
_POLICY_KEYS = (
    "GLEAM_UDD_STATEMENT_TIMEOUT",
    "GLEAM_UDD_MAX_CELL_CHARS",
    "GLEAM_UDD_MAX_TOTAL_ROW_CHARS",
    "GLEAM_UDD_MAX_QUERIES",
    "GLEAM_UDD_MAX_DATASETS",
    "GLEAM_UDD_MAX_ROWS",
    "GLEAM_UDD_MAX_ROWS_PER_DATASET",
)


def parser(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> BoundConfig:
    """Bind one udd call from argv/env file/defaults.

    The flag surface is declarative (``MODULE_SPECS["udd"]``);
    unknown flags, stray positionals, and contract violations (package
    name, ranges) raise ``UsageError`` here. Mode checks stay with
    :func:`run` (binder-deliberate: required-ness is an entry concern).
    """
    return bind(_MODULE, argv=argv, env_file=env_file, **overrides)


def run(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> Result:
    """Function API with the same capability as :func:`main`.

    Returns the finalized :class:`gleam.core.result.Result`; rendering
    and the exit code stay with the caller. ``overrides`` are explicit
    function-argument values (highest precedence).
    """
    config = parser(argv=argv, env_file=env_file, **overrides)
    if config.verbose and config.json:
        raise UsageError("udd: --verbose and --json are mutually "
                         "exclusive")
    # Same env source the bind call used, so the client policy keys see
    # the identical .env view (explicit file > --env-file flag > ./.env).
    resolved_env = env_file if env_file is not None else config.env_file
    return _execute(config, resolved_env)


def _execute(config: BoundConfig, env_file: str | None) -> Result:
    """Execute one bound udd request (pure: no storage)."""
    if config.list_datasets:
        if config.package is not None or config.datasets:
            raise UsageError(
                "udd: --list-datasets conflicts with the query inputs "
                "--package/--dataset"
            )
        return Result(
            status="ok",
            kind=KIND_LIST_DATASETS,
            data={
                "action": "list-datasets",
                "package": None,
                "release": None,
                "datasets": [entry["name"] for entry in list_datasets()],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "more_available": False,
                "provenance": [],
            },
        )
    if config.package is None:
        raise UsageError("udd: --package is required in query mode")
    if not config.datasets:
        raise UsageError("udd: at least one --dataset is required in query mode")

    policy_values = bind_keys(_POLICY_KEYS, env_file=env_file)
    policy = UddPolicy(
        timeout_s=config.timeout,
        statement_timeout_ms=policy_values["GLEAM_UDD_STATEMENT_TIMEOUT"],
        max_cell_chars=policy_values["GLEAM_UDD_MAX_CELL_CHARS"],
        max_total_row_chars=policy_values[
            "GLEAM_UDD_MAX_TOTAL_ROW_CHARS"],
        max_queries=policy_values["GLEAM_UDD_MAX_QUERIES"],
        max_datasets=policy_values["GLEAM_UDD_MAX_DATASETS"],
        max_rows_total=policy_values["GLEAM_UDD_MAX_ROWS"],
        max_rows_per_dataset=policy_values["GLEAM_UDD_MAX_ROWS_PER_DATASET"],
    )
    client = UddClient(dsn=config.dsn, policy=policy,
                       verbose=make_verbose("udd", config.verbose))
    udd = client.query(
        config.datasets,
        config.package,
        release=config.release,
        limit=config.limit,
    )

    if udd.status == "failed":
        status = "failed"  # query/transport/parse failure
    elif udd.truncated:
        status = "partial"  # truncation is never masked
    else:
        status = "ok"  # includes the honest empty result
    return finalize(
        Result(
            status=status,
            kind=KIND_QUERY_RESULT,
            data={"action": "query", **udd.to_data()},
            errors=list(udd.errors),
            diagnostics=list(udd.diagnostics),
            warnings=list(udd.warnings),
        )
    )


def render_json(result: Result) -> str:
    """Single-line machine Result in the golden field order."""
    return result.to_json()


def render_human(result: Result) -> str:
    """Human-readable text (stdout whenever ``--json`` is absent)."""
    data = result.data
    lines: list[str] = []
    if result.kind == KIND_LIST_DATASETS:
        names = data["datasets"]
        lines.append(f"udd datasets ({len(names)}):")
        lines.extend(f"  {name}" for name in names)
    elif result.kind == KIND_QUERY_RESULT:
        count = data["row_count"]
        header = (f"udd {data['package']} · {', '.join(data['datasets'])}"
                  f" · {count} {'row' if count == 1 else 'rows'}")
        if data["release"]:
            header += f" · release {data['release']}"
        if data["truncated"]:
            header += " (more available)"
        lines.append(header)
        for position, row in enumerate(data["rows"], 1):
            lines.append(f"  row {position}")
            width = max((len(key) for key in row), default=0)
            for key in sorted(row):
                value_lines = str(row[key]).splitlines() or [""]
                lines.append(
                    f"    {key:<{width}s}  {value_lines[0]}".rstrip())
                for extra in value_lines[1:]:
                    lines.append(
                        f"    {'':<{width}s}  {extra}".rstrip())
    else:  # unreachable for the frozen two-kind vocabulary
        lines.append(f"udd: {result.status} ({result.kind})")
    lines.extend(problem_lines(result))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code.

    Business results go through ``exit_code_for`` only. Usage/config
    errors print to stderr and return 2 WITHOUT emitting a Result.
    """
    args = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        result = run(argv=args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    # stderr dump before rendering, every status; the --json clause
    # is belt-and-suspenders (run() already rejects both flags).
    if "--verbose" in args and "--json" not in args:
        dump_diagnostics(result, make_verbose("udd", True))
    # The Result shell carries no request flags; the boolean switch is
    # set iff the exact token is in argv (an inline value would already
    # have failed bind, and a value consumed by --env-file fails there).
    if "--json" in args:
        print(render_json(result))
    else:
        print(render_human(result))
    return exit_code_for(result.status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
