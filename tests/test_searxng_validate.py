"""Offline tests for the side-effect-free validate probe chain.

Zero docker, zero network, zero writes: the lifecycle probes, the
healthz opener, and SearxngClient are faked at the validate module's
own seams (``probe_daemon`` / ``probe_container`` /
``_HEALTHZ_OPENER`` / ``SearxngClient``), while the graded
``SearxngResult`` instances are the real typed dataclass. Every
BLOCKER cause and every FAIL shape rule of gate.sh's frozen contract
gets its own case, and the short-circuit property (a refused step
prevents every later call) is asserted with recording fakes.
"""

from __future__ import annotations

import contextlib
import io
import types
import unittest
import urllib.error
from unittest.mock import patch

from gleam.core.result import DiagnosticItem
from gleam.core.sources.searxng.client import SearxngResult
from gleam.core.sources.searxng.lifecycle import (
    CODE_DAEMON_UNREACHABLE,
    CODE_DOCKER_COMMAND_FAILED,
    CODE_DOCKER_UNAVAILABLE,
    ContainerState,
    DaemonOk,
    GuardFailure,
)
from gleam.core.verbose import _quiet
from gleam.core.sources.searxng.validate import (
    CODE_CONTAINER_ABSENT,
    CODE_HEALTHZ_UNHEALTHY,
    CODE_QUERY_SHAPE_INVALID,
    HEALTHZ_TIMEOUT_S,
    VALIDATE_QUERY,
    VERDICT_BLOCKER,
    VERDICT_FAIL,
    VERDICT_PASS,
    ValidationOutcome,
    ValidationStep,
    WHERE,
    run_validate,
)

MODULE = "gleam.core.sources.searxng.validate"
BASE = "http://127.0.0.1:8888"
OTHER_BASE = "http://10.1.2.3:9999"
NAME = "gleam-searxng"
LIFECYCLE_WHERE = "gleam.core.sources.searxng.lifecycle"

STEP_NAMES = ("docker-availability", "container-presence", "healthz",
              "search-query")


def daemon_ok():
    return DaemonOk(docker_path="/usr/bin/docker")


def container_present():
    return ContainerState(
        name=NAME, present=True, running=True, image_id="sha256:cafebabe",
        host_bindings=("127.0.0.1:8888",), searxng_image=True)


def good_results():
    return [
        {"title": "Debian -- wizznic",
         "url": "https://packages.debian.org/sid/wizznic",
         "snippet": "upstream search", "engines": ["debian", "duckduckgo"]},
        {"title": "Wizznic!", "url": "https://github.com/DusteDdk/wizznic",
         "snippet": "", "engines": ["github"]},
    ]


def search_result(status="ok", results=None, ambiguous=False, errors=None,
                  unresponsive=None, warnings=None):
    """A real typed SearxngResult with test-chosen grading fields."""
    return SearxngResult(
        status=status, query=VALIDATE_QUERY,
        final_url=BASE + "/search?q=" + VALIDATE_QUERY.replace(" ", "+"),
        http_status=200,
        results=results if results is not None else good_results(),
        unresponsive_engines=unresponsive or [],
        ambiguous=ambiguous,
        errors=errors or [], warnings=warnings or [])


class FakeHealthzResponse:
    def __init__(self, status=200, body=b"OK"):
        self.status = status
        self._body = body

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHealthzOpener:
    """Records every open and answers with a canned response or a
    canned exception."""

    def __init__(self, response=None, error=None):
        self.response = response or FakeHealthzResponse()
        self.error = error
        self.calls = []

    def open(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class FakeSearxngClient:
    """Class-level recording double (reset per test via ``reset``)."""

    calls = []
    result = None

    @classmethod
    def reset(cls, result):
        cls.calls = []
        cls.result = result

    def __init__(self, base=None, timeout=None, policy=None):
        self.base = base
        self.queries = []
        cls = type(self)
        cls.calls.append(self)

    def search(self, query):
        self.queries.append(query)
        return type(self).result


def http_error(code, reason):
    return urllib.error.HTTPError(
        "http://127.0.0.1:8888/healthz", code, reason,
        types.SimpleNamespace(get=lambda name, default=None: default),
        io.BytesIO(b""))


def blocker_outcome():
    """A minimal BLOCKER outcome (entry/matrix tests reuse it)."""
    step = ValidationStep("docker-availability", False,
                          "docker: command not found")
    return ValidationOutcome(
        VERDICT_BLOCKER, (step,),
        (DiagnosticItem(code=CODE_DOCKER_UNAVAILABLE,
                        message="docker: command not found",
                        where=LIFECYCLE_WHERE),))


def pass_outcome():
    """A minimal PASS outcome (entry/matrix tests reuse it)."""
    return ValidationOutcome(VERDICT_PASS, (
        ValidationStep("docker-availability", True,
                       "docker found at /usr/bin/docker; daemon reachable"),
        ValidationStep("container-presence", True,
                       f"container '{NAME}' present; running; "
                       f"image ok; at 127.0.0.1:8888"),
        ValidationStep("healthz", True,
                       f"GET {BASE}/healthz: HTTP 200 (OK)"),
        ValidationStep("search-query", True,
                       "status ok, 2 results, each an absolute http(s) URL "
                       "with engine attribution"),
    ))


class ValidateChainTests(unittest.TestCase):
    def setUp(self):
        self.opener = FakeHealthzOpener()

    def wire(self, *, daemon=None, container=None, opener=None, result=None):
        """Patch the four module seams; returns handles for
        short-circuit assertions."""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        handles = types.SimpleNamespace(opener=opener if opener is not None
                                        else self.opener)
        handles.daemon = stack.enter_context(patch(
            f"{MODULE}.probe_daemon",
            return_value=daemon if daemon is not None else daemon_ok()))
        handles.container = stack.enter_context(patch(
            f"{MODULE}.probe_container",
            return_value=container if container is not None
            else container_present()))
        stack.enter_context(patch(f"{MODULE}._HEALTHZ_OPENER", handles.opener))
        FakeSearxngClient.reset(result if result is not None
                                else search_result())
        stack.enter_context(patch(f"{MODULE}.SearxngClient",
                                  FakeSearxngClient))
        return handles

    # --- PASS -----------------------------------------------------------

    def test_pass_verdict_steps_and_calls(self):
        handles = self.wire()
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertEqual(outcome.errors, ())
        self.assertEqual([s.step for s in outcome.steps], list(STEP_NAMES))
        self.assertTrue(all(s.ok for s in outcome.steps))
        self.assertIn("HTTP 200 (OK)", outcome.steps[2].detail)
        self.assertIn("2 results", outcome.steps[3].detail)
        handles.daemon.assert_called_once_with(verbose=_quiet)
        handles.container.assert_called_once_with(NAME, verbose=_quiet)
        self.assertEqual(
            self.opener.calls,
            [(BASE + "/healthz", HEALTHZ_TIMEOUT_S)])
        (client,) = FakeSearxngClient.calls
        self.assertEqual(client.base, BASE)
        self.assertEqual(client.queries, [VALIDATE_QUERY])

    def test_healthz_and_query_use_the_configured_base(self):
        self.wire()
        outcome = run_validate(NAME, OTHER_BASE)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertEqual(self.opener.calls[0][0], OTHER_BASE + "/healthz")
        self.assertEqual(FakeSearxngClient.calls[0].base, OTHER_BASE)

    def test_pass_records_unresponsive_engines_in_step_detail(self):
        self.wire(result=search_result(unresponsive=[["bing", "timeout"]]))
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertIn("unresponsive_engines: ['bing', 'timeout']",
                      outcome.steps[3].detail)

    def test_client_warnings_never_leak_into_errors(self):
        # A PASS must stay clean: warnings on the envelope would
        # downgrade the entry Result to partial (exit 1).
        warning = DiagnosticItem(code="captcha-reported",
                                 message="some engines reported captcha",
                                 where="gleam.core.sources.searxng")
        self.wire(result=search_result(warnings=[warning]))
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertEqual(outcome.errors, ())

    def test_pass_chain_never_issues_mutating_docker_subcommands(self):
        """Plan acceptance: ``--validate`` never invokes docker run.
        The chain runs for REAL (validate's seams untouched) against a
        scripted docker subprocess seam that records every argv, so a
        mutating subcommand (run/create/start/pull) on the PASS path
        would fail this assertion."""
        from tests.test_searxng_lifecycle import (
            FakeDocker,
            default_script,
            lifecycle_env,
        )

        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(lifecycle_env(docker=docker))
        stack.enter_context(
            patch(f"{MODULE}._HEALTHZ_OPENER", FakeHealthzOpener()))
        FakeSearxngClient.reset(search_result())
        stack.enter_context(patch(f"{MODULE}.SearxngClient",
                                  FakeSearxngClient))

        outcome = run_validate(NAME, BASE)

        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertTrue(docker.calls, "the docker seam must be exercised")
        mutating = {"run", "create", "start", "pull"}
        for argv in docker.calls:
            leaked = [part for part in argv[1:] if part in mutating]
            self.assertEqual(
                leaked, [],
                f"--validate issued a mutating docker call: {list(argv)}")
        probes = {tuple(argv[1:3]) for argv in docker.calls}
        self.assertIn(("info",), probes)
        self.assertIn(("ps", "-a"), probes)
        self.assertIn(("container", "inspect"), probes)

    # --- BLOCKER: step 1 -------------------------------------------------

    def test_blocker_docker_unavailable(self):
        guard = GuardFailure(CODE_DOCKER_UNAVAILABLE,
                             "docker: command not found",
                             {"command": "docker"})
        handles = self.wire(daemon=guard)
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKER)
        (error,) = outcome.errors
        self.assertEqual(error.code, CODE_DOCKER_UNAVAILABLE)
        self.assertEqual(error.where, LIFECYCLE_WHERE)
        self.assertEqual(outcome.steps,
                         (ValidationStep("docker-availability", False,
                                         "docker: command not found"),))
        handles.container.assert_not_called()
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(FakeSearxngClient.calls, [])

    def test_blocker_daemon_unreachable(self):
        guard = GuardFailure(CODE_DAEMON_UNREACHABLE,
                             "Docker daemon is not reachable.",
                             {"stderr_tail": []})
        self.wire(daemon=guard)
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKER)
        self.assertEqual(outcome.errors[0].code, CODE_DAEMON_UNREACHABLE)

    # --- BLOCKER: step 2 -------------------------------------------------

    def test_blocker_probe_guard_failure(self):
        guard = GuardFailure(CODE_DOCKER_COMMAND_FAILED, "docker ps failed",
                             {"argv": ["docker", "ps"]})
        handles = self.wire(container=guard)
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKER)
        self.assertEqual(outcome.errors[0].code, CODE_DOCKER_COMMAND_FAILED)
        self.assertEqual(len(outcome.steps), 2)
        self.assertFalse(outcome.steps[1].ok)
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(FakeSearxngClient.calls, [])

    def test_blocker_container_absent(self):
        self.wire(container=ContainerState(name=NAME, present=False))
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKER)
        (error,) = outcome.errors
        self.assertEqual(error.code, CODE_CONTAINER_ABSENT)
        self.assertEqual(error.where, WHERE)
        self.assertIn("--start", error.message)
        self.assertEqual(error.details, {"container": NAME})
        self.assertFalse(outcome.steps[1].ok)
        self.assertIn("not present", outcome.steps[1].detail)
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(FakeSearxngClient.calls, [])

    def test_container_step_detail_carries_state_facts(self):
        self.wire(container=ContainerState(
            name=NAME, present=True, running=False,
            image_id="sha256:cafebabe", host_bindings=(),
            searxng_image=False))
        outcome = run_validate(NAME, BASE)
        # Present-but-stopped is NOT a blocker: healthz decides next.
        self.assertTrue(outcome.steps[1].ok)
        self.assertIn("not running", outcome.steps[1].detail)
        self.assertIn("image mismatch", outcome.steps[1].detail)
        self.assertNotIn("at ", outcome.steps[1].detail)

    # --- FAIL: step 3 ------------------------------------------------------

    def test_fail_healthz_connection_refused(self):
        error = urllib.error.URLError(
            OSError(111, "Connection refused"))
        handles = self.wire(opener=FakeHealthzOpener(error=error))
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        (item,) = outcome.errors
        self.assertEqual(item.code, CODE_HEALTHZ_UNHEALTHY)
        self.assertEqual(item.details, {"url": BASE + "/healthz"})
        self.assertIn("URLError", outcome.steps[2].detail)
        self.assertFalse(outcome.steps[2].ok)
        self.assertEqual(FakeSearxngClient.calls, [])

    def test_fail_healthz_non_2xx(self):
        self.wire(opener=FakeHealthzOpener(
            error=http_error(503, "Service Unavailable")))
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertEqual(outcome.errors[0].code, CODE_HEALTHZ_UNHEALTHY)
        self.assertIn("HTTP 503", outcome.steps[2].detail)

    def test_fail_healthz_redirect_is_not_followed(self):
        opener = FakeHealthzOpener(error=http_error(302, "Found"))
        self.wire(opener=opener)
        outcome = run_validate(NAME, BASE)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertEqual(len(opener.calls), 1)

    def test_no_redirect_handler_never_follows(self):
        from gleam.core.sources.searxng.validate import _NoRedirect

        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "Found", {}, "http://elsewhere/healthz"))

    # --- FAIL: step 4 (one case per shape rule) ---------------------------

    def _shape_failure(self, result):
        self.wire(result=result)
        return run_validate(NAME, BASE)

    @staticmethod
    def _problems(outcome):
        (item,) = [e for e in outcome.errors
                   if e.code == CODE_QUERY_SHAPE_INVALID]
        return item.details["problems"]

    def test_fail_client_status_not_ok(self):
        cause = DiagnosticItem(code="connection-error",
                               message="ConnectionRefusedError",
                               where="gleam.core.sources.searxng")
        outcome = self._shape_failure(
            search_result(status="failed", results=[], errors=[cause]))
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("client status is 'failed', expected 'ok'",
                      self._problems(outcome))
        self.assertEqual([e.code for e in outcome.errors],
                         ["connection-error", CODE_QUERY_SHAPE_INVALID])
        self.assertFalse(outcome.steps[3].ok)

    def test_fail_explicit_ambiguity(self):
        outcome = self._shape_failure(
            search_result(status="failed", ambiguous=True, results=[]))
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("explicit ambiguity: all engines failed or empty",
                      self._problems(outcome))

    def test_fail_zero_results_clean_empty(self):
        outcome = self._shape_failure(search_result(results=[]))
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("zero results: conservative engine subset unusable",
                      self._problems(outcome))

    def test_fail_non_absolute_result_url(self):
        outcome = self._shape_failure(search_result(results=[
            good_results()[0],
            {"title": "local", "url": "about:blank", "snippet": "",
             "engines": ["debian"]},
        ]))
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("result 1: not an absolute http(s) URL: "
                      "'about:blank'", self._problems(outcome))

    def test_fail_missing_engine_attribution(self):
        outcome = self._shape_failure(search_result(results=[
            {"title": "Debian", "url": "https://packages.debian.org/",
             "snippet": "", "engines": []},
        ]))
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("result 0: missing engine attribution",
                      self._problems(outcome))

    def test_fail_collects_every_violation(self):
        outcome = self._shape_failure(search_result(results=[
            {"title": "x", "url": "ftp://host/x", "snippet": "",
             "engines": []},
        ]))
        self.assertEqual(self._problems(outcome), [
            "result 0: not an absolute http(s) URL: 'ftp://host/x'",
            "result 0: missing engine attribution",
        ])


if __name__ == "__main__":
    unittest.main()
