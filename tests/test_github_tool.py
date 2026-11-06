"""GitHub API 工具辅助测试：路径构造白名单、整形、Link 分页、限流头。离线。"""

import unittest

from gleam import github


class ValidationTest(unittest.TestCase):
    def test_owner_charset_enforced(self):
        for bad in ("../etc", "a b", "a;b", "", None, "x" * 41,
                    "-leading", "a:b"):
            with self.assertRaises(github.GithubArgError, msg=repr(bad)):
                github.validate_owner(bad)
        self.assertEqual(github.validate_owner("RusXMMS"), "RusXMMS")
        self.assertEqual(github.validate_owner("a-b-c9"), "a-b-c9")

    def test_repo_charset_enforced(self):
        for bad in ("../etc", "a b", "a/b", "", None, "..", ".hidden",
                    "x" * 101):
            with self.assertRaises(github.GithubArgError, msg=repr(bad)):
                github.validate_repo(bad)
        self.assertEqual(github.validate_repo("librcd"), "librcd")
        self.assertEqual(github.validate_repo("my_repo.v2"), "my_repo.v2")


class BuildUrlTest(unittest.TestCase):
    BASE = "https://api.github.com"

    def test_paths_are_whitelisted_and_bounded(self):
        self.assertEqual(
            github.build_github_url(self.BASE, "repo", "o", "r"),
            self.BASE + "/repos/o/r")
        self.assertEqual(
            github.build_github_url(self.BASE, "readme", "o", "r"),
            self.BASE + "/repos/o/r/readme")
        # page/per_page 缺省时不附加查询串（agent 层会补默认值 1/10）
        self.assertEqual(
            github.build_github_url(self.BASE, "releases", "o", "r"),
            self.BASE + "/repos/o/r/releases")
        self.assertEqual(
            github.build_github_url(self.BASE, "commits", "o", "r",
                                    page=3, per_page=5),
            self.BASE + "/repos/o/r/commits?page=3&per_page=5")

    def test_pagination_only_for_list_actions(self):
        url = github.build_github_url(self.BASE, "repo", "o", "r",
                                      page=9, per_page=99)
        self.assertNotIn("page=", url)
        url = github.build_github_url(self.BASE, "readme", "o", "r",
                                      page=9, per_page=99)
        self.assertNotIn("page=", url)

    def test_pagination_bounds_and_types(self):
        for page, per_page in ((0, 10), (101, 10), (1, 0), (1, 101),
                               (True, 10), (1, True), ("2", 10), (1, "5"),
                               (-1, 10)):
            with self.assertRaises(github.GithubArgError,
                                   msg=repr((page, per_page))):
                github.build_github_url(self.BASE, "commits", "o", "r",
                                        page=page, per_page=per_page)

    def test_unknown_action_rejected(self):
        with self.assertRaises(github.GithubArgError):
            github.build_github_url(self.BASE, "user", "o", "r")

    def test_model_cannot_inject_path_via_owner(self):
        with self.assertRaises(github.GithubArgError):
            github.build_github_url(self.BASE, "repo", "o/other#", "r")

    def test_custom_base_path_prefix_kept(self):
        url = github.build_github_url("https://gh.example/api/", "repo",
                                      "o", "r")
        self.assertEqual(url, "https://gh.example/api/repos/o/r")

    def test_base_validation(self):
        self.assertEqual(github.validate_api_base("https://api.github.com"),
                         "")
        self.assertEqual(github.validate_api_base(
            "http://127.0.0.1:8443/"), "")
        for bad in ("ftp://x", "http://u:p@x", "http://", "gopher://x",
                    "http://x:99999"):
            self.assertNotEqual(github.validate_api_base(bad), "", bad)


class ShapeTest(unittest.TestCase):
    def test_repo_view_and_urls(self):
        payload = {
            "full_name": "o/r", "html_url": "https://github.com/o/r",
            "homepage": "https://o.example/", "description": "d",
            "archived": True, "pushed_at": "2024-01-01T00:00:00Z",
            "default_branch": "main", "owner": {"login": "o"},
            "license": {"spdx_id": "MIT"},
        }
        view, urls = github.shape_repo(payload)
        self.assertEqual(view["html_url"], "https://github.com/o/r")
        self.assertTrue(view["archived"])
        self.assertEqual(view["license_spdx"], "MIT")
        self.assertIn("not official proof", view["note"])
        self.assertEqual(urls, ["https://github.com/o/r",
                                "https://o.example/"])

    def test_repo_non_http_homepage_excluded_from_urls(self):
        _, urls = github.shape_repo(
            {"html_url": "https://github.com/o/r", "homepage": "about:blank"})
        self.assertEqual(urls, ["https://github.com/o/r"])

    def test_releases_shaped_bounded_with_urls(self):
        payload = [{"tag_name": f"v{i}", "html_url": f"https://h/{i}",
                    "published_at": "2024-01-01", "author": {"login": "o"},
                    "prerelease": False} for i in range(150)]
        view, urls = github.shape_releases(payload)
        self.assertEqual(view["count"], 100)
        self.assertEqual(view["releases"][0]["tag_name"], "v0")
        self.assertEqual(urls[0], "https://h/0")

    def test_commits_shaped_with_dates_and_urls(self):
        payload = [{"sha": "abcdef1234567890",
                    "commit": {"author": {"name": "N", "date": "2024-02-02"},
                               "message": "first line\nsecond",
                               "committer": {"date": "2024-02-03"}},
                    "author": {"login": "o"},
                    "html_url": "https://github.com/o/r/commit/abc"}]
        view, urls = github.shape_commits(payload)
        self.assertEqual(view["commits"][0]["sha"], "abcdef123456")
        self.assertEqual(view["commits"][0]["commit_author_date"],
                         "2024-02-02")
        self.assertEqual(view["commits"][0]["committer_date"], "2024-02-03")
        self.assertEqual(view["commits"][0]["message_first_line"],
                         "first line")
        self.assertEqual(urls, ["https://github.com/o/r/commit/abc"])

    def test_non_list_payloads_report_error_not_crash(self):
        view, urls = github.shape_releases({"error": "obj"})
        self.assertIn("error", view)
        self.assertEqual(urls, [])
        view, urls = github.shape_commits(None)
        self.assertIn("error", view)


