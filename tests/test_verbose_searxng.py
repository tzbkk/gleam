"""--verbose narration for the searxng module (plan verbose-flag
§7.5): the ``$ docker ...`` intent lines at the single ``_run`` seam
(argv[0] normalized to its basename), the per-call outcome lines,
the compressed step outcome words (docker ok / container / healthz /
search), teardown narration incl. the honest absent no-op, and the
quiet default (zero stderr).

Zero docker, zero network: the lifecycle subprocess seam, the
validate opener, and the validate search client are the established
fakes; the clock is patched for deterministic stamps. Entry runs sit
in a fresh temp dir so a repo ``./.env`` can never leak into the
resolution under test (the test_entry_searxng.py pattern)."""

from __future__ import annotations

import contextlib
import io
import os
import unittest
import urllib.error
from contextlib import chdir, redirect_stderr
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gleam.core.sources.searxng.lifecycle import (
    MARKERS_TEMPLATE,
    SearxngTarget,
    build_run_argv,
    tear_down,
)
from gleam.core.sources.searxng.validate import (
    VALIDATE_QUERY,
    run_validate,
)
from gleam.core.verbose import make_verbose
from tests.test_entry_searxng import SETTINGS_RESOURCE, run_main
from tests.test_searxng_lifecycle import (
    DOCKER,
    FOREIGN_LABELS,
    IMAGE,
    IMG_ID,
    NAME,
    SEARXNG_LABELS,
    FakeDocker,
    FakeSocketModule,
    FakeTime,
    default_script,
    lifecycle_env,
)
from tests.test_searxng_validate import (
    BASE,
    FakeHealthzOpener,
    FakeSearxngClient,
    good_results,
    http_error,
    search_result,
)
from tests.test_verbose import FIXED_TIME, LINE_SHAPE

VALIDATE_MODULE = "gleam.core.sources.searxng.validate"
STAMP = "[searxng 12:34:56] "


def stamped(messages):
    return [STAMP + message for message in messages]


def ps_line():
    return (f"$ docker ps -a --filter name=^/{NAME}$ "
            f"--format {{{{.Names}}}}")


def validate_pass_messages():
    """The exact PASS transcript of run_validate (message bodies, no
    stamps): every docker call narrated at the ``_run`` seam plus the
    four compressed step outcomes."""
    return [
        "$ docker info",
        "-> exit 0",
        "docker found at /usr/bin/docker; daemon reachable",
        "$ docker info",
        "-> exit 0",
        ps_line(),
        "-> exit 0",
        f"$ docker container inspect -f {{{{.Image}}}} {NAME}",
        "-> exit 0",
        f"$ docker container inspect -f {{{{.State.Status}}}} {NAME}",
        "-> exit 0",
        f"$ docker port {NAME} 8080",
        "-> exit 0",
        f"$ docker image inspect --format {MARKERS_TEMPLATE} {IMG_ID}",
        "-> exit 0",
        "container present, running, image ok",
        f"GET {BASE}/healthz -> 200",
        f"search '{VALIDATE_QUERY}': 2 results",
    ]


class VerboseTestCase(unittest.TestCase):
    """A capturing sink plus the fixed clock every transcript test
    pins its exact lines against."""

    def setUp(self):
        self.stream = io.StringIO()
        self.verbose = make_verbose("searxng", True, stream=self.stream)
        patcher = patch("gleam.core.verbose.time.gmtime",
                        return_value=FIXED_TIME)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self):
        return self.stream.getvalue().splitlines()


