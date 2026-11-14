"""check - validate an evidence store for structural integrity

Scan an evidence store directory and report run counts plus per-run
diagnostics: how many runs are valid, incomplete, or broken, and what
was found wrong with each problematic run. There is no repair
function; an empty or missing store is a valid store with zero counts.
`--store` takes the store ROOT (the directory whose children are run
directories); `--run` validates exactly one run directory instead.
Passing one run's own directory to `--store`, or a directory that
holds no runs at all, is rejected as a usage error (exit 2) instead of
being misread as a store full of broken runs; so is a `--run` path
that is not a run directory.

Usage: gleam check [flags]

Flags:
  --store DIR        evidence store root to scan, not a single run
                     directory (default runs; .env key
                     GLEAM_CHECK_STORE)
  --run DIR          validate exactly this one run directory instead
                     of a whole store (must contain run.json or the
                     run's evidence directories; cannot be combined
                     with --store)
   --json             print the machine-readable Result envelope
   --verbose          narrate progress to stderr (mutually exclusive
                      with --json)
   --env-file PATH    explicit .env path (default: ./.env when present)

Value flags also read from their GLEAM_* .env keys; a flag beats the
.env value, which beats the default.

Output and exit codes: human-readable text by default; --json switches
stdout to the Result envelope. A fully valid store exits 0; any
incomplete or broken run makes the command exit 1; usage errors exit 2.

Storage: never writes to disk - the scan is strictly read-only;
nothing under the store is created, modified, or removed.

Examples:
  python -m gleam check --store runs
  python -m gleam check --store runs --json
  python -m gleam check --run runs/20260101T000000Z-wizznic-11111111-1111-4111-8111-111111111111
"""

# Dev notes: all classification capability lives in the storage domain;
# this module only wires it and never re-derives run states or reason
# codes. Two scan modes: `--store` (whole store root) and `--run` (one
# run directory). `_validate_store_root` judges whether the store
# argument can be a store root at all: a run-shaped root is one run's
# directory passed by mistake, and a root whose child directories are all
# non-empty yet none run-shaped is not a store -- both exit 2 with no
# Result. A root with at least one run-shaped child scans anyway, keeping
# stray children visible as incomplete runs. `_validate_run_dir` accepts
# only a run-shaped directory (an interrupted run without run.json still
# classifies); anything else is exit 2. Any explicit --store (even the
# default value), or a configured store that differs from the
# closed-table default, cannot be combined with --run: one call names
# one scan target. kind/status mapping is shared by both
# modes: any storage-error run -> store-error / storage-error; else any
# incomplete run -> store-incomplete / partial; else store-valid / ok
# (the empty, missing, or file-shaped store lands here with zero counts,
# deliberately). data carries store (null in run mode), run (null in
# store mode), run_count, valid_count, incomplete_count, error_count,
# runs[] (each with run_id, the raw run.json status or storage-error,
# and one structured diagnostic per reason code). main -> exit mapping
# (usage/config errors exit 2 with no Result; every other status via the
# unified mapper, so both integrity findings land on exit 1). No
# anti-forgery re-validation here; that boundary stays with the
# investigate entry. Structure shared by every entry module: parser /
# run / render_human / render_json / main.

from __future__ import annotations

import os
import sys
from typing import Sequence

from gleam.core.config import BoundConfig, bind
from gleam.core.config.spec import ENV_KEYS
from gleam.core.result import (
    DiagnosticItem,
    Result,
    UsageError,
    exit_code_for,
    finalize,
)
from gleam.entries._human import plural, problem_lines, wrap
from gleam.core.storage import (
    REASON_RUN_JSON_MISSING,
    REASON_RUN_JSON_UNREADABLE,
    REASON_RUN_STATUS_MISSING,
    REASON_STORAGE_ERROR_RECORDED,
    REASON_TMP_RESIDUE,
    STATE_ERROR,
    STATE_INCOMPLETE,
    STATE_VALID,
    RunInfo,
    infer_run,
    infer_store,
    is_run_dir,
)
from gleam.core.verbose import dump_diagnostics, make_verbose

__all__ = [
    "KIND_STORE_ERROR",
    "KIND_STORE_INCOMPLETE",
    "KIND_STORE_VALID",
    "main",
    "parser",
    "render_human",
    "render_json",
    "run",
]

_MODULE = "check"

#: Stable kind vocabulary of the check entry.
KIND_STORE_VALID = "store-valid"
KIND_STORE_INCOMPLETE = "store-incomplete"
KIND_STORE_ERROR = "store-error"

