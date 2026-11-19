"""Offline tests for the searxng entry shell.

Zero docker, zero network, zero filesystem effects. The six
orchestration scenarios run against fakes at the entry's own seams
(``run_validate`` / ``bring_up`` / ``tear_down``) for the
Result-contract surface, and the argv-level contracts run through the
REAL lifecycle + validate layers against a scripted FakeDocker: the
read-only package-data settings mount on ``docker run``, the graceful
stop-then-rm ordering (never ``rm -f``), the foreign-container
refusal, and the daemon-unreachable failure (never a silent no-op).
Usage-error paths run the real binder; everything runs in a fresh
temporary directory so a repo-root ``.env`` can never leak into the
resolution under test.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import os
import tempfile
import types
import unittest
from contextlib import chdir
from dataclasses import replace
from unittest.mock import patch

from gleam.core.result import DiagnosticItem
from gleam.core.sources.searxng.lifecycle import (
    CODE_DAEMON_UNREACHABLE,
    CODE_IMAGE_NOT_SEARXNG,
    CODE_PORT_IN_USE,
    BringUpSuccess,
    GuardFailure,
    SearxngTarget,
    TearDownOutcome,
)
from gleam.core.verbose import _quiet
from gleam.core.sources.searxng.validate import (
    VERDICT_FAIL,
    ValidationOutcome,
    ValidationStep,
)
from gleam.entries import searxng as searxng_entry
from gleam.entries.help import general_help, module_help
from gleam.entries.searxng import (
    KIND_SEARXNG_STARTED,
    KIND_SEARXNG_STOPPED,
    KIND_SEARXNG_VALIDATED,
    main,
)
from gleam.router import dispatch
from tests._helpers import capture_run
from tests.test_searxng_lifecycle import (
    DOCKER,
    FOREIGN_LABELS,
    IMAGE,
    IMG_ID,
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
    blocker_outcome,
    pass_outcome,
    search_result,
)

EIGHT_MODULES = ("udd", "bts", "repology", "check", "investigate",
                 "searxng", "help", "version")

LIFECYCLE = "gleam.core.sources.searxng.lifecycle"
VALIDATE = "gleam.core.sources.searxng.validate"

NAME = "gleam-searxng"
IMAGE_REF = "searxng/searxng:latest"
DIGEST = "docker.io/searxng/searxng@sha256:aaaa"
#: Resolved at import like the entry does: the mount source pinned
#: here must be the real installed resource, never a hardcoded path.
SETTINGS_RESOURCE = str(importlib.resources.files("gleam") / "data"
                        / "searxng-settings.yml")


def run_main(argv):
    return capture_run(main, argv)


def bring_up_success(action="created"):
    return BringUpSuccess(
        container=NAME, image=IMAGE_REF, url="http://127.0.0.1:8888",
        action=action, image_digest=DIGEST)


def absent_noop():
    return TearDownOutcome(container=NAME, action="absent-noop")


def healthz_fail_outcome():
    """A post-bring-up FAIL: healthy probe chain, dead healthz."""
    message = f"healthz endpoint {BASE}/healthz is not healthy: HTTP 503"
    error = DiagnosticItem(
        code="searxng-healthz-unhealthy",
        message=message,
        where="gleam.core.sources.searxng.validate",
        details={"url": BASE + "/healthz"})
    steps = (
        ValidationStep("docker-availability", True,
                       "docker found at /usr/bin/docker; daemon reachable"),
        ValidationStep("container-presence", True,
                       f"container '{NAME}' present; running; "
                       f"image ok; at 127.0.0.1:8888"),
        ValidationStep("healthz", False, message),
    )
    return ValidationOutcome(VERDICT_FAIL, steps, (error,))


class SearxngEntryTestCase(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(chdir(tmp))


class RouterDispatchTests(SearxngEntryTestCase):
    def test_router_routes_searxng_to_the_entry_main(self):
        with patch("gleam.entries.searxng.main", return_value=7) as stub:
            self.assertEqual(dispatch(["searxng", "--validate"]), 7)
        stub.assert_called_once_with(["--validate"])


class OneActionRuleTests(SearxngEntryTestCase):
    """Exit-2 paths: zero or multiple action switches, before any
    action happens (mirrors the udd --list-datasets precedent)."""

    def test_no_action_flag_exits_two(self):
        code, out, err = run_main([])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("exactly one of --start/--stop/--validate", err)

    def test_conflicting_action_flags_exit_two(self):
        conflicts = [
            ["--start", "--stop"],
            ["--start", "--validate"],
            ["--stop", "--validate"],
            ["--start", "--stop", "--validate"],
        ]
        for argv in conflicts:
            with self.subTest(argv=argv):
                code, out, err = run_main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("exactly one of --start/--stop/--validate",
                              err)

    def test_one_action_passes_the_mode_check(self):
        # The real actions need their layer fakes; StartEntryTests
        # and StopEntryTests own the full contracts.
        with patch("gleam.entries.searxng.run_validate",
                   return_value=pass_outcome()):
            result = searxng_entry.run(argv=["--start"])
            self.assertEqual(result.status, "ok")
        with patch("gleam.entries.searxng.tear_down",
                   return_value=absent_noop()):
            result = searxng_entry.run(argv=["--stop"])
            self.assertEqual(result.status, "ok")


class FlagValidationTests(SearxngEntryTestCase):
    """Exit-2 paths from the real binder: port range/int, URL rules
    on the shared search-base spec, unknown flags, positionals."""

    def test_port_garbage_exits_two(self):
        code, out, err = run_main(["--validate", "--port", "garbage"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--port", err)

    def test_port_out_of_range_exits_two(self):
        for port in ("0", "-1", "65536", "99999"):
            with self.subTest(port=port):
                code, _, err = run_main(["--validate", "--port", port])
                self.assertEqual(code, 2)
                self.assertIn("--port", err)

    def test_search_base_invalid_scheme_exits_two(self):
        code, out, err = run_main(
            ["--validate", "--search-base", "ftp://127.0.0.1:8888"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--search-base", err)

    def test_search_base_malformed_exits_two(self):
        for value in ("oops", "http://user:pass@127.0.0.1:8888"):
            with self.subTest(value=value):
                code, _, err = run_main(
                    ["--validate", "--search-base", value])
                self.assertEqual(code, 2)
                self.assertIn("--search-base", err)

    def test_unknown_flag_exits_two(self):
        code, out, err = run_main(["--validate", "--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("--bogus", err)

    def test_stray_positional_exits_two(self):
        code, out, err = run_main(["searxng", "--validate"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")

    def test_action_switch_takes_no_value(self):
        code, _, err = run_main(["--start=x"])
        self.assertEqual(code, 2)
        self.assertIn("--start", err)


class DefaultsAndSearchBaseReuseTests(SearxngEntryTestCase):
    """The deployment defaults and the reused GLEAM_SEARCH_BASE spec
    (one spec object, both surfaces, flag > env precedence)."""

    def test_defaults_bind_the_frozen_surface(self):
        config = searxng_entry.parser(argv=["--validate"])
        self.assertEqual(config.image, "searxng/searxng:latest")
        self.assertEqual(config.name, "gleam-searxng")
        self.assertEqual(config.port, 8888)
        self.assertEqual(config.bind, "127.0.0.1")
        self.assertFalse(config.pull)
        self.assertEqual(config.search_base, "http://127.0.0.1:8888")

    def test_search_base_reads_the_env_file(self):
        with open(".env", "w", encoding="utf-8") as handle:
            handle.write("GLEAM_SEARCH_BASE=http://127.0.0.1:9999\n")
        config = searxng_entry.parser(argv=["--validate"])
        self.assertEqual(config.search_base, "http://127.0.0.1:9999")

    def test_search_base_flag_wins_over_env_file(self):
        with open(".env", "w", encoding="utf-8") as handle:
            handle.write("GLEAM_SEARCH_BASE=http://127.0.0.1:9999\n")
        config = searxng_entry.parser(
            argv=["--validate", "--search-base", "http://10.0.0.1:80"])
        self.assertEqual(config.search_base, "http://10.0.0.1:80")

    def test_search_base_param_is_shared_with_investigate(self):
        from gleam.core.config.spec import MODULE_SPECS

        investigate_param = next(
            param for param in MODULE_SPECS["investigate"]
            if param.name == "search_base"
        )
        searxng_param = next(
            param for param in MODULE_SPECS["searxng"]
            if param.name == "search_base"
        )
        self.assertIs(investigate_param, searxng_param)


class StartEntryTests(SearxngEntryTestCase):
    """--start orchestration with the lifecycle + validate layers
    faked at the entry's seams: the pre-validate reuse short-circuit,
    the guarded bring-up wiring (target, pull, settings mount), and
    the post-validate success gate."""

    def wire(self, outcomes, brought_up=None):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        validate = stack.enter_context(patch(
            "gleam.entries.searxng.run_validate",
            side_effect=list(outcomes)))
        if brought_up is None:
            brought_up = bring_up_success()
        lifecycle = stack.enter_context(patch(
            "gleam.entries.searxng.bring_up", return_value=brought_up))
        return validate, lifecycle

    def test_prevalidate_pass_reuses_without_any_bring_up_call(self):
        validate, lifecycle = self.wire([pass_outcome()])
        code, out, err = run_main(["--start", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], KIND_SEARXNG_STARTED)
        self.assertEqual(payload["data"]["action"], "start")
        self.assertTrue(payload["data"]["reused"])
        self.assertEqual(payload["data"]["verdict"], "PASS")
        self.assertEqual(payload["data"]["container"], NAME)
        self.assertEqual([s["ok"] for s in payload["data"]["steps"]],
                         [True] * 4)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["diagnostics"], [])
        self.assertEqual(payload["warnings"], [])
        validate.assert_called_once_with(name=NAME, search_base=BASE,
                                         verbose=_quiet)
        lifecycle.assert_not_called()

    def test_prevalidate_pass_human_render_says_reused(self):
        self.wire([pass_outcome()])
        code, out, err = run_main(["--start"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("searxng start: deployment already valid, reused",
                      out)
        self.assertIn("[ok] health check", out)
        self.assertNotIn(f"image {DIGEST}", out)

    def test_bring_up_then_postvalidate_pass(self):
        self.wire([blocker_outcome(), pass_outcome()],
                  bring_up_success("created"))
        code, out, err = run_main(["--start", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], KIND_SEARXNG_STARTED)
        self.assertFalse(payload["data"]["reused"])
        self.assertEqual(payload["data"]["verdict"], "PASS")
        self.assertEqual(payload["data"]["bring_up"], {
            "container": NAME, "image": IMAGE_REF,
            "url": "http://127.0.0.1:8888", "action": "created",
            "image_digest": DIGEST})
        self.assertEqual([s["ok"] for s in payload["data"]["steps"]],
                         [True] * 4)
        self.assertEqual(payload["errors"], [])

    def test_bring_up_facts_render_in_human_output(self):
        self.wire([blocker_outcome(), pass_outcome()])
        code, out, err = run_main(["--start"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("searxng start: ok (4/4 checks)", out)
        self.assertIn(f"container {NAME} at http://127.0.0.1:8888 "
                      f"(created; image", out)
        self.assertIn(DIGEST, out)
        self.assertIn("[ok] search query", out)

    def test_bring_up_receives_target_pull_and_real_settings_mount(self):
        _, lifecycle = self.wire([blocker_outcome(), pass_outcome()])
        code, _, err = run_main(
            ["--start", "--json", "--image", "myimg:2", "--name", "myng",
             "--port", "9999", "--bind", "127.0.0.2", "--pull"])
        self.assertEqual((code, err), (0, ""))
        lifecycle.assert_called_once_with(
            SearxngTarget(image="myimg:2", name="myng",
                          bind="127.0.0.2", port=9999),
            pull=True, settings_path=SETTINGS_RESOURCE, verbose=_quiet)

    def test_bring_up_then_postvalidate_fail_is_failed_exit_one(self):
        self.wire([blocker_outcome(), healthz_fail_outcome()])
        code, out, err = run_main(["--start", "--json"])
        self.assertEqual((code, err), (1, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["kind"], KIND_SEARXNG_STARTED)
        self.assertEqual(payload["data"]["verdict"], "FAIL")
        self.assertFalse(payload["data"]["reused"])
        self.assertEqual(payload["data"]["bring_up"]["action"], "created")
        self.assertEqual(payload["errors"][0]["code"],
                         "searxng-healthz-unhealthy")

    def test_postvalidate_fail_human_render_shows_the_failed_step(self):
        self.wire([blocker_outcome(), healthz_fail_outcome()])
        code, out, err = run_main(["--start"])
        self.assertEqual((code, err), (1, ""))
        self.assertIn("searxng start: failed (2/4 checks)", out)
        self.assertIn("[FAIL] health check", out)
        self.assertIn(f"healthz endpoint {BASE}/healthz is not", out)
        self.assertIn("HTTP 503", out)
        # the step carries the message; the duplicate diagnostic is
        # suppressed, so its code is not printed twice
        self.assertNotIn("searxng-healthz-unhealthy", out)

    def test_bring_up_guard_refusal_is_failed_exit_one(self):
        guard = GuardFailure(CODE_PORT_IN_USE,
                             "port 127.0.0.1:8888 is already in use",
                             {"bind": "127.0.0.1", "port": 8888})
        self.wire([blocker_outcome()], brought_up=guard)
        code, out, err = run_main(["--start", "--json"])
        self.assertEqual((code, err), (1, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["errors"][0]["code"], CODE_PORT_IN_USE)
        self.assertNotIn("verdict", payload["data"])
        self.assertNotIn("bring_up", payload["data"])

    def test_start_and_stop_write_no_files(self):
        before = sorted(os.listdir("."))
        self.wire([pass_outcome()])
        run_main(["--start", "--json"])
        with patch("gleam.entries.searxng.tear_down",
                   return_value=absent_noop()):
            run_main(["--stop", "--json"])
        self.assertEqual(sorted(os.listdir(".")), before)


class StopEntryTests(SearxngEntryTestCase):
    """--stop mapping with the lifecycle layer faked at the entry's
    seam: the absent no-op, the stopped-and-removed success, and the
    flag flow into the probe."""

    def wire(self, outcome):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        return stack.enter_context(patch(
            "gleam.entries.searxng.tear_down", return_value=outcome))

    def test_absent_container_is_idempotent_noop(self):
        probe = self.wire(absent_noop())
        code, out, err = run_main(["--stop", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], KIND_SEARXNG_STOPPED)
        self.assertEqual(payload["data"], {"action": "stop",
                                           "container": NAME,
                                           "outcome": "absent-noop"})
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["diagnostics"], [])
        self.assertEqual(payload["warnings"], [])
        probe.assert_called_once_with(NAME, verbose=_quiet)
        human_code, human, _ = run_main(["--stop"])
        self.assertEqual(human_code, 0)
        self.assertIn(f"no container named '{NAME}'; nothing to do",
                      human)

    def test_stopped_and_removed_maps_ok(self):
        self.wire(TearDownOutcome(container=NAME,
                                  action="stopped-and-removed"))
        code, out, err = run_main(["--stop", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["outcome"],
                         "stopped-and-removed")
        human_code, human, _ = run_main(["--stop"])
        self.assertEqual(human_code, 0)
        self.assertIn(f"stopped and removed container '{NAME}'; "
                      f"unrelated containers untouched", human)

    def test_name_flag_flows_into_the_probe(self):
        probe = self.wire(absent_noop())
        code, _, err = run_main(["--stop", "--json", "--name", "other-ng"])
        self.assertEqual((code, err), (0, ""))
        probe.assert_called_once_with("other-ng", verbose=_quiet)


class StopRealLifecycleTests(SearxngEntryTestCase):
    """--stop through the REAL tear-down against a scripted docker:
    the graceful stop-then-rm argv order (never ``rm -f``), the
    foreign same-name refusal, the absent no-op with zero mutating
    calls, and the daemon-unreachable failure (never a no-op)."""

    def wire_docker(self, docker):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(lifecycle_env(docker=docker))
        return stack

    def test_graceful_stop_then_rm_in_order_never_rm_f(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        self.wire_docker(docker)
        code, out, err = run_main(["--stop", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["data"]["outcome"],
                         "stopped-and-removed")
        (stop_call,) = docker.called("stop", NAME)
        (rm_call,) = docker.called("rm", NAME)
        self.assertEqual(stop_call, [DOCKER, "stop", NAME])
        self.assertEqual(rm_call, [DOCKER, "rm", NAME])
        self.assertLess(docker.calls.index(tuple(stop_call)),
                        docker.calls.index(tuple(rm_call)))
        for argv in docker.calls:
            if argv[1] == "rm":
                self.assertEqual(
                    list(argv), [DOCKER, "rm", NAME],
                    f"graceful contract broken: {list(argv)}")

    def test_absent_container_issues_no_mutating_docker_call(self):
        docker = FakeDocker()
        default_script(docker, ps_out=b"")
        self.wire_docker(docker)
        code, out, err = run_main(["--stop", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["data"]["outcome"], "absent-noop")
        for argv in docker.calls:
            self.assertNotIn(argv[1], {"stop", "rm", "run", "start",
                                       "exec", "kill"},
                             f"no-op must stay read-only: {list(argv)}")

    def test_foreign_same_name_container_is_never_stopped(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running",
                       markers_map={IMAGE: SEARXNG_LABELS,
                                    IMG_ID: FOREIGN_LABELS})
        self.wire_docker(docker)
        code, out, err = run_main(["--stop", "--json"])
        self.assertEqual((code, err), (1, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["kind"], KIND_SEARXNG_STOPPED)
        self.assertEqual(payload["errors"][0]["code"],
                         CODE_IMAGE_NOT_SEARXNG)
        self.assertEqual(docker.called("stop"), [])
        self.assertEqual(docker.called("rm"), [])

    def test_docker_unreachable_is_failed_never_a_silent_noop(self):
        docker = FakeDocker()
        docker.on("info", rc=1,
                  err=b"Cannot connect to the Docker daemon")
        self.wire_docker(docker)
        code, out, err = run_main(["--stop", "--json"])
        self.assertEqual((code, err), (1, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["errors"][0]["code"],
                         CODE_DAEMON_UNREACHABLE)
        human_code, human, _ = run_main(["--stop"])
        self.assertEqual(human_code, 1)
        self.assertNotIn("nothing to do", human)
        self.assertIn(CODE_DAEMON_UNREACHABLE, human)


class StartRealLifecycleTests(SearxngEntryTestCase):
    """--start through the REAL lifecycle + validate layers against a
    scripted docker: the create branch's ``docker run`` argv carries
    the package-data settings mount in the exact read-only form, and
    the deployment then validates end to end."""

    def test_bring_up_mounts_the_packaged_settings_read_only(self):
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
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(lifecycle_env(docker=docker,
                                          sock=FakeSocketModule(),
                                          clock=FakeTime()))
        stack.enter_context(patch(f"{VALIDATE}._HEALTHZ_OPENER",
                                  FakeHealthzOpener()))
        FakeSearxngClient.reset(search_result())
        stack.enter_context(patch(f"{VALIDATE}.SearxngClient",
                                  FakeSearxngClient))
        code, out, err = run_main(["--start", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["data"]["reused"])
        self.assertEqual(payload["data"]["bring_up"]["action"],
                         "created")
        self.assertEqual(payload["data"]["verdict"], "PASS")
        (run_call,) = docker.called("run", "-d")
        self.assertEqual(run_call, [
            DOCKER, "run", "-d",
            "--name", NAME,
            "--publish", "127.0.0.1:8888:8080",
            "--env", "FORCE_OWNERSHIP=false",
            "--volume",
            f"{SETTINGS_RESOURCE}:/etc/searxng/settings.yml:ro",
            "--security-opt", "no-new-privileges",
            IMAGE_REF])


class ValidateEntryTests(SearxngEntryTestCase):
    """The real --validate surface: verdict/status/exit mapping,
    envelope shape, human rendering, configured-base flow, zero
    writes (probe outcomes faked at the entry's own seam)."""

    def wire(self, outcome):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        return stack.enter_context(patch(
            "gleam.entries.searxng.run_validate", return_value=outcome))

    def test_pass_envelope_is_ok_exit_zero(self):
        self.wire(pass_outcome())
        code, out, err = run_main(["--validate", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], KIND_SEARXNG_VALIDATED)
        self.assertEqual(payload["data"]["action"], "validate")
        self.assertEqual(payload["data"]["verdict"], "PASS")
        self.assertEqual(payload["data"]["container"], "gleam-searxng")
        self.assertEqual(payload["data"]["search_base"], BASE)
        self.assertEqual(payload["data"]["query"],
                         "debian wizznic upstream")
        self.assertEqual([s["ok"] for s in payload["data"]["steps"]],
                         [True] * 4)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["diagnostics"], [])
        self.assertEqual(payload["warnings"], [])

    def test_fail_envelope_is_failed_exit_one(self):
        outcome = replace(blocker_outcome(), verdict="FAIL")
        self.wire(outcome)
        code, out, err = run_main(["--validate", "--json"])
        self.assertEqual((code, err), (1, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["data"]["verdict"], "FAIL")
        self.assertEqual(payload["errors"][0]["code"],
                         "searxng-docker-unavailable")

    def test_blocker_envelope_is_failed_exit_one(self):
        self.wire(blocker_outcome())
        code, out, err = run_main(["--validate", "--json"])
        self.assertEqual((code, err), (1, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["data"]["verdict"], "BLOCKER")
        self.assertEqual(len(payload["errors"]), 1)

    def test_human_render_prints_verdict_and_step_lines(self):
        self.wire(pass_outcome())
        code, out, err = run_main(["--validate"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("searxng validate: ok (4/4 checks)", out)
        self.assertIn("[ok] Docker available", out)
        self.assertIn(
            f"[ok] health check — GET {BASE}/healthz: HTTP 200 (OK)", out)
        self.assertIn('[ok] search "debian wizznic upstream"', out)

    def test_human_render_prints_blocker_evidence(self):
        self.wire(blocker_outcome())
        code, out, err = run_main(["--validate"])
        self.assertEqual((code, err), (1, ""))
        self.assertIn("searxng validate: failed (0/4 checks)", out)
        self.assertIn("[FAIL] Docker available", out)
        self.assertIn("docker: command not found", out)
        self.assertIn("[ -- ] search", out)  # never-reached steps shown

    def test_env_file_search_base_flows_into_the_probe(self):
        with open(".env", "w", encoding="utf-8") as handle:
            handle.write("GLEAM_SEARCH_BASE=http://127.0.0.1:9999\n")
        probe = self.wire(pass_outcome())
        code, _, err = run_main(["--validate", "--json"])
        self.assertEqual((code, err), (0, ""))
        probe.assert_called_once_with(
            name="gleam-searxng",
            search_base="http://127.0.0.1:9999", verbose=_quiet)

    def test_validate_writes_no_files(self):
        before = sorted(os.listdir("."))
        self.wire(pass_outcome())
        run_main(["--validate", "--json"])
        run_main(["--validate"])
        self.assertEqual(sorted(os.listdir(".")), before)


class ValidateReadOnlyDockerTests(SearxngEntryTestCase):
    """End-to-end through the REAL lifecycle probes: a scripted
    read-only docker answers, and the audit proves --validate never
    issues a mutating docker call."""

    MUTATING = {"run", "create", "start", "stop", "restart", "rm",
                "pull", "push", "kill", "exec", "update", "pause",
                "unpause", "wait", "logs", "build", "commit", "load",
                "import", "save", "tag"}

    def test_validate_never_issues_a_mutating_docker_call(self):
        docker = FakeDocker()
        docker.on("info")
        docker.on("ps", "-a", out=b"gleam-searxng\n")

        def container_inspect(argv):
            if argv[4] == "{{.Image}}":
                return (0, IMG_ID.encode(), b"")
            return (0, b"running", b"")

        docker.on("container", "inspect", "-f",
                  responder=container_inspect)
        docker.on("port", "gleam-searxng", out=b"127.0.0.1:8888")
        docker.on("image", "inspect", "--format", out=SEARXNG_LABELS)
        FakeSearxngClient.reset(search_result())
        with patch(f"{LIFECYCLE}.subprocess",
                   docker.subprocess_module()), \
                patch(f"{LIFECYCLE}.shutil",
                      types.SimpleNamespace(which=lambda command: DOCKER)), \
                patch(f"{VALIDATE}._HEALTHZ_OPENER", FakeHealthzOpener()), \
                patch(f"{VALIDATE}.SearxngClient", FakeSearxngClient):
            code, out, err = run_main(["--validate", "--json"])
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["data"]["verdict"], "PASS")
        self.assertTrue(docker.calls)
        for argv in docker.calls:
            self.assertNotIn(
                argv[1], self.MUTATING,
                f"--validate must stay read-only: {list(argv)}")


class HelpListingTests(unittest.TestCase):
    def test_overview_lists_all_eight_modules(self):
        text = general_help()
        for name in EIGHT_MODULES:
            self.assertIn(name, text)
        self.assertIn("manage the local SearXNG search service", text)

    def test_module_help_is_self_contained(self):
        text = module_help("searxng")
        # self-contained user help: usage line, full flag inventory,
        # zero-storage invariant
        self.assertIn("Usage: gleam searxng", text)
        for token in ("--start", "--stop", "--validate", "--image",
                      "--name", "--port", "--bind", "--pull",
                      "--search-base", "--json", "--env-file"):
            self.assertIn(token, text)
        self.assertIn("never writes", text)
        self.assertNotEqual(text, general_help())


if __name__ == "__main__":
    unittest.main()
