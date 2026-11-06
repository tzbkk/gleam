# noqa: SIZE_OK — plan-mandated single module: the binding spec (todo 8)
# requires every start.sh/stop.sh guard in this one file because todos
# 9/10 consume it as a single import seam; siblings client.py and
# pgdriver.py are the same size class.
"""Guarded docker lifecycle for gleam's self-hosted SearXNG container.

Faithful pure-stdlib port of the guard chain in
``scripts/searxng/start.sh`` and ``scripts/searxng/stop.sh`` (those
scripts are deleted by a later todo; this module replaces them).
Docker is driven through the CLI only -- no docker-py, no shell:
every child process is a flat argv list run with a bounded timeout
and captured output decoded as UTF-8 with ``errors="replace"``.

The module is fully stateless: it writes no file anywhere, and the
only side effects are the docker commands themselves.

Guard chain, in order -- every step refuses before the next runs:

1. **Docker discovery**: ``shutil.which("docker")``; a missing binary
   is a friendly BLOCKER-style refusal (the udd-driver-unavailable
   analogy) pointing at the install docs.
2. **Daemon pre-check**: ``docker info``; failures are classified
   (permission denied / daemon not reachable / other with the stderr
   tail kept) exactly like the shell preflight.
3. **Image identity**: ``docker image inspect --format`` with the
   script's Go template (source/title labels, env, entrypoint; the
   description label is added to match the plan's wording). Primary
   signal: the upstream OCI labels -- source is the searxng
   repository and the title or description labels say SearXNG.
   Fallback for rebuilt images without labels: the image's
   ``__SEARXNG_VERSION=`` env marker plus an entrypoint matching
   ``*searxng*entrypoint.sh*``. A non-SearXNG image is refused,
   whatever name requested it.
4. **Pull only when missing**: the image is pulled when the inspect
   fails or ``pull=True`` forces it, then the identity check runs
   (post-pull markers, exactly the script's order).
5. **Same-named-container guard**: ``docker ps -a --filter
   name=^/<name>$`` (plus an exact-name line comparison, because the
   filter is a regex); an existing container under the requested
   name must run a verified SearXNG image or nothing is touched.
6. **Existing-container reuse**: a verified container whose host
   binding (``docker port <name> 8080``, first line) matches the
   configured bind/port is reused -- ``docker start``ed when
   stopped, left alone when running; any binding mismatch (including
   no binding) is refused.
7. **Port pre-check**: with the name free, the bind target is probed
   by ``socket.bind((bind, port))`` then close -- never listened, so
   no TIME_WAIT is left; equivalent-or-stricter than the shell's
   ``/dev/tcp`` connect probe on every OS (any holder of the port
   blocks the bind, even one that refuses connections).
8. **Bring-up**: ``docker run -d`` with the script's exact argv
   shape -- name, ``--publish bind:port:8080``, ``FORCE_OWNERSHIP=
   false``, the read-only settings.yml mount when a path is given,
   ``--security-opt no-new-privileges``.
9. **In-container readiness**: the script's exact probe --
   ``docker exec <name> wget -q -T 3 -O -
   http://127.0.0.1:8080/healthz`` -- polled at a fixed interval
   under a bounded wait. On timeout the container is LEFT RUNNING
   for inspection and the refusal carries the ``docker logs --tail
   20`` excerpt.

``tear_down`` ports stop.sh: absent -> idempotent no-op; a foreign
same-named container is refused untouched; otherwise a graceful
``docker stop`` (only when running) then ``docker rm`` -- never
``docker rm -f``.

Offline test seams (module attributes, like the pgdriver pattern):
``shutil`` (which), ``subprocess`` (run), ``socket`` (bind probe),
``time`` (readiness monotonic clock + sleep). Usage mistakes (empty
image/name/bind, non-int or out-of-range port) raise ``UsageError``
(exit 2 at the entry); every runtime refusal is a typed
:class:`GuardFailure` whose code/message can surface in a Result
envelope via :meth:`GuardFailure.to_diagnostic`.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gleam.core.result import DiagnosticItem, UsageError
from gleam.core.verbose import _quiet

__all__ = [
    "CODE_BINDING_MISMATCH",
    "CODE_DAEMON_UNREACHABLE",
    "CODE_DOCKER_COMMAND_FAILED",
    "CODE_DOCKER_TIMEOUT",
    "CODE_DOCKER_UNAVAILABLE",
    "CODE_IMAGE_NOT_SEARXNG",
    "CODE_IMAGE_PULL_FAILED",
    "CODE_PORT_IN_USE",
    "CODE_PORT_UNBINDABLE",
    "CODE_READINESS_TIMEOUT",
    "BringUpSuccess",
    "ContainerState",
    "CONTAINER_PORT",
    "DaemonOk",
    "GuardFailure",
    "LifecyclePolicy",
    "MARKERS_TEMPLATE",
    "probe_container",
    "probe_daemon",
    "port_is_free",
    "build_run_argv",
    "bring_up",
    "tear_down",
    "SearxngTarget",
    "SETTINGS_MOUNT",
    "TearDownOutcome",
    "WHERE",
]

#: Stable ``where`` marker for every diagnostic this module emits.
WHERE = "gleam.core.sources.searxng.lifecycle"

DOCKER_COMMAND = "docker"
#: Container-side port fixed by the searxng image (EXPOSE / GRANIAN).
CONTAINER_PORT = 8080
CONTAINER_HEALTH_URL = f"http://127.0.0.1:{CONTAINER_PORT}/healthz"
SETTINGS_MOUNT = "/etc/searxng/settings.yml:ro"

CODE_DOCKER_UNAVAILABLE = "searxng-docker-unavailable"
CODE_DAEMON_UNREACHABLE = "searxng-docker-daemon-unreachable"
CODE_IMAGE_NOT_SEARXNG = "searxng-image-not-searxng"
CODE_IMAGE_PULL_FAILED = "searxng-image-pull-failed"
CODE_BINDING_MISMATCH = "searxng-binding-mismatch"
CODE_PORT_IN_USE = "searxng-port-in-use"
CODE_PORT_UNBINDABLE = "searxng-port-unbindable"
CODE_READINESS_TIMEOUT = "searxng-readiness-timeout"
CODE_DOCKER_TIMEOUT = "searxng-docker-timeout"
CODE_DOCKER_COMMAND_FAILED = "searxng-docker-command-failed"

DOCKER_INSTALL_HINT = (
    "docker: command not found. Install Docker Engine first:\n"
    "  https://docs.docker.com/engine/install/")

#: The script's Go template, verbatim, plus the description label
#: the plan wording names; five tab-separated fields.
MARKERS_TEMPLATE = (
    '{{index .Config.Labels "org.opencontainers.image.source"}}\t'
    '{{index .Config.Labels "org.opencontainers.image.title"}}\t'
    '{{index .Config.Labels "org.opencontainers.image.description"}}\t'
    '{{join .Config.Env " "}}\t'
    '{{join .Config.Entrypoint " "}}')

SEARXNG_SOURCE_LABEL = "https://github.com/searxng/searxng"
SEARXNG_TITLE_LABEL = "SearXNG"
SEARXNG_NAME_TOKEN = "searxng"
SEARXNG_ENV_MARKER = "__SEARXNG_VERSION="
SEARXNG_ENTRYPOINT_GLOB = "*searxng*entrypoint.sh*"

#: The script's exact in-container probe arguments.
WGET_ARGS = ("wget", "-q", "-T", "3", "-O", "-")
LOG_TAIL_LINES = 20


@dataclass(frozen=True)
class LifecyclePolicy:
    """Per-command timeouts (seconds) and readiness-loop bounds."""

    probe_timeout_s: float = 30.0
    pull_timeout_s: float = 600.0
    run_timeout_s: float = 60.0
    exec_timeout_s: float = 15.0
    stop_timeout_s: float = 60.0
    wait_s: float = 45.0
    probe_interval_s: float = 2.0


@dataclass(frozen=True)
class GuardFailure:
    """One guard's refusal: stable code, human message, details.

    ``to_diagnostic`` maps 1:1 onto a Result envelope item so the
    entries (todos 9/10) can surface refusals without rewording.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_diagnostic(self) -> DiagnosticItem:
        return DiagnosticItem(code=self.code, message=self.message,
                              where=WHERE, details=self.details)


