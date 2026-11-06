"""Offline tests for the typed Repology source adapter.

Zero network: the transport module boundary is patched with
``unittest.mock.patch``, exactly like the github adapter tests;
every fetch-level test feeds real ``FetchOutcome``/``HopResult``
objects so the grading logic runs against the true transport shapes.
The digest/comparator tests are pure-function tests.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from gleam.core.result import UsageError
from gleam.core.transport import FetchOutcome, HopResult
from gleam.core.sources.repology import (
    LADDER_TIER_CAP,
    QUERY_MAX_CHARS,
    SEARCH_PROJECTS_CAP,
    SLUG_MAX_CHARS,
    TIER_REPOS_CAP,
    RepologyArgError,
    RepologyClient,
    RepologyError,
    RepologyPolicy,
    build_project_url,
    build_search_url,
    project_digest,
    search_digest,
    validate_api_base,
    validate_query,
    validate_slug,
    version_sort_key,
)

PATCH_TARGET = "gleam.core.sources.repology.transport"

#: A fixture entry shaped like the real /api/v1/project/ncdu payload
#: (only the statuses/values needed by the digest assertions are
#: varied per test).
NCDU_ENTRIES = [
    {"repo": "debian_13", "srcname": "ncdu", "visiblename": "ncdu",
     "version": "1.16", "origversion": "1.16-1", "status": "outdated"},
    {"repo": "debian_sid", "srcname": "ncdu", "visiblename": "ncdu",
     "version": "1.19", "origversion": "1.19-1", "status": "legacy"},
    {"repo": "aur", "srcname": "ncdu", "visiblename": "ncdu",
     "version": "1.21", "status": "newest"},
    {"repo": "fedora_rawhide", "visiblename": "ncdu",
     "version": "1.10", "status": "outdated"},
    {"repo": "ubuntu_24_04", "visiblename": "ncdu",
     "version": "2.9.2", "status": "outdated", "vulnerable": True},
    {"repo": "gentoo", "visiblename": "ncdu",
     "version": "2.10", "status": "outdated"},
    {"repo": "homebrew", "visiblename": "ncdu",
     "version": "1.22", "status": "legacy"},
    {"repo": "macports", "visiblename": "ncdu",
     "version": "1.9", "status": "legacy"},
    {"repo": "alpine_3_20", "visiblename": "ncdu",
     "version": "1.22", "status": "legacy", "vulnerable": True},
    {"repo": "UNKNOWN-REPO", "visiblename": "ncdu",
     "version": "some-snapshot", "status": "ignored"},
]


def outcome(status: int, body: bytes = b"", ok: bool = True,
            error: dict | None = None) -> FetchOutcome:
    url = "https://repology.org/api/v1/project/ncdu"
    hop = HopResult(url=url, status=status, headers={})
    return FetchOutcome(ok=ok, status=status, final_url=url, body=body,
                        hops=[hop], error=error)


def client_with(testcase, out: FetchOutcome,
                **client_kwargs) -> RepologyClient:
    """Build a client whose internal fetcher returns ``out``; the
    transport patch is cleaned up automatically (github-test style)."""

    patcher = patch(PATCH_TARGET)
    mocked = patcher.start()
    testcase.addCleanup(patcher.stop)
    fetcher = mocked.Fetcher.return_value
    fetcher.fetch.return_value = out
    fetcher.request_count = 0
    client_kwargs.setdefault("policy", RepologyPolicy(
        max_requests=6, min_interval_s=0.0, timeout_s=1.0))
    return RepologyClient(**client_kwargs)


class VersionSortKeyTests(unittest.TestCase):
    """The tolerant dotted-numeric comparator (pure function)."""

    def test_ordering_traps_are_numeric_not_lexicographic(self):
        self.assertLess(version_sort_key("2.9.2"),
                        version_sort_key("2.10"))
        self.assertGreater(version_sort_key("1.22"),
                           version_sort_key("1.9"))
        self.assertLess(version_sort_key("1.9"), version_sort_key("1.10"))
        self.assertGreater(version_sort_key("1.21"), version_sort_key(
            "1.19"))

    def test_non_numeric_segment_sorts_after_numeric_at_position(self):
        self.assertGreater(version_sort_key("2.a"), version_sort_key(
            "2.9"))
        self.assertLess(version_sort_key("2.9"), version_sort_key("2.a"))

    def test_shorter_prefix_sorts_before_extension(self):
        self.assertLess(version_sort_key("1.22"), version_sort_key(
            "1.22a"))
        self.assertLess(version_sort_key("2"), version_sort_key("2.1"))

    def test_unknown_formats_fall_below_everything_numeric(self):
        # the ladder is DESCENDING (newest first): "below everything
        # numeric" = the tail of the ladder, in first-seen order.
        ordered = sorted(["1.0", "UNKNOWN", "2.0", "beta"],
                         key=version_sort_key)
        self.assertEqual(ordered, ["UNKNOWN", "beta", "1.0", "2.0"])
        ladder = sorted(["1.0", "UNKNOWN", "2.0", "beta"],
                        key=version_sort_key, reverse=True)
        self.assertEqual(ladder, ["2.0", "1.0", "UNKNOWN", "beta"])

    def test_unknown_formats_keep_first_seen_order(self):
        versions = ["1.0", "zzz", "2.0", "aaa", "mmm"]
        ladder = sorted(versions, key=version_sort_key, reverse=True)
        # tail = first-seen order zzz, aaa, mmm (not alphabetical)
        self.assertEqual(ladder, ["2.0", "1.0", "zzz", "aaa", "mmm"])

    def test_non_numeric_first_token_joins_the_tail_bucket(self):
        # live ncdu regression: AUR's VCS snapshot v2.8.2.r0.g5b96a48
        # once ranked ABOVE 2.12.1 at the head of the descending
        # ladder because its first token (v) outranked numeric heads.
        snapshot = "v2.8.2.r0.g5b96a48"
        self.assertEqual(version_sort_key(snapshot),
                         version_sort_key("nightly"))
        self.assertLess(version_sort_key(snapshot),
                        version_sort_key("1.0"))
        ladder = sorted(["2.12.1", snapshot, "1.22", "2.9.2"],
                        key=version_sort_key, reverse=True)
        self.assertEqual(ladder, ["2.12.1", "2.9.2", "1.22", snapshot])

class UrlAssemblyTests(unittest.TestCase):
    def test_project_url_percent_encodes_the_slug(self):
        self.assertEqual(
            build_project_url("https://repology.org", "python:flask"),
            "https://repology.org/api/v1/project/python%3Aflask")
        # path structure can never be injected through the slug
        self.assertEqual(
            build_project_url("https://repology.org", "a/b"),
            "https://repology.org/api/v1/project/a%2Fb")

    def test_search_url_percent_encodes_the_query(self):
        self.assertEqual(
            build_search_url("https://repology.org", "nc du"),
            "https://repology.org/api/v1/projects/?search=nc+du")
        self.assertIn("search=ncdu",
                      build_search_url("https://repology.org", "ncdu"))

    def test_base_with_path_prefix_is_preserved(self):
        self.assertEqual(
            build_project_url("https://mirror.example/api/", "ncdu"),
            "https://mirror.example/api/api/v1/project/ncdu")

    def test_bad_base_is_rejected(self):
        for base in ("ftp://repology.org", "https://u:p@repology.org",
                     "https://repology.org:99999", "", "not a url"):
            with self.subTest(base=base):
                with self.assertRaises(RepologyArgError):
                    validate_api_base(base)

    def test_bad_query_and_slug_are_rejected(self):
        with self.assertRaises(UsageError):
            validate_query("")
        with self.assertRaises(UsageError):
            validate_query("x" * (QUERY_MAX_CHARS + 1))
        with self.assertRaises(UsageError):
            validate_query(None)
        with self.assertRaises(UsageError):
            validate_slug("x" * (SLUG_MAX_CHARS + 1))
        with self.assertRaises(UsageError):
            validate_slug(123)

    def test_bad_action_is_rejected_before_any_network(self):
        with patch(PATCH_TARGET) as mocked:
            client = RepologyClient(policy=RepologyPolicy(
                min_interval_s=0.0))
            with self.assertRaises(RepologyArgError):
                client.query("bogus", slug="ncdu")
            self.assertEqual(client.request_count, 0)
            mocked.Fetcher.return_value.fetch.assert_not_called()


class SearchDigestTests(unittest.TestCase):
    def _projects(self, count):
        return {
            f"slug-{i}": [
                {"repo": "aur", "version": f"1.{i}", "status": "outdated",
                 "visiblename": f"slug-{i}"},
                {"repo": "fedora", "version": f"0.{i}", "status": "newest",
                 "visiblename": f"slug-{i}-visible"},
            ]
            for i in range(count)
        }

    def test_exact_slug_present_is_case_insensitive(self):
        digest = search_digest(
            "NCDU", {"r:ncdump": [], "ncdu": [
                {"repo": "aur", "version": "1.21", "status": "newest",
                 "visiblename": "ncdu"}]})
        self.assertTrue(digest["exact_match"])
        self.assertEqual(digest["slug"], "NCDU")
        self.assertEqual(digest["query"], "NCDU")

    def test_exact_slug_absent(self):
        digest = search_digest("git", {"cgit": [], "gitg": []})
        self.assertFalse(digest["exact_match"])

    def test_rows_capped_at_twelve_with_more_count(self):
        digest = search_digest("q", self._projects(15))
        self.assertEqual(len(digest["projects"]), SEARCH_PROJECTS_CAP)
        self.assertEqual(digest["more"], 3)
        self.assertEqual(digest["total_entries"], 30)

    def test_visiblename_from_newest_status_entry_else_first(self):
        projects = self._projects(1)
        digest = search_digest("slug-0", projects)
        # the newest-status entry is the second one (visible name differs)
        self.assertEqual(digest["projects"][0]["visiblename"],
                         "slug-0-visible")
        self.assertEqual(digest["projects"][0]["newest_version"], "0.0")
        self.assertEqual(digest["projects"][0]["repo_count"], 2)


class ProjectDigestTests(unittest.TestCase):
    def test_ncdu_shaped_ladder(self):
        digest = project_digest("ncdu", NCDU_ENTRIES)
        self.assertEqual(digest["slug"], "ncdu")
        self.assertEqual(digest["total_entries"], len(NCDU_ENTRIES))
        versions = [tier["version"] for tier in digest["versions"]]
        # dotted-numeric descending: 2.10 > 2.9.2 > 1.22 > 1.22(dup
        # merged) > 1.21 > 1.19 > 1.16 > 1.10 > 1.9 > unknown tail
        self.assertEqual(versions, [
            "2.10", "2.9.2", "1.22", "1.21", "1.19", "1.16", "1.10",
            "1.9", "some-snapshot"])
        # the two 1.22 entries merged into ONE tier with both repos
        tier_122 = digest["versions"][2]
        self.assertEqual(tier_122["repo_count"], 2)
        self.assertEqual(tier_122["repos"], ["homebrew", "alpine_3_20"])
        self.assertEqual(tier_122["statuses"], ["legacy"])
        self.assertEqual(tier_122["vulnerable_repos"], 1)
        # vulnerable flag counts per tier
        tier_292 = digest["versions"][1]
        self.assertEqual(tier_292["vulnerable_repos"], 1)

    def test_debian_marking_and_block(self):
        digest = project_digest("ncdu", NCDU_ENTRIES)
        tier_119 = digest["versions"][4]  # debian_sid at 1.19
        self.assertEqual(tier_119["repos"], ["deb:debian_sid"])
        tier_116 = digest["versions"][5]  # debian_13 at 1.16
        self.assertEqual(tier_116["repos"], ["deb:debian_13"])
        # the debian block lists every debian_* entry, all of them
        self.assertEqual(digest["debian"], [
            {"repo": "debian_13", "version": "1.16"},
            {"repo": "debian_sid", "version": "1.19"},
        ])

    def test_repos_capped_at_eight_plus_marker(self):
        entries = [{"repo": f"r{i}", "version": "1.0", "status": "ok"}
                   for i in range(TIER_REPOS_CAP + 3)]
        digest = project_digest("x", entries)
        repos = digest["versions"][0]["repos"]
        self.assertEqual(len(repos), TIER_REPOS_CAP + 1)
        self.assertEqual(repos[-1], "+3 more")
        self.assertEqual(digest["versions"][0]["repo_count"],
                         TIER_REPOS_CAP + 3)

    def test_more_than_fifteen_tiers_aggregate_the_remainder(self):
        entries = [{"repo": "r", "version": f"1.{i}", "status": "legacy"}
                   for i in range(LADDER_TIER_CAP + 4)]
        digest = project_digest("x", entries)
        self.assertEqual(len(digest["versions"]), LADDER_TIER_CAP + 1)
        tail = digest["versions"][-1]
        self.assertEqual(tail["version"], "(4 older versions)")
        self.assertEqual(tail["repo_count"], 4)
        self.assertEqual(tail["statuses"], ["legacy"])

    def test_empty_entry_list_is_the_honest_no_such_project(self):
        digest = project_digest("no-such", [])
        self.assertEqual(digest["total_entries"], 0)
        self.assertEqual(digest["versions"], [])
        self.assertEqual(digest["debian"], [])
        self.assertIn("no such project", digest["note"])


class ClientGradingTests(unittest.TestCase):
    def test_project_ok(self):
        body = json.dumps(NCDU_ENTRIES).encode("utf-8")
        client = client_with(self, outcome(200, body))
        result = client.query("project", slug="ncdu")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.digest["slug"], "ncdu")
        self.assertEqual(result.to_data(), result.digest)
        self.assertEqual(result.query, None)
        self.assertEqual(result.slug, "ncdu")
        self.assertEqual(client.request_count, 1)

    def test_project_200_empty_array_is_ok_not_failed(self):
        client = client_with(self, outcome(200, b"[]"))
        result = client.query("project", slug="ghost")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.digest["total_entries"], 0)
        self.assertEqual(result.errors, [])
        self.assertIn("no such project", result.digest["note"])

    def test_search_ok(self):
        body = json.dumps({
            "r:ncdump": [{"repo": "r", "version": "1.0",
                          "status": "outdated", "visiblename": "ncdump"}],
            "ncdu": [{"repo": "aur", "version": "1.21",
                      "status": "newest", "visiblename": "ncdu"}],
        }).encode("utf-8")
        client = client_with(self, outcome(200, body))
        result = client.query("search", query="ncdu")
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.digest["exact_match"])
        self.assertEqual([row["slug"] for row in
                          result.digest["projects"]],
                         ["r:ncdump", "ncdu"])

    def test_search_200_empty_object_is_ok(self):
        client = client_with(self, outcome(200, b"{}"))
        result = client.query("search", query="zzz-no-match")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.digest["projects"], [])
        self.assertFalse(result.digest["exact_match"])

    def test_429_is_explicit_failure(self):
        client = client_with(self, outcome(429, b"{}"))
        result = client.query("search", query="ncdu")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "rate-limited")
        self.assertIn("never", result.note)

    def test_404_is_failure_not_no_such_project(self):
        client = client_with(self, outcome(404, b"{}"))
        result = client.query("project", slug="ncdu")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "http-404")
        self.assertIn("unknown", result.note)

    def test_transport_failure_is_explicit_failure(self):
        client = client_with(self, outcome(
            None, ok=False, error={"kind": "http-5xx",
                                   "message": "server error status 503"}))
        result = client.query("project", slug="ncdu")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "fetch-http-5xx")

    def test_timeout_is_explicit_failure(self):
        client = client_with(self, outcome(
            None, ok=False, error={"kind": "timeout",
                                   "message": "deadline"}))
        result = client.query("project", slug="ncdu")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "fetch-timeout")

    def test_malformed_json_is_explicit_failure(self):
        client = client_with(self, outcome(200, b"<html>not json"))
        result = client.query("project", slug="ncdu")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "malformed-json")

    def test_wrong_payload_shape_is_malformed(self):
        cases = [
            ("project", b'{"slug": "not-a-list"}'),
            ("search", b'["not", "an", "object"]'),
            ("search", b'{"ok": "entry-value-not-a-list"}'),
            ("project", b'["entry-not-an-object"]'),
        ]
        for action, body in cases:
            with self.subTest(action=action, body=body):
                client = client_with(self, outcome(200, body))
                result = client.query(
                    action, **({"query": "q"} if action == "search"
                               else {"slug": "s"}))
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.errors[0].code, "malformed-json")

    def test_strict_json_rejects_nonfinite_constants(self):
        client = client_with(self, outcome(200, b"[{\"version\": NaN}]"))
        result = client.query("project", slug="ncdu")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "malformed-json")


class ClientBudgetTests(unittest.TestCase):
    def test_per_client_cap_enforced(self):
        body = b"[]"
        client = client_with(self, outcome(200, body), policy=RepologyPolicy(
            max_requests=2, min_interval_s=0.0, timeout_s=1.0))
        self.assertEqual(client.query("project", slug="a").status, "ok")
        self.assertEqual(client.query("project", slug="b").status, "ok")
        third = client.query("project", slug="c")
        self.assertEqual(third.status, "failed")
        self.assertEqual(third.errors[0].code, "budget-exhausted")
        self.assertEqual(client.request_count, 2)

    def test_budget_check_hook_reserves_per_request(self):
        calls = []

        def budget_check(what):
            calls.append(what)

        client = client_with(
            self, outcome(200, b"[]"),
            policy=RepologyPolicy(max_requests=6, min_interval_s=0.0,
                                  timeout_s=1.0),
            budget_check=budget_check)
        client.query("project", slug="a")
        client.query("search", query="b")
        # one reservation per query, in query order; the label prose
        # itself is not pinned
        self.assertEqual(len(calls), 2)
        self.assertIn("project", calls[0])
        self.assertIn("search", calls[1])

    def test_budget_check_hook_veto_becomes_failed_result(self):
        def budget_check(what):
            raise RepologyError("budget-exhausted",
                                "shared request cap reached")

        client = client_with(
            self, outcome(200, b"[]"),
            policy=RepologyPolicy(max_requests=6, min_interval_s=0.0,
                                  timeout_s=1.0),
            budget_check=budget_check)
        result = client.query("project", slug="a")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "budget-exhausted")
        self.assertEqual(client.request_count, 0)


class IntrinsicExemptionTests(unittest.TestCase):
    """The client's fetcher exempts operator set UNION the
    configured base host; nothing else changes."""

    def test_default_base_host_is_exempt(self):
        with patch(PATCH_TARGET) as mocked:
            RepologyClient(policy=RepologyPolicy(min_interval_s=0.0))
            kwargs = mocked.Fetcher.call_args.kwargs
            self.assertEqual(kwargs["exempt_hosts"],
                             frozenset({"repology.org"}))

    def test_operator_set_unions_the_base_host(self):
        with patch(PATCH_TARGET) as mocked:
            RepologyClient(base="https://repology.example",
                           policy=RepologyPolicy(min_interval_s=0.0),
                           operator_exempt_hosts=frozenset(
                               {"example.org", "repology.example"}))
            kwargs = mocked.Fetcher.call_args.kwargs
            self.assertEqual(kwargs["exempt_hosts"], frozenset({
                "example.org", "repology.example"}))

    def test_operator_set_can_not_remove_the_base_host(self):
        with patch(PATCH_TARGET) as mocked:
            RepologyClient(policy=RepologyPolicy(min_interval_s=0.0),
                           operator_exempt_hosts=frozenset({"other.org"}))
            kwargs = mocked.Fetcher.call_args.kwargs
            self.assertEqual(kwargs["exempt_hosts"], frozenset({
                "other.org", "repology.org"}))

    def test_policy_carries_timeout_interval_and_cap(self):
        with patch(PATCH_TARGET) as mocked:
            RepologyClient(policy=RepologyPolicy(
                max_requests=3, min_interval_s=0.5, timeout_s=9.0))
            policy = mocked.Fetcher.call_args.args[0]
            self.assertEqual(policy["max_requests"], 3)
            self.assertEqual(policy["min_interval_s"], 0.5)
            self.assertEqual(policy["timeout_s"], 9.0)


if __name__ == "__main__":
    unittest.main()
