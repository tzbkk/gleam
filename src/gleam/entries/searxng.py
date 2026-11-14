# noqa: SIZE_OK — the whole --start/--stop/--validate orchestration
# lives in this one module (parser/run/render_human/render_json/main),
# like every sibling entry module.
"""searxng - manage the local SearXNG search service

Start, stop, or check the self-hosted SearXNG instance that powers the
search_web tool: one guarded, loopback-bound Docker deployment. The
only requirement is a working Docker CLI with daemon access, and only
when search_web is used. Exactly one action per invocation; any other
combination is a usage error (exit 2).

Usage: gleam searxng (--start | --stop | --validate) [flags]

Actions:
  --validate  side-effect-free end-to-end probe: Docker availability,
              the gleam-named container, its healthz endpoint, and one
              real search query. PASS exits 0; FAIL and BLOCKER exit 1.
              It never starts anything.
  --start     guarded bring-up: an already-valid deployment is reused
              as-is (exit 0, no bring-up at all); otherwise the
              configured image is brought up, pulling only when the
              image is missing (--pull forces an update), refusing any
              image or same-named container that does not verify as
              SearXNG, and the same probe must pass before success is
              reported.
  --stop      stop and remove only the gleam-named container: an
              absent container is a success no-op; an unreachable
              Docker daemon is a failure (never a silent no-op); a
              present container is stopped gracefully then removed,
              never force-removed. Unrelated containers are untouched.

Flags:
  --image REF         Docker image reference (default
                      searxng/searxng:latest)
  --name NAME         container name; only this name is ever touched
                      (default gleam-searxng)
  --port N            port to serve on, 1..65535 (default 8888)
  --bind ADDRESS      address to bind the service to (default
                      127.0.0.1)
  --pull              force an image update before bring-up
  --search-base URL   base URL to probe (default
                      http://127.0.0.1:8888, the same trusted base
                      investigate uses; .env key GLEAM_SEARCH_BASE)
   --json              print the machine-readable Result envelope
   --verbose           narrate progress to stderr (mutually exclusive
                       with --json)
   --env-file PATH     explicit .env path (default: ./.env when present)

The module adds no .env key of its own.

Output and exit codes: human-readable text by default; --json switches
stdout to the Result envelope. ok exits 0; failed exits 1; usage
errors exit 2.

Deployment shape: the official docker.io/searxng/searxng image bound
to 127.0.0.1:8888 by default -- no host networking, no privileged
mode, no Docker socket mounted. The shipped settings file enables the
JSON format and disables the limiter for local-only use.

Storage: never writes a file anywhere.

Examples:
  python -m gleam searxng --validate
  python -m gleam searxng --start
  python -m gleam searxng --start --pull
  python -m gleam searxng --stop
"""

# Dev notes: this module owns the flag surface and the usage contract;
# the lifecycle and probe capability lives in the core sources. parser
# -> bind over the declarative flag surface (port and search-base
# ranges validated there); run -> the exactly-one-action check, then
# one lifecycle call; kinds: searxng-started / searxng-stopped /
# searxng-validated (data holds the action and deployment facts,
# non-applicable fields empty/null/false); render_human/render_json ->
# text; main -> exit mapping (usage/config errors exit 2 with no
# Result; every other status via the unified mapper). Zero storage: no
# file is ever written anywhere.

from __future__ import annotations

import importlib.resources
import sys
from collections.abc import Callable
from typing import Sequence

from gleam.core.config import BoundConfig, bind
from gleam.core.result import (
    Result,
    UsageError,
    exit_code_for,
    finalize,
)
from gleam.core.sources.searxng.lifecycle import (
    GuardFailure,
    SearxngTarget,
    bring_up,
    tear_down,
)
from gleam.core.sources.searxng.validate import (
    VALIDATE_QUERY,
    VERDICT_PASS,
    run_validate,
)
from gleam.core.verbose import dump_diagnostics, make_verbose
from gleam.entries._human import problem_lines, wrap

__all__ = [
    "KIND_SEARXNG_STARTED",
    "KIND_SEARXNG_STOPPED",
    "KIND_SEARXNG_VALIDATED",
    "main",
    "parser",
    "render_human",
    "render_json",
    "run",
]