#: Human-facing evidence text for each reason code (the codes are
#: the stable machine contract; these strings only explain them).
_REASON_MESSAGES = {
    REASON_RUN_JSON_MISSING: "run.json is absent",
    REASON_RUN_JSON_UNREADABLE: (
        "run.json exists but does not parse as a JSON object"
    ),
    REASON_RUN_STATUS_MISSING: (
        "run.json lacks a usable completion status field"
    ),
    REASON_TMP_RESIDUE: (
        "temp-file residue from an interrupted atomic write remains in "
        "the run tree"
    ),
    REASON_STORAGE_ERROR_RECORDED: (
        "run.json records the storage-error status"
    ),
}


def parser(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> BoundConfig:
    """Bind one check call from argv/env file/defaults.

    The flag surface is declarative (``MODULE_SPECS["check"]``);
    unknown flags, stray positionals, and value-type violations raise
    ``UsageError`` here.
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
        raise UsageError("check: --verbose and --json are mutually "
                         "exclusive")
    return _execute(config, list(argv) if argv is not None else [])


def _run_entry(info: RunInfo) -> dict:
    """Build one ``data.runs[]`` entry from a :class:`RunInfo`.

    ``status`` is the raw run status string recorded in run.json
    (``None`` when the summary is missing or unreadable) or the
    recorded ``storage-error`` -- never a check kind.
    ``diagnostics`` carries one structured item per reason code so
    the evidence of what was missing/broken travels with the run.
    """
    where = f"runs/{info.run_id}"
    diagnostics = [
        DiagnosticItem(
            code=reason,
            message=_REASON_MESSAGES.get(
                reason, "structure inference reported a problem"
            ),
            where=where,
        ).to_payload()
        for reason in info.reasons
    ]
    return {
        "run_id": info.run_id,
        "status": info.status,
        "diagnostics": diagnostics,
    }


def _validate_store_root(store: str) -> None:
    """Raise ``UsageError`` when ``store`` cannot be a store root.

    A store root holds run directories. A run-shaped root is one run's
    directory passed by mistake; a directory whose child directories are
    all non-empty yet none run-shaped is not a store. Missing paths,
    files, and empty directories stay on the zero-count valid path.
    """
    if is_run_dir(store):
        raise UsageError(
            f"--store {store}: this is a run directory, not a store root; "
            f"pass the store directory that contains it (e.g. --store runs)"
        )
    if not os.path.isdir(store):
        return
    child_dirs = [
        os.path.join(store, name)
        for name in sorted(os.listdir(store))
        if os.path.isdir(os.path.join(store, name))
    ]
    if any(is_run_dir(child) for child in child_dirs):
        return
    if any(os.listdir(child) for child in child_dirs):
        raise UsageError(
            f"--store {store}: does not look like an evidence store; no "
            f"child directory is a run (no run.json, no evidence "
            f"directories)"
        )


#: Closed-table default of ``--store``; only this value (from the
#: closed table or ``.env``) may coexist with ``--run``, meaning "no
#: store was chosen".
_DEFAULT_STORE = ENV_KEYS["GLEAM_CHECK_STORE"].default


def _flag_given(args: Sequence[str], flag: str) -> bool:
    """True when ``flag`` appears in ``args`` (space or ``=`` form)."""
    return any(
        token == flag or token.startswith(flag + "=") for token in args
    )


def _validate_run_dir(path: str) -> None:
    """Raise ``UsageError`` when ``path`` cannot be one run directory."""
    if not os.path.isdir(path):
        raise UsageError(
            f"--run {path}: not a directory; pass one run directory"
        )
    if not is_run_dir(path):
        raise UsageError(
            f"--run {path}: not a run directory (no run.json, no evidence "
            f"directories)"
        )


def _execute(config: BoundConfig, args: Sequence[str]) -> Result:
    """Execute one read-only scan (nothing is written)."""
    verbose = make_verbose(_MODULE, config.verbose)
    if config.run is not None:
        _validate_run_dir(config.run)
        if _flag_given(args, "--store") or config.store != _DEFAULT_STORE:
            raise UsageError(
                f"--run {config.run}: cannot be combined with --store "
                f"{config.store}; name one scan target"
            )
        infos = [infer_run(config.run)]
        store, run_path = None, config.run
        for info in infos:
            verbose(_run_line(info))
    else:
        _validate_store_root(config.store)
        infos = infer_store(config.store)
        store, run_path = config.store, None
        # not plural(): "directory" is the one irregular noun here
        # (plural() would render "6 run directorys").
        verbose(f"scanning {store}/: {len(infos)} run "
                f"{'directory' if len(infos) == 1 else 'directories'}")
        for info in infos:
            verbose(_run_line(info))
    if any(info.state == STATE_ERROR for info in infos):
        kind, status = KIND_STORE_ERROR, "storage-error"
    elif any(info.state == STATE_INCOMPLETE for info in infos):
        kind, status = KIND_STORE_INCOMPLETE, "partial"
    else:
        # Includes the empty/missing/unusable store (zero counts,
        # nothing to verify -- deliberately not an error).
        kind, status = KIND_STORE_VALID, "ok"
    run_count = len(infos)
    valid_count = sum(1 for info in infos if info.state == STATE_VALID)
    incomplete_count = sum(
        1 for info in infos if info.state == STATE_INCOMPLETE
    )
    error_count = sum(1 for info in infos if info.state == STATE_ERROR)
    if store is not None:
        verbose(f"scan done: {plural(run_count, 'run')}, "
                f"{incomplete_count} incomplete, {error_count} broken")
    return finalize(
        Result(
            status=status,
            kind=kind,
            data={
                "store": store,
                "run": run_path,
                "run_count": run_count,
                "valid_count": valid_count,
                "incomplete_count": incomplete_count,
                "error_count": error_count,
                "runs": [_run_entry(info) for info in infos],
            },
        )
    )


def render_json(result: Result) -> str:
    """Single-line machine Result in the golden field order."""
    return result.to_json()


#: Human phrasing for the kebab-case reason codes; a code without an
#: entry falls back to the raw code so nothing is ever dropped.
_REASON_TEXT = {
    "run-json-missing":
        "no run.json (interrupted before the run record was written)",
    "run-json-unreadable":
        "run.json is unreadable (corrupt or half-written)",
    "run-status-missing": "run.json has no completion status",
    "tmp-residue": "temp-file residue from an interrupted write",
    "storage-error-recorded": "run recorded a storage error",
}


def _reasons_text(codes: Sequence[str]) -> str:
    """``; ``-joined renderer phrases for reason codes.

    Shared by the human renderer and the ``--verbose`` per-run lines
    so the two surfaces narrate identical reason vocabulary.
    """
    return "; ".join(_REASON_TEXT.get(code, code) for code in codes)


def _run_line(info: RunInfo) -> str:
    """One ``--verbose`` per-run line in the renderer's verdict
    vocabulary (``intact`` / ``incomplete`` / ``broken``)."""
    if info.state == STATE_ERROR:
        label = "broken"
    elif info.state == STATE_INCOMPLETE:
        label = "incomplete"
    else:
        label = "intact"
    reasons = _reasons_text(info.reasons)
    if reasons:
        return f"run {info.run_id}: {label} ({reasons})"
    return f"run {info.run_id}: {label}"


def _problem_description(entry: dict) -> str:
    label = ("broken" if entry["status"] == "storage-error"
             else "incomplete")
    reasons = _reasons_text(
        [item["code"] for item in entry["diagnostics"]]
    )
    return f"{label}, {reasons}" if reasons else label


def render_human(result: Result) -> str:
    """Human-readable text (stdout whenever ``--json`` is absent)."""
    data = result.data
    runs = data["runs"]
    if data["run"] is not None:
        entry = runs[0]
        if not entry["diagnostics"]:
            lines = [f"check {entry['run_id']}: intact"]
        else:
            lines = [f"check {entry['run_id']}: "
                     f"{_problem_description(entry)}"]
        lines.extend(problem_lines(result))
        return "\n".join(lines)
    count = data["run_count"]
    problems = [entry for entry in runs if entry["diagnostics"]]
    if not problems:
        lines = [f"check {data['store']}: {plural(count, 'run')} — "
                 f"all intact"]
    else:
        lines = [
            f"check {data['store']}: {plural(count, 'run')} — "
            f"{data['valid_count']} intact, "
            f"{data['incomplete_count']} incomplete, "
            f"{data['error_count']} broken"
        ]
        for entry in problems:
            lines.append(wrap(f"{entry['run_id']}: "
                              f"{_problem_description(entry)}",
                              subsequent="    "))
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
        dump_diagnostics(result, make_verbose(_MODULE, True))
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
