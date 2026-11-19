# Evidence store

Each `investigate` run gets its own directory, created only after all budget and configuration checks pass:

```text
<store>/<UTC-stamp>-<package>-<uuid4>/
  run.json                     # close-out summary: run id, package, status, per-round entries, observation ids
  rounds/NNNN.json             # one self-contained document per model round: request, response, and tool calls with embedded results
  observations/<sha256>.json   # one file per observation; the file name IS the observation id
  fetch_url/<sha256>.json      # normalized per-source documents, one directory per source
  search_web/<sha256>.json
  github/<sha256>.json
  fetch_bug/<sha256>.json
  fetch_udd/<sha256>.json
  repology/<sha256>.json
  robots/<sha256>.json
```

Every evidence file name is a content hash except the rounds series, which is numbered NNNN in run order. A per-source document's file name is the sha256 of the exact bytes stored, so identical content lands a single file per run. An observation's file name is its id (the bare sha256 of the record content), so identical observations deduplicate the same way, and a round is identified by its `round` number plus its file path rather than by a synthetic call id. A per-source document normalizes one tool call's evidence: requested and final URL, status, one entry per redirect hop with its structured headers, a `content_sha256` over the payload bytes, and the body as UTF-8 text or base64. `fetch_udd` documents instead carry the queried datasets' rows (`extra.datasets`); their observations hold only per-dataset row counts. A robots document stores the exact robots.txt body behind a fetch decision; its `content_sha256` hashes that body (the file name hashes the whole document instead), and an observation citing the decision references the document and the body hash, null when no body existed. HTTP headers live inside the documents; there is no separate headers folder.

Storage is append-only: a closed run is never rewritten, and a write failure preserves the half-written directory (recorded as `storage-error`) so the evidence trail stays auditable. The `udd`, `bts`, `repology`, `check`, `searxng`, `help`, and `version` modules never write to disk. `python -m gleam check --store runs` scans the whole store read-only and reports run counts plus per-run diagnostics.
