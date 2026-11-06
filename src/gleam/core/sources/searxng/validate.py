"""Side-effect-free end-to-end validation probe for the self-hosted
SearXNG deployment (the ``--validate`` chain).

Pure-stdlib port of ``scripts/searxng/gate.sh``'s probe semantics MINUS
its bootstrap step: validate never starts anything (bring-up is
``--start``'s job) and writes no file anywhere. The chain runs in
order and short-circuits at the first non-passing step:

1. **Docker availability** (BLOCKER): the lifecycle layer's
   ``probe_daemon`` (docker discovery plus the classified
   ``docker info`` pre-check).
2. **Named-container presence** (BLOCKER): ``probe_container`` for
   the configured container name (the ``docker ps`` filter probe);
   a guard failure and an absent container are both blockers, the
   latter pointing at ``python -m gleam searxng --start``.
3. **healthz** (FAIL): one bounded GET on the CONFIGURED search base
   (stdlib urllib, fixed 5s timeout, redirects never followed); any
   transport error or non-2xx answer is unhealthy.
4. **One real query** (FAIL): ``SearxngClient(base=<configured>
   ).search`` with gate.sh's fixed query, graded against its frozen
   shape rules: status ok, not ambiguous, non-zero results, every
   result an absolute http(s) URL, engine attribution present.

Verdicts: PASS (every step ok), FAIL (steps 3-4 violated), BLOCKER
(steps 1-2 impossible). :class:`ValidationOutcome` carries the
verdict, per-step evidence, and Result-ready structured errors
(empty iff PASS) -- ``--start``'s post-validate reuses this exact
shape.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gleam.core.result import DiagnosticItem
from gleam.core.verbose import _quiet, format_count

from .client import SearxngClient, SearxngResult
from .lifecycle import (
    CODE_DAEMON_UNREACHABLE,
    CODE_DOCKER_UNAVAILABLE,
    GuardFailure,
    probe_container,
    probe_daemon,
)

__all__ = [
    "CODE_CONTAINER_ABSENT",
    "CODE_HEALTHZ_UNHEALTHY",
    "CODE_QUERY_SHAPE_INVALID",
    "HEALTHZ_TIMEOUT_S",
    "VALIDATE_QUERY",
    "VERDICT_BLOCKER",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "ValidationOutcome",
    "ValidationStep",
    "WHERE",
    "run_validate",
]

#: Stable ``where`` marker for every diagnostic this module emits.
WHERE = "gleam.core.sources.searxng.validate"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_BLOCKER = "BLOCKER"

#: gate.sh's fixed probe query (the shape contract's pinned input).
VALIDATE_QUERY = "debian wizznic upstream"
HEALTHZ_TIMEOUT_S = 5.0
#: How much of the healthz body rides in the step evidence.
HEALTHZ_BODY_SNIPPET = 64

CODE_CONTAINER_ABSENT = "searxng-container-absent"
CODE_HEALTHZ_UNHEALTHY = "searxng-healthz-unhealthy"
CODE_QUERY_SHAPE_INVALID = "searxng-query-shape-invalid"

#: Compressed narration words for the daemon-probe failure codes (the
#: module's own wording; the GuardFailure messages stay in the steps).
_NARRATED_DAEMON_FAILURES = {
    CODE_DOCKER_UNAVAILABLE: "docker not found",
    CODE_DAEMON_UNREACHABLE: "daemon unreachable",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A healthz answered by a redirect is not verified-healthy; the
    hook returns None so urllib raises ``HTTPError`` instead of
    following (the same never-follow rule as the search client)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: The opener seam (offline tests patch this module attribute).
_HEALTHZ_OPENER = urllib.request.build_opener(_NoRedirect())


@dataclass(frozen=True)
class ValidationStep:
    """One chain step's evidence: stable name, pass flag, detail."""

    step: str
    ok: bool
    detail: str

    def to_data(self) -> dict[str, Any]:
        return {"step": self.step, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class ValidationOutcome:
    """The probe verdict plus per-step evidence and Result-ready
    errors (empty iff the verdict is PASS)."""

    verdict: str
    steps: tuple[ValidationStep, ...] = ()
    errors: tuple[DiagnosticItem, ...] = ()


def _healthz(base: str,
             verbose: Callable[[str], None] = _quiet) -> tuple[bool, str, str]:
    """One bounded healthz GET on the configured base: stdlib
    urllib, fixed timeout, redirects never followed. Returns
    ``(healthy, url, detail)``; every transport failure is an honest
    unhealthy verdict carrying its reason."""
    url = base.rstrip("/") + "/healthz"
    try:
        with _HEALTHZ_OPENER.open(url, timeout=HEALTHZ_TIMEOUT_S) as response:
            body = response.read(HEALTHZ_BODY_SNIPPET).decode(
                "utf-8", errors="replace").strip()
            detail = f"HTTP {response.status}"
            if body:
                detail += f" ({body})"
            verbose(f"GET {url} -> {response.status}")
            return True, url, detail
    except urllib.error.HTTPError as exc:
        verbose(f"GET {url} -> {exc.code}")
        return False, url, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        verbose(f"GET {url} -> error: {exc.reason}")
        return False, url, f"URLError: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        verbose(f"GET {url} -> error: {exc}")
        return False, url, f"{type(exc).__name__}: {exc}"


def _shape_problems(result: SearxngResult) -> list[str]:
    """gate.sh's frozen shape rules, verbatim semantics: status ok,
    not ambiguous, non-zero results, every result an absolute
    http(s) URL, engine attribution on every result."""
    problems: list[str] = []
    if result.status != "ok":
        problems.append(f"client status is {result.status!r}, expected 'ok'")
    if result.ambiguous:
        problems.append("explicit ambiguity: all engines failed or empty")
    if not result.results:
        problems.append("zero results: conservative engine subset unusable")
    for position, item in enumerate(result.results):
        url = item.get("url", "")
        if not url.startswith(("http://", "https://")):
            problems.append(
                f"result {position}: not an absolute http(s) URL: {url!r}")
        if not item.get("engines"):
            problems.append(f"result {position}: missing engine attribution")
    return problems


def run_validate(name: str, search_base: str,
                 verbose: Callable[[str], None] | None = None
                 ) -> ValidationOutcome:
    """Run the four-step read-only chain (module docstring order) and
    grade the verdict; every refusal short-circuits the rest.
    ``verbose`` (default quiet) narrates the compressed step outcomes
    (the module's own words, not the step-detail strings)."""
    verbose = verbose or _quiet
    steps: list[ValidationStep] = []
    daemon = probe_daemon(verbose=verbose)
    if isinstance(daemon, GuardFailure):
        steps.append(ValidationStep("docker-availability", False,
                                    daemon.message))
        verbose(_NARRATED_DAEMON_FAILURES.get(daemon.code,
                                              "docker check failed"))
        return ValidationOutcome(VERDICT_BLOCKER, tuple(steps),
                                 (daemon.to_diagnostic(),))
    steps.append(ValidationStep(
        "docker-availability", True,
        f"docker found at {daemon.docker_path}; daemon reachable"))
    verbose(f"docker found at {daemon.docker_path}; daemon reachable")
    state = probe_container(name, verbose=verbose)
    if isinstance(state, GuardFailure):
        steps.append(ValidationStep("container-presence", False,
                                    state.message))
        verbose("container check failed")
        return ValidationOutcome(VERDICT_BLOCKER, tuple(steps),
                                 (state.to_diagnostic(),))
    if not state.present:
        message = (f"container '{name}' is not present; start it first "
                    f"(`python -m gleam searxng --start`)")
        steps.append(ValidationStep("container-presence", False, message))
        verbose("container absent")
        return ValidationOutcome(
            VERDICT_BLOCKER, tuple(steps),
            (DiagnosticItem(code=CODE_CONTAINER_ABSENT, message=message,
                            where=WHERE, details={"container": name}),))
    facts = [f"container '{name}' present",
             "running" if state.running else "not running",
             "image ok" if state.searxng_image else "image mismatch"]
    if state.host_bindings:
        facts.append("at " + ",".join(state.host_bindings))
    steps.append(ValidationStep("container-presence", True,
                                "; ".join(facts)))
    verbose(f"container present, "
            f"{'running' if state.running else 'not running'}, "
            f"{'image ok' if state.searxng_image else 'image mismatch'}")
    healthy, url, detail = _healthz(search_base, verbose)
    if not healthy:
        message = f"healthz endpoint {url} is not healthy: {detail}"
        steps.append(ValidationStep("healthz", False, message))
        return ValidationOutcome(
            VERDICT_FAIL, tuple(steps),
            (DiagnosticItem(code=CODE_HEALTHZ_UNHEALTHY, message=message,
                            where=WHERE, details={"url": url}),))
    steps.append(ValidationStep("healthz", True, f"GET {url}: {detail}"))
    result = SearxngClient(base=search_base).search(VALIDATE_QUERY)
    count = len(result.results)
    verbose(f"search '{VALIDATE_QUERY}': {format_count(count)} "
            f"result{'' if count == 1 else 's'}")
    problems = _shape_problems(result)
    if problems:
        detail = "; ".join(problems)
        message = f"search query shape validation failed: {detail}"
        steps.append(ValidationStep("search-query", False, message))
        shape_item = DiagnosticItem(
            code=CODE_QUERY_SHAPE_INVALID,
            message=message,
            where=WHERE,
            details={"query": VALIDATE_QUERY, "problems": problems})
        return ValidationOutcome(VERDICT_FAIL, tuple(steps),
                                 (*result.errors, shape_item))
    extras = ""
    if result.unresponsive_engines:
        extras = "; unresponsive_engines: " + ",".join(
            str(entry) for entry in result.unresponsive_engines)
    count = len(result.results)
    steps.append(ValidationStep(
        "search-query", True,
        f"status ok, {count} result{'' if count == 1 else 's'}, each an "
        f"absolute http(s) URL with engine attribution{extras}"))
    return ValidationOutcome(VERDICT_PASS, tuple(steps), ())