class ValidateTranscriptTests(VerboseTestCase):
    def wire(self, *, opener=None, result=None, ps_out=NAME.encode(),
             state=b"running", **script):
        docker = FakeDocker()
        default_script(docker, ps_out=ps_out, state=state, **script)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(lifecycle_env(docker=docker))
        stack.enter_context(patch(
            f"{VALIDATE_MODULE}._HEALTHZ_OPENER",
            opener if opener is not None else FakeHealthzOpener()))
        FakeSearxngClient.reset(result if result is not None
                                else search_result())
        stack.enter_context(patch(f"{VALIDATE_MODULE}.SearxngClient",
                                  FakeSearxngClient))
        return docker

    def test_pass_transcript_pinned_line_by_line(self):
        self.wire()
        outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "PASS")
        self.assertEqual(self.lines(), stamped(validate_pass_messages()))
        for line in self.lines():
            self.assertRegex(line, LINE_SHAPE)

    def test_dollar_lines_normalize_argv0_to_basename(self):
        docker = self.wire()
        outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "PASS")
        self.assertEqual(docker.calls[0][0], DOCKER,
                         "fixture must exercise an absolute docker path")
        lines = self.lines()
        self.assertIn(STAMP + "$ docker info", lines)
        self.assertFalse(
            any(line.startswith(STAMP + "$ /") for line in lines), lines)

    def test_basename_normalization_not_constant_specific(self):
        docker = FakeDocker()
        default_script(docker, ps_out=b"")
        with lifecycle_env(docker=docker,
                           which=lambda command: "/opt/docker/bin/docker"):
            outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "BLOCKER")
        lines = self.lines()
        self.assertIn(STAMP + "$ docker info", lines)
        self.assertFalse(
            any(line.startswith(STAMP + "$ /opt") for line in lines),
            lines)

    def test_failing_docker_missing_narrates_one_line(self):
        with lifecycle_env(which=lambda command: None):
            outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "BLOCKER")
        self.assertEqual(self.lines(), stamped(["docker not found"]))

    def test_failing_daemon_unreachable(self):
        docker = FakeDocker()
        docker.on("info", rc=1,
                  err=b"Cannot connect to the Docker daemon")
        with lifecycle_env(docker=docker):
            outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "BLOCKER")
        self.assertEqual(self.lines(), stamped(
            ["$ docker info", "-> exit 1", "daemon unreachable"]))

    def test_failing_healthz_non_200(self):
        self.wire(opener=FakeHealthzOpener(
            error=http_error(503, "Service Unavailable")))
        outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "FAIL")
        lines = self.lines()
        self.assertEqual(lines[-1],
                         STAMP + f"GET {BASE}/healthz -> 503")
        self.assertFalse(
            any(line.startswith(STAMP + "search '") for line in lines),
            lines)

    def test_failing_healthz_connection_refused(self):
        error = urllib.error.URLError(OSError(111, "Connection refused"))
        self.wire(opener=FakeHealthzOpener(error=error))
        outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "FAIL")
        self.assertEqual(
            self.lines()[-1],
            STAMP + f"GET {BASE}/healthz -> error: "
                    "[Errno 111] Connection refused")

    def test_failing_container_absent_short_circuits(self):
        self.wire(ps_out=b"")
        outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "BLOCKER")
        self.assertEqual(self.lines(), stamped([
            "$ docker info",
            "-> exit 0",
            "docker found at /usr/bin/docker; daemon reachable",
            "$ docker info",
            "-> exit 0",
            ps_line(),
            "-> exit 0",
            "container absent",
        ]))

    def test_container_failure_words_not_running_image_mismatch(self):
        self.wire(state=b"exited",
                  markers_map={IMAGE: SEARXNG_LABELS,
                               IMG_ID: FOREIGN_LABELS})
        outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertTrue(outcome.steps[1].ok)
        self.assertIn(
            STAMP + "container present, not running, image mismatch",
            self.lines())

    def test_search_line_singular_at_one_and_zero_on_shape_fail(self):
        self.wire(result=search_result(results=[good_results()[0]]))
        run_validate(NAME, BASE, verbose=self.verbose)
        self.assertIn(STAMP + f"search '{VALIDATE_QUERY}': 1 result",
                      self.lines())
        self.wire(result=search_result(results=[]))
        outcome = run_validate(NAME, BASE, verbose=self.verbose)
        self.assertEqual(outcome.verdict, "FAIL")
        self.assertIn(STAMP + f"search '{VALIDATE_QUERY}': 0 results",
                      self.lines())


