# Investigation stack (SearXNG)

`search_web` talks to a self-hosted SearXNG bound to loopback. Without it, `search_web` fails explicitly (a connection error, not an "empty result"). Without trafilatura, `fetch_url` falls back to stdlib extraction and labels the result accordingly. Both degradations are honest and visible in the output.

The `searxng` module manages that deployment; the full flags and semantics are documented in the README's [`searxng`](../README.md#searxng) section:

```bash
python -m gleam searxng --validate    # side-effect-free probe: PASS exits 0, FAIL/BLOCKER exit 1
python -m gleam searxng --start       # guarded bring-up; reuses an already-valid deployment
python -m gleam searxng --stop        # stops and removes only the gleam-searxng container
```

The only requirement is a working Docker CLI with daemon access, needed only when `search_web` is used. Settings ship as package data (`src/gleam/data/searxng-settings.yml`, mounted read-only at bring-up) and the module is fully stateless: nothing is ever written to disk.