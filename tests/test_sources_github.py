"""Offline tests for the typed GitHub source adapter.

Zero network: the transport module boundary is patched with
``unittest.mock.patch``; every fetch-level test feeds real
``FetchOutcome``/``HopResult`` objects so the grading logic runs
against the true transport shapes.
"""

from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

from gleam.core.result import UsageError
from gleam.core.transport import FetchOutcome, HopResult
from gleam.core.sources.github import (
    GithubArgError,
    GithubClient,
    build_github_url,
    parse_next_page,
)

PATCH_TARGET = "gleam.core.sources.github.client.transport"

REPO_PAYLOAD = {
    "full_name": "wizznic/wizznic",
    "html_url": "https://github.com/wizznic/wizznic",
    "homepage": "https://wizznic.example.org",
    "description": "puzzle game",
    "archived": False,
    "disabled": False,
    "fork": False,
    "created_at": "2011-01-01T00:00:00Z",
    "updated_at": "2020-01-01T00:00:00Z",
    "pushed_at": "2020-02-02T00:00:00Z",
    "default_branch": "master",
    "language": "C",
    "stargazers_count": 12,
    "owner": {"login": "wizznic"},
    "license": {"spdx_id": "GPL-3.0"},
}

RELEASES_PAYLOAD = [
    {"tag_name": "v1.0", "name": "first", "published_at": "2020-01-01",
     "created_at": "2019-12-31", "prerelease": False,
     "author": {"login": "someone"},
     "html_url": "https://github.com/o/r/releases/tag/v1.0"},
]

COMMITS_PAYLOAD = [
    {"sha": "abcdef1234567890abcdef",
     "commit": {"author": {"name": "Dev", "date": "2021-01-01"},
                "committer": {"date": "2021-01-01"},
                "message": "first line\nsecond line"},
     "author": {"login": "dev"},
     "html_url": "https://github.com/o/r/commit/abcdef1234567890"},
]

FORKS_PAYLOAD = [
    {"full_name": "danielmarschall/vgwhois",
     "html_url": "https://github.com/danielmarschall/vgwhois",
     "description": "fork with monthly commits",
     "fork": True,
     "pushed_at": "2026-10-28T00:00:00Z",
     "created_at": "2024-01-01T00:00:00Z",
     "stargazers_count": 3},
    {"full_name": "someone/stale-mirror",
     "html_url": "https://github.com/someone/stale-mirror",
     "description": None,
     "fork": True,
     "pushed_at": "2019-05-05T00:00:00Z",
     "created_at": "2019-05-05T00:00:00Z",
     "stargazers_count": 0},
]


def outcome(status: int, body: bytes = b"", headers: dict | None = None,
            ok: bool = True, error: dict | None = None) -> FetchOutcome:
    url = "https://api.github.com/repos/o/r"
    hop = HopResult(url=url, status=status, headers=headers or {})
    return FetchOutcome(ok=ok, status=status, final_url=url, body=body,
                        hops=[hop], error=error)


def client_with(testcase, out: FetchOutcome) -> GithubClient:
    """Build a client whose internal fetcher returns ``out``; the
    transport patch is cleaned up automatically."""

    patcher = patch(PATCH_TARGET)
    mocked = patcher.start()
    testcase.addCleanup(patcher.stop)
    mocked.Fetcher.return_value.fetch.return_value = out
    return GithubClient()


class WhitelistAndPathAssemblyTests(unittest.TestCase):
    def test_url_assembled_per_action(self):
        self.assertEqual(
            build_github_url("https://api.github.com", "repo", "o", "r"),
            "https://api.github.com/repos/o/r")
        self.assertEqual(
            build_github_url("https://api.github.com", "readme", "o", "r"),
            "https://api.github.com/repos/o/r/readme")
        self.assertEqual(
            build_github_url("https://api.github.com", "releases", "o",
                             "r", 2, 5),
            "https://api.github.com/repos/o/r/releases?page=2&per_page=5")
        self.assertEqual(
            build_github_url("https://api.github.com", "commits", "o",
                             "r", 1, 10),
            "https://api.github.com/repos/o/r/commits?page=1&per_page=10")
        self.assertEqual(
            build_github_url("https://api.github.com", "forks", "o", "r"),
            "https://api.github.com/repos/o/r/forks?sort=newest")
        self.assertEqual(
            build_github_url("https://api.github.com", "forks", "o",
                             "r", 2, 50),
            "https://api.github.com/repos/o/r/forks"
            "?sort=newest&page=2&per_page=50")
        # operator-configured base with a path prefix keeps the prefix
        self.assertEqual(
            build_github_url("https://gh.example.com/api/", "repo", "o",
                             "r"),
            "https://gh.example.com/api/repos/o/r")

    def test_page_params_not_sent_for_repo_action(self):
        url = build_github_url("https://api.github.com", "repo", "o", "r",
                               3, 20)
        self.assertNotIn("?", url)

    def test_bad_owner_rejected_before_any_network(self):
        for bad in ("../x", "a b", "user@evil", "ownër", "",
                    None, 42):
            with self.subTest(owner=bad):
                with patch(PATCH_TARGET) as mocked:
                    client = GithubClient()
                    with self.assertRaises(GithubArgError):
                        client.repo(bad, "r")
                    self.assertFalse(
                        mocked.Fetcher.return_value.fetch.called)

    def test_bad_repo_rejected_before_any_network(self):
        for bad in ("../x", "a b", "a..b", ".hidden", "répo", "",
                    None, True):
            with self.subTest(repo=bad):
                with patch(PATCH_TARGET) as mocked:
                    client = GithubClient()
                    with self.assertRaises(GithubArgError):
                        client.repo("o", bad)
                    self.assertFalse(
                        mocked.Fetcher.return_value.fetch.called)

    def test_bad_action_rejected(self):
        with self.assertRaises(GithubArgError):
            build_github_url("https://api.github.com", "delete_repo",
                             "o", "r")
        with self.assertRaises(GithubArgError):
            build_github_url("https://api.github.com", "fork", "o", "r")

    def test_usage_error_family(self):
        # GithubArgError is both the exit-2 UsageError and a ValueError
        self.assertTrue(issubclass(GithubArgError, UsageError))
        self.assertTrue(issubclass(GithubArgError, ValueError))

    def test_pagination_hard_bounds(self):
        for bad in (0, 101, -1, "5", True, 1.5):
            with self.subTest(page=bad):
                with self.assertRaises(ValueError):
                    GithubClient().repo("o", "r", page=bad)
        for bad in (0, 101, "5", True, 1.5):
            with self.subTest(per_page=bad):
                with self.assertRaises(ValueError):
                    GithubClient().releases("o", "r", per_page=bad)
        # boundaries 1 and 100 pass validation on every action
        build_github_url("https://api.github.com", "repo", "o", "r", 1,
                         100)

    def test_bad_api_base_rejected(self):
        for bad in ("ftp://api.github.com",
                    "https://user:pw@api.github.com",
                    "https://api.github.com:notaport", "not a url"):
            with self.subTest(base=bad):
                with self.assertRaises(GithubArgError):
                    GithubClient(api_base=bad)

    def test_defaults_applied_only_for_paginated_actions(self):
        body = json.dumps(RELEASES_PAYLOAD).encode()
        client = client_with(self, outcome(200, body))
        result = client.releases("o", "r")
        self.assertEqual(result.page, 1)
        self.assertEqual(result.per_page, 10)
        self.assertIn("page=1", result.api_url)
        self.assertIn("per_page=10", result.api_url)

    def test_forks_defaults_to_full_page(self):
        body = json.dumps(FORKS_PAYLOAD).encode()
        client = client_with(self, outcome(200, body))
        result = client.forks("o", "r")
        self.assertEqual(result.page, 1)
        self.assertEqual(result.per_page, 100)
        self.assertEqual(
            result.api_url,
            "https://api.github.com/repos/o/r/forks"
            "?sort=newest&page=1&per_page=100")


