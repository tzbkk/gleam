# Configuration (.env)

Every module reads an optional `.env` file. Configuration resolution is explicit args, then the `.env` file, then built-in defaults: a CLI flag always wins over an `.env` value, which always wins over the default.

- Pass `--env-file PATH` to read an explicit file; without it, gleam reads `./.env` in the current directory when present.
- The process environment is never read. Configuration comes from CLI flags and the `.env` file only.
- Only `GLEAM_*` keys are recognized, and the table is closed: 61 keys in total, shared keys such as `GLEAM_UDD_DSN`, `GLEAM_MODEL`, `GLEAM_MODEL_BASE`, `GLEAM_MODEL_API_KEY`, `GLEAM_SEARCH_BASE`, and `GLEAM_GITHUB_API_BASE`, plus per-module policy keys. An unknown `GLEAM_*` key is rejected (exit 2); non-GLEAM lines are ignored; `KEY=` with an empty value behaves as unset.
- Action switches (`--json`, `--list-datasets`, `--no-*`) have no env keys by design.

The closed table in `gleam/core/config/spec.py` is the source of truth for every key, default, and range; each module's help text describes its flags and the env keys behind them. A minimal example for `investigate`:

```text
GLEAM_MODEL_BASE=http://127.0.0.1:8000/v1
GLEAM_MODEL=my-model
GLEAM_UDD_DSN=postgresql://udd-mirror.debian.net/udd
# Optional: bearer token for authenticated endpoints (unset = unauthenticated)
# GLEAM_MODEL_API_KEY=sk-...
```
