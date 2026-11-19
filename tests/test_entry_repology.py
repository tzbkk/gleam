"""Offline tests for the repology entry shell.

Zero network: the adapter class is patched at the entry module
boundary (``gleam.entries.repology.RepologyClient``), the same
technique as the udd/bts entry tests. Usage-error paths run the real
binder (validation raises ``UsageError`` before any network call).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import chdir
from unittest.mock import patch

from gleam.core.result import DiagnosticItem, UsageError
from gleam.core.sources.repology import RepologyPolicy, RepologyResult
from gleam.entries import repology as repology_entry
from gleam.entries.repology import KIND_REPOLOGY_RESULT, main
from tests._helpers import capture_run

REPOLOGY_WHERE = "gleam.core.sources.repology"


def fake_repology_result(status="ok", action="project", slug="ncdu",
                         digest=None, errors=None, **overrides):
    """A fully-fixed adapter result (byte-stable for the goldens)."""

    if digest is None:
        if action == "search":
            digest = {
                "action": "search", "query": "ncdu", "slug": "ncdu",
                "exact_match": True,
                "projects": [{"slug": "ncdu", "visiblename": "ncdu",
                              "newest_version": "1.21",
                              "repo_count": 57}],
                "more": 0, "total_entries": 57,
                "note": "search note",
            }
        else:
            digest = {
                "action": "project", "slug": slug, "total_entries": 2,
                "versions": [{"version": "1.21", "repo_count": 1,
                              "repos": ["aur"], "statuses": ["newest"],
                              "vulnerable_repos": 0}],
                "debian": [{"repo": "debian_sid", "version": "1.19"}],
                "note": "project note",
            }
    return RepologyResult(
        status=status, action=action,
        query="ncdu" if action == "search" else None,
        slug=slug if action == "project" else None,
        api_url="https://repology.org/api/v1/"
                + ("projects/?search=ncdu" if action == "search"
                   else f"project/{slug}"),
        final_url="https://repology.org/api/v1/"
                  + ("projects/?search=ncdu" if action == "search"
                     else f"project/{slug}"),
        http_status=200, digest=digest,
        content_sha256="0" * 64, content_bytes=42, body=b"[]",
        note="adapter note",
        errors=list(errors or []),
        fetched_at="2026-01-01T00:00:00Z", **overrides)


def run_main(argv):
    return capture_run(main, argv)


class RepologyEntryOkPaths(unittest.TestCase):
    def test_project_ok_json(self):
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = \
                fake_repology_result()
            code, out, err = run_main(
                ["--project", "ncdu", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], KIND_REPOLOGY_RESULT)
        self.assertEqual(payload["data"]["slug"], "ncdu")
        self.assertEqual(payload["data"]["total_entries"], 2)
        self.assertEqual(payload["data"]["versions"][0]["version"],
                         "1.21")
        self.assertEqual(payload["errors"], [])
        # the entry called query("project", slug=...) with the bound base
        client_cls.return_value.query.assert_called_once_with(
            "project", slug="ncdu")
        self.assertEqual(
            client_cls.call_args.kwargs["base"],
            "https://repology.org")

    def test_search_ok_json(self):
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = \
                fake_repology_result(action="search")
            code, out, err = run_main(
                ["--search", "ncdu", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["data"]["exact_match"])
        self.assertEqual(payload["data"]["projects"][0]["slug"], "ncdu")
        client_cls.return_value.query.assert_called_once_with(
            "search", query="ncdu")

    def test_human_rendering_search(self):
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = \
                fake_repology_result(action="search")
            code, out, err = run_main(["--search", "ncdu"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ncdu", out)  # the query text
        self.assertIn("1.21", out)  # the matched project's newest version
        self.assertIn("exact match", out)  # the matched-project marker
        self.assertIn("57 repos", out)  # the project's repository count

    def test_human_rendering_project(self):
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = \
                fake_repology_result()
            code, out, err = run_main(["--project", "ncdu"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ncdu", out)  # the project slug
        self.assertIn("newest 1.21", out)  # the newest version
        self.assertIn("sid=1.19", out)  # the debian repository pin
        self.assertIn("1 repo", out)  # the tier's repository count
        self.assertNotIn("aur", out)  # per-repo names stay in --json
        self.assertNotIn("debian_sid", out)  # unprefixed for humans

    def test_policy_values_reach_the_client(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            with open(".env", "w", encoding="utf-8") as handle:
                handle.write(
                    "GLEAM_REPOLOGY_MAX_REQUESTS=3\n"
                    "GLEAM_REPOLOGY_MIN_INTERVAL=0.5\n"
                    "GLEAM_REPOLOGY_TIMEOUT=9\n")
            with patch("gleam.entries.repology.RepologyClient") \
                    as client_cls:
                client_cls.return_value.query.return_value = \
                    fake_repology_result()
                code, out, err = run_main(["--project", "ncdu"])
        self.assertEqual((code, err), (0, ""))
        policy = client_cls.call_args.kwargs["policy"]
        self.assertIsInstance(policy, RepologyPolicy)
        self.assertEqual(policy.max_requests, 3)
        self.assertEqual(policy.min_interval_s, 0.5)
        self.assertEqual(policy.timeout_s, 9.0)


class RepologyEntryFailedPath(unittest.TestCase):
    def test_adapter_failure_is_failed_exit_one(self):
        failed = fake_repology_result(
            status="failed",
            errors=[DiagnosticItem(
                code="fetch-connection-error",
                message="repology.org unreachable in test",
                where=REPOLOGY_WHERE)])
        with patch("gleam.entries.repology.RepologyClient") as client_cls:
            client_cls.return_value.query.return_value = failed
            code, out, err = run_main(
                ["--project", "ncdu", "--json"])
        self.assertEqual((code, err), (1, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["kind"], KIND_REPOLOGY_RESULT)
        self.assertEqual(payload["errors"][0]["code"],
                         "fetch-connection-error")


class RepologyEntryUsageErrors(unittest.TestCase):
    """Exit 2 paths: mode exclusivity, unknown flags, over-length
    values, out-of-range policy -- all BEFORE any network call."""

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(chdir(tmp))

    def test_missing_both_mode_flags_exits_two(self):
        code, out, err = run_main([])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("exactly one of --project/--search", err)

    def test_both_mode_flags_exit_two(self):
        code, out, err = run_main(["--project", "a", "--search", "b"])
        self.assertEqual(code, 2)
        self.assertIn("exactly one of --project/--search", err)

    def test_unknown_flag_exits_two(self):
        code, out, err = run_main(["--project", "ncdu", "--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("--bogus", err)

    def test_stray_positional_exits_two(self):
        code, out, err = run_main(["ncdu", "--project", "ncdu"])
        self.assertEqual(code, 2)

    def test_overlong_slug_exits_two_before_network(self):
        # the REAL client validates the slug before any fetch (the
        # constructor builds a fetcher but never issues a request)
        with patch("gleam.core.sources.repology.transport") as mocked:
            with self.assertRaises(UsageError):
                repology_entry.run(argv=["--project", "x" * 201])
            mocked.Fetcher.return_value.fetch.assert_not_called()

    def test_out_of_range_policy_flag_exits_two(self):
        code, out, err = run_main(
            ["--project", "ncdu", "--repology-max-requests", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--repology-max-requests", err)

    def test_bad_base_exits_two(self):
        code, out, err = run_main(
            ["--project", "ncdu", "--repology-base", "ftp://x"])
        self.assertEqual(code, 2)
        self.assertIn("--repology-base", err)

    def test_help_doc_is_self_contained(self):
        from gleam.entries.help import module_help
        text = module_help("repology")
        # self-contained user help: usage line, full flag inventory,
        # read-only invariant
        self.assertIn("Usage: gleam repology", text)
        for token in ("--project", "--search", "--repology-base",
                      "--repology-max-requests", "--repology-min-interval",
                      "--repology-timeout", "--json", "--env-file"):
            self.assertIn(token, text)
        self.assertIn("never writes to disk", text)


if __name__ == "__main__":
    unittest.main()
