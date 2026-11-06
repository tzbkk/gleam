"""Strict ``.env`` parsing with closed-key validation.

Implements the frozen contract sections:

- UTF-8 files of blank lines, whole-line ``#`` comments, and
  ``KEY=VALUE`` lines with surrounding whitespace stripped. No ``export``,
  quotes, escapes, variable expansion, or inline comments (``#`` and any
  other character belong to the value). Irrelevant non-GLEAM keys are
  ignored; malformed lines, unknown or removed ``GLEAM_*`` keys, and
  duplicate keys are errors — duplicates error even when the values are
  equal.
- An explicit env file that is missing, a directory, unreadable,
  or not valid UTF-8 is an error; an empty or comment-only file parses as
  an empty mapping; a missing *default* ``.env`` simply proceeds (see
  :func:`load_default_env`).
- ``KEY=`` is equivalent to unset, so empty values are dropped by
  the parser itself; callers never see empty-string values.
- Only keys of the closed table may appear; anything else under
  the ``GLEAM_`` prefix is rejected as unknown.

A UTF-8 BOM is a syntax error. CRLF line endings are silently stripped.

Both error classes subclass :class:`gleam.core.result.UsageError`, so the
entry-level exit-2 path stays unified. This module performs
no process-environment access of any kind.
"""

from __future__ import annotations

import re
from pathlib import Path

from gleam.core.result import UsageError

from gleam.core.config.spec import ALL_ENV_KEYS

__all__ = [
    "EnvSyntaxError",
    "EnvFileError",
    "DEFAULT_ENV_NAME",
    "parse_env_text",
    "parse_env_file",
    "load_default_env",
]

#: File name of the default env file in the current working directory
#: (no parent-directory search).
DEFAULT_ENV_NAME = ".env"

_KEY_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BOM = "\ufeff"


class EnvSyntaxError(UsageError):
    """Invalid ``.env`` *content*: BOM, malformed line, duplicate key,
    or unknown ``GLEAM_*`` key. Exit code 2."""


class EnvFileError(UsageError):
    """The env *file* itself failed to load: missing path, directory,
    unreadable, or not valid UTF-8. Exit code 2."""


def parse_env_text(text: str, origin: str = DEFAULT_ENV_NAME) -> dict[str, str]:
    """Parse env-file text into a mapping of closed-table keys.

    Non-GLEAM keys are ignored entirely (duplicates included). GLEAM
    keys are validated against the closed table; empty values are
    dropped (``KEY=`` equals unset).
    """
    if text.startswith(_BOM):
        raise EnvSyntaxError(f"{origin}: UTF-8 BOM is not allowed")
    values: dict[str, str] = {}
    seen: set[str] = set()
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise EnvSyntaxError(
                f"{origin}:{lineno}: expected KEY=VALUE, got {line!r}"
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _KEY_SHAPE.fullmatch(key):
            raise EnvSyntaxError(
                f"{origin}:{lineno}: malformed key {key!r}"
            )
        if not key.startswith("GLEAM_"):
            continue  # irrelevant key: ignored, never validated further
        if key not in ALL_ENV_KEYS:
            raise EnvSyntaxError(
                f"{origin}:{lineno}: unknown GLEAM key {key!r} "
                f"(not in the closed key table)"
            )
        if key in seen:
            raise EnvSyntaxError(
                f"{origin}:{lineno}: duplicate key {key!r}"
            )
        seen.add(key)
        if value:
            values[key] = value
    return values


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Read and parse an explicitly given env file.

    File-level failures raise :class:`EnvFileError`; content failures
    raise :class:`EnvSyntaxError`. An explicit path never falls back to
    the default env file.
    """
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except FileNotFoundError:
        raise EnvFileError(f"env file not found: {file_path}") from None
    except IsADirectoryError:
        raise EnvFileError(f"env file is a directory: {file_path}") from None
    except OSError as exc:
        raise EnvFileError(f"env file unreadable: {file_path}: {exc}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvFileError(
            f"env file is not valid UTF-8: {file_path}: {exc}"
        ) from None
    return parse_env_text(text, origin=str(file_path))


def load_default_env() -> dict[str, str]:
    """Load ``./.env`` from the current working directory.

    A missing default file is not an error: an empty mapping is
    returned and defaults still apply. A default file that exists but
    is broken surfaces its error like any explicit file would.
    """
    path = Path.cwd() / DEFAULT_ENV_NAME
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise EnvFileError(f"default env file unreadable: {path}: {exc}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvFileError(
            f"default env file is not valid UTF-8: {path}: {exc}"
        ) from None
    return parse_env_text(text, origin=str(path))
