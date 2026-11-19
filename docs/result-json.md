# Result JSON

Every module prints human-readable output by default; `--json` switches stdout to the machine-readable Result envelope (`schema_version` is fixed at 1). Real output of `python -m gleam udd --list-datasets --json`:

```json
{
  "schema_version": 1,
  "status": "ok",
  "kind": "list-datasets",
  "data": {
    "action": "list-datasets",
    "package": null,
    "release": null,
    "datasets": ["sources", "upstream", "upstream_metadata", "vcswatch", "urls", "binaries", "bugs", "bugs_count", "archived_bugs", "wnpp", "upload_history", "upload_history_closes", "removals", "testing_autoremovals", "popcon_src", "ci", "reproducible", "lintian"],
    "rows": [],
    "row_count": 0,
    "truncated": false,
    "more_available": false,
    "provenance": []
  },
  "errors": [],
  "diagnostics": [],
  "warnings": []
}
```

`status` is one of `ok`, `partial`, `failed`, `storage-error`. `kind` is a stable, module-owned label (`list-datasets`, `query-result`, `bug-result`, `store-valid`, `store-incomplete`, `store-error`, `completed`, `partial`, `no-final-report`, `invalid-report`, `model-error`, `storage-error`, `searxng-started`, `searxng-stopped`, `searxng-validated`). `data` is module-specific. `errors`, `diagnostics`, and `warnings` are arrays of structured items with the fixed keys `code`, `message`, `where`, `details`. An `ok` Result carrying warnings or diagnostics is downgraded to `partial` before it is reported.

Passing `--verbose` narrates progress to stderr: lines carry a `[module HH:MM:SS]` UTC timestamp, covering the run's progress (queries, fetches, docker commands, model rounds) and safety verdicts (robots decisions, DNS pinning, budget lines), and a final `! {json}` line dumps the Result's `errors`, `diagnostics`, and `warnings` on the same stream. `--verbose` and `--json` are mutually exclusive (combining them is a usage error, exit 2); it has no `.env` key, and `help` and `version` reject it as an unknown flag. Without it nothing changes: stderr stays silent and stdout is exactly the output described here.
