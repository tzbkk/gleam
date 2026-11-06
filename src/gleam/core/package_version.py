"""The single package-version source for every module and User-Agent.

Reads the installed package metadata first (it is built from the same
``pyproject.toml``) and falls back to reading the repository's
``pyproject.toml`` directly for source trees without installed
metadata. Both paths surface the identical value; no secondary version
constant exists anywhere in gleam, and no User-Agent may hardcode a
version number.
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

__all__ = ["package_version"]

_PACKAGE_NAME = "gleam"

#: Repository pyproject.toml, resolved from this file's package tree.
_PYPROJECT_PATH = Path(__file__).resolve().parents[3] / "pyproject.toml"


def package_version() -> str:
    """The gleam version: installed metadata first, pyproject fallback."""
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        with _PYPROJECT_PATH.open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]
