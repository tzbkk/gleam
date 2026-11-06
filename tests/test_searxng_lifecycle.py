"""Offline tests for the guarded docker lifecycle layer.

Zero real docker, zero network, zero file writes: every child
process is faked by patching ``gleam.core.sources.searxng.lifecycle.
subprocess`` with a scripted FakeDocker (which also asserts the
wrapper contract -- no shell, captured output, bounded positive
timeout -- on every single call); ``shutil``/``socket``/``time``
module attributes are faked the same way for discovery, the bind
probe, and the readiness clock. The one real-OS test holds a real
listening socket to prove the bind probe refuses a live listener.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from gleam.core.result import DiagnosticItem, UsageError
from gleam.core.sources.searxng.lifecycle import (
    CODE_BINDING_MISMATCH,
    CODE_DAEMON_UNREACHABLE,
    CODE_DOCKER_COMMAND_FAILED,
    CODE_DOCKER_TIMEOUT,
    CODE_DOCKER_UNAVAILABLE,
    CODE_IMAGE_NOT_SEARXNG,
    CODE_IMAGE_PULL_FAILED,
    CODE_PORT_IN_USE,
    CODE_PORT_UNBINDABLE,
    CODE_READINESS_TIMEOUT,
    BringUpSuccess,
    ContainerState,
    LOG_TAIL_LINES,
    MARKERS_TEMPLATE,
    SearxngTarget,
    WHERE,
    build_run_argv,
    bring_up,
    port_is_free,
    probe_container,
    probe_daemon,
    tear_down,
)

MODULE = "gleam.core.sources.searxng.lifecycle"
DOCKER = "/usr/bin/docker"
IMAGE = "searxng/searxng:latest"
NAME = "gleam-searxng"
IMG_ID = "sha256:cafebabe"
SETTINGS = "/pkg/gleam/data/searxng-settings.yml"
REPODIGEST = "docker.io/searxng/searxng@sha256:aaaa"
REPODIGEST_TEMPLATE = "{{index .RepoDigests 0}}"
ID_TEMPLATE = "{{.Id}}"


def markers_line(source="", title="", description="", env="",
                 entrypoint="") -> bytes:
    return "\t".join((source, title, description, env,
                      entrypoint)).encode()


SEARXNG_LABELS = markers_line(
    source="https://github.com/searxng/searxng", title="SearXNG",
    description="A privacy-respecting, hackable metasearch engine",
    env="PATH=/usr/bin __SEARXNG_VERSION=2026.11.17",
    entrypoint="searxng/docker-entrypoint.sh")
FOREIGN_LABELS = markers_line(
    source="https://github.com/nginx/nginx", title="nginx",
    description="nginx web server", env="PATH=/usr/bin",
    entrypoint="/docker-entrypoint.sh")


class FakeCompleted:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeDocker:
    """Scripted ``subprocess`` seam. Handlers match on the argv
    prefix after the docker binary; the first match wins and later
    registrations take precedence over earlier ones, so a test can
    tighten or break one step of ``default_script`` by registering
    after it."""

    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.handlers = []

    def on(self, *prefix, rc=0, out=b"", err=b"", responder=None):
        self.handlers.insert(
            0, (prefix, responder or (lambda argv: (rc, out, err))))

    def run(self, argv, **kwargs):
        assert "shell" not in kwargs, "the wrapper must never use a shell"
        assert kwargs.get("capture_output") is True, \
            "the wrapper must capture output"
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, (int, float)) and timeout > 0, \
            "every docker call needs a bounded positive timeout"
        argv = tuple(argv)
        self.calls.append(argv)
        self.timeouts.append(timeout)
        rest = argv[1:]
        for prefix, respond in self.handlers:
            if rest[:len(prefix)] == prefix:
                rc, out, err = respond(list(argv))
                return FakeCompleted(rc, out, err)
        raise AssertionError(f"unexpected docker call: {list(argv)}")

    def subprocess_module(self):
        return types.SimpleNamespace(run=self.run,
                                     TimeoutExpired=subprocess.TimeoutExpired)

    def called(self, *prefix):
        return [list(argv) for argv in self.calls
                if argv[1:1 + len(prefix)] == prefix]


class FakeSocket:
    def __init__(self, module):
        self.module = module

    def bind(self, addr):
        self.module.binds.append(addr)
        if self.module.bind_error is not None:
            errno_val, message = self.module.bind_error
            raise OSError(errno_val, message)

    def close(self):
        self.module.closed += 1


class FakeSocketModule:
    """``socket`` stand-in for the bind probe."""

    AF_INET = socket.AF_INET
    SOCK_STREAM = socket.SOCK_STREAM
    gaierror = socket.gaierror

    def __init__(self, bind_error=None, resolve_error=None):
        self.bind_error = bind_error
        self.resolve_error = resolve_error
        self.binds = []
        self.closed = 0

    def getaddrinfo(self, host, port, type=None, flags=0):
        if self.resolve_error is not None:
            raise socket.gaierror(self.resolve_error)
        return [(self.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    def socket(self, family, type):
        return FakeSocket(self)


class FakeTime:
    """Monotonic clock advanced only by sleep (deterministic loop)."""

    def __init__(self, now=0.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@contextlib.contextmanager
def lifecycle_env(docker=None, which=lambda command: DOCKER,
                  sock=None, clock=None):
    with contextlib.ExitStack() as stack:
        if docker is not None:
            stack.enter_context(patch(
                f"{MODULE}.subprocess", docker.subprocess_module()))
        stack.enter_context(patch(
            f"{MODULE}.shutil", types.SimpleNamespace(which=which)))
        if sock is not None:
            stack.enter_context(patch(f"{MODULE}.socket", sock))
        if clock is not None:
            stack.enter_context(patch(f"{MODULE}.time", clock))
        yield


def default_script(docker, *, markers_map=None, digest=REPODIGEST.encode(),
                   image=IMAGE, name=NAME, ps_out=b"",
                   port_out=b"127.0.0.1:8888", state=b"running",
                   exec_rc=0, pull_rc=0):
    """Install the happy-path handlers; individual tests tighten or
    break one step via earlier, more specific registrations."""
    if markers_map is None:
        markers_map = {image: SEARXNG_LABELS, IMG_ID: SEARXNG_LABELS}

    docker.on("info")

    def image_inspect(argv):
        template, ref = argv[4], argv[-1]
        if template == MARKERS_TEMPLATE:
            out = markers_map.get(ref)
            if out is None:
                return (1, b"", b"Error: No such image")
            return (0, out, b"")
        if template == REPODIGEST_TEMPLATE:
            return (0, digest, b"") if digest else (1, b"", b"no digests")
        if template == ID_TEMPLATE:
            return (0, b"sha256:beef", b"")
        raise AssertionError(f"unexpected template {template}")

    docker.on("image", "inspect", "--format", responder=image_inspect)

    docker.on("ps", "-a", out=ps_out)

    def container_inspect(argv):
        if argv[4] == "{{.Image}}":
            return (0, IMG_ID.encode(), b"")
        return (0, state, b"")

    docker.on("container", "inspect", "-f", responder=container_inspect)
    docker.on("port", name, out=port_out)
    docker.on("run", "-d")
    docker.on("exec", name, rc=exec_rc)
    docker.on("start", name)
    docker.on("logs", "--tail", str(LOG_TAIL_LINES),
              out=b"[uwsgi] worker 1 ready")
    docker.on("stop", name)
    docker.on("rm", name)
    docker.on("pull", image, rc=pull_rc)


def run_bring_up(docker, target=None, sock=None, clock=None, **kwargs):
    sock = sock or FakeSocketModule()
    clock = clock or FakeTime()
    with lifecycle_env(docker, sock=sock, clock=clock):
        return bring_up(target or SearxngTarget(), **kwargs), sock, clock


class TargetTests(unittest.TestCase):
    def test_defaults_and_derived_urls(self):
        target = SearxngTarget()
        self.assertEqual(target.image, IMAGE)
        self.assertEqual(target.name, NAME)
        self.assertEqual(target.host_binding, "127.0.0.1:8888")
        self.assertEqual(target.url, "http://127.0.0.1:8888")

    def test_invalid_inputs_raise_usage_error(self):
        bad = [
            {"image": ""}, {"image": "  "}, {"image": 5},
            {"name": ""}, {"bind": ""}, {"bind": None},
            {"port": "8888"}, {"port": 0}, {"port": 70000},
            {"port": True}, {"port": 1.5},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(UsageError):
                    SearxngTarget(**overrides)


class GuardFailureTests(unittest.TestCase):
    def test_to_diagnostic_maps_envelope_fields(self):
        from gleam.core.sources.searxng.lifecycle import GuardFailure
        failure = GuardFailure(CODE_PORT_IN_USE, "busy",
                               {"bind": "127.0.0.1"})
        item = failure.to_diagnostic()
        self.assertIsInstance(item, DiagnosticItem)
        self.assertEqual(item.code, CODE_PORT_IN_USE)
        self.assertEqual(item.message, "busy")
        self.assertEqual(item.where, WHERE)
        self.assertEqual(item.details, {"bind": "127.0.0.1"})


class DiscoveryAndDaemonTests(unittest.TestCase):
    def test_wrapper_contract_absolute_path_no_shell(self):
        docker = FakeDocker()
        docker.on("info")
        with lifecycle_env(docker):
            ok = probe_daemon()
        self.assertEqual(ok.docker_path, DOCKER)
        self.assertEqual(docker.calls, [(DOCKER, "info")])
        self.assertTrue(all(t > 0 for t in docker.timeouts))

    def test_docker_missing_is_friendly_blocker(self):
        docker = FakeDocker()
        with lifecycle_env(docker, which=lambda command: None):
            failure = probe_daemon()
        self.assertEqual(failure.code, CODE_DOCKER_UNAVAILABLE)
        self.assertIn("Install Docker Engine", failure.message)
        self.assertIn("https://docs.docker.com/engine/install/",
                      failure.message)
        self.assertEqual(docker.calls, [])

    def test_permission_denied_classification(self):
        docker = FakeDocker()
        docker.on("info", rc=1, err=b"got permission denied while trying "
                                   b"to connect to the Docker daemon socket")
        with lifecycle_env(docker):
            failure = probe_daemon()
        self.assertEqual(failure.code, CODE_DAEMON_UNREACHABLE)
        self.assertIn("docker group", failure.message)

    def test_daemon_not_running_classification(self):
        docker = FakeDocker()
        docker.on("info", rc=1, err=b"Cannot connect to the Docker "
                                   b"daemon. Is the docker daemon running?")
        with lifecycle_env(docker):
            failure = probe_daemon()
        self.assertEqual(failure.code, CODE_DAEMON_UNREACHABLE)
        self.assertIn("not reachable", failure.message)
        self.assertIn("systemctl start docker", failure.message)

    def test_generic_failure_keeps_stderr_tail(self):
        docker = FakeDocker()
        docker.on("info", rc=1, err=b"\n".join(
            b"line%d something odd" % i for i in range(1, 8)))
        with lifecycle_env(docker):
            failure = probe_daemon()
        self.assertEqual(failure.code, CODE_DAEMON_UNREACHABLE)
        self.assertIn("docker info failed", failure.message)
        self.assertIn("line7 something odd", failure.message)
        self.assertNotIn("line1 something odd", failure.message)
        self.assertNotIn("line2 something odd", failure.message)

    def test_timeout_and_missing_executable(self):
        def raise_timeout(argv):
            raise subprocess.TimeoutExpired("docker", 9)

        docker = FakeDocker()
        docker.on("info", responder=raise_timeout)
        with lifecycle_env(docker):
            failure = probe_daemon()
        self.assertEqual(failure.code, CODE_DOCKER_TIMEOUT)

        def raise_missing(argv):
            raise FileNotFoundError(DOCKER)

        docker = FakeDocker()
        docker.on("info", responder=raise_missing)
        with lifecycle_env(docker):
            failure = probe_daemon()
        self.assertEqual(failure.code, CODE_DOCKER_UNAVAILABLE)


class ImageIdentityTests(unittest.TestCase):
    def test_upstream_oci_labels_verify(self):
        docker = FakeDocker()
        default_script(docker)
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)

    def test_description_label_verifies(self):
        docker = FakeDocker()
        default_script(docker, markers_map={
            IMAGE: markers_line(
                source="https://github.com/searxng/searxng", title="",
                description="A SearXNG metasearch engine")})
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)

    def test_env_entrypoint_fallback_verifies(self):
        docker = FakeDocker()
        default_script(docker, markers_map={
            IMAGE: markers_line(env="PATH=/bin __SEARXNG_VERSION=1.0.0",
                                entrypoint="/usr/local/searxng/docker-entrypoint.sh")})
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)

    def test_foreign_labels_refused(self):
        docker = FakeDocker()
        default_script(docker, markers_map={IMAGE: FOREIGN_LABELS})
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_IMAGE_NOT_SEARXNG)
        self.assertIn("refusing to run it", result.message)
        self.assertIn(IMAGE, result.message)
        for forbidden in ("ps", "run", "start", "stop", "rm"):
            self.assertEqual(docker.called(forbidden), [])

    def test_matching_title_with_wrong_source_refused(self):
        docker = FakeDocker()
        default_script(docker, markers_map={
            IMAGE: markers_line(source="https://github.com/evil/clone",
                                title="SearXNG")})
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_IMAGE_NOT_SEARXNG)

    def test_env_marker_without_entrypoint_refused(self):
        docker = FakeDocker()
        default_script(docker, markers_map={
            IMAGE: markers_line(env="PATH=/bin __SEARXNG_VERSION=1.0.0",
                                entrypoint="/bin/sh")})
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_IMAGE_NOT_SEARXNG)

    def test_markers_decode_utf8_replace(self):
        docker = FakeDocker()
        raw = b"https://github.com/searxng/searxng\tSearX\xffNG\t" \
              b"a searxng metasearch engine\t\t"
        default_script(docker, markers_map={IMAGE: raw})
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)


class PullTests(unittest.TestCase):
    def test_missing_image_is_pulled_then_used(self):
        docker = FakeDocker()
        pulled = {"done": False}
        default_script(docker)

        def after_pull(argv):
            pulled["done"] = True
            return (0, b"", b"")

        docker.on("pull", IMAGE, responder=after_pull)
        state = {"seen": 0}

        def markers(argv):
            if argv[-1] == IMAGE and not pulled["done"]:
                return (1, b"", b"Error: No such image")
            return (0, SEARXNG_LABELS, b"")

        def image_inspect(argv):
            if argv[4] == MARKERS_TEMPLATE:
                return markers(argv)
            return (0, b"", b"")

        docker.on("image", "inspect", "--format",
                  responder=image_inspect)
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertTrue(pulled["done"])
        self.assertEqual(docker.called("pull"),
                         [[DOCKER, "pull", IMAGE]])

    def test_present_image_is_not_pulled(self):
        docker = FakeDocker()
        default_script(docker)
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertEqual(docker.called("pull"), [])

    def test_force_pull_even_when_present(self):
        docker = FakeDocker()
        default_script(docker)
        result, _, _ = run_bring_up(docker, pull=True)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertEqual(docker.called("pull"), [[DOCKER, "pull", IMAGE]])

    def test_pull_failure_refused(self):
        docker = FakeDocker()
        default_script(docker, markers_map={}, pull_rc=1)
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_IMAGE_PULL_FAILED)
        self.assertIn(f"docker pull failed for {IMAGE}", result.message)
        self.assertEqual(docker.called("run"), [])


class ContainerGuardTests(unittest.TestCase):
    def test_reuse_running_container_without_touching(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertEqual(result.action, "reused-running")
        self.assertEqual(docker.called("run"), [])
        self.assertEqual(docker.called("start"), [])

    def test_reuse_stopped_container_starts_it(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"exited")
        sock = FakeSocketModule()
        result, _, _ = run_bring_up(docker, sock=sock)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertEqual(result.action, "started-existing")
        self.assertEqual(docker.called("start"), [[DOCKER, "start", NAME]])
        self.assertEqual(docker.called("run"), [])
        self.assertEqual(sock.binds, [], "no port probe when reusing")

    def test_foreign_same_name_container_refused(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(),
                       markers_map={IMAGE: SEARXNG_LABELS,
                                    IMG_ID: FOREIGN_LABELS})
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_IMAGE_NOT_SEARXNG)
        self.assertIn("refusing to touch it", result.message)
        self.assertIn(IMG_ID, result.message)
        for forbidden in ("run", "start", "stop", "rm", "exec"):
            self.assertEqual(docker.called(forbidden), [])

    def test_binding_mismatch_refused(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(),
                       port_out=b"0.0.0.0:8080")
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_BINDING_MISMATCH)
        self.assertIn("0.0.0.0:8080 -> 8080", result.message)
        self.assertIn("wanted 127.0.0.1:8888", result.message)

    def test_missing_binding_refused(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), port_out=b"")
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_BINDING_MISMATCH)
        self.assertIn("none -> 8080", result.message)

    def test_container_filter_is_exact_name_anchored(self):
        docker = FakeDocker()
        default_script(docker, ps_out=b"ogleam-searxng")
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)
        filter_arg = docker.called("ps", "-a")[0][4]
        self.assertEqual(filter_arg, "name=^/gleam-searxng$")
        self.assertEqual(docker.called("run"), [[
            DOCKER, "run", "-d", "--name", NAME,
            "--publish", "127.0.0.1:8888:8080",
            "--env", "FORCE_OWNERSHIP=false",
            "--security-opt", "no-new-privileges", IMAGE]])


class PortGuardTests(unittest.TestCase):
    def free_check(self, sock):
        with lifecycle_env(sock=sock):
            return port_is_free("127.0.0.1", 8888), sock

    def test_free_when_bind_succeeds(self):
        sock = FakeSocketModule()
        failure, sock = self.free_check(sock)
        self.assertIsNone(failure)
        self.assertEqual(sock.binds, [("127.0.0.1", 8888)])
        self.assertEqual(sock.closed, 1)

    def test_in_use_refused(self):
        sock = FakeSocketModule(
            bind_error=(errno.EADDRINUSE, "Address already in use"))
        failure, _ = self.free_check(sock)
        self.assertEqual(failure.code, CODE_PORT_IN_USE)
        self.assertIn("already in use", failure.message)
        self.assertIn("refusing to bind", failure.message)

    def test_unbindable_address_refused(self):
        sock = FakeSocketModule(
            bind_error=(errno.EADDRNOTAVAIL, "Cannot assign requested "
                                             "address"))
        failure, _ = self.free_check(sock)
        self.assertEqual(failure.code, CODE_PORT_UNBINDABLE)

    def test_unresolvable_bind_refused(self):
        sock = FakeSocketModule(resolve_error="name resolution failure")
        failure, _ = self.free_check(sock)
        self.assertEqual(failure.code, CODE_PORT_UNBINDABLE)
        self.assertIn("cannot resolve", failure.message)

    def test_busy_port_never_reaches_docker_run(self):
        docker = FakeDocker()
        default_script(docker)
        sock = FakeSocketModule(
            bind_error=(errno.EADDRINUSE, "Address already in use"))
        result, _, _ = run_bring_up(docker, sock=sock)
        self.assertEqual(result.code, CODE_PORT_IN_USE)
        self.assertEqual(docker.called("run"), [])

    def test_real_listener_blocks_bind(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        failure = port_is_free("127.0.0.1", port)
        self.assertEqual(failure.code, CODE_PORT_IN_USE)


class BringUpArgvTests(unittest.TestCase):
    def test_create_reaches_docker_run_with_exact_argv(self):
        docker = FakeDocker()
        default_script(docker)
        result, _, _ = run_bring_up(docker, settings_path=SETTINGS)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertEqual(docker.called("run", "-d"), [[
            DOCKER, "run", "-d", "--name", NAME,
            "--publish", "127.0.0.1:8888:8080",
            "--env", "FORCE_OWNERSHIP=false",
            "--volume", f"{SETTINGS}:/etc/searxng/settings.yml:ro",
            "--security-opt", "no-new-privileges", IMAGE]])

    def test_argv_builder_without_settings_has_no_mount(self):
        argv = build_run_argv(DOCKER, SearxngTarget())
        self.assertNotIn("--volume", argv)
        self.assertEqual(argv[-1], IMAGE)
        with self.assertRaises(UsageError):
            build_run_argv(DOCKER, SearxngTarget(), settings_path=" ")

    def test_run_failure_surfaces_stderr_tail(self):
        docker = FakeDocker()
        default_script(docker)
        docker.on("run", "-d", rc=125, err=b"port is already allocated")
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_DOCKER_COMMAND_FAILED)
        self.assertIn("port is already allocated", result.message)

    def test_success_fields_and_digest_chain(self):
        docker = FakeDocker()
        default_script(docker)
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.container, NAME)
        self.assertEqual(result.image, IMAGE)
        self.assertEqual(result.url, "http://127.0.0.1:8888")
        self.assertEqual(result.action, "created")
        self.assertEqual(result.image_digest, REPODIGEST)

        docker = FakeDocker()
        default_script(docker, digest=None)
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.image_digest, "sha256:beef")

    def test_digest_falls_back_to_image_ref(self):
        docker = FakeDocker()
        default_script(docker, digest=None)

        def image_inspect(argv):
            if argv[4] == MARKERS_TEMPLATE:
                return (0, SEARXNG_LABELS, b"")
            return (1, b"", b"")

        docker.on("image", "inspect", "--format",
                  responder=image_inspect)
        result, _, _ = run_bring_up(docker)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertEqual(result.image_digest, IMAGE)


class ReadinessTests(unittest.TestCase):
    def test_healthy_on_first_probe(self):
        docker = FakeDocker()
        default_script(docker)
        clock = FakeTime()
        result, _, clock = run_bring_up(docker, clock=clock)
        self.assertIsInstance(result, BringUpSuccess)
        self.assertEqual(len(docker.called("exec", NAME)), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(
            docker.called("exec", NAME)[0],
            [DOCKER, "exec", NAME, "wget", "-q", "-T", "3", "-O", "-",
             "http://127.0.0.1:8080/healthz"])

    def test_readiness_timeout_leaves_container_running(self):
        docker = FakeDocker()
        default_script(docker, exec_rc=1)
        result, _, clock = run_bring_up(docker)
        self.assertEqual(result.code, CODE_READINESS_TIMEOUT)
        self.assertIn("left running for inspection", result.message)
        self.assertEqual(result.details["waited_s"], 46.0)
        self.assertEqual(result.details["log_excerpt"],
                         "[uwsgi] worker 1 ready")
        self.assertEqual(len(docker.called("exec", NAME)), 24)
        self.assertEqual(len(clock.sleeps), 23)
        self.assertTrue(all(s == 2.0 for s in clock.sleeps))
        self.assertEqual(docker.called("logs"),
                         [[DOCKER, "logs", "--tail", "20", NAME]])

    def test_exec_probe_timeout_is_honest(self):
        def raise_timeout(argv):
            raise subprocess.TimeoutExpired("exec", 15)

        docker = FakeDocker()
        default_script(docker)
        docker.on("exec", NAME, responder=raise_timeout)
        result, _, _ = run_bring_up(docker)
        self.assertEqual(result.code, CODE_DOCKER_TIMEOUT)
        self.assertEqual(docker.called("logs"), [])


class ProbeContainerTests(unittest.TestCase):
    def test_absent_container_is_not_a_failure(self):
        docker = FakeDocker()
        default_script(docker, ps_out=b"")
        with lifecycle_env(docker):
            state = probe_container(NAME)
        self.assertIsInstance(state, ContainerState)
        self.assertIs(state.present, False)
        self.assertIs(state.running, False)

    def test_present_running_searxng_state(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        with lifecycle_env(docker):
            state = probe_container(NAME)
        self.assertIs(state.present, True)
        self.assertIs(state.running, True)
        self.assertEqual(state.image_id, IMG_ID)
        self.assertEqual(state.host_bindings, ("127.0.0.1:8888",))
        self.assertIs(state.searxng_image, True)

    def test_foreign_container_flagged(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(),
                       markers_map={IMG_ID: FOREIGN_LABELS})
        with lifecycle_env(docker):
            state = probe_container(NAME)
        self.assertIs(state.present, True)
        self.assertIs(state.searxng_image, False)

    def test_daemon_down_is_a_classified_failure(self):
        docker = FakeDocker()
        default_script(docker)
        docker.on("info", rc=1, err=b"Cannot connect to the Docker daemon")
        with lifecycle_env(docker):
            failure = probe_container(NAME)
        self.assertEqual(failure.code, CODE_DAEMON_UNREACHABLE)

    def test_empty_name_is_usage_error(self):
        with self.assertRaises(UsageError):
            probe_container(" ")


class TearDownTests(unittest.TestCase):
    def test_absent_is_idempotent_noop(self):
        docker = FakeDocker()
        default_script(docker, ps_out=b"")
        with lifecycle_env(docker):
            outcome = tear_down(NAME)
        self.assertEqual(outcome.action, "absent-noop")
        self.assertEqual(outcome.container, NAME)
        self.assertEqual(docker.called("stop"), [])
        self.assertEqual(docker.called("rm"), [])

    def test_running_stops_then_removes_gracefully(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"running")
        with lifecycle_env(docker):
            outcome = tear_down(NAME)
        self.assertEqual(outcome.action, "stopped-and-removed")
        self.assertEqual(docker.called("stop"), [[DOCKER, "stop", NAME]])
        self.assertEqual(docker.called("rm"), [[DOCKER, "rm", NAME]])
        stop_at = docker.calls.index((DOCKER, "stop", NAME))
        rm_at = docker.calls.index((DOCKER, "rm", NAME))
        self.assertLess(stop_at, rm_at)

    def test_stopped_container_removed_without_stop(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"exited")
        with lifecycle_env(docker):
            outcome = tear_down(NAME)
        self.assertEqual(outcome.action, "stopped-and-removed")
        self.assertEqual(docker.called("stop"), [])
        self.assertEqual(docker.called("rm"), [[DOCKER, "rm", NAME]])

    def test_foreign_container_never_touched(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(),
                       markers_map={IMG_ID: FOREIGN_LABELS})
        with lifecycle_env(docker):
            failure = tear_down(NAME)
        self.assertEqual(failure.code, CODE_IMAGE_NOT_SEARXNG)
        self.assertIn("refusing to touch it", failure.message)
        self.assertEqual(docker.called("stop"), [])
        self.assertEqual(docker.called("rm"), [])

    def test_daemon_unreachable_is_failure_not_noop(self):
        docker = FakeDocker()
        default_script(docker)
        docker.on("info", rc=1, err=b"Cannot connect to the Docker daemon")
        with lifecycle_env(docker):
            failure = tear_down(NAME)
        self.assertEqual(failure.code, CODE_DAEMON_UNREACHABLE)

    def test_rm_failure_surfaced(self):
        docker = FakeDocker()
        default_script(docker, ps_out=NAME.encode(), state=b"exited")
        docker.on("rm", NAME, rc=1, err=b"cannot remove")
        with lifecycle_env(docker):
            failure = tear_down(NAME)
        self.assertEqual(failure.code, CODE_DOCKER_COMMAND_FAILED)
        self.assertIn("cannot remove", failure.message)

    def test_empty_name_is_usage_error(self):
        with self.assertRaises(UsageError):
            tear_down("")


class StatelessTests(unittest.TestCase):
    def test_no_files_created_by_any_lifecycle_call(self):
        def snapshot(root):
            entries = []
            for dirpath, dirnames, filenames in os.walk(root):
                rel = os.path.relpath(dirpath, root)
                entries.append(rel)
                entries.extend(os.path.join(rel, name)
                               for name in filenames)
            return sorted(entries)

        happy = FakeDocker()
        default_script(happy)
        foreign_image = FakeDocker()
        default_script(foreign_image, markers_map={IMAGE: FOREIGN_LABELS})
        foreign_container = FakeDocker()
        default_script(foreign_container, ps_out=NAME.encode(),
                       markers_map={IMG_ID: FOREIGN_LABELS})
        reuse = FakeDocker()
        default_script(reuse, ps_out=NAME.encode(), state=b"exited")
        teardown = FakeDocker()
        default_script(teardown, ps_out=NAME.encode(), state=b"running")

        with tempfile.TemporaryDirectory() as tmp:
            before = snapshot(tmp)
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                with lifecycle_env(happy, sock=FakeSocketModule(),
                                   clock=FakeTime()):
                    bring_up(SearxngTarget(), settings_path=SETTINGS)
                    probe_daemon()
                    probe_container(NAME)
                with lifecycle_env(foreign_image):
                    bring_up(SearxngTarget())
                with lifecycle_env(foreign_container):
                    bring_up(SearxngTarget())
                port_busy = FakeDocker()
                default_script(port_busy)
                with lifecycle_env(port_busy, sock=FakeSocketModule(
                        bind_error=(errno.EADDRINUSE, "busy"))):
                    bring_up(SearxngTarget())
                with lifecycle_env(reuse, sock=FakeSocketModule(),
                                   clock=FakeTime()):
                    bring_up(SearxngTarget())
                    tear_down(NAME)
            finally:
                os.chdir(previous)
            self.assertEqual(snapshot(tmp), before)
            self.assertEqual(before, ["."])


if __name__ == "__main__":
    unittest.main()
