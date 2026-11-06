"""Anti-bot challenge detection and bounded solving with frozen
semantics.

Observed challenge shapes (from field evidence):

- ``varnish-pow``: bugs.debian.org fronts some web GETs with a Varnish
  PoW (page self-describes as "Adapted from haphash for Varnish PoW",
  title "I Challenge Thee"). The challenge value arrives in the
  ``pow_challenge`` cookie; the solution is a ``tries`` value with
  SHA256(challenge + ";" + tries) having a zero first byte, answered
  via the ``pow_nonce`` cookie before retrying the original URL.
- ``pow-challenge``: Anubis-style challenges (.within.website) with an
  embedded JSON script carrying ``challenge`` and ``difficulty``.

Solving is bounded by CPU seconds and the client's request budget; an
unrecognized shape fails explicitly as ``challenge-unknown`` -- success
is never faked and captcha/login walls are never circumvented. All
challenge-kind detection is conservative: a false negative simply
reaches the parsers (which report an honest parse-error), while a false
positive would mislabel normal content.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import urllib.parse

__all__ = [
    "BTS_CHALLENGE_PATH",
    "CHALLENGE_MARKERS",
    "POW_TRIES_CAP",
    "detect_challenge",
    "extract_anubis_challenge",
    "solve_hashcash",
    "solve_varnish_pow",
]

CHALLENGE_MARKERS = (
    "i challenge thee",              # Debian Varnish PoW page title (observed)
    "pow_challenge",                 # Debian challenge cookie name
    "haphash",                       # Debian challenge page lineage
    "anubis",                        # Anubis (.within.website family)
    ".within.website",
    "proof-of-work", "proof of work", "pow challenge",
    "checking your browser",
    "making sure you're not a bot",
)
BTS_CHALLENGE_PATH = "/.internal/challenge.html"
POW_TRIES_CAP = 10_000_000          # the challenge page JS loop cap, adopted verbatim


def detect_challenge(status: int | None, headers: dict, body: bytes
                     ) -> dict | None:
    """Detect whether a response is an anti-bot challenge. Judgement is
    conservative: a misclassification would rather pass the payload to
    the parsers (honest parse-error) than treat normal content as a
    challenge. Returns ``{"type", "detail"}`` or ``None``.

    Type vocabulary:

    - ``varnish-pow``: observed Debian shape -- a 302 to
      ``/.internal/challenge.html``, or an HTML page carrying
      pow_challenge / I Challenge Thee / haphash markers;
    - ``pow-challenge``: Anubis-style (.within.website) and other known
      PoW shapes;
    - ``forbidden-html``: 403 + HTML (possible challenge variant,
      graded as a challenge without guessing a solver).
    """

    if status in (301, 302, 303, 307, 308):
        location = (headers.get("location") or [None])[0] or ""
        path = urllib.parse.urlsplit(location).path or location
        if path.split("?")[0] == BTS_CHALLENGE_PATH \
                or location.startswith(BTS_CHALLENGE_PATH):
            return {"type": "varnish-pow",
                    "detail": f"302 to {BTS_CHALLENGE_PATH} "
                              f"(Debian Varnish PoW challenge)"}
        return None
    content_type = ""
    for values in [headers.get("content-type", [])]:
        if values:
            content_type = values[0].lower()
            break
    head = body[:65536].decode("utf-8", "replace").lower()
    is_html = "html" in content_type \
        or head.lstrip().startswith("<!doctype html") or "<html" in head
    if not is_html:
        return None
    lowered = head
    if ("pow_challenge" in lowered or "i challenge thee" in lowered
            or "haphash" in lowered):
        return {"type": "varnish-pow",
                "detail": "Debian Varnish PoW challenge markers in HTML"}
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return {"type": "pow-challenge",
                    "detail": f"challenge marker {marker!r} in HTML response"}
    title = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.S | re.I)
    if title and re.search(r"challenge|verify|checking|not a bot|puzzle",
                           title.group(1)):
        return {"type": "pow-challenge",
                "detail": f"challenge-like title {title.group(1).strip()!r}"}
    if status == 403 and is_html:
        return {"type": "forbidden-html",
                "detail": "403 with HTML body (possible challenge variant)"}
    return None


_ANUBIS_CHALLENGE_JSON_RE = re.compile(
    r'<script[^>]*id="anubis_challenge_data"[^>]*type="application/json"'
    r"[^>]*>(.*?)</script>", re.S | re.I)


def extract_anubis_challenge(body: bytes) -> dict | None:
    """Extract Anubis-shape challenge parameters (documented client
    flow): an inline ``script#anubis_challenge_data`` JSON block with a
    ``challenge`` string and ``difficulty``. Returns ``None`` when the
    shape is not recognized (the caller reports an honest
    ``challenge-unknown``; blind guessing is refused)."""

    text = body.decode("utf-8", "replace")
    m = _ANUBIS_CHALLENGE_JSON_RE.search(text)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "challenge" not in payload:
        return None
    return payload


def solve_varnish_pow(challenge: str, max_seconds: float) \
        -> tuple[int, str, float] | None:
    """Debian Varnish PoW (haphash adaptation, matching the challenge
    page JS verbatim): find ``tries`` in ``[0, 10^7)`` such that
    SHA256(challenge + ";" + tries) has a zero first byte (difficulty
    2, ~256 expected attempts). Returns ``(tries, digest_hex,
    elapsed_s)``; a time or attempt overrun returns ``None`` -- CPU is
    never burned without bound. This is the normal client computation
    publicly required by the challenger; captcha/login walls are never
    in scope here."""

    started = time.monotonic()
    prefix = f"{challenge};".encode("utf-8")
    for tries in range(POW_TRIES_CAP):
        if (tries & 0xFF) == 0 and time.monotonic() - started > max_seconds:
            return None
        digest = hashlib.sha256(prefix + str(tries).encode("ascii")).digest()
        if digest[0] == 0:
            return tries, digest.hex(), time.monotonic() - started
    return None


def solve_hashcash(challenge: str, difficulty: int,
                   max_seconds: float, hash_name: str = "sha256") \
        -> tuple[str, str, float] | None:
    """Bounded hashcash solving (Anubis shape): find a nonce such that
    H(challenge:nonce) has at least ``difficulty`` leading '0' hex
    digits. Returns ``(nonce, hexdigest, elapsed_s)``; a timeout
    returns ``None``."""

    started = time.monotonic()
    prefix = "0" * max(0, int(difficulty))
    while time.monotonic() - started < max_seconds:
        nonce = secrets.token_hex(8)
        digest = hashlib.new(hash_name,
                             f"{challenge}:{nonce}".encode("utf-8")
                             ).hexdigest()
        if digest.startswith(prefix):
            return nonce, digest, time.monotonic() - started
    return None