class RequestSemanticsTests(unittest.TestCase):
    def test_fixed_headers_sent_exactly(self):
        with patch(PATCH_TARGET) as mocked:
            mocked.Fetcher.return_value.fetch.return_value = outcome(
                200, b"{}")
            client = GithubClient()
            client.repo("o", "r")
            _, kwargs = mocked.Fetcher.return_value.fetch.call_args
            # the literal fixed-header contract, not the constant
            # echoed back at itself
            headers = kwargs["headers"]
            self.assertEqual(headers.get("Accept"),
                             "application/vnd.github+json")
            self.assertEqual(headers.get("X-GitHub-Api-Version"),
                             "2022-11-28")

    def test_403_surfaces_rate_limit_headers_without_retry(self):
        headers = {
            "x-ratelimit-limit": ["60"],
            "x-ratelimit-remaining": ["0"],
            "x-ratelimit-reset": ["1700000000"],
            "retry-after": ["37"],
            "link": [],
        }
        with patch(PATCH_TARGET) as mocked:
            mocked.Fetcher.return_value.fetch.return_value = outcome(
                403, b"", headers)
            client = GithubClient()
            result = client.commits("o", "r")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "http-403")
        self.assertEqual(result.errors[0].details["rate_limit"]
                         ["x-ratelimit-remaining"], "0")
        self.assertEqual(result.errors[0].details["rate_limit"]
                         ["retry-after"], "37")
        self.assertEqual(result.rate_limit["x-ratelimit-reset"],
                         "1700000000")
        self.assertEqual(mocked.Fetcher.return_value.fetch.call_count, 1)

    def test_429_graded_rate_limited(self):
        headers = {"retry-after": ["60"]}
        client = client_with(self, outcome(429, b"", headers))
        result = client.releases("o", "r")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "rate-limited")
        self.assertEqual(result.errors[0].details["rate_limit"]
                         ["retry-after"], "60")

    def test_404_is_explicit_unknown_not_death(self):
        client = client_with(self, outcome(404, b""))
        result = client.releases("o", "r")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "http-404")
        self.assertIn("unknown", result.note)
        self.assertIn("NOT proof", result.note)
        self.assertEqual(result.view, {})
        self.assertEqual(result.evidence_urls, [])

    def test_transport_failure_means_unknown(self):
        client = client_with(self, outcome(None, b"", ok=False, error={
            "kind": "robots-disallow", "message": "robots said no"}))
        result = client.repo("o", "r")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "fetch-robots-disallow")
        self.assertIn("unknown", result.note)

    def test_malformed_json_graded(self):
        client = client_with(self, outcome(200, b"<html>not json"))
        result = client.repo("o", "r")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[0].code, "malformed-json")

    def test_wrong_payload_shape_graded_malformed(self):
        client = client_with(self, outcome(200, b"[1, 2, 3]"))
        result = client.repo("o", "r")
        self.assertEqual(result.errors[0].code, "malformed-json")
        self.assertIn("not an object", result.errors[0].message)
        client2 = client_with(self, outcome(200, b'{"not": "a list"}'))
        result2 = client2.releases("o", "r")
        self.assertEqual(result2.errors[0].code, "malformed-json")
        self.assertIn("not an array", result2.errors[0].message)
        client3 = client_with(self, outcome(200, b'{"not": "a list"}'))
        result3 = client3.forks("o", "r")
        self.assertEqual(result3.errors[0].code, "malformed-json")
        self.assertIn("not an array", result3.errors[0].message)


