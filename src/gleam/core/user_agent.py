"""The one User-Agent sent by every outbound HTTP client."""

from __future__ import annotations

from .package_version import package_version

USER_AGENT = (
    f"gleam/{package_version()} (evidence collector for Debian packages; "
    "https://github.com/tzbkk/gleam; mailto:tzbkk@outlook.com)"
)
