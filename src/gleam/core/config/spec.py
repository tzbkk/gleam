"""Declarative FlagSpec/ConfigSpec data for the eight entry modules.

Three layers are expressed here:

- the CLI flag surfaces of ``udd`` / ``bts`` / ``repology`` / ``check`` /
  ``investigate`` / ``searxng`` / ``help`` / ``version``;
- the closed table of ``GLEAM_*`` ``.env`` keys. The table below is a
  verbatim transcription of the frozen contract; keys must never be
  generated from field names, and no key outside the table may exist.
  Action switches (``--json``, ``--list-datasets``, ``--no-*``) are
  deliberately without an env key;
- one declarative definition per rule: ``investigate`` reuses the
  identical ``ParamSpec`` objects of ``udd`` for the inherited query
  fields, and ``searxng`` reuses the investigate ``search_base``
  param, instead of copying them.

Request fields, ConfigSpec keys, and Result data are three distinct
layers; this module expresses only the first two.

The investigate group lists the investigation policy keys
(``GLEAM_INVESTIGATE_STORE`` .. ``GLEAM_MIN_QUOTE_CHARS``), so the
closed table holds 6 + 8 + 10 + 4 + 32 + 1 = 61 keys. Where hard ranges
are frozen (budgets, ``per_page``, model token/timeout caps) they are
transcribed exactly; where only a default is frozen, the range is the
minimal safe bound (positive/non-negative) and is documented per key.

``GLEAM_MODEL_API_KEY`` joins the shared group after
``GLEAM_MODEL_BASE`` — an optional bearer token for authenticated
OpenAI-compatible endpoints, default unset (an empty ``.env`` value
means unset). The key is a secret: it is consumed only as a request
header by the model client and never appears in audit records, results,
or storage.

``GLEAM_ROBOTS_EXEMPT_FILE`` joins the investigate group — the
operator-owned robots.txt exemption list (a bare-hostname file, default
``robots-exempt.txt`` resolved relative to the process CWD). The file is
read once at build_config time; the derived ``robots_exempt_hosts``
frozenset is what downstream consumes.

Four Repology policy keys join the table (budget follows the BTS
precedent; the API terms require <=1 request/second) —
``GLEAM_REPOLOGY_MAX_REQUESTS`` (6), ``GLEAM_REPOLOGY_MIN_INTERVAL``
(1.1), ``GLEAM_REPOLOGY_TIMEOUT`` (15.0), ``GLEAM_REPOLOGY_BASE``
(https://repology.org). The base key is shared by the ``repology``
entry and the investigate ``repology`` tool; the ``searxng`` entry
adds no key of its own (its ``--search-base`` reuses
``GLEAM_SEARCH_BASE``).

Purity: this module is data plus consistency checks. No I/O, no output,
no process-environment access, no argv parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "KeySpec",
    "ParamSpec",
    "ENV_KEYS",
    "ALL_ENV_KEYS",
    "MODULE_SPECS",
    "MODULES",
    "SOURCE_NAME_PATTERN",
    "BUG_ID_PATTERN",
    "SOURCE_NAME_MAX",
]

#: The eight entry modules in canonical order (``repology`` follows
#: ``bts`` so the source modules stay grouped; ``searxng`` — the
#: deployment manager for the shared search backend — precedes the
#: informational pair).
MODULES = ("udd", "bts", "repology", "check", "investigate", "searxng",
           "help", "version")

_KINDS = ("str", "int", "float", "url", "choice", "source_name", "bug_id", "bool")

_FLAG_SHAPE = re.compile(r"--[a-z][a-z0-9-]*\Z")
_ENV_KEY_SHAPE = re.compile(r"GLEAM_[A-Z0-9_]+\Z")
# Fixed Debian source-package input contract: lowercase policy
# characters, 64 characters at most, shared by udd and investigate and
# never configurable.
SOURCE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9+.-]{0,63}\Z")
SOURCE_NAME_MAX = 64
# Fixed BTS bug id contract: 1..9999999, no leading zeros.
BUG_ID_PATTERN = re.compile(r"[1-9][0-9]{0,6}\Z")
#: Maximum per-dataset row selection (every dataset max_limit=50).
_UDD_LIMIT_MAX = 50


@dataclass(frozen=True)
class KeySpec:
    """One member of the closed ``GLEAM_*`` key table (ConfigSpec).

    ``maps`` documents the capability-domain target(s); it is
    documentation, not a lookup key.
    """

    key: str
    kind: str
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False
    choices: tuple[str, ...] = ()
    maps: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise AssertionError(f"unknown kind {self.kind!r} for {self.key}")
        if not _ENV_KEY_SHAPE.fullmatch(self.key):
            raise AssertionError(f"malformed env key {self.key!r}")


@dataclass(frozen=True)
class ParamSpec:
    """One request-field surface: flag, env key, parsing and validation.

    A param with ``env_key`` defers kind/default/range authority to the
    closed :data:`ENV_KEYS` entry and must not carry its own default;
    the import-time consistency check enforces this. A param without
    ``env_key`` (action switches, one-shot inputs) is CLI/function-arg
    only.
    """

    name: str
    kind: str
    flag: str | None = None
    env_key: str | None = None
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False
    choices: tuple[str, ...] = ()
    boolean: bool = False
    repeatable: bool = False
    positional: bool = False
    help: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise AssertionError(f"unknown kind {self.kind!r} for {self.name}")
        if self.flag is not None and not _FLAG_SHAPE.fullmatch(self.flag):
            raise AssertionError(f"malformed flag {self.flag!r}")
        if self.kind == "bool":
            if not self.boolean:
                raise AssertionError(f"bool kind requires boolean=True: {self.name}")
        elif self.boolean:
            raise AssertionError(f"boolean=True requires bool kind: {self.name}")


# ---------------------------------------------------------------------------
# The closed key table — transcribed verbatim, in fixed order.
# ---------------------------------------------------------------------------

_KEY_TABLE: tuple[KeySpec, ...] = (
    # Shared keys.
    KeySpec("GLEAM_UDD_DSN", "str",
            default="postgresql://udd-mirror.debian.net/udd",
            maps="udd.dsn; investigate.dsn"),
    KeySpec("GLEAM_MODEL", "str", maps="investigate.model"),
    KeySpec("GLEAM_MODEL_BASE", "url", maps="investigate.model_base"),
    # Optional bearer token; default unset, no range.
    KeySpec("GLEAM_MODEL_API_KEY", "str", maps="investigate.model_api_key"),
    KeySpec("GLEAM_SEARCH_BASE", "url", default="http://127.0.0.1:8888",
            maps="investigate.search_base; searxng.search_base"),
    KeySpec("GLEAM_GITHUB_API_BASE", "url", default="https://api.github.com",
            maps="investigate.github_api_base"),
    # UDD policy keys.
    KeySpec("GLEAM_UDD_STATEMENT_TIMEOUT", "int", default=15000, minimum=1,
            maps="udd client policy: per-statement timeout, ms"),
    KeySpec("GLEAM_UDD_MAX_CELL_CHARS", "int", default=16384, minimum=1,
            maps="udd client policy: per-cell character cap"),
    KeySpec("GLEAM_UDD_MAX_TOTAL_ROW_CHARS", "int", default=262144,
            minimum=1, maps="udd client policy: total row characters cap"),
    KeySpec("GLEAM_UDD_MAX_QUERIES", "int", default=10, minimum=1,
            maps="udd client policy: statements per fetch"),
    KeySpec("GLEAM_UDD_FIELD_CHAR_CAP", "int", default=600, minimum=1,
            maps="udd client policy: per-field display cap"),
    KeySpec("GLEAM_UDD_MAX_DATASETS", "int", default=6, minimum=1,
            maps="udd client policy: datasets per query"),
    KeySpec("GLEAM_UDD_MAX_ROWS", "int", default=120, minimum=1,
            maps="udd client policy: effective rows per query"),
    KeySpec("GLEAM_UDD_MAX_ROWS_PER_DATASET", "int", default=20, minimum=1,
            maximum=_UDD_LIMIT_MAX,
            maps="udd client policy: rows per dataset (<= registry max_limit)"),
    # BTS policy keys.
    KeySpec("GLEAM_BTS_SOURCE", "choice", default="auto",
            choices=("auto", "soap", "mbox"), maps="bts.source"),
    KeySpec("GLEAM_BTS_TIMEOUT", "float", default=20.0, minimum=0.0,
            exclusive_minimum=True, maps="bts client policy: timeout seconds"),
    KeySpec("GLEAM_BTS_MAX_OUTPUT_BYTES", "int", default=8 * 1024 * 1024,
            minimum=1, maps="bts client policy: single response cap"),
    KeySpec("GLEAM_BTS_MAX_REQUESTS", "int", default=6, minimum=1,
            maps="bts client policy: cumulative requests"),
    KeySpec("GLEAM_BTS_MIN_INTERVAL", "float", default=2.0, minimum=0.0,
            maps="bts client policy: min interval seconds"),
    KeySpec("GLEAM_BTS_MAX_MESSAGES", "int", default=200, minimum=1,
            maps="bts client policy: parsed messages cap"),
    KeySpec("GLEAM_BTS_MAX_MESSAGE_CHARS", "int", default=20000, minimum=1,
            maps="bts client policy: single message body chars"),
    KeySpec("GLEAM_BTS_MAX_URLS_PER_MESSAGE", "int", default=20, minimum=1,
            maps="bts client policy: URLs registered per message"),
    KeySpec("GLEAM_BTS_POW_CPU_SECONDS", "float", default=20.0, minimum=0.0,
            exclusive_minimum=True, maps="bts client policy: PoW CPU seconds"),
    KeySpec("GLEAM_BTS_ROBOTS_MAX_BYTES", "int", default=65536, minimum=1,
            maps="bts client policy: robots.txt read cap"),
    # Repology policy keys (BTS budget precedent; API terms require
    # <=1 request/second).
    KeySpec("GLEAM_REPOLOGY_MAX_REQUESTS", "int", default=6, minimum=1,
            maps="repology client policy: cumulative requests"),
    KeySpec("GLEAM_REPOLOGY_MIN_INTERVAL", "float", default=1.1, minimum=0.0,
            maps="repology client policy: min interval seconds "
                 "(API terms: <=1 rps)"),
    KeySpec("GLEAM_REPOLOGY_TIMEOUT", "float", default=15.0, minimum=0.0,
            exclusive_minimum=True,
            maps="repology client policy: timeout seconds"),
    KeySpec("GLEAM_REPOLOGY_BASE", "url", default="https://repology.org",
            maps="repology.base; investigate.repology_base"),
    # investigate model/fetch/tool/report policy keys.
    KeySpec("GLEAM_INVESTIGATE_STORE", "str", default="runs",
            maps="investigate.store"),
    KeySpec("GLEAM_MODEL_MAX_TOKENS", "int", default=8192, minimum=1,
            maximum=65536, maps="investigate.model_max_tokens"),
    KeySpec("GLEAM_MODEL_TIMEOUT", "float", default=120.0, minimum=0.0,
            exclusive_minimum=True, maximum=600.0,
            maps="investigate.model_timeout"),
    KeySpec("GLEAM_MODEL_TEMPERATURE", "float", default=0.0, minimum=0.0,
            maximum=2.0, maps="investigate.temperature"),
    KeySpec("GLEAM_MODEL_RESPONSE_MAX_BYTES", "int", default=1024 * 1024,
            minimum=1, maps="investigate.model_response_max_bytes"),
    KeySpec("GLEAM_MODEL_PROXY", "url", maps="investigate.model_proxy"),
    KeySpec("GLEAM_MAX_ROUNDS", "int", default=48, minimum=1, maximum=64,
            maps="investigate.max_rounds"),
    KeySpec("GLEAM_MAX_TOOL_CALLS", "int", default=48, minimum=1, maximum=512,
            maps="investigate.max_tool_calls"),
    KeySpec("GLEAM_MAX_BODY_CHARS", "int", default=40000, minimum=200,
            maximum=200000, maps="investigate.max_body_chars"),
    KeySpec("GLEAM_MAX_LINKS", "int", default=120, minimum=1, maximum=200,
            maps="investigate.max_links"),
    KeySpec("GLEAM_MAX_CONTEXT_CHARS", "int", default=320000, minimum=4000,
            maximum=2000000, maps="investigate.max_context_chars"),
    KeySpec("GLEAM_MAX_HTTP_REQUESTS", "int", default=52, minimum=1,
            maps="investigate.max_http_requests"),
    KeySpec("GLEAM_MAX_BYTES", "int", default=300000, minimum=1,
            maps="investigate.fetch_max_bytes"),
    KeySpec("GLEAM_TIMEOUT", "float", default=15.0, minimum=0.0,
            exclusive_minimum=True, maps="investigate.fetch_timeout"),
    KeySpec("GLEAM_MAX_REDIRECTS", "int", default=3, minimum=0,
            maps="investigate.fetch_max_redirects"),
    KeySpec("GLEAM_MIN_INTERVAL", "float", default=1.0, minimum=0.0,
            maps="investigate.fetch_min_interval"),
    # Operator-owned robots exemption list (path-like
    # str, resolved relative to the process CWD; absent file = empty
    # exemption set, the derived frozenset lives on the config object).
    KeySpec("GLEAM_ROBOTS_EXEMPT_FILE", "str", default="robots-exempt.txt",
            maps="investigate.robots_exempt_file"),
    KeySpec("GLEAM_SEARCH_RESPONSE_MAX_BYTES", "int", default=1024 * 1024,
            minimum=1, maps="search client policy: response cap"),
    KeySpec("GLEAM_GITHUB_README_TEXT_CAP", "int", default=12000, minimum=1,
            maps="github tool policy: readme text cap"),
    KeySpec("GLEAM_GITHUB_PER_PAGE", "int", default=10, minimum=1, maximum=100,
            maps="github tool policy: per_page (hard bound 1..100)"),
    KeySpec("GLEAM_REPORT_CELL_MAX_CHARS", "int", default=160, minimum=1,
            maps="report policy: table cell cap"),
    KeySpec("GLEAM_REPORT_MAX_ERRORS", "int", default=30, minimum=1,
            maps="report policy: listed errors cap"),
    KeySpec("GLEAM_REPORT_MAX_REPAIR_REASONS", "int", default=10, minimum=1,
            maps="report policy: rejection reasons cap"),
    KeySpec("GLEAM_CONTEXT_KEEP_CHARS", "int", default=100, minimum=1,
            maps="context truncation envelope: keep chars"),
    KeySpec("GLEAM_CONTEXT_OVERHEAD_CHARS", "int", default=260, minimum=1,
            maps="context truncation envelope: overhead estimate"),
    KeySpec("GLEAM_CONTEXT_MAX_ITERATIONS", "int", default=200, minimum=1,
            maps="context truncation envelope: iteration cap"),
    KeySpec("GLEAM_MAILTO_MAX", "int", default=10, minimum=1,
            maps="origin signals: mailto cap"),
    KeySpec("GLEAM_COPYRIGHT_MAX", "int", default=8, minimum=1,
            maps="origin signals: copyright lines cap"),
    KeySpec("GLEAM_FOOTER_MAX", "int", default=8, minimum=1,
            maps="origin signals: footer lines cap"),
    KeySpec("GLEAM_MAX_URL_CHARS", "int", default=2048, minimum=1,
            maps="origin signals: URL char cap"),
    KeySpec("GLEAM_MAX_SEARCH_QUERY_CHARS", "int", default=400, minimum=1,
            maps="search policy: query char cap"),
    KeySpec("GLEAM_MIN_QUOTE_CHARS", "int", default=8, minimum=1,
            maps="final report policy: minimum verbatim quote"),
    # check.
    KeySpec("GLEAM_CHECK_STORE", "str", default="runs", maps="check.store"),
)

#: The closed key table keyed by env key name.
ENV_KEYS: Mapping[str, KeySpec] = {spec.key: spec for spec in _KEY_TABLE}

#: Convenience view of the closed key set.
ALL_ENV_KEYS = frozenset(ENV_KEYS)


# ---------------------------------------------------------------------------
# Shared param objects — defined once, reused by reference.
# ---------------------------------------------------------------------------

_UDD_QUERY_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("package", "source_name", flag="--package",
              help="Debian source package name"),
    ParamSpec("datasets", "str", flag="--dataset", repeatable=True,
              default=(),
              help="dataset name; repeatable, at least one in query mode"),
    ParamSpec("release", "str", flag="--release",
              help="release filter; the sources layer applies its default"),
    ParamSpec("limit", "int", flag="--limit", minimum=1,
              maximum=_UDD_LIMIT_MAX,
              help="per-dataset row selection cap (1..50)"),
    ParamSpec("dsn", "str", flag="--dsn", env_key="GLEAM_UDD_DSN",
              help="UDD connection DSN"),
    ParamSpec("timeout", "float", flag="--timeout", default=45.0,
              minimum=0.0, exclusive_minimum=True,
              help="query deadline seconds (socket deadline plus "
                   "per-statement budget)"),
)

_JSON_PARAM = ParamSpec("json", "bool", flag="--json", default=False,
                        boolean=True,
                        help="write the machine-readable Result to stdout")
_VERBOSE_PARAM = ParamSpec("verbose", "bool", flag="--verbose",
                           default=False, boolean=True,
                           help="narrate progress to stderr")
_ENV_FILE_PARAM = ParamSpec("env_file", "str", flag="--env-file",
                            help="explicit .env path (default: ./.env)")
#: Trusted local SearXNG JSON base URL — shared by the investigate
#: ``search_web`` tool and the ``searxng`` deployment entry.
_SEARCH_BASE_PARAM = ParamSpec("search_base", "url", flag="--search-base",
                               env_key="GLEAM_SEARCH_BASE",
                               help="trusted local SearXNG JSON base URL")

# udd flag surface.
_UDD_PARAMS: tuple[ParamSpec, ...] = _UDD_QUERY_PARAMS + (
    ParamSpec("list_datasets", "bool", flag="--list-datasets", default=False,
              boolean=True,
              help="print the dataset catalog without contacting the database"),
    _JSON_PARAM,
    _VERBOSE_PARAM,
    _ENV_FILE_PARAM,
)

# bts flag surface.
_BTS_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("bug_id", "bug_id", flag="--bug-id",
              help="Debian BTS bug id"),
    ParamSpec("source", "choice", flag="--source", env_key="GLEAM_BTS_SOURCE",
              help="fetch path: auto, soap, or mbox"),
    _JSON_PARAM,
    _VERBOSE_PARAM,
    _ENV_FILE_PARAM,
)

# check flag surface.
_CHECK_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("store", "str", flag="--store", env_key="GLEAM_CHECK_STORE",
              help="evidence store root to scan"),
    ParamSpec("run", "str", flag="--run",
              help="one run directory to validate instead of a whole store"),
    _JSON_PARAM,
    _VERBOSE_PARAM,
    _ENV_FILE_PARAM,
)

# repology flag surface: exactly one of --project/--search is required
# (the mode check stays with the entry); the four policy fields are
# env-keyed closed-table values.
_REPOLOGY_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("project", "str", flag="--project",
              help="project slug; exactly one of --project/--search"),
    ParamSpec("search", "str", flag="--search",
              help="search query; exactly one of --project/--search"),
    ParamSpec("repology_base", "url", flag="--repology-base",
              env_key="GLEAM_REPOLOGY_BASE",
              help="Repology API base URL (operator only)"),
    ParamSpec("repology_max_requests", "int",
              flag="--repology-max-requests",
              env_key="GLEAM_REPOLOGY_MAX_REQUESTS",
              help="cumulative request cap"),
    ParamSpec("repology_min_interval", "float",
              flag="--repology-min-interval",
              env_key="GLEAM_REPOLOGY_MIN_INTERVAL",
              help="min interval seconds (API terms require <=1 rps)"),
    ParamSpec("repology_timeout", "float", flag="--repology-timeout",
              env_key="GLEAM_REPOLOGY_TIMEOUT",
              help="request timeout seconds"),
    _JSON_PARAM,
    _VERBOSE_PARAM,
    _ENV_FILE_PARAM,
)

# investigate flag surface (inherited udd query fields plus the budget
# flags). Policy keys without a CLI flag are env/function-arg only and
# appear here without ``flag`` so they still bind onto the investigate
# Request.
_INVESTIGATE_PARAMS: tuple[ParamSpec, ...] = _UDD_QUERY_PARAMS + (
    ParamSpec("store", "str", flag="--store",
              env_key="GLEAM_INVESTIGATE_STORE",
              help="evidence store root"),
    ParamSpec("model", "str", flag="--model", env_key="GLEAM_MODEL",
              help="model name; required at investigate start-up"),
    ParamSpec("model_base", "url", flag="--model-base",
              env_key="GLEAM_MODEL_BASE",
              help="model endpoint base URL; required at start-up"),
    ParamSpec("model_api_key", "str", flag="--model-api-key",
              env_key="GLEAM_MODEL_API_KEY",
              help="bearer token for the model endpoint; "
                   "unset = no authentication"),
    _SEARCH_BASE_PARAM,
    ParamSpec("github_api_base", "url", flag="--github-api-base",
              env_key="GLEAM_GITHUB_API_BASE",
              help="GitHub API base URL (operator only)"),
    ParamSpec("model_max_tokens", "int", flag="--model-max-tokens",
              env_key="GLEAM_MODEL_MAX_TOKENS",
              help="per-round output token budget (1..65536)"),
    ParamSpec("model_timeout", "float", flag="--model-timeout",
              env_key="GLEAM_MODEL_TIMEOUT",
              help="per-round model timeout seconds (0..600]"),
    ParamSpec("temperature", "float", env_key="GLEAM_MODEL_TEMPERATURE",
              help="sampling temperature (0..2); env/function-arg only"),
    ParamSpec("model_response_max_bytes", "int",
              env_key="GLEAM_MODEL_RESPONSE_MAX_BYTES",
              help="model response read cap in bytes"),
    ParamSpec("model_proxy", "url", env_key="GLEAM_MODEL_PROXY",
              help="explicit proxy URL for the model client; unset = direct"),
    ParamSpec("max_rounds", "int", flag="--max-rounds",
              env_key="GLEAM_MAX_ROUNDS",
              help="model round budget (1..64)"),
    ParamSpec("max_tool_calls", "int", flag="--max-tool-calls",
              env_key="GLEAM_MAX_TOOL_CALLS",
              help="tool call budget (1..512)"),
    ParamSpec("max_body_chars", "int", flag="--max-body-chars",
              env_key="GLEAM_MAX_BODY_CHARS",
              help="per-tool-result body chars (200..200000)"),
    ParamSpec("max_links", "int", flag="--max-links",
              env_key="GLEAM_MAX_LINKS",
              help="extracted links cap (1..200)"),
    ParamSpec("max_context_chars", "int", flag="--max-context-chars",
              env_key="GLEAM_MAX_CONTEXT_CHARS",
              help="conversation serialization cap (4000..2000000)"),
    ParamSpec("max_http_requests", "int", flag="--max-http-requests",
              env_key="GLEAM_MAX_HTTP_REQUESTS",
              help="shared HTTP request budget"),
    ParamSpec("fetch_max_bytes", "int", flag="--fetch-max-bytes",
              env_key="GLEAM_MAX_BYTES",
              help="per-fetch byte cap"),
    ParamSpec("fetch_timeout", "float", flag="--fetch-timeout",
              env_key="GLEAM_TIMEOUT",
              help="per-fetch timeout seconds"),
    ParamSpec("fetch_max_redirects", "int", flag="--fetch-max-redirects",
              env_key="GLEAM_MAX_REDIRECTS",
              help="redirect hop cap"),
    ParamSpec("fetch_min_interval", "float", flag="--fetch-min-interval",
              env_key="GLEAM_MIN_INTERVAL",
              help="same-host minimum interval seconds"),
    ParamSpec("robots_exempt_file", "str", flag="--robots-exempt-file",
              env_key="GLEAM_ROBOTS_EXEMPT_FILE",
              help="operator-owned robots.txt exemption hostname list; "
                   "resolved relative to the CWD, absent file = no "
                   "exemptions"),
    ParamSpec("search_timeout", "float", flag="--search-timeout",
              default=10.0, minimum=0.0, exclusive_minimum=True, maximum=60.0,
              help="SearXNG query timeout seconds; CLI/function-arg only"),
    ParamSpec("search_response_max_bytes", "int",
              env_key="GLEAM_SEARCH_RESPONSE_MAX_BYTES",
              help="search response cap in bytes"),
    ParamSpec("github_readme_text_cap", "int",
              env_key="GLEAM_GITHUB_README_TEXT_CAP",
              help="github readme tool text cap"),
    ParamSpec("github_per_page", "int", env_key="GLEAM_GITHUB_PER_PAGE",
              help="github per_page (1..100 hard bound)"),
    ParamSpec("repology_base", "url", flag="--repology-base",
              env_key="GLEAM_REPOLOGY_BASE",
              help="Repology API base URL (operator only)"),
    ParamSpec("repology_max_requests", "int",
              flag="--repology-max-requests",
              env_key="GLEAM_REPOLOGY_MAX_REQUESTS",
              help="repology request cap"),
    ParamSpec("repology_min_interval", "float",
              flag="--repology-min-interval",
              env_key="GLEAM_REPOLOGY_MIN_INTERVAL",
              help="repology min interval seconds (API terms <=1 rps)"),
    ParamSpec("repology_timeout", "float", flag="--repology-timeout",
              env_key="GLEAM_REPOLOGY_TIMEOUT",
              help="repology request timeout seconds"),
    ParamSpec("report_cell_max_chars", "int",
              env_key="GLEAM_REPORT_CELL_MAX_CHARS",
              help="report table cell cap"),
    ParamSpec("report_max_errors", "int", env_key="GLEAM_REPORT_MAX_ERRORS",
              help="report listed errors cap"),
    ParamSpec("report_max_repair_reasons", "int",
              env_key="GLEAM_REPORT_MAX_REPAIR_REASONS",
              help="report rejection reasons cap"),
    ParamSpec("context_keep_chars", "int",
              env_key="GLEAM_CONTEXT_KEEP_CHARS",
              help="context envelope kept chars"),
    ParamSpec("context_overhead_chars", "int",
              env_key="GLEAM_CONTEXT_OVERHEAD_CHARS",
              help="context envelope overhead estimate"),
    ParamSpec("context_max_iterations", "int",
              env_key="GLEAM_CONTEXT_MAX_ITERATIONS",
              help="context envelope iteration cap"),
    ParamSpec("mailto_max", "int", env_key="GLEAM_MAILTO_MAX",
              help="origin signal: mailto cap"),
    ParamSpec("copyright_max", "int", env_key="GLEAM_COPYRIGHT_MAX",
              help="origin signal: copyright lines cap"),
    ParamSpec("footer_max", "int", env_key="GLEAM_FOOTER_MAX",
              help="origin signal: footer lines cap"),
    ParamSpec("max_url_chars", "int", env_key="GLEAM_MAX_URL_CHARS",
              help="origin signal: URL char cap"),
    ParamSpec("max_search_query_chars", "int",
              env_key="GLEAM_MAX_SEARCH_QUERY_CHARS",
              help="search query char cap"),
    ParamSpec("min_quote_chars", "int", env_key="GLEAM_MIN_QUOTE_CHARS",
              help="final report minimum verbatim quote"),
    ParamSpec("udd_max_calls", "int", flag="--udd-max-calls", default=6,
              minimum=1, maximum=50,
              help="fetch_udd call budget; CLI/function-arg only"),
    ParamSpec("udd_max_rows", "int", flag="--udd-max-rows", default=200,
              minimum=1, maximum=2000,
              help="fetch_udd exposed-rows budget; CLI/function-arg only"),
    ParamSpec("max_report_repairs", "int", flag="--max-report-repairs",
              default=2, minimum=0,
              help="bounded final-report repair attempts"),
    ParamSpec("no_search_web", "bool", flag="--no-search-web",
              default=False, boolean=True,
              help="disable the search_web tool"),
    ParamSpec("no_github", "bool", flag="--no-github", default=False,
              boolean=True, help="disable the github tool"),
    ParamSpec("no_bts", "bool", flag="--no-bts", default=False,
              boolean=True, help="disable the fetch_bug tool"),
    ParamSpec("no_udd", "bool", flag="--no-udd", default=False,
              boolean=True, help="disable fetch_udd fetches (list stays available)"),
    ParamSpec("no_repology", "bool", flag="--no-repology", default=False,
              boolean=True, help="disable the repology tool"),
    _JSON_PARAM,
    _VERBOSE_PARAM,
    _ENV_FILE_PARAM,
)

# searxng flag surface: exactly one of the three action switches is
# required (the mode check stays with the entry); every deployment
# field is CLI/function-arg only (no searxng env key exists).
_SEARXNG_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("start", "bool", flag="--start", default=False,
              boolean=True,
              help="guarded bring-up; exactly one of "
                   "--start/--stop/--validate"),
    ParamSpec("stop", "bool", flag="--stop", default=False,
              boolean=True,
              help="stop and remove only the gleam-named container; "
                   "exactly one of --start/--stop/--validate"),
    ParamSpec("validate", "bool", flag="--validate", default=False,
              boolean=True,
              help="side-effect-free end-to-end probe; exactly one of "
                   "--start/--stop/--validate"),
    ParamSpec("image", "str", flag="--image", default="searxng/searxng:latest",
              help="SearXNG docker image reference"),
    ParamSpec("name", "str", flag="--name", default="gleam-searxng",
              help="container name (only this name is ever touched)"),
    ParamSpec("port", "int", flag="--port", default=8888,
              minimum=1, maximum=65535,
              help="loopback port to serve on (1..65535)"),
    ParamSpec("bind", "str", flag="--bind", default="127.0.0.1",
              help="address to bind the service to"),
    ParamSpec("pull", "bool", flag="--pull", default=False,
              boolean=True,
              help="force an image update before bring-up"),
    _SEARCH_BASE_PARAM,
    _JSON_PARAM,
    _VERBOSE_PARAM,
    _ENV_FILE_PARAM,
)

# help/version pure subcommands.
_HELP_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("target_module", "choice", positional=True,
              choices=MODULES,
              help="module whose self-contained help to show; default: overview"),
)

_VERSION_PARAMS: tuple[ParamSpec, ...] = ()

#: Flag/Config surfaces of the eight entry modules.
MODULE_SPECS: Mapping[str, tuple[ParamSpec, ...]] = {
    "udd": _UDD_PARAMS,
    "bts": _BTS_PARAMS,
    "repology": _REPOLOGY_PARAMS,
    "check": _CHECK_PARAMS,
    "investigate": _INVESTIGATE_PARAMS,
    "searxng": _SEARXNG_PARAMS,
    "help": _HELP_PARAMS,
    "version": _VERSION_PARAMS,
}


def _check_table_consistency() -> None:
    """Fail at import on any drift inside the declarative data.

    Checks: unique keys; every param env key exists in the closed table
    and agrees with it in kind/default/range/choices; env-keyed params
    never are action switches; per-module field names unique.
    """
    if len(ENV_KEYS) != len(_KEY_TABLE):
        raise AssertionError("duplicate key in closed table")
    for module, params in MODULE_SPECS.items():
        seen: set[str] = set()
        for param in params:
            if param.name in seen:
                raise AssertionError(
                    f"{module}: duplicate field name {param.name!r}"
                )
            seen.add(param.name)
            key_spec = param.env_key and ENV_KEYS.get(param.env_key)
            if param.env_key is not None:
                if key_spec is None:
                    raise AssertionError(
                        f"{module}.{param.name}: env key "
                        f"{param.env_key!r} is outside the closed table"
                    )
                if param.default is not None:
                    raise AssertionError(
                        f"{module}.{param.name}: default must come from the "
                        f"closed table, not from the param"
                    )
                if param.kind != key_spec.kind:
                    raise AssertionError(
                        f"{module}.{param.name}: kind {param.kind!r} "
                        f"disagrees with {param.env_key}"
                    )
                if param.boolean or param.repeatable or param.positional:
                    raise AssertionError(
                        f"{module}.{param.name}: env-keyed param must be a "
                        f"plain value field"
                    )
            if param.kind == "choice" and not (
                param.choices or (key_spec and key_spec.choices)
            ):
                raise AssertionError(f"{module}.{param.name}: empty choices")


_check_table_consistency()
