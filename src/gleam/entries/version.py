"""version - print the gleam version

Print exactly one line: the gleam version. The single source of truth
is pyproject.toml, so the installed package and a source-tree checkout
always report the same value. Any argument at all is a usage error
(exit 2) -- there is no --version flag; run gleam version itself.

Usage: gleam version

Output: one version line on stdout, exit 0. On a usage error the
diagnostic goes to stderr, no version line is printed, and the exit
code is 2.

Storage: never writes to disk (no network, no storage).

Examples:
  python -m gleam version
"""

# Dev notes: pure informational subcommand -- exactly one version line
# to stdout on success, no Result, no storage, no network. The version
# comes from the shared package-version helper: installed package
# metadata first (built from pyproject.toml), falling back to reading
# the repository's pyproject.toml directly for source trees; no
# secondary version constant exists. The flag surface is empty, so
# every argument is a usage error. Structure: parser / version_string /
# main.

from __future__ import annotations

import sys
from typing import Sequence

from gleam.core.config import BoundConfig, bind
from gleam.core.package_version import package_version
from gleam.core.result import UsageError

__all__ = ["main", "parser", "version_string"]

_MODULE = "version"


def parser(
    argv: Sequence[str] | None = None,
    env_file: str | None = None,
    **overrides: object,
) -> BoundConfig:
    """Bind one version call: the spec has zero fields.

    Every argument is therefore a usage error here: an unknown flag
    (``-h``/``--help``/``--version`` included) or a stray positional.
    """
    return bind(_MODULE, argv=argv, env_file=env_file, **overrides)


def version_string() -> str:
    """The gleam version from the single source."""
    return package_version()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: one version line to stdout, exit 0.

    Usage errors print the diagnostic to stderr and return 2 with no
    stdout line (``--version`` itself is rejected as a flag).
    """
    args = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        parser(argv=args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    print(version_string())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
