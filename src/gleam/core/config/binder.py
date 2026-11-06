"""Shared binder: explicit args > ``.env`` > defaults.

Combines the declarative specs of :mod:`gleam.core.config.spec` with the
strict env parser of :mod:`gleam.core.config.env` into one binding path
shared by the CLI adapters and the ``run(...)`` function API:

1. explicit function arguments (highest),
2. parsed CLI flags,
3. ``.env`` values (explicit file, else ``./.env`` when present),
4. module defaults from the closed key table or the param itself.

Every explicitly supplied value is validated eagerly (type, range,
choice, http(s)-without-credentials URL, fixed input contracts);
violations raise :class:`gleam.core.result.UsageError` (the
exit-2 path). Defaults are trusted because the spec table
validates itself at import time. Required-ness and mode exclusivity are
entry-layer concerns and are deliberately not checked here.

This module never reads the process environment; the only file
inputs are the env file itself and, for the investigate entry, the
operator-owned robots exemption list
(:func:`load_robots_exempt_hosts`).
"""

from __future__ import annotations

import math
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Sequence

from gleam.core.result import UsageError

from gleam.core.config.env import load_default_env, parse_env_file
from gleam.core.config.spec import (
    BUG_ID_PATTERN,
    ENV_KEYS,
    MODULES,
    MODULE_SPECS,
    ParamSpec,
    SOURCE_NAME_MAX,
    SOURCE_NAME_PATTERN,
)

__all__ = ["BoundConfig", "bind", "bind_keys", "load_robots_exempt_hosts"]

_INT_TEXT = re.compile(r"[+-]?[0-9]+\Z")
_FLOAT_TEXT = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z"
)
_BUG_ID_MAX = 9_999_999
#: Hostname-list entry shape: letters/digits/hyphens per label,
#: dot-separated with at least one dot — and nothing else (no scheme,
#: path, port, wildcard, or whitespace can match).
_EXEMPT_HOST = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")


class BoundConfig:
    """Resolved, validated fields of one module call.

    Field access is attribute-based (``config.dsn``); unknown fields
    raise ``AttributeError``. ``as_dict`` gives entries a plain mapping
    to build their typed Request objects from.
    """

    __slots__ = ("_module", "_fields")

    def __init__(self, module: str, fields: dict[str, Any]) -> None:
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_fields", dict(fields))

    @property
    def module(self) -> str:
        return self._module

    def field_names(self) -> tuple[str, ...]:
        return tuple(self._fields)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._fields)

    def __getattr__(self, name: str) -> Any:
        fields = object.__getattribute__(self, "_fields")
        try:
            return fields[name]
        except KeyError:
            module = object.__getattribute__(self, "_module")
            raise AttributeError(
                f"{module} config has no field {name!r}"
            ) from None

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, BoundConfig)
            and other._module == self._module
            and other._fields == self._fields
        )

    def __repr__(self) -> str:
        return f"BoundConfig({self._module!r}, {self._fields!r})"


# ---------------------------------------------------------------------------
# Value coercion and validation
# ---------------------------------------------------------------------------


def _check_range(
    value: float,
    where: str,
    minimum: float | None,
    maximum: float | None,
    exclusive_minimum: bool,
    exclusive_maximum: bool,
) -> None:
    if minimum is not None:
        if exclusive_minimum:
            if not value > minimum:
                raise UsageError(
                    f"{where}: must be greater than {minimum}, got {value}"
                )
        elif not value >= minimum:
            raise UsageError(f"{where}: must be at least {minimum}, got {value}")
    if maximum is not None:
        if exclusive_maximum:
            if not value < maximum:
                raise UsageError(
                    f"{where}: must be less than {maximum}, got {value}"
                )
        elif not value <= maximum:
            raise UsageError(f"{where}: must be at most {maximum}, got {value}")


