# Release process

Releases are cut by pushing a `v*` tag. The `publish` workflow
(`.github/workflows/publish.yml`) uploads to PyPI **if and only if** both
hold: a new `v*` tag was pushed, and the `ci` workflow succeeded on that
exact commit (a `ci-gate` job waits for the ci run of the tagged commit and
fails closed). PyPI uploads use trusted publishing (OIDC); no API token is
stored anywhere.

## One-time PyPI setup

Configure GitHub trusted publishing on pypi.org (Account settings →
Publishing → "Add a pending publisher"):

- PyPI project name: `debian-gleam`
- Owner: `tzbkk`
- Repository: `gleam`
- Workflow filename: `publish.yml`
- Environment name: `pypi`

The first successful publish creates the project. The `publish` job runs in
the GitHub environment `pypi`; add protection rules there if you want
manual approval before uploads.

**Naming**: the PyPI distribution name is `debian-gleam` because plain
`gleam` on PyPI is an unrelated 2014 package (interactive visualization,
last upload 0.1.2). The import package and the `gleam` console script keep
their names; only the distribution name differs.

## Cutting a release

1. Set the new version in `pyproject.toml` (`version = "X.Y.Z"`) — it is
   the single source of truth; `gleam version` and the package metadata
   follow it.
2. Commit the bump (suite green), then tag and push:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```
3. The `publish` workflow starts, waits for the ci run of that commit,
   then verifies `tag == pyproject version == gleam version`, builds and
   checks the sdist/wheel (twine, package data, LICENSE), and uploads.

A failed ci, a version mismatch, or no tag means nothing is published.

## Notes

- `ci.yml` triggers on every push, including tags. That is load-bearing:
  the gate needs a ci run for the tagged commit. Do not filter ci to
  branches only.
- Distributions are built once (in the `build` job) and published from the
  uploaded artifact, so what is checked is exactly what is uploaded.