@dataclass(frozen=True)
class SearxngTarget:
    """The four bring-up knobs as plain values (defaults mirror the
    CLI spec from todo 7; the config domain owns live values)."""

    image: str = "searxng/searxng:latest"
    name: str = "gleam-searxng"
    bind: str = "127.0.0.1"
    port: int = 8888

    def __post_init__(self) -> None:
        for label in ("image", "name", "bind"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise UsageError(
                    f"{label} must be a non-empty string; got {value!r}")
        if isinstance(self.port, bool) or not isinstance(self.port, int) \
                or not 1 <= self.port <= 65535:
            raise UsageError(
                f"port must be an integer in 1..65535; got {self.port!r}")

    @property
    def host_binding(self) -> str:
        return f"{self.bind}:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.bind}:{self.port}"


@dataclass(frozen=True)
class DaemonOk:
    """Docker discovery + daemon pre-check passed."""

    docker_path: str


@dataclass(frozen=True)
class ContainerState:
    """Read-only state probe for one container name (todo 9/10)."""

    name: str
    present: bool
    running: bool = False
    image_id: str = ""
    host_bindings: tuple[str, ...] = ()
    searxng_image: bool = False


@dataclass(frozen=True)
class BringUpSuccess:
    """The guarded chain completed and the container answers /healthz.

    ``action`` distinguishes the three script outcomes:
    ``created`` (docker run), ``started-existing`` (docker start on
    a stopped verified container), ``reused-running`` (already up).
    """

    container: str
    image: str
    url: str
    action: str
    image_digest: str


@dataclass(frozen=True)
class TearDownOutcome:
    """Stop outcome: ``absent-noop`` or ``stopped-and-removed``."""

    container: str
    action: str


@dataclass(frozen=True)
class CmdResult:
    """One captured docker invocation (output already decoded)."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ImageMarkers:
    """The five identity fields extracted by ``docker image inspect``."""

    source: str
    title: str
    description: str
    env: str
    entrypoint: str

    @classmethod
    def from_stdout(cls, stdout: str) -> "ImageMarkers":
        parts = (stdout.rstrip("\n").split("\t") + [""] * 5)[:5]
        return cls(*parts)

    def verifies_as_searxng(self) -> bool:
        """Primary: upstream OCI labels (source repository plus a
        title/description that says SearXNG); fallback: the image's
        SearXNG env marker plus its searxng entrypoint script."""
        if self.source == SEARXNG_SOURCE_LABEL and (
                self.title == SEARXNG_TITLE_LABEL
                or SEARXNG_NAME_TOKEN in self.description.lower()):
            return True
        return (SEARXNG_ENV_MARKER in self.env
                and fnmatch.fnmatch(self.entrypoint,
                                    SEARXNG_ENTRYPOINT_GLOB))


def _tail(text: str, limit: int = 400) -> str:
    return (text or "").strip()[-limit:]


def _last_lines(text: str, count: int) -> list[str]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-count:]


def _run(docker_path: str, args: tuple[str, ...],
         timeout_s: float,
         verbose: Callable[[str], None] = _quiet) -> CmdResult | GuardFailure:
    """The single child-process seam: flat argv, no shell, bounded
    timeout, captured output decoded UTF-8 with errors replaced.
    ``verbose`` narrates one ``$ docker ...`` intent line per call
    (argv[0] normalized to its basename; argv is public data) plus a
    one-line outcome."""
    argv = [docker_path, *args]
    verbose("$ " + " ".join([os.path.basename(docker_path), *args]))
    try:
        proc = subprocess.run(argv, capture_output=True,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        verbose("-> timeout")
        return GuardFailure(
            CODE_DOCKER_TIMEOUT,
            f"docker {' '.join(args[:2])} timed out after {timeout_s}s",
            details={"argv": argv})
    except FileNotFoundError as exc:
        verbose(f"-> error: {exc}")
        return GuardFailure(CODE_DOCKER_UNAVAILABLE,
                            DOCKER_INSTALL_HINT,
                            details={"command": DOCKER_COMMAND})
    except OSError as exc:
        verbose(f"-> error: {exc}")
        return GuardFailure(
            CODE_DOCKER_COMMAND_FAILED,
            f"cannot execute {docker_path}: {exc}",
            details={"argv": argv})
    verbose(f"-> exit {proc.returncode}")
    return CmdResult(argv=tuple(argv), exit_code=proc.returncode,
                     stdout=proc.stdout.decode("utf-8", errors="replace"),
                     stderr=proc.stderr.decode("utf-8", errors="replace"))


def _command_failed(step: str, res: CmdResult, subject: str) -> GuardFailure:
    return GuardFailure(
        CODE_DOCKER_COMMAND_FAILED,
        f"docker {step} failed for {subject}"
        + (f": {_tail(res.stderr)}" if _tail(res.stderr) else ""),
        details={"argv": list(res.argv), "exit_code": res.exit_code,
                 "stderr_tail": _tail(res.stderr)})


def _require_docker() -> tuple[str | None, GuardFailure | None]:
    path = shutil.which(DOCKER_COMMAND)
    if path is None:
        return None, GuardFailure(CODE_DOCKER_UNAVAILABLE,
                                  DOCKER_INSTALL_HINT,
                                  details={"command": DOCKER_COMMAND})
    return path, None


def _daemon_failure(stderr: str) -> GuardFailure:
    lowered = stderr.lower()
    if "permission denied" in lowered:
        message = ("no permission to access the Docker daemon as this "
                   "user. Add yourself to the docker group and start a "
                   "new login session (sudo usermod -aG docker "
                   "<your-user>), or run with sudo")
    elif "cannot connect" in lowered \
            or "is the docker daemon running" in lowered or "daemon" in lowered:
        message = ("Docker daemon is not reachable. Start it first "
                   "(sudo systemctl start docker)")
    else:
        tail = _last_lines(stderr, 5)
        message = "docker info failed; cannot verify Docker access" \
            + (":\n" + "\n".join(tail) if tail else "")
    return GuardFailure(CODE_DAEMON_UNREACHABLE, message,
                        details={"stderr_tail": _last_lines(stderr, 5)})


def _daemon_ok(docker_path: str, policy: LifecyclePolicy,
               verbose: Callable[[str], None] = _quiet
               ) -> tuple[DaemonOk | None, GuardFailure | None]:
    res = _run(docker_path, ("info",), policy.probe_timeout_s, verbose)
    if isinstance(res, GuardFailure):
        return None, res
    if res.exit_code != 0:
        return None, _daemon_failure(res.stderr)
    return DaemonOk(docker_path=docker_path), None


def probe_daemon(policy: LifecyclePolicy | None = None,
                 verbose: Callable[[str], None] | None = None
                 ) -> DaemonOk | GuardFailure:
    """Guards 1+2: docker discovery and the classified daemon check.
    ``verbose`` (default quiet) narrates the docker calls."""
    policy = policy or LifecyclePolicy()
    verbose = verbose or _quiet
    docker, failure = _require_docker()
    if failure is not None:
        return failure
    ok, failure = _daemon_ok(docker, policy, verbose)
    if failure is not None:
        return failure
    return ok


def _markers(docker_path: str, ref: str, policy: LifecyclePolicy,
             verbose: Callable[[str], None] = _quiet
             ) -> tuple[ImageMarkers | None, GuardFailure | None]:
    """None markers (without failure) = ref not present locally."""
    res = _run(docker_path, ("image", "inspect", "--format",
                             MARKERS_TEMPLATE, ref),
                policy.probe_timeout_s, verbose)
    if isinstance(res, GuardFailure):
        return None, res
    if res.exit_code != 0:
        return None, None
    return ImageMarkers.from_stdout(res.stdout), None


def _ensure_image(docker_path: str, image: str, force_pull: bool,
                  policy: LifecyclePolicy,
                  verbose: Callable[[str], None] = _quiet
                  ) -> tuple[ImageMarkers | None, GuardFailure | None]:
    """Guard 3+4: pull only when missing (or forced), then return the
    post-pull markers for identity verification."""
    res_markers, failure = _markers(docker_path, image, policy, verbose)
    if failure is not None:
        return None, failure
    if res_markers is None or force_pull:
        res = _run(docker_path, ("pull", image), policy.pull_timeout_s,
                   verbose)
        if isinstance(res, GuardFailure):
            return None, res
        if res.exit_code != 0:
            return None, GuardFailure(
                CODE_IMAGE_PULL_FAILED,
                f"docker pull failed for {image}"
                + (f": {_tail(res.stderr)}" if _tail(res.stderr) else ""),
                details={"image": image,
                         "stderr_tail": _tail(res.stderr)})
        res_markers, failure = _markers(docker_path, image, policy,
                                        verbose)
        if failure is not None:
            return None, failure
    return res_markers, None


def _container_exists(docker_path: str, name: str, policy: LifecyclePolicy,
                      verbose: Callable[[str], None] = _quiet
                      ) -> tuple[bool | None, GuardFailure | None]:
    """Guard 5: the plan's ``docker ps -a --filter name=^/<name>$``
    existence probe; the anchored regex plus an exact-name line
    comparison (the filter is a regex, names are not)."""
    res = _run(docker_path, ("ps", "-a", "--filter", f"name=^/{name}$",
                             "--format", "{{.Names}}"),
                policy.probe_timeout_s, verbose)
    if isinstance(res, GuardFailure):
        return None, res
    if res.exit_code != 0:
        return None, _command_failed("ps", res, name)
    names = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    return name in names, None


def _container_image(docker_path: str, name: str, policy: LifecyclePolicy,
                     verbose: Callable[[str], None] = _quiet
                     ) -> tuple[str | None, GuardFailure | None]:
    res = _run(docker_path, ("container", "inspect", "-f", "{{.Image}}",
                             name), policy.probe_timeout_s, verbose)
    if isinstance(res, GuardFailure):
        return None, res
    if res.exit_code != 0:
        return None, _command_failed("container inspect", res, name)
    return res.stdout.strip(), None


def _container_status(docker_path: str, name: str, policy: LifecyclePolicy,
                      verbose: Callable[[str], None] = _quiet
                      ) -> tuple[str | None, GuardFailure | None]:
    res = _run(docker_path, ("container", "inspect", "-f",
                             "{{.State.Status}}", name),
                policy.probe_timeout_s, verbose)
    if isinstance(res, GuardFailure):
        return None, res
    if res.exit_code != 0:
        return None, _command_failed("container inspect", res, name)
    return res.stdout.strip(), None


def _container_bindings(docker_path: str, name: str,
                        policy: LifecyclePolicy,
                        verbose: Callable[[str], None] = _quiet
                        ) -> tuple[tuple[str, ...] | None,
                                   GuardFailure | None]:
    """All host bindings for the fixed container port (``docker port``
    ``name`` 8080); the script reads the first line and tolerates
    failure as no-binding."""
    res = _run(docker_path, ("port", name, str(CONTAINER_PORT)),
                policy.probe_timeout_s, verbose)
    if isinstance(res, GuardFailure):
        return None, res
    if res.exit_code != 0:
        return (), None
    lines = tuple(ln.strip() for ln in res.stdout.splitlines() if ln.strip())
    return lines, None


def probe_container(name: str, policy: LifecyclePolicy | None = None,
                    verbose: Callable[[str], None] | None = None
                    ) -> ContainerState | GuardFailure:
    """Read-only state probe for one container name (absent is a
    valid ``present=False`` state, not a failure). ``verbose``
    (default quiet) narrates the docker calls."""
    if not isinstance(name, str) or not name.strip():
        raise UsageError(f"name must be a non-empty string; got {name!r}")
    policy = policy or LifecyclePolicy()
    verbose = verbose or _quiet
    docker, failure = _require_docker()
    if failure is not None:
        return failure
    _, failure = _daemon_ok(docker, policy, verbose)
    if failure is not None:
        return failure
    exists, failure = _container_exists(docker, name, policy, verbose)
    if failure is not None:
        return failure
    if not exists:
        return ContainerState(name=name, present=False)
    image_id, failure = _container_image(docker, name, policy, verbose)
    if failure is not None:
        return failure
    status, failure = _container_status(docker, name, policy, verbose)
    if failure is not None:
        return failure
    bindings, failure = _container_bindings(docker, name, policy, verbose)
    if failure is not None:
        return failure
    markers, failure = _markers(docker, image_id, policy, verbose)
    if failure is not None:
        return failure
    return ContainerState(
        name=name, present=True, running=status == "running",
        image_id=image_id, host_bindings=bindings,
        searxng_image=markers is not None
        and markers.verifies_as_searxng())


def port_is_free(bind: str, port: int) -> GuardFailure | None:
    """Guard 7: bind the target then close (never listened, so no
    TIME_WAIT is left). Any holder of the port -- even one refusing
    connections -- blocks the bind, which is equivalent-or-stricter
    than the shell's connect probe on every OS."""
    infos = None
    try:
        infos = socket.getaddrinfo(bind, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return GuardFailure(
            CODE_PORT_UNBINDABLE,
            f"cannot resolve bind address {bind!r}: {exc}",
            details={"bind": bind, "port": port})
    if not infos:
        return GuardFailure(
            CODE_PORT_UNBINDABLE,
            f"bind address {bind!r} resolved to nothing",
            details={"bind": bind, "port": port})
    family = infos[0][0]
    sock = None
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.bind((bind, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return GuardFailure(
                CODE_PORT_IN_USE,
                f"port {bind}:{port} is already in use by another "
                f"process; refusing to bind. Choose another --port or "
                f"stop that process.",
                details={"bind": bind, "port": port})
        reason = errno.errorcode.get(exc.errno, "OS error")
        return GuardFailure(
            CODE_PORT_UNBINDABLE,
            f"cannot bind {bind}:{port}: {reason}: "
            f"{exc.strerror or exc}",
            details={"bind": bind, "port": port, "errno": reason})
    finally:
        if sock is not None:
            sock.close()
    return None


def build_run_argv(docker_path: str, target: SearxngTarget,
                   settings_path: str | None = None) -> list[str]:
    """Guard 8's exact ``docker run`` argv (the script's shape; the
    settings mount is wired by the orchestration todo, not here)."""
    argv = [docker_path, "run", "-d",
            "--name", target.name,
            "--publish", f"{target.bind}:{target.port}:{CONTAINER_PORT}",
            "--env", "FORCE_OWNERSHIP=false"]
    if settings_path is not None:
        if not isinstance(settings_path, str) or not settings_path.strip():
            raise UsageError("settings_path must be a non-empty string "
                             f"when given; got {settings_path!r}")
        argv += ["--volume", f"{settings_path}:{SETTINGS_MOUNT}"]
    argv += ["--security-opt", "no-new-privileges", target.image]
    return argv


def _failure_log_excerpt(docker_path: str, name: str,
                         policy: LifecyclePolicy,
                         verbose: Callable[[str], None] = _quiet) -> str:
    """``docker logs --tail 20`` excerpt for a readiness refusal;
    best-effort like the script's ``|| true``."""
    res = _run(docker_path, ("logs", "--tail", str(LOG_TAIL_LINES), name),
                policy.probe_timeout_s, verbose)
    if isinstance(res, GuardFailure) or res.exit_code != 0:
        return ""
    return res.stdout.strip()


def _wait_ready(docker_path: str, name: str, policy: LifecyclePolicy,
                verbose: Callable[[str], None] = _quiet
                ) -> GuardFailure | None:
    """Guard 9: the script's exact in-container healthz loop -- probe,
    then deadline check, then sleep; never a host-side HTTP client."""
    started = time.monotonic()
    deadline = started + policy.wait_s
    while True:
        res = _run(docker_path, ("exec", name, *WGET_ARGS,
                                  CONTAINER_HEALTH_URL),
                    policy.exec_timeout_s, verbose)
        if isinstance(res, GuardFailure):
            return res
        if res.exit_code == 0:
            return None
        now = time.monotonic()
        if now >= deadline:
            waited = round(now - started, 3)
            return GuardFailure(
                CODE_READINESS_TIMEOUT,
                f"container {name} did not become healthy within "
                f"{policy.wait_s}s; it was left running for inspection "
                f"(stop with `python -m gleam searxng --stop`)",
                details={"container": name, "waited_s": waited,
                         "health_url": CONTAINER_HEALTH_URL,
                         "log_excerpt": _failure_log_excerpt(
                             docker_path, name, policy, verbose)})
        time.sleep(policy.probe_interval_s)


def _image_digest(docker_path: str, image: str, policy: LifecyclePolicy,
                  verbose: Callable[[str], None] = _quiet) -> str:
    """The script's success-line digest: first RepoDigest, else the
    image Id, else the requested reference (all best-effort)."""
    for fmt in ("{{index .RepoDigests 0}}", "{{.Id}}"):
        res = _run(docker_path, ("image", "inspect", "--format", fmt,
                                  image), policy.probe_timeout_s, verbose)
        if not isinstance(res, GuardFailure) and res.exit_code == 0 \
                and res.stdout.strip():
            return res.stdout.strip()
    return image


def bring_up(target: SearxngTarget, *, pull: bool = False,
             settings_path: str | None = None,
             policy: LifecyclePolicy | None = None,
             verbose: Callable[[str], None] | None = None
             ) -> BringUpSuccess | GuardFailure:
    """The full guarded bring-up chain (guards 1-9). Refuses before
    the first mutating docker call whenever any guard fails.
    ``verbose`` (default quiet) narrates the docker calls."""
    policy = policy or LifecyclePolicy()
    verbose = verbose or _quiet
    docker, failure = _require_docker()
    if failure is not None:
        return failure
    _, failure = _daemon_ok(docker, policy, verbose)
    if failure is not None:
        return failure
    markers, failure = _ensure_image(docker, target.image, pull, policy,
                                     verbose)
    if failure is not None:
        return failure
    if markers is None or not markers.verifies_as_searxng():
        return GuardFailure(
            CODE_IMAGE_NOT_SEARXNG,
            f"image {target.image} is not SearXNG (no searxng/searxng "
            f"OCI labels and no SearXNG image markers); refusing to "
            f"run it",
            details={"image": target.image})
    exists, failure = _container_exists(docker, target.name, policy,
                                        verbose)
    if failure is not None:
        return failure
    action = "created"
    if exists:
        image_id, failure = _container_image(docker, target.name, policy,
                                             verbose)
        if failure is not None:
            return failure
        c_markers, failure = _markers(docker, image_id, policy, verbose)
        if failure is not None:
            return failure
        if c_markers is None or not c_markers.verifies_as_searxng():
            return GuardFailure(
                CODE_IMAGE_NOT_SEARXNG,
                f"container '{target.name}' exists but its image "
                f"({image_id}) is not SearXNG; refusing to touch it",
                details={"container": target.name, "image_id": image_id})
        bindings, failure = _container_bindings(docker, target.name,
                                                policy, verbose)
        if failure is not None:
            return failure
        binding = bindings[0] if bindings else ""
        if binding != target.host_binding:
            return GuardFailure(
                CODE_BINDING_MISMATCH,
                f"container '{target.name}' exists with host binding "
                f"{binding or 'none'} -> {CONTAINER_PORT}, wanted "
                f"{target.host_binding}. Stop it first (`python -m "
                f"gleam searxng --stop`) or match the flags to the "
                f"existing binding.",
                details={"container": target.name, "binding": binding,
                         "wanted": target.host_binding})
        status, failure = _container_status(docker, target.name, policy,
                                            verbose)
        if failure is not None:
            return failure
        if status == "running":
            action = "reused-running"
        else:
            res = _run(docker, ("start", target.name),
                        policy.probe_timeout_s, verbose)
            if isinstance(res, GuardFailure):
                return res
            if res.exit_code != 0:
                return _command_failed("start", res, target.name)
            action = "started-existing"
    else:
        failure = port_is_free(target.bind, target.port)
        if failure is not None:
            return failure
        argv = build_run_argv(docker, target, settings_path)
        res = _run(docker, tuple(argv[1:]), policy.run_timeout_s, verbose)
        if isinstance(res, GuardFailure):
            return res
        if res.exit_code != 0:
            return _command_failed("run", res, target.name)
    failure = _wait_ready(docker, target.name, policy, verbose)
    if failure is not None:
        return failure
    return BringUpSuccess(
        container=target.name, image=target.image, url=target.url,
        action=action,
        image_digest=_image_digest(docker, target.image, policy, verbose))


def tear_down(name: str, policy: LifecyclePolicy | None = None,
              verbose: Callable[[str], None] | None = None
              ) -> TearDownOutcome | GuardFailure:
    """stop.sh port: absent -> idempotent no-op; foreign same-named
    container -> refusal, untouched; else graceful stop (only when
    running) then rm -- never ``docker rm -f``. ``verbose`` (default
    quiet) narrates the docker calls and the no-op outcome."""
    if not isinstance(name, str) or not name.strip():
        raise UsageError(f"name must be a non-empty string; got {name!r}")
    policy = policy or LifecyclePolicy()
    verbose = verbose or _quiet
    docker, failure = _require_docker()
    if failure is not None:
        return failure
    _, failure = _daemon_ok(docker, policy, verbose)
    if failure is not None:
        return failure
    exists, failure = _container_exists(docker, name, policy, verbose)
    if failure is not None:
        return failure
    if not exists:
        verbose("container absent, nothing to do")
        return TearDownOutcome(container=name, action="absent-noop")
    image_id, failure = _container_image(docker, name, policy, verbose)
    if failure is not None:
        return failure
    markers, failure = _markers(docker, image_id, policy, verbose)
    if failure is not None:
        return failure
    if markers is None or not markers.verifies_as_searxng():
        return GuardFailure(
            CODE_IMAGE_NOT_SEARXNG,
            f"container '{name}' runs image {image_id}, which is not "
            f"SearXNG; refusing to touch it",
            details={"container": name, "image_id": image_id})
    status, failure = _container_status(docker, name, policy, verbose)
    if failure is not None:
        return failure
    if status == "running":
        res = _run(docker, ("stop", name), policy.stop_timeout_s, verbose)
        if isinstance(res, GuardFailure):
            return res
        if res.exit_code != 0:
            return _command_failed("stop", res, name)
    res = _run(docker, ("rm", name), policy.stop_timeout_s, verbose)
    if isinstance(res, GuardFailure):
        return res
    if res.exit_code != 0:
        return _command_failed("rm", res, name)
    return TearDownOutcome(container=name, action="stopped-and-removed")
