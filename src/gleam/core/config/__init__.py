"""L3 config domain: declarative specs, strict ``.env``, shared binder.

Public surface (consumed by the entry modules and the ``.env``
end-to-end tests):

- :func:`bind` — bind one module call's fields with the frozen
  precedence explicit args > CLI > ``.env`` > defaults.
- :func:`bind_keys` — the same precedence for arbitrary closed-table
  key subsets (capability-domain policy consumers).
- :func:`load_robots_exempt_hosts` — the operator-owned robots.txt
  exemption list parser (absent file = empty set, invalid
  lines raise :class:`gleam.core.result.UsageError`).
- :func:`parse_env_file` / :func:`parse_env_text` /
  :func:`load_default_env` — the strict env parser.
- :class:`EnvSyntaxError` / :class:`EnvFileError` — the env error
  classes; both subclass :class:`gleam.core.result.UsageError` so the
  exit-2 path stays unified.
- :data:`ENV_KEYS` / :data:`ALL_ENV_KEYS` / :data:`MODULE_SPECS` — the
  closed ``GLEAM_*`` key table and the eight modules' flag
  surfaces, as data.

The whole package is offline and side-effect free apart from reading
the env file itself; the process environment is never accessed,
and hard-boundary fields get no
override surface here.
"""

from gleam.core.config.binder import (
    BoundConfig,
    bind,
    bind_keys,
    load_robots_exempt_hosts,
)
from gleam.core.config.env import (
    DEFAULT_ENV_NAME,
    EnvFileError,
    EnvSyntaxError,
    load_default_env,
    parse_env_file,
    parse_env_text,
)
from gleam.core.config.spec import (
    ALL_ENV_KEYS,
    BUG_ID_PATTERN,
    ENV_KEYS,
    KeySpec,
    MODULES,
    MODULE_SPECS,
    ParamSpec,
    SOURCE_NAME_MAX,
    SOURCE_NAME_PATTERN,
)

__all__ = [
    "ALL_ENV_KEYS",
    "BUG_ID_PATTERN",
    "BoundConfig",
    "DEFAULT_ENV_NAME",
    "ENV_KEYS",
    "EnvFileError",
    "EnvSyntaxError",
    "KeySpec",
    "MODULES",
    "MODULE_SPECS",
    "ParamSpec",
    "SOURCE_NAME_MAX",
    "SOURCE_NAME_PATTERN",
    "bind",
    "bind_keys",
    "load_default_env",
    "load_robots_exempt_hosts",
    "parse_env_file",
    "parse_env_text",
]
