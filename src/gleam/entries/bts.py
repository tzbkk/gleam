"""bts - fetch one Debian BTS bug's public discussion

Download one Debian bug tracker bug's complete public discussion from
bugs.debian.org: subjects, senders, dates, message bodies, and links.
The default path uses the documented SOAP machine interface first and
falls back to the official mbox view only on transport-class errors and
only when robots.txt explicitly allows it; rate limits, unknown bugs,
and budget exhaustion never fall back, and there is no bypass switch.

Usage: gleam bts --bug-id ID [flags]

Flags:
  --bug-id ID        Debian BTS bug id, 1..9999999, no leading zeros
                     (required)
  --source MODE      fetch path: auto (default), soap, or mbox (.env
                     key GLEAM_BTS_SOURCE)
   --json             print the machine-readable Result envelope
   --verbose          narrate progress to stderr (mutually exclusive
                      with --json)
   --env-file PATH    explicit .env path (default: ./.env when present)

Value flags also read from their GLEAM_* .env keys; a flag beats the
.env value, which beats the default.

Output and exit codes: human-readable text by default; --json switches
stdout to the Result envelope. ok exits 0 (an honest empty discussion
included); any other result status exits 1; usage errors exit 2.

Storage: never writes to disk.

Examples:
  python -m gleam bts --bug-id 1000000 --json
  python -m gleam bts --bug-id 1000000 --source soap
"""

# Dev notes: all capability lives in the core domains; this module only
# wires it. parser -> bind over the declarative flag surface (bug id
# and source choice validated there); run -> finalized Result (kind:
# bug-result; data is the adapter's field set); render_human/
# render_json -> text; main -> exit mapping (usage/config errors exit 2
# with no Result; every other status via the unified mapper). Honest
# statuses: adapter failure -> failed; truncation -> partial (never
# masked); a real result -> ok. Zero storage: no run directory, no file
# writes. Structure shared by every entry module: parser / run /
# render_human / render_json / main.

from __future__ import annotations

import sys
from typing import Sequence

from gleam.core.config import BoundConfig, bind, bind_keys
from gleam.core.result import Result, UsageError, exit_code_for, finalize
from gleam.core.sources.bts import BtsClient, BtsPolicy
from gleam.core.verbose import dump_diagnostics, make_verbose
from gleam.entries._human import WIDTH, problem_lines, wrap

__all__ = [
    "KIND_BUG_RESULT",
    "main",
    "parser",
    "render_human",
    "render_json",
    "run",
]

_MODULE = "bts"

#: Stable kind vocabulary of the bts entry.
KIND_BUG_RESULT = "bug-result"

#: BTS client policy keys of the closed config table; resolved
#: per call and mapped onto ``BtsPolicy`` (numeric defaults live there,
#: not here). ``GLEAM_BTS_SOURCE`` is a request field (``--source``),
#: not client policy.
_POLICY_KEYS = (
    "GLEAM_BTS_TIMEOUT",
    "GLEAM_BTS_MAX_OUTPUT_BYTES",
    "GLEAM_BTS_MAX_REQUESTS",
    "GLEAM_BTS_MIN_INTERVAL",
    "GLEAM_BTS_MAX_MESSAGES",
    "GLEAM_BTS_MAX_MESSAGE_CHARS",
    "GLEAM_BTS_MAX_URLS_PER_MESSAGE",
    "GLEAM_BTS_POW_CPU_SECONDS",
    "GLEAM_BTS_ROBOTS_MAX_BYTES",
)


