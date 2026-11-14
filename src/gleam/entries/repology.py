"""repology - query the Repology cross-distro version tracker

Ask repology.org where a package stands across Linux distributions:
one project's version ladder (--project) or a name search that resolves
to project slugs (--search). Requests go through the public fetcher
safety layer (SSRF guards, DNS pinning, byte caps, timeouts, request
budget). An HTTP 200 with an empty entry list is the honest "no such
project" answer; failures stay failures and are never reported as
"no results".

Usage: gleam repology (--project SLUG | --search QUERY) [flags]

Exactly one of --project/--search is required (otherwise exit 2).

Flags:
  --project SLUG                  one project's version ladder
  --search QUERY                  resolve a name to project slugs
  --repology-base URL             API base (default
                                  https://repology.org)
  --repology-max-requests N       request cap (default 6)
  --repology-min-interval SECONDS pause between requests (default 1.1;
                                  the API terms require at most one
                                  request per second)
  --repology-timeout SECONDS      request timeout (default 15)
   --json                          print the machine-readable Result
                                   envelope
   --verbose                       narrate progress to stderr (mutually
                                   exclusive with --json)
   --env-file PATH                 explicit .env path (default: ./.env
                                   when present)

Value flags also read from their GLEAM_* .env keys; a flag beats the
.env value, which beats the default. The built-in client skips the
robots.txt pre-fetch only for its own API host (Repology's API terms
authorize programmatic access there); every other safety layer still
applies.

Output and exit codes: human-readable text by default; --json switches
stdout to the Result envelope. ok exits 0 (the honest empty answer
included); failed exits 1; usage errors exit 2.

Storage: never writes to disk (read-only queries).

Examples:
  python -m gleam repology --project ncdu --json
  python -m gleam repology --search ncurses
"""

# Dev notes: all capability lives in the core sources; this module only
# wires it. parser -> bind over the declarative flag surface (policy
# value ranges validated there); run -> the exactly-one-of mode check,
# then one client query; Result kind: repology-result (the same digest
# shape the investigate repology tool returns); render_human/
# render_json -> text; main -> exit mapping (usage/config errors exit 2
# with no Result; every other status via the unified mapper). Honest
# statuses: adapter failure -> failed; 200-with-empty / zero matches ->
# ok, never masked. Zero storage: no run directory, no file writes.
# Structure shared by every entry module: parser / run / render_human /
# render_json / main.

from __future__ import annotations

import sys
from typing import Sequence

from gleam.core.config import BoundConfig, bind, bind_keys
from gleam.core.result import Result, UsageError, exit_code_for, finalize
from gleam.entries._human import plural, problem_lines, wrap
from gleam.core.sources.repology import RepologyClient, RepologyPolicy
from gleam.core.verbose import dump_diagnostics, make_verbose

__all__ = [
    "KIND_REPOLOGY_RESULT",
    "main",
    "parser",
    "render_human",
    "render_json",
    "run",
]

_MODULE = "repology"

#: Stable kind vocabulary of the repology entry.
KIND_REPOLOGY_RESULT = "repology-result"

#: Repology client policy keys of the closed config table;
#: resolved per call and mapped onto ``RepologyPolicy``. The base URL
#: is a request field (``--repology-base`` / ``GLEAM_REPOLOGY_BASE``),
#: not client policy.
_POLICY_KEYS = (
    "GLEAM_REPOLOGY_MAX_REQUESTS",
    "GLEAM_REPOLOGY_MIN_INTERVAL",
    "GLEAM_REPOLOGY_TIMEOUT",
)


