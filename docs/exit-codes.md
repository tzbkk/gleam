# Exit codes

| Code | Meaning |
|---|---|
| 0 | Success: the Result status is `ok`. |
| 1 | The run completed with a Result whose status is `partial`, `failed`, or `storage-error`. Errors and the evidence gathered so far are preserved in storage. |
| 2 | Usage or configuration error: missing, invalid, or out-of-range arguments, unknown flags, `.env` syntax errors or unknown keys. Nothing is written and no Result exists. |

All business outcomes go through one mapping: `ok` exits 0, every other status exits 1. A storage failure during `investigate` surfaces as the `storage-error` status (exit 1), and a half-written run directory is deliberately preserved so `check` can report it.