class TearDownTranscriptTests(VerboseTestCase):
    def test_stop_narrates_every_call_and_outcome(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        with lifecycle_env(docker=docker):
            outcome = tear_down(NAME, verbose=self.verbose)
        self.assertEqual(outcome.action, "stopped-and-removed")
        self.assertEqual(self.lines(), stamped([
            "$ docker info",
            "-> exit 0",
            ps_line(),
            "-> exit 0",
            f"$ docker container inspect -f {{{{.Image}}}} {NAME}",
            "-> exit 0",
            f"$ docker image inspect --format {MARKERS_TEMPLATE} {IMG_ID}",
            "-> exit 0",
            f"$ docker container inspect -f {{{{.State.Status}}}} {NAME}",
            "-> exit 0",
            f"$ docker stop {NAME}",
            "-> exit 0",
            f"$ docker rm {NAME}",
            "-> exit 0",
        ]))

    def test_absent_noop_narrates_honestly(self):
        docker = FakeDocker()
        default_script(docker, ps_out=b"")
        with lifecycle_env(docker=docker):
            outcome = tear_down(NAME, verbose=self.verbose)
        self.assertEqual(outcome.action, "absent-noop")
        self.assertEqual(self.lines(), stamped([
            "$ docker info",
            "-> exit 0",
            ps_line(),
            "-> exit 0",
            "container absent, nothing to do",
        ]))
        self.assertEqual(docker.called("stop"), [])
        self.assertEqual(docker.called("rm"), [])

    def test_quiet_default_writes_nothing_to_stderr(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        err = io.StringIO()
        with redirect_stderr(err), lifecycle_env(docker=docker):
            outcome = tear_down(NAME)
        self.assertEqual(outcome.action, "stopped-and-removed")
        self.assertEqual(err.getvalue(), "")


class EntrySmokeTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(chdir(tmp))
        patcher = patch("gleam.core.verbose.time.gmtime",
                        return_value=FIXED_TIME)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wire_validate(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        stack = self.enterContext(contextlib.ExitStack())
        stack.enter_context(lifecycle_env(docker=docker))
        stack.enter_context(patch(
            f"{VALIDATE_MODULE}._HEALTHZ_OPENER", FakeHealthzOpener()))
        FakeSearxngClient.reset(search_result())
        stack.enter_context(patch(f"{VALIDATE_MODULE}.SearxngClient",
                                  FakeSearxngClient))
        return docker

    def test_validate_entry_verbose_pins_the_transcript(self):
        self.wire_validate()
        code, out, err = run_main(["--validate", "--verbose"])
        self.assertEqual(code, 0)
        self.assertEqual(err.splitlines(),
                         stamped(validate_pass_messages()))

    def test_validate_entry_quiet_writes_zero_stderr(self):
        self.wire_validate()
        code, out, err = run_main(["--validate"])
        self.assertEqual((code, err), (0, ""))

    def test_stop_entry_verbose_narrates_stop_then_rm(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        with lifecycle_env(docker=docker):
            code, out, err = run_main(["--stop", "--verbose"])
        self.assertEqual(code, 0)
        lines = err.splitlines()
        stop_line = STAMP + f"$ docker stop {NAME}"
        rm_line = STAMP + f"$ docker rm {NAME}"
        self.assertIn(stop_line, lines)
        self.assertIn(rm_line, lines)
        self.assertLess(lines.index(stop_line), lines.index(rm_line))
        self.assertIn(STAMP + "-> exit 0", lines)

    def test_stop_entry_absent_verbose_narrates_the_noop(self):
        docker = FakeDocker()
        default_script(docker, ps_out=b"")
        with lifecycle_env(docker=docker):
            code, out, err = run_main(["--stop", "--verbose"])
        self.assertEqual(code, 0)
        lines = err.splitlines()
        self.assertEqual(lines[-1],
                         STAMP + "container absent, nothing to do")
        self.assertNotIn(STAMP + f"$ docker stop {NAME}", lines)

    def test_start_entry_verbose_narrates_run_readiness_and_outcomes(self):
        docker = FakeDocker()
        default_script(docker)
        # The ps filter answers "absent" for the pre-validate probe
        # and the bring-up existence check, "present" afterwards (the
        # post-validate probe must see the created container).
        ps_answers = [b"", b""]

        def ps_responder(argv):
            if ps_answers:
                return (0, ps_answers.pop(0), b"")
            return (0, NAME.encode(), b"")

        docker.on("ps", "-a", responder=ps_responder)
        stack = self.enterContext(contextlib.ExitStack())
        stack.enter_context(lifecycle_env(docker=docker,
                                          sock=FakeSocketModule(),
                                          clock=FakeTime()))
        stack.enter_context(patch(
            f"{VALIDATE_MODULE}._HEALTHZ_OPENER", FakeHealthzOpener()))
        FakeSearxngClient.reset(search_result())
        stack.enter_context(patch(f"{VALIDATE_MODULE}.SearxngClient",
                                  FakeSearxngClient))
        code, out, err = run_main(["--start", "--verbose"])
        self.assertEqual(code, 0)
        lines = err.splitlines()
        run_argv = build_run_argv(DOCKER, SearxngTarget(), SETTINGS_RESOURCE)
        self.assertIn(
            STAMP + "$ " + " ".join([os.path.basename(DOCKER),
                                     *run_argv[1:]]), lines)
        self.assertIn(
            STAMP + f"$ docker exec {NAME} wget -q -T 3 -O - "
                    "http://127.0.0.1:8080/healthz", lines)
        for outcome_line in (
                "docker found at /usr/bin/docker; daemon reachable",
                "container absent",
                "container present, running, image ok",
                f"GET {BASE}/healthz -> 200",
                f"search '{VALIDATE_QUERY}': 2 results"):
            self.assertIn(STAMP + outcome_line, lines)
        self.assertFalse(
            any(line.startswith(STAMP + "$ /") for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