class RateLimitInfoTest(unittest.TestCase):
    def test_extracts_known_headers(self):
        info = github.rate_limit_info({
            "x-ratelimit-limit": ["60"],
            "x-ratelimit-remaining": ["3"],
            "x-ratelimit-reset": ["1730000000"],
            "retry-after": ["42"]})
        self.assertEqual(info["x-ratelimit-remaining"], "3")
        self.assertEqual(info["retry-after"], "42")
        self.assertIsNone(info["x-ratelimit-used"])

    def test_none_when_absent(self):
        self.assertIsNone(github.rate_limit_info({"content-type": ["x"]}))
        self.assertIsNone(github.rate_limit_info(None))


class HttpUrlTest(unittest.TestCase):
    def test_userinfo_urls_rejected(self):
        self.assertIsNone(github._http_url("http://u:p@x/"))
        self.assertIsNone(github._http_url("https://user@host.example/p"))

    def test_invalid_or_out_of_range_port_rejected(self):
        for bad in ("http://x:99999/", "http://x:notaport/", "https://x:0/"):
            self.assertIsNone(github._http_url(bad), bad)

    def test_plain_urls_kept_fragment_dropped(self):
        self.assertEqual(github._http_url("https://x.example/a?b=1#frag"),
                         "https://x.example/a?b=1")
        self.assertEqual(github._http_url("http://x.example"),
                         "http://x.example/")


class ParseNextPageTest(unittest.TestCase):
    REQ = "https://api.github.com/repos/o/r/commits?page=1&per_page=10"

    def test_same_origin_next_page_accepted(self):
        link = ('<https://api.github.com/repos/o/r/commits?page=2'
                '&per_page=10>; rel="next", '
                '<https://api.github.com/repos/o/r/commits?page=5'
                '&per_page=10>; rel="last"')
        self.assertEqual(github.parse_next_page(link, self.REQ), 2)

    def test_cross_origin_link_rejected(self):
        link = '<http://evil.example/repos/o/r/commits?page=2>; rel="next"'
        self.assertIsNone(github.parse_next_page(link, self.REQ))

    def test_out_of_range_port_link_returns_none_not_crash(self):
        # 越界端口让 .port 抛 ValueError：必须吃掉并返回 None
        link = ('<https://api.github.com:99999/repos/o/r/commits?page=2'
                '>; rel="next"')
        self.assertIsNone(github.parse_next_page(link, self.REQ))

    def test_path_prefix_is_segment_aware(self):
        # "/repos/o/rx/..." 不得因裸 startswith("/repos/o/r") 被误采纳
        link = ('<https://api.github.com/repos/o/rx/commits?page=2'
                '>; rel="next"')
        self.assertIsNone(github.parse_next_page(link, self.REQ))
        ok = ('<https://api.github.com/repos/o/r/commits?page=2'
              '>; rel="next"')
        self.assertEqual(github.parse_next_page(ok, self.REQ), 2)

    def test_different_path_prefix_rejected(self):
        link = ('<https://api.github.com/repos/other/r/commits?page=2'
                '>; rel="next"')
        self.assertIsNone(github.parse_next_page(link, self.REQ))

    def test_missing_rel_next_returns_none(self):
        link = ('<https://api.github.com/repos/o/r/commits?page=2'
                '>; rel="prev"')
        self.assertIsNone(github.parse_next_page(link, self.REQ))

    def test_garbage_and_bounds(self):
        self.assertIsNone(github.parse_next_page("", self.REQ))
        self.assertIsNone(github.parse_next_page(None, self.REQ))
        self.assertIsNone(github.parse_next_page("not-a-link", self.REQ))
        huge = ('<https://api.github.com/repos/o/r/commits?page=9999'
                '>; rel="next"')
        self.assertIsNone(github.parse_next_page(huge, self.REQ))


if __name__ == "__main__":
    unittest.main()
