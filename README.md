# Gleam

[![PyPI](https://img.shields.io/pypi/v/debian-gleam.svg)](https://pypi.org/project/debian-gleam/)

Gleam is Debian QA evidence-investigation infrastructure. It collects verifiable facts about Debian packages, keeps every observation in an append-only evidence store, and runs bounded, tool-calling investigations driven by a local model on top of that store. Upstream archaeology, tracing where an orphaned Debian package's development went, is the first built-in workflow. The report it produces is a proposal for human review: nothing is confirmed automatically and no action is ever executed.

## Quick start

The CLI has eight modules: `udd`, `bts`, `repology`, `check`, `investigate`, `searxng`, `help`, and `version`. Install the Python dependencies (trafilatura for HTML body text, markdown-it-py for README markdown, verbatimeter for quote verification, pg8000 for the UDD database connection; everything else is the standard library), then try the offline commands:

```bash
cd /path/to/gleam
uv venv .venv && uv pip install -r requirements.txt    # or: uv sync

python -m gleam help                    # one-page overview of all eight modules
python -m gleam help investigate        # one module's self-contained help
python -m gleam version                 # print the gleam version
python -m gleam udd --list-datasets     # the 18-dataset catalog, no database contact
python -m gleam check --store runs      # validate an evidence store (read-only)
```

Examples that need a live backend (documented here, but they will not run offline):

```bash
# Requires network (queries the public UDD mirror)
python -m gleam udd --package wizznic --dataset sources --json
# Requires network (fetches a bug's discussion from bugs.debian.org)
python -m gleam bts --bug-id 1000000 --json
# Requires network (queries the Repology cross-distro version tracker)
python -m gleam repology --project ncdu --json
# Requires a local model endpoint and network (see docs/configuration.md)
python -m gleam investigate --package wizznic --store runs \
    --model my-model --model-base http://127.0.0.1:8000/v1
```

`investigate` rejects startup without a model name and endpoint (exit 2) before it creates any run directory.

## The eight modules

### udd

Query live package facts from the public read-only UDD mirror (`postgresql://udd-mirror.debian.net/udd`) through the built-in pg8000 driver, a pure-Python PostgreSQL wire-protocol client with no native library and no external database tools, running a fixed read-only template set per dataset. Runs are read-only and write nothing to disk.

Flags: `--package NAME`, `--dataset NAME` (repeatable), `--release NAME`, `--limit N` (1..50 per dataset), `--dsn URL`, `--timeout SECONDS` (query deadline: socket deadline plus a per-statement budget), `--list-datasets`, `--json`, `--verbose`, `--env-file PATH`.

Two modes. `--list-datasets` prints the 18-dataset catalog without touching the database. Query mode requires `--package` and at least one `--dataset`; the UDD data appears only then. An honest empty result (zero rows) is a success, truncation is reported as partial, and failures are reported as failures, never masked.

### bts

Fetch one Debian BTS bug's complete public discussion. Flags: `--bug-id ID` (1..9999999, no leading zeros), `--source auto|soap|mbox` (default `auto`), `--json`, `--verbose`, `--env-file PATH`. The default path uses the documented SOAP machine interface first and falls back to the official mbox view only on transport-class errors and only when robots.txt explicitly allows it. Rate limits, unknown bugs, and budget exhaustion never fall back, and there is no bypass switch.

### repology

Query the Repology cross-distro version tracker (default base `https://repology.org`) through the public fetcher safety layer (SSRF guards, DNS pinning, byte caps, timeouts, request budget). Flags: `--project SLUG` (version ladder for one project) or `--search QUERY` (resolve a name to project slugs; exactly one of the two is required, otherwise exit 2), `--json`, `--verbose`, `--env-file PATH`, plus the policy flags `--repology-base`, `--repology-max-requests` (default 6), `--repology-min-interval` (default 1.1s; the API terms require <=1 req/s), `--repology-timeout` (default 15s). HTTP 200 with an empty entry list is the honest "no such project" answer; failures are failures (unknown), never "no results". The client carries a tool-intrinsic robots.txt exemption for the host of its configured base: Repology's own API terms authorize programmatic access on that surface, so the tool is the specific permission; manual `fetch_url` visits of the same host stay governed by the operator exemption file. Read-only: never writes to disk.

### check

Validate an evidence store for structural integrity. Flags: `--store DIR` (default `runs`), `--run DIR`, `--json`, `--verbose`, `--env-file PATH`. `--store` takes the store root, the directory whose children are run directories: a single run's own directory, or a directory that holds no runs, is rejected as a usage error (exit 2) instead of being misread as a store full of broken runs. `--run DIR` validates exactly one run directory instead (it must carry `run.json` or the run's evidence directories; a configured non-default `--store` cannot be combined with it). The scan is strictly read-only: nothing under the store is created, modified, or removed, and there is no repair function. An empty or missing store is a valid store with zero counts.

### investigate

Run a bounded, tool-calling investigation of one package with a local model over an OpenAI-compatible endpoint. The model sees the investigation tools (`fetch_url`, `search_web`, `github`, `fetch_bug`, `fetch_udd`, `read_evidence`, `repology`, `inspect_report_state`, `validate_report_draft`, and `finish_investigation`), results come back by `tool_call_id`, and every tool executes serially through the same safety layer. There is zero prefetch: no UDD query runs before the loop starts, and the package name is only policy-validated. The final report is anti-forgery checked against the evidence actually exposed to the model: candidate URLs must come from cited observations, quotes must be verbatim substrings of returned text, GitHub repositories observed in search results or fetched-page links must be covered by a `github` call or a repository-page fetch before the report is accepted. The model can inspect that ledger and repeatedly validate revised drafts before final submission.

Flags, grouped:

- Inherited query fields (same meaning as udd): `--package`, `--dataset`, `--release`, `--limit`, `--dsn`, `--timeout`.
- Endpoints: `--model NAME` (required), `--model-base URL` (required), `--model-api-key KEY` (optional bearer token; unset = unauthenticated), `--search-base URL` (default `http://127.0.0.1:8888`), `--github-api-base URL` (default `https://api.github.com`), `--repology-base URL` (default `https://repology.org`), `--store DIR` (default `runs`).
- Model policy: `--model-max-tokens` (default 8192, cap 65536), `--model-timeout` (default 120s, cap 600s), `--search-timeout` (default 10s, cap 60s).
- Budgets: `--max-rounds` (default 48, cap 64), `--max-tool-calls` (default 48, cap 512; `finish_investigation` is never counted), `--max-http-requests` (default 52, one shared budget for fetch, robots, search reservations, BTS requests, GitHub client usage, and Repology requests), `--max-body-chars` (default 40000), `--max-links` (default 120), `--max-context-chars` (default 320000; overflow halves the longest tool results inside a legal JSON envelope with double disclosure), `--udd-max-calls` (default 6, cap 50), `--udd-max-rows` (default 200, cap 2000), `--repology-max-requests` (default 6, per-run cap), `--repology-min-interval` (default 1.1s; API terms require <=1 req/s), `--repology-timeout` (default 15s), `--max-report-repairs` (default 2).
- Fetch policy: `--fetch-max-bytes` (default 300000), `--fetch-timeout` (default 15s), `--fetch-max-redirects` (default 3), `--fetch-min-interval` (default 1s per host), `--robots-exempt-file` (default `robots-exempt.txt` relative to the working directory; the operator-owned robots.txt exemption hostname list, `GLEAM_ROBOTS_EXEMPT_FILE`).
- Tool toggles: `--no-search-web`, `--no-github`, `--no-bts`, `--no-udd` (fetching stays disabled, the dataset catalog stays available), `--no-repology`.
- Output: `--json`, `--verbose`, `--env-file PATH`.

### searxng

Manage the self-hosted SearXNG instance behind the `search_web` tool: one guarded, loopback-bound Docker deployment, addressed by exactly one action per invocation (`--start`, `--stop`, or `--validate`; any other combination is a usage error, exit 2). The only requirement is a working Docker CLI with daemon access, and only when `search_web` is used.

Flags: `--image REF` (default `searxng/searxng:latest`), `--name NAME` (default `gleam-searxng`), `--port N` (default 8888, 1..65535), `--bind ADDRESS` (default `127.0.0.1`), `--pull` (force an image update before bring-up), `--search-base URL` (default `http://127.0.0.1:8888`; the same trusted base investigate uses, `GLEAM_SEARCH_BASE`), `--json`, `--verbose`, `--env-file PATH`. The module adds no `.env` key of its own and never writes a file anywhere.

| Action | Semantics |
|---|---|
| `--validate` | Side-effect-free end-to-end probe: docker availability, the gleam-named container, its healthz endpoint, and one real search query through the same client investigate uses. PASS exits 0; FAIL and BLOCKER exit 1. It never starts anything. |
| `--start` | Pre-validates first: an already-valid deployment is reused as-is (exit 0, no bring-up at all). Otherwise it brings the configured image up, pulling only when the image is missing (`--pull` forces an update) and refusing any image or same-named container that does not verify as SearXNG, then must pass the same probe before it reports success. |
| `--stop` | Stops and removes only the gleam-named container: an absent container is a success no-op, an unreachable docker daemon is a failure (never a silent no-op), and a present container is stopped gracefully then removed, never force-removed. Unrelated containers are untouched. |

Deployment shape: the official `docker.io/searxng/searxng` image, bound to `127.0.0.1:8888` by default, no host networking, no privileged mode, no docker socket mounted. The shipped `searxng-settings.yml` (package data under `src/gleam/data/`, mounted read-only at bring-up) enables the JSON format and disables the limiter for local-only use; its loopback-only `secret_key` is a deliberate public constant, replaced only if you ever expose the service beyond loopback.

### help

Print the overview (`gleam help`) or one module's self-contained documentation (`gleam help MODULE`). The module name is a positional argument, not a flag. There are no `-h`/`--help`/`--version` builtins anywhere in gleam: unknown flags, unknown targets, and stray arguments are usage errors (exit 2).

### version

Print one version line. The single source of truth is `pyproject.toml`; the installed package metadata and the source-tree file always agree. Any argument at all is a usage error (exit 2).

## Documentation

- [Exit codes](docs/exit-codes.md)
- [Result JSON](docs/result-json.md)
- [Configuration (.env)](docs/configuration.md)
- [Security boundaries](docs/security-boundaries.md)
- [Evidence store](docs/evidence-store.md)
- [Investigation stack (SearXNG)](docs/investigation-stack-searxng.md)
- [Release process](docs/release.md)
