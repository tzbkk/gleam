"""Gleam: Debian QA evidence-investigation infrastructure.

Seven entry modules (udd, bts, repology, check, investigate, help,
version) over a shared core (config, transport, storage, sources,
model). Every business outcome is a unified Result; storage is
append-only and read paths never mutate the store. The package version
lives in pyproject.toml (see gleam.entries.version); this module
carries no state.
"""
