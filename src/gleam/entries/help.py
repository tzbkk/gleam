"""help - show the gleam overview or one module's own help

With no argument, print the one-page overview of all eight modules.
With a module name, print that module's own self-contained help: what
it does, its modes, every flag with its default, exit codes, storage
behavior, and examples. The module name is a positional argument, not
a flag. There are no -h/--help/--version builtins anywhere in gleam:
unknown flags, unknown targets, and stray arguments are usage errors
(exit 2).

Usage: gleam help [MODULE]

Argument:
  MODULE    one of: udd, bts, repology, check, investigate, searxng,
            help, version (omitted: the overview)

Output: the requested text on stdout, exit 0. On a usage error the
diagnostic goes to stderr, nothing goes to stdout, and the exit code
is 2.

Storage: never writes to disk (and never touches the network).

Examples:
  python -m gleam help
  python -m gleam help investigate
"""

# Dev notes: pure informational subcommand -- no Result, no storage,
# no network; stdout only on success, stderr usage diagnostics on
# rejection. help only aggregates and delegates: the module texts ARE
# the entry modules' own docstrings (each module stays the single
# owner of its documentation; no parallel copy is maintained here).
# Delegation imports are resolved lazily so the overview path imports
# no heavyweight module stack. Structure: parser / general_help /
# module_help / main.

from __future__ import annotations

import importlib
import sys
import textwrap
from typing import Sequence

from gleam.core.config import BoundConfig, bind
from gleam.core.result import UsageError

__all__ = ["general_help", "main", "module_help", "parser"]

_MODULE = "help"

#: Import paths of the eight entry modules, keyed by target name
#: (canonical order). Resolved lazily per delegation so that the
#: overview path imports no heavyweight module stack.
_ENTRY_MODULES = {
    "udd": "gleam.entries.udd",
    "bts": "gleam.entries.bts",
    "repology": "gleam.entries.repology",
    "check": "gleam.entries.check",
    "investigate": "gleam.entries.investigate",
    "searxng": "gleam.entries.searxng",
    "help": "gleam.entries.help",
    "version": "gleam.entries.version",
}

#: The one-page overview: every module with a one-line
#: summary, plus the delegation form.
_OVERVIEW = textwrap.dedent(
    """\
    gleam - upstream archaeology investigation for Debian QA packages

    Usage: gleam <module> [flags]

    Modules:
      udd          query live UDD package facts for Debian QA packages
      bts          fetch one Debian BTS bug's public discussion
      repology     query the Repology cross-distro version tracker
      check        validate an evidence store for structural integrity
      investigate  run a bounded tool-calling investigation of one package
      searxng      manage the local SearXNG search service (start/stop/validate)
      help         show this overview or one module's own help
      version      print the gleam version

    Run "gleam help <module>" to read the module's self-contained help.
    """
).rstrip()


def parser(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> BoundConfig:
    """Bind one help call from argv/env file/defaults.

    The flag surface is the declarative ``MODULE_SPECS["help"]``:
    one optional positional ``target_module`` restricted to
    the eight module names. Unknown flags (including ``-h``/``--help``),
    unknown targets (choice violation), and extra positionals
    raise ``UsageError`` here.
    """
    return bind(_MODULE, argv=argv, env_file=env_file, **overrides)


def general_help() -> str:
    """The one-page overview shown when no target is given."""
    return _OVERVIEW


def module_help(target: str) -> str:
    """The target module's self-contained documentation.

    Delegation, not a parallel copy: the text IS the target entry
    module's own docstring, so each module stays the single owner of
    its flags, description, and examples.
    """
    module = importlib.import_module(_ENTRY_MODULES[target])
    doc = module.__doc__ or ""
    return textwrap.dedent(doc).strip()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code.

    No target: the overview goes to stdout, exit 0. With a target: the
    module's own documentation goes to stdout, exit 0. Usage errors
    print the diagnostic to stderr and return 2 WITHOUT any stdout
    output (``-h``/``--help``/``--version`` included).
    """
    args = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        config = parser(argv=args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    if config.target_module is None:
        print(general_help())
        return 0
    print(module_help(config.target_module))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
