"""The packaged SearXNG settings resource (todo 10 mounts it read-only).

Covers the ``gleam/data/searxng-settings.yml`` package-data contract:
the resource resolves through ``importlib.resources`` from the
installed package (Windows-path-safe), and it is the loopback settings
file -- the JSON API enabled and the limiter off.
"""

import importlib.resources
import unittest

_SETTINGS = importlib.resources.files("gleam") / "data" / "searxng-settings.yml"


class SearxngSettingsResourceTests(unittest.TestCase):
    """``gleam`` ships its SearXNG settings as a package data resource."""

    def test_settings_resource_exists(self):
        self.assertTrue(_SETTINGS.is_file(),
                        f"missing packaged resource: {_SETTINGS}")

    def test_settings_enable_the_json_api(self):
        text = _SETTINGS.read_text(encoding="utf-8")
        self.assertIn("formats:", text)
        self.assertIn("- json", text)

    def test_settings_disable_the_limiter(self):
        text = _SETTINGS.read_text(encoding="utf-8")
        self.assertIn("limiter: false", text)


if __name__ == "__main__":
    unittest.main()