_MODULE = "searxng"

#: Stable kind vocabulary of the searxng entry (one per action).
KIND_SEARXNG_STARTED = "searxng-started"
KIND_SEARXNG_STOPPED = "searxng-stopped"
KIND_SEARXNG_VALIDATED = "searxng-validated"


def parser(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> BoundConfig:
    """Bind one searxng call from argv/env file/defaults.

    The flag surface is declarative (``MODULE_SPECS["searxng"]``);
    unknown flags, stray positionals, and out-of-range values (port,
    search base) raise ``UsageError`` here. The exactly-one-of
    action check stays with :func:`run` (binder-deliberate, as in
    the udd/repology templates).
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
        raise UsageError("searxng: --verbose and --json are mutually "
                         "exclusive")
    return _execute(config)


def _execute(config: BoundConfig) -> Result:
    """Execute one bound searxng request (zero storage: no file is
    ever written anywhere)."""
    selected = sum((config.start, config.stop, config.validate))
    if selected != 1:
        raise UsageError(
            "searxng: exactly one of --start/--stop/--validate is required"
        )
    verbose = make_verbose("searxng", config.verbose)
    if config.start:
        return _start(config, verbose)
    if config.stop:
        return _stop(config, verbose)
    return _validate(config, verbose)


def _settings_mount_source() -> str:
    """The package-data settings.yml as a host path for the read-only
    docker mount, resolved at runtime through ``importlib.resources``
    so it works from a wheel and an editable tree alike (Windows
    paths stay intact; the docker CLI interprets them)."""
    return str(importlib.resources.files("gleam") / "data"
               / "searxng-settings.yml")


def _start(config: BoundConfig,
             verbose: Callable[[str], None]) -> Result:
    """Guarded bring-up with an idempotent reuse short-circuit.

    Pre-validate first: an already-valid deployment is reused as-is
    (status ok, no bring-up call at all). Otherwise the lifecycle
    layer brings the configured target up with the package-data
    settings mounted read-only, and the deployment must THEN validate
    end to end -- a bring-up that never validates is a failure, never
    a reported success."""
    pre = run_validate(name=config.name, search_base=config.search_base,
                       verbose=verbose)
    if pre.verdict == VERDICT_PASS:
        return finalize(Result(
            status="ok",
            kind=KIND_SEARXNG_STARTED,
            data={
                "action": "start",
                "reused": True,
                "verdict": pre.verdict,
                "container": config.name,
                "search_base": config.search_base,
                "steps": [step.to_data() for step in pre.steps],
            },
        ))
    target = SearxngTarget(image=config.image, name=config.name,
                           bind=config.bind, port=config.port)
    brought_up = bring_up(target, pull=config.pull,
                          settings_path=_settings_mount_source(),
                          verbose=verbose)
    if isinstance(brought_up, GuardFailure):
        return finalize(Result(
            status="failed",
            kind=KIND_SEARXNG_STARTED,
            data={
                "action": "start",
                "reused": False,
                "container": config.name,
                "image": config.image,
            },
            errors=[brought_up.to_diagnostic()],
        ))
    post = run_validate(name=config.name, search_base=config.search_base,
                        verbose=verbose)
    return finalize(Result(
        status="ok" if post.verdict == VERDICT_PASS else "failed",
        kind=KIND_SEARXNG_STARTED,
        data={
            "action": "start",
            "reused": False,
            "verdict": post.verdict,
            "container": config.name,
            "search_base": config.search_base,
            "bring_up": {
                "container": brought_up.container,
                "image": brought_up.image,
                "url": brought_up.url,
                "action": brought_up.action,
                "image_digest": brought_up.image_digest,
            },
            "steps": [step.to_data() for step in post.steps],
        },
        errors=list(post.errors),
    ))


def _stop(config: BoundConfig,
            verbose: Callable[[str], None]) -> Result:
    """Stop and remove ONLY the gleam-named container, via the
    lifecycle layer's guarded tear-down: an absent container is an
    idempotent no-op (ok, exit 0); an unreachable docker daemon is a
    failed Result, never a silent no-op; a present container is
    stopped gracefully then removed (never ``docker rm -f``); a
    foreign same-named container is refused untouched."""
    outcome = tear_down(config.name, verbose=verbose)
    if isinstance(outcome, GuardFailure):
        return finalize(Result(
            status="failed",
            kind=KIND_SEARXNG_STOPPED,
            data={"action": "stop", "container": config.name},
            errors=[outcome.to_diagnostic()],
        ))
    return finalize(Result(
        status="ok",
        kind=KIND_SEARXNG_STOPPED,
        data={
            "action": "stop",
            "container": config.name,
            "outcome": outcome.action,
        },
    ))


def _validate(config: BoundConfig,
               verbose: Callable[[str], None]) -> Result:
    """Run the side-effect-free probe chain (docker, named container,
    healthz on the configured base, one real query) and map the
    verdict onto the Result contract: PASS -> ``ok`` (exit 0); FAIL
    and BLOCKER -> ``failed`` (exit 1 via the shared mapping). The
    verdict distinguishes the two failure grades inside ``data``."""
    outcome = run_validate(name=config.name,
                           search_base=config.search_base,
                           verbose=verbose)
    return finalize(
        Result(
            status="ok" if outcome.verdict == VERDICT_PASS else "failed",
            kind=KIND_SEARXNG_VALIDATED,
            data={
                "action": "validate",
                "verdict": outcome.verdict,
                "container": config.name,
                "search_base": config.search_base,
                "query": VALIDATE_QUERY,
                "steps": [step.to_data() for step in outcome.steps],
            },
            errors=list(outcome.errors),
        )
    )


def render_json(result: Result) -> str:
    """Single-line machine Result in the golden field order."""
    return result.to_json()


#: Probe chain order; the human view always shows all four, with the
#: ones a short-circuit never reached marked "not checked".
_STEP_ORDER = ("docker-availability", "container-presence", "healthz",
               "search-query")


def _step_label(step_name: str, data: dict) -> str:
    if step_name == "docker-availability":
        return "Docker available"
    if step_name == "container-presence":
        return f"container {data.get('container')}"
    if step_name == "healthz":
        return "health check"
    if step_name == "search-query":
        query = data.get("query")
        return f'search "{query}"' if query else "search query"
    return step_name


def render_human(result: Result) -> str:
    """Human-readable text (stdout whenever ``--json`` is absent)."""
    data = result.data
    action = data.get("action")
    if action == "stop":
        outcome = data.get("outcome")
        if outcome == "absent-noop":
            headline = (f"no container named "
                        f"'{data.get('container')}'; nothing to do")
        elif outcome == "stopped-and-removed":
            headline = (f"stopped and removed container "
                        f"'{data.get('container')}'; unrelated "
                        f"containers untouched")
        else:
            headline = result.status
        lines = [f"searxng stop: {headline}"]
        lines.extend(problem_lines(result))
        return "\n".join(lines)
    steps = data.get("steps") or []
    passed = sum(1 for step in steps if step.get("ok"))
    if action == "start" and data.get("reused"):
        headline = "searxng start: deployment already valid, reused"
    else:
        headline = (f"searxng {action}: "
                    f"{'ok' if result.status == 'ok' else 'failed'}")
    lines: list[str] = [
        f"{headline} ({passed}/{len(_STEP_ORDER)} checks)"
    ]
    for step in steps:
        marker = "ok" if step.get("ok") else "FAIL"
        lines.append(wrap(
            f"[{marker}] {_step_label(step.get('step'), data)} — "
            f"{step.get('detail')}", subsequent="    "))
    checked = {step.get("step") for step in steps}
    for step_name in _STEP_ORDER:
        if step_name not in checked:
            lines.append(f"  [ -- ] {_step_label(step_name, data)} — "
                         f"not checked")
    bring_up = data.get("bring_up")
    if bring_up:
        lines.append(wrap(
            f"container {bring_up.get('container')} at "
            f"{bring_up.get('url')} ({bring_up.get('action')}; "
            f"image {bring_up.get('image_digest')})", subsequent="    "))
    lines.extend(problem_lines(
        result, seen=[step.get("detail") for step in steps]))
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
        dump_diagnostics(result, make_verbose("searxng", True))
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