def parser(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> BoundConfig:
    """Bind one bts call from argv/env file/defaults.

    The flag surface is declarative (``MODULE_SPECS["bts"]``);
    unknown flags, stray positionals, and contract violations (bug
    id, source choice) raise ``UsageError`` here. Required-ness
    stays with :func:`run` (binder-deliberate, as in the udd template).
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
        raise UsageError("bts: --verbose and --json are mutually "
                         "exclusive")
    # Same env source the bind call used, so the client policy keys see
    # the identical .env view (explicit file > --env-file flag > ./.env).
    resolved_env = env_file if env_file is not None else config.env_file
    return _execute(config, resolved_env)


def _execute(config: BoundConfig, env_file: str | None) -> Result:
    """Execute one bound bts request (pure: no storage)."""
    if config.bug_id is None:
        raise UsageError("bts: --bug-id is required")
    policy_values = bind_keys(_POLICY_KEYS, env_file=env_file)
    policy = BtsPolicy(
        timeout_s=policy_values["GLEAM_BTS_TIMEOUT"],
        max_bytes=policy_values["GLEAM_BTS_MAX_OUTPUT_BYTES"],
        max_requests=policy_values["GLEAM_BTS_MAX_REQUESTS"],
        min_interval_s=policy_values["GLEAM_BTS_MIN_INTERVAL"],
        max_messages=policy_values["GLEAM_BTS_MAX_MESSAGES"],
        max_msg_body_chars=policy_values["GLEAM_BTS_MAX_MESSAGE_CHARS"],
        max_urls_per_message=policy_values["GLEAM_BTS_MAX_URLS_PER_MESSAGE"],
        max_pow_seconds=policy_values["GLEAM_BTS_POW_CPU_SECONDS"],
        robots_max_bytes=policy_values["GLEAM_BTS_ROBOTS_MAX_BYTES"],
    )
    client = BtsClient(policy=policy,
                       verbose=make_verbose("bts", config.verbose))
    bts = client.fetch(config.bug_id, config.source)

    if bts.status == "failed":
        status = "failed"  # fetch/transport/parse failure
    elif bts.truncated:
        status = "partial"  # truncation is never masked
    else:
        status = "ok"  # includes the honest empty discussion
    return finalize(
        Result(
            status=status,
            kind=KIND_BUG_RESULT,
            data={**bts.to_data()},
            errors=list(bts.errors),
            diagnostics=list(bts.diagnostics),
            warnings=list(bts.warnings),
        )
    )


def render_json(result: Result) -> str:
    """Single-line machine Result in the golden field order."""
    return result.to_json()


def render_human(result: Result) -> str:
    """Human-readable text (stdout whenever ``--json`` is absent)."""
    data = result.data
    messages = data["messages"]
    stamps = [(m.get("date_iso") or m.get("date") or "")[:10]
              for m in messages]
    stamps = [stamp for stamp in stamps if stamp]
    span = ""
    if stamps:
        span = (f" ({stamps[0]})" if len(set(stamps)) == 1
                else f" ({stamps[0]} → {stamps[-1]})")
    lines: list[str] = [
        f"bts #{data['bug_id']} — {len(messages)} "
        f"{'message' if len(messages) == 1 else 'messages'}{span}"
    ]
    if data["truncated"]:
        lines.append(
            "truncated: more messages available (shown messages are "
            "complete)"
        )
    for position, message in enumerate(messages, 1):
        lines.append("-" * WIDTH)
        stamp = message.get("date_iso") or message.get("date") or "(no date)"
        sender = message.get("from") or "(unknown sender)"
        lines.append(f"[{position}/{len(messages)}] {stamp}  {sender}")
        subject = (message.get("subject_decoded")
                   or message.get("subject") or "(no subject)")
        lines.append(wrap(subject))
        lines.append("")
        body = (message.get("body_text") or "").strip("\n")
        if body:
            lines.extend(("    " + line).rstrip()
                         for line in body.splitlines())
        else:
            lines.append("    (empty body)")
        urls = message.get("urls") or []
        if urls:
            lines.append("")
            lines.append(wrap("links: " + ", ".join(urls), "    "))
        lines.append("")
    lines.extend(problem_lines(result))
    return "\n".join(lines).rstrip()


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
        dump_diagnostics(result, make_verbose("bts", True))
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