class SuccessShapingTests(unittest.TestCase):
    def test_repo_success_extracts_html_url_and_homepage(self):
        client = client_with(self, 
            outcome(200, json.dumps(REPO_PAYLOAD).encode()))
        result = client.repo("wizznic", "wizznic")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.errors, [])
        self.assertEqual(result.evidence_urls, [
            "https://github.com/wizznic/wizznic",
            "https://wizznic.example.org/"])
        self.assertEqual(result.view["full_name"], "wizznic/wizznic")
        self.assertEqual(result.view["owner_login"], "wizznic")
        self.assertEqual(result.view["license_spdx"], "GPL-3.0")
        self.assertIn("hosting-account records", result.note)

    def test_repo_invalid_homepage_dropped_from_evidence(self):
        payload = dict(REPO_PAYLOAD, homepage="not a url at all")
        client = client_with(self, 
            outcome(200, json.dumps(payload).encode()))
        result = client.repo("o", "r")
        self.assertEqual(result.evidence_urls,
                         ["https://github.com/wizznic/wizznic"])

    def test_releases_success_with_same_origin_next_page(self):
        link = ('<https://api.github.com/repos/o/r/releases?page=2>; '
                'rel="next", '
                '<https://api.github.com/repos/o/r/releases?page=1>; '
                'rel="prev"')
        headers = {"link": [link]}
        client = client_with(self, 
            outcome(200, json.dumps(RELEASES_PAYLOAD).encode(), headers))
        result = client.releases("o", "r")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.next_page, 2)
        self.assertEqual(result.view["next_page"], 2)
        self.assertIn("not followed automatically",
                      result.view["pagination_note"])
        self.assertEqual(result.view["releases"][0]["tag_name"], "v1.0")
        self.assertEqual(result.evidence_urls,
                         ["https://github.com/o/r/releases/tag/v1.0"])

    def test_commits_success_shaping(self):
        client = client_with(self, 
            outcome(200, json.dumps(COMMITS_PAYLOAD).encode()))
        result = client.commits("o", "r")
        self.assertEqual(result.status, "ok")
        entry = result.view["commits"][0]
        self.assertEqual(entry["sha"], "abcdef123456")
        self.assertEqual(entry["message_first_line"], "first line")
        self.assertEqual(entry["author_login"], "dev")
        self.assertEqual(result.evidence_urls,
                         ["https://github.com/o/r/commit/abcdef1234567890"])

    def test_forks_success_projection_and_evidence(self):
        client = client_with(self,
            outcome(200, json.dumps(FORKS_PAYLOAD).encode()))
        result = client.forks("o", "r")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.errors, [])
        self.assertEqual(result.view["count"], 2)
        alive = result.view["forks"][0]
        self.assertEqual(alive["full_name"], "danielmarschall/vgwhois")
        self.assertEqual(alive["pushed_at"], "2026-10-28T00:00:00Z")
        self.assertEqual(alive["created_at"], "2024-01-01T00:00:00Z")
        self.assertEqual(alive["stargazers_count"], 3)
        self.assertTrue(alive["fork"])
        stale = result.view["forks"][1]
        self.assertEqual(stale["pushed_at"], stale["created_at"])
        self.assertEqual(stale["stargazers_count"], 0)
        self.assertIsNone(stale["description"])
        self.assertNotIn("parent", alive)
        self.assertEqual(result.evidence_urls, [
            "https://github.com/danielmarschall/vgwhois",
            "https://github.com/someone/stale-mirror"])
        self.assertEqual(result.view["forks"][0]["html_url"],
                         result.evidence_urls[0])

    def test_forks_non_dict_entries_skipped(self):
        payload = FORKS_PAYLOAD + ["not-an-object", 42]
        client = client_with(self,
            outcome(200, json.dumps(payload).encode()))
        result = client.forks("o", "r")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.view["count"], 2)

    def test_readme_base64_decoded_and_capped(self):
        text = "A" * 500
        payload = {
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
            "path": "README.md",
            "sha": "abc",
            "size": 500,
            "html_url": "https://github.com/o/r/blob/master/README.md",
        }
        from gleam.core.sources.github import GithubPolicy
        with patch(PATCH_TARGET + ".Fetcher") as fetcher_cls:
            fetcher_cls.return_value.fetch.return_value = outcome(
                200, json.dumps(payload).encode())
            client = GithubClient(policy=GithubPolicy(readme_text_cap=100))
            result = client.readme("o", "r")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.view["decoding"],
                         "decoded-from-base64+markdown-text")
        self.assertTrue(result.view["readme_text"].startswith("AAA"))
        self.assertIn("readme truncated at 100 chars",
                      result.view["readme_text"])
        self.assertEqual(result.evidence_urls,
                         ["https://github.com/o/r/blob/master/README.md"])

    def test_to_data_field_set_excludes_raw_body(self):
        client = client_with(self, 
            outcome(200, json.dumps(REPO_PAYLOAD).encode()))
        result = client.repo("o", "r")
        data = result.to_data()
        self.assertNotIn("body", data)
        self.assertNotIn("raw_body", data)
        for key in ("action", "owner", "repo", "api_url", "http_status",
                    "view", "next_page", "rate_limit", "evidence_urls",
                    "content_sha256", "content_bytes", "note"):
            self.assertIn(key, data)
        self.assertEqual(data["http_status"], 200)
        self.assertTrue(data["content_sha256"])
        self.assertEqual(data["content_bytes"],
                         len(json.dumps(REPO_PAYLOAD).encode()))
        # raw bytes stay available on the typed result for archival
        self.assertTrue(result.body)


class LinkPaginationTests(unittest.TestCase):
    def test_same_origin_same_prefix_parsed(self):
        url = "https://api.github.com/repos/o/r/releases?page=1"
        link = ('<https://api.github.com/repos/o/r/releases?page=2&'
                'per_page=10>; rel="next"')
        self.assertEqual(parse_next_page(link, url), 2)

    def test_cross_origin_link_not_parsed(self):
        url = "https://api.github.com/repos/o/r/releases?page=1"
        link = '<https://evil.example.org/repos/o/r/releases?page=2>; rel="next"'
        self.assertIsNone(parse_next_page(link, url))

    def test_scheme_mismatch_is_cross_origin(self):
        url = "https://api.github.com/repos/o/r/releases?page=1"
        link = '<http://api.github.com/repos/o/r/releases?page=2>; rel="next"'
        self.assertIsNone(parse_next_page(link, url))

    def test_wrong_path_prefix_not_parsed(self):
        url = "https://api.github.com/repos/o/r/releases?page=1"
        # segment-aware: /repos/o/rx must not match prefix /repos/o/r
        link = ('<https://api.github.com/repos/o/rx/releases?page=2>; '
                'rel="next"')
        self.assertIsNone(parse_next_page(link, url))

    def test_out_of_bound_page_dropped(self):
        url = "https://api.github.com/repos/o/r/releases?page=1"
        link = ('<https://api.github.com/repos/o/r/releases?page=101>; '
                'rel="next"')
        self.assertIsNone(parse_next_page(link, url))

    def test_no_next_rel_returns_none(self):
        url = "https://api.github.com/repos/o/r/releases?page=2"
        link = ('<https://api.github.com/repos/o/r/releases?page=1>; '
                'rel="prev"')
        self.assertIsNone(parse_next_page(link, url))
        self.assertIsNone(parse_next_page(None, url))
        self.assertIsNone(parse_next_page("", url))


if __name__ == "__main__":
    unittest.main()