def _to_int(raw: Any, where: str) -> int:
    if isinstance(raw, bool):
        raise UsageError(f"{where}: expected an integer, got {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and _INT_TEXT.fullmatch(raw):
        return int(raw)
    raise UsageError(f"{where}: expected an integer, got {raw!r}")


def _to_float(raw: Any, where: str) -> float:
    value: float
    if isinstance(raw, bool):
        raise UsageError(f"{where}: expected a number, got {raw!r}")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and _FLOAT_TEXT.fullmatch(raw):
        value = float(raw)
    else:
        raise UsageError(f"{where}: expected a number, got {raw!r}")
    if not math.isfinite(value):
        raise UsageError(f"{where}: expected a finite number, got {raw!r}")
    return value


def _to_str(raw: Any, where: str) -> str:
    if not isinstance(raw, str):
        raise UsageError(f"{where}: expected a string, got {raw!r}")
    return raw


def _to_source_name(raw: Any, where: str) -> str:
    if (
        not isinstance(raw, str)
        or len(raw) > SOURCE_NAME_MAX
        or not SOURCE_NAME_PATTERN.fullmatch(raw)
    ):
        raise UsageError(
            f"{where}: invalid Debian source package name {raw!r} "
            f"(lowercase policy characters, at most {SOURCE_NAME_MAX})"
        )
    return raw


def _to_bug_id(raw: Any, where: str) -> int:
    if isinstance(raw, bool):
        raise UsageError(f"{where}: invalid BTS bug id {raw!r}")
    if isinstance(raw, int):
        if 1 <= raw <= _BUG_ID_MAX:
            return raw
        raise UsageError(
            f"{where}: bug id must be within 1..{_BUG_ID_MAX}, got {raw!r}"
        )
    if isinstance(raw, str) and BUG_ID_PATTERN.fullmatch(raw):
        return int(raw)
    raise UsageError(
        f"{where}: invalid BTS bug id {raw!r} "
        f"(1..{_BUG_ID_MAX}, no leading zeros)"
    )


def _to_choice(raw: Any, where: str, choices: tuple[str, ...]) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise UsageError(
            f"{where}: expected one of ({', '.join(choices)}), got {raw!r}"
        )
    return raw


def _to_http_url(raw: Any, where: str) -> str:
    value = _to_str(raw, where)
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # validates the port when present
    except ValueError:
        raise UsageError(f"{where}: malformed URL {value!r}") from None
    if parts.scheme not in ("http", "https"):
        raise UsageError(
            f"{where}: URL scheme must be http or https, got {value!r}"
        )
    if not parts.hostname:
        raise UsageError(f"{where}: URL has no host: {value!r}")
    if parts.username is not None or parts.password is not None:
        raise UsageError(f"{where}: URL must not carry credentials: {value!r}")
    return value


def _coerce_value(
    kind: str,
    raw: Any,
    where: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
    exclusive_maximum: bool = False,
    choices: tuple[str, ...] = (),
) -> Any:
    """Validate one explicit value against its kind and constraints.

    Accepts both text (CLI/env origin) and native Python values
    (function-arg origin) so the same path serves ``main(argv)`` and
    ``run(request)``.
    """
    if kind == "int":
        value = _to_int(raw, where)
        _check_range(
            value, where, minimum, maximum, exclusive_minimum, exclusive_maximum
        )
        return value
    if kind == "float":
        value = _to_float(raw, where)
        _check_range(
            value, where, minimum, maximum, exclusive_minimum, exclusive_maximum
        )
        return value
    if kind == "url":
        return _to_http_url(raw, where)
    if kind == "source_name":
        return _to_source_name(raw, where)
    if kind == "bug_id":
        return _to_bug_id(raw, where)
    if kind == "choice":
        return _to_choice(raw, where, choices)
    if kind == "bool":
        if not isinstance(raw, bool):
            raise UsageError(f"{where}: expected a boolean, got {raw!r}")
        return raw
    return _to_str(raw, where)


def _constraints_of(param: ParamSpec) -> dict[str, Any]:
    """Resolve kind/default/range authority (closed table wins)."""
    if param.env_key is not None:
        key_spec = ENV_KEYS[param.env_key]
        return {
            "kind": key_spec.kind,
            "default": key_spec.default,
            "minimum": key_spec.minimum,
            "maximum": key_spec.maximum,
            "exclusive_minimum": key_spec.exclusive_minimum,
            "exclusive_maximum": key_spec.exclusive_maximum,
            "choices": key_spec.choices,
        }
    return {
        "kind": param.kind,
        "default": param.default,
        "minimum": param.minimum,
        "maximum": param.maximum,
        "exclusive_minimum": param.exclusive_minimum,
        "exclusive_maximum": param.exclusive_maximum,
        "choices": param.choices,
    }


# ---------------------------------------------------------------------------
# argv parsing (driven purely by the declarative specs)
# ---------------------------------------------------------------------------


def _parse_argv(
    module: str, params: Sequence[ParamSpec], argv: Sequence[str]
) -> dict[str, Any]:
    """Parse module arguments per the flag specs (``--flag value``,
    ``--flag=value``, bare boolean switches, spec positionals).

    Unknown flags, stray positionals, switches given a value, and value
    flags missing their value are usage errors. A repeatable flag
    accumulates; a plain value flag uses the last occurrence.
    """
    by_flag = {param.flag: param for param in params if param.flag}
    positionals = [param for param in params if param.positional]
    parsed: dict[str, Any] = {}
    pos_index = 0
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            for tail in argv[index + 1 :]:
                if pos_index >= len(positionals):
                    raise UsageError(f"{module}: unexpected argument {tail!r}")
                parsed[positionals[pos_index].name] = tail
                pos_index += 1
            break
        if token.startswith("-") and token != "-":
            name, has_inline, inline = token.partition("=")
            param = by_flag.get(name)
            if param is None:
                raise UsageError(f"{module}: unknown flag {name!r}")
            if param.boolean:
                if has_inline:
                    raise UsageError(f"{module}: flag {name} takes no value")
                parsed[param.name] = True
            elif has_inline:
                value: str = inline
                _store_value(module, param, value, parsed)
            else:
                index += 1
                if index >= len(argv):
                    raise UsageError(
                        f"{module}: flag {name} requires a value"
                    )
                _store_value(module, param, argv[index], parsed)
        else:
            if pos_index >= len(positionals):
                raise UsageError(f"{module}: unexpected argument {token!r}")
            parsed[positionals[pos_index].name] = token
            pos_index += 1
        index += 1
    return parsed


def _store_value(
    module: str, param: ParamSpec, value: str, parsed: dict[str, Any]
) -> None:
    if param.repeatable:
        parsed.setdefault(param.name, []).append(value)
    else:
        parsed[param.name] = value


def _where_of(param: ParamSpec) -> str:
    if param.flag is not None:
        return f"flag {param.flag}"
    return f"argument {param.name}"


# ---------------------------------------------------------------------------
# Public binders
# ---------------------------------------------------------------------------


def bind(
    module: str,
    argv: Sequence[str] | None = None,
    env_file: str | Path | None = None,
    **overrides: Any,
) -> BoundConfig:
    """Bind one module call's fields from args, env file, and defaults.

    ``module`` is one of the eight entry names. ``argv`` holds the
    module's own arguments (no subcommand name); ``env_file``
    selects an explicit env file (missing default proceeds);
    ``overrides`` are explicit function-argument values keyed by field
    name and win over everything else. Explicit CLI empty strings are
    kept as-is; only ``.env`` empty values are unset-equivalent.
    """
    if module not in MODULE_SPECS:
        raise ValueError(
            f"unknown module {module!r}; expected one of {MODULES}"
        )
    params = MODULE_SPECS[module]
    by_name = {param.name: param for param in params}
    for name in overrides:
        if name not in by_name:
            raise UsageError(f"{module}: unknown field {name!r}")
    argv_items = list(argv) if argv is not None else []
    from_cli = _parse_argv(module, params, argv_items)
    resolved_env = (
        env_file if env_file is not None else from_cli.get("env_file")
    )
    if resolved_env:
        env_values = parse_env_file(resolved_env)
    else:
        env_values = load_default_env()
    fields: dict[str, Any] = {}
    for param in params:
        constraints = _constraints_of(param)
        if param.name in overrides:
            raw: Any = overrides[param.name]
            where = f"argument {param.name}"
        elif param.name in from_cli:
            raw = from_cli[param.name]
            where = _where_of(param)
        elif param.env_key is not None and param.env_key in env_values:
            raw = env_values[param.env_key]
            where = f"env {param.env_key}"
        else:
            fields[param.name] = constraints["default"]
            continue
        if param.repeatable:
            items = raw if isinstance(raw, (list, tuple)) else [raw]
            fields[param.name] = tuple(
                _coerce_value(
                    constraints["kind"],
                    item,
                    f"{where}[{position}]",
                    minimum=constraints["minimum"],
                    maximum=constraints["maximum"],
                    exclusive_minimum=constraints["exclusive_minimum"],
                    exclusive_maximum=constraints["exclusive_maximum"],
                    choices=constraints["choices"],
                )
                for position, item in enumerate(items)
            )
        else:
            fields[param.name] = _coerce_value(
                constraints["kind"],
                raw,
                where,
                minimum=constraints["minimum"],
                maximum=constraints["maximum"],
                exclusive_minimum=constraints["exclusive_minimum"],
                exclusive_maximum=constraints["exclusive_maximum"],
                choices=constraints["choices"],
            )
    return BoundConfig(module, fields)


def bind_keys(
    keys: Iterable[str],
    env_file: str | Path | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Resolve a subset of closed-table keys with the same precedence.

    Serves capability-domain consumers whose keys are not request
    fields of any entry module (the udd/bts client policy keys): the
    same explicit-arg > env > default path as :func:`bind`, restricted
    to the closed table. Unknown key names are programming
    errors (``ValueError``), not usage errors.
    """
    key_list = list(keys)
    for key in key_list:
        if key not in ENV_KEYS:
            raise ValueError(f"key {key!r} is outside the closed table")
    for key in overrides:
        if key not in ENV_KEYS:
            raise ValueError(f"key {key!r} is outside the closed table")
    if env_file is not None:
        env_values = parse_env_file(env_file)
    else:
        env_values = load_default_env()
    resolved: dict[str, Any] = {}
    for key in key_list:
        key_spec = ENV_KEYS[key]
        if key in overrides:
            raw: Any = overrides[key]
            where = f"argument {key}"
        elif key in env_values:
            raw = env_values[key]
            where = f"env {key}"
        else:
            resolved[key] = key_spec.default
            continue
        resolved[key] = _coerce_value(
            key_spec.kind,
            raw,
            where,
            minimum=key_spec.minimum,
            maximum=key_spec.maximum,
            exclusive_minimum=key_spec.exclusive_minimum,
            exclusive_maximum=key_spec.exclusive_maximum,
            choices=key_spec.choices,
        )
    return resolved


def load_robots_exempt_hosts(path: str | Path) -> frozenset[str]:
    """Read the operator-owned robots.txt exemption list.

    One hostname per line, lowercased into a frozenset; ``#`` comment
    lines and blank lines are ignored. An absent file is the normal
    no-exemptions case (empty frozenset). Any other line that is not a
    bare dot-containing hostname (letters/digits/hyphens/dots only —
    no scheme, path, port, wildcard, or whitespace) raises
    :class:`gleam.core.result.UsageError` naming the file, the line
    number, and the offending text (the exit-2 startup path; no run is
    created).
    """

    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise UsageError(
            f"robots exemption file {str(path)!r} is not readable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    hosts: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        if not _EXEMPT_HOST.fullmatch(line):
            raise UsageError(
                f"robots exemption file {str(path)!r} line {number}: "
                f"invalid hostname entry {line!r} (expected a bare "
                f"hostname: letters/digits/hyphens/dots with at least "
                f"one dot; no scheme, path, port, wildcard, or "
                f"whitespace)"
            )
        hosts.add(line.lower())
    return frozenset(hosts)