def parser(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> BoundConfig:
    """Bind one repology call from argv/env file/defaults.

    The flag surface is declarative (``MODULE_SPECS["repology"]``);
    unknown flags, stray positionals, and out-of-range policy
    values raise ``UsageError`` here. The exactly-one-of mode check
    stays with :func:`run` (binder-deliberate, as in the udd/bts
    templates).
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
        raise UsageError("repology: --verbose and --json are mutually "
                         "exclusive")
    resolved_env = env_file if env_file is not None else config.env_file
    return _execute(config, resolved_env)


def _execute(config: BoundConfig, env_file: str | None) -> Result:
    """Execute one bound repology request (pure: no storage)."""
    if (config.project is None) == (config.search is None):
        raise UsageError(
            "repology: exactly one of --project/--search is required"
        )
    policy_values = bind_keys(_POLICY_KEYS, env_file=env_file)
    policy = RepologyPolicy(
        max_requests=policy_values["GLEAM_REPOLOGY_MAX_REQUESTS"],
        min_interval_s=policy_values["GLEAM_REPOLOGY_MIN_INTERVAL"],
        timeout_s=policy_values["GLEAM_REPOLOGY_TIMEOUT"],
    )
    client = RepologyClient(base=config.repology_base, policy=policy,
                            verbose=make_verbose("repology",
                                                 config.verbose))
    if config.project is not None:
        repology = client.query("project", slug=config.project)
    else:
        repology = client.query("search", query=config.search)

    status = "failed" if repology.status == "failed" else "ok"
    return finalize(
        Result(
            status=status,
            kind=KIND_REPOLOGY_RESULT,
            data={**repology.to_data()},
            errors=list(repology.errors),
            diagnostics=list(repology.diagnostics),
            warnings=list(repology.warnings),
        )
    )


def render_json(result: Result) -> str:
    """Single-line machine Result in the golden field order."""
    return result.to_json()


#: How many version tiers the human view lists before summarizing the
#: rest (the full ladder stays in ``--json``).
_HUMAN_TIER_LIMIT = 8


def _debian_repo_label(repo: str) -> str:
    """``debian_unstable`` -> ``unstable`` for the human summary."""
    return repo[len("debian_"):] if repo.startswith("debian_") else repo


def _tier_state(tier: dict) -> str:
    """``{newest}`` is the default state and stays unprinted."""
    statuses = [s for s in tier.get("statuses") or [] if s != "newest"]
    return f" ({', '.join(statuses)})" if statuses else ""


def render_human(result: Result) -> str:
    """Human-readable text (stdout whenever ``--json`` is absent)."""
    data = result.data
    lines: list[str] = []
    if data.get("action") == "search":
        projects = data.get("projects") or []
        header = (f"repology search '{data.get('query')}': "
                  f"{len(projects)} project(s) shown")
        if data.get("more"):
            header += f", {data['more']} more"
        header += (", exact match" if data.get("exact_match")
                   else ", no exact match")
        lines.append(header)
        if not data.get("exact_match"):
            lines.append(
                wrap("no exact match: retry the canonical name with "
                     "--project SLUG", "  ")
            )
        for row in projects:
            lines.append(f"  {row['slug']} — {row['newest_version']}, "
                         f"{plural(row['repo_count'], 'repo')}")
    else:
        tiers = data.get("versions") or []
        debian = data.get("debian") or []
        slug = data.get("slug")
        if not tiers and not debian:
            lines.append(f"repology project '{slug}': no such project")
        else:
            newest = tiers[0]["version"] if tiers else "(none)"
            header = f"repology {slug}: newest {newest}"
            if debian:
                shipped = {e["repo"]: e["version"] for e in debian}
                header += f"; Debian {shipped.get('debian_unstable', debian[-1]['version'])}"
            header += (f" — {data.get('total_entries')} entries across "
                       f"{plural(len(tiers), 'version tier')}")
            lines.append(header)
            for tier in tiers[:_HUMAN_TIER_LIMIT]:
                lines.append(f"  {tier['version']:>10s}  "
                             f"{tier['repo_count']:>2d} "
                             f"{'repo' if tier['repo_count'] == 1 else 'repos'}"
                             f"{_tier_state(tier)}")
            older = tiers[_HUMAN_TIER_LIMIT:]
            if older:
                lines.append(
                    f"  {plural(len(older), 'older version')} — "
                    f"{plural(sum(tier['repo_count'] for tier in older), 'repo')}"
                )
            if debian:
                lines.append("  debian: " + ", ".join(
                    f"{_debian_repo_label(e['repo'])}={e['version']}"
                    for e in debian))
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
        dump_diagnostics(result, make_verbose("repology", True))
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
