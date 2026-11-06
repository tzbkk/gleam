"""Fixed BTS endpoints, bug-id validation, policy bounds, and the
application-side URL/envelope builders.

Hard bounds: the only trusted host is ``bugs.debian.org`` with
the official SOAP path ``/cgi-bin/soap.cgi`` and mbox path
``/cgi-bin/bugreport.cgi``. No CLI flag, environment key, or function
parameter of this package accepts a base URL, host, or path override;
every URL is reconstructed by the application from a strictly validated
numeric bug id.

Bug-id rules: decimal integer ``1..9_999_999``, no leading
zeros, regex ``^[1-9][0-9]{0,6}$``; shared by the standalone bts entry
and the investigate fetch_bug tool; never configurable. Violations raise
``UsageError`` (exit 2 at the entry) before any network request.

Numeric policy defaults may be tightened through ``BtsPolicy``
by the config domain at call time; the dataclass defaults exist only
for standalone testing. The User-Agent template is fixed; the version
segment is spliced in from the single version source.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from gleam.core.user_agent import USER_AGENT
from gleam.core.result import UsageError

__all__ = [
    "BTS_ALLOWED_HOSTS",
    "BTS_MBOX_PATH",
    "BTS_SOURCES",
    "BTS_SOAP_PATH",
    "BUG_ID_MAX",
    "BUG_ID_RE",
    "BtsPolicy",
    "DEFAULT_BTS_WEB_BASE",
    "DEFAULT_POLICY",
    "DEFAULT_USER_AGENT",
    "AUTO_FALLBACK_ELIGIBLE",
    "bug_page_url",
    "build_mbox_url",
    "build_soap_get_bug_log",
    "build_soap_url",
    "validate_bug_id",
]

#: Fixed official endpoints: constants only, never configurable.
DEFAULT_BTS_WEB_BASE = "https://bugs.debian.org"
BTS_SOAP_PATH = "/cgi-bin/soap.cgi"
BTS_MBOX_PATH = "/cgi-bin/bugreport.cgi"
BTS_ALLOWED_HOSTS = frozenset({"bugs.debian.org"})

#: Bug-id rules, frozen and shared by every caller.
BUG_ID_MAX = 9_999_999
BUG_ID_RE = re.compile(r"^[1-9][0-9]{0,6}$")  # digits only, no leading zero

#: Shared User-Agent; the version is spliced in from the single version
#: source at import.
DEFAULT_USER_AGENT = USER_AGENT

#: Source selector values for ``BtsClient.fetch``.
BTS_SOURCES = ("auto", "soap", "mbox")

#: Auto-mode fallback eligibility (frozen): only these SOAP
#: failure kinds ever consider the mbox fallback. Even an eligible
#: error still requires robots to
#: explicitly permit the mbox endpoint; robots is never bypassed.
AUTO_FALLBACK_ELIGIBLE = frozenset({
    "transport-error", "timeout", "soap-fault", "parse-error",
})

#: Internal policy dictionary; the parsing layer indexes it.
DEFAULT_POLICY = {
    "timeout_s": 20.0,
    "max_bytes": 8 * 1024 * 1024,   # per-response cap (mbox/SOAP are large)
    "max_requests": 6,              # per client instance, robots/challenge included
    "min_interval_s": 2.0,          # polite pacing against the BTS host
    "max_messages": 200,            # parsed-message cap
    "max_msg_body_chars": 20000,    # per-message body display cap
    "max_urls_per_message": 20,
    "max_pow_seconds": 20.0,        # PoW CPU budget
    "robots_max_bytes": 65536,
    "user_agent": DEFAULT_USER_AGENT,
}


@dataclass(frozen=True)
class BtsPolicy:
    """Numeric bounds for one fetch (defaults; live values come
    from the config domain at call time -- these defaults exist only
    for standalone testing). Hosts and paths are deliberately absent:
    they are fixed constants, not policy. The User-Agent is
    likewise fixed and therefore not a policy field.
    """

    timeout_s: float = 20.0
    max_bytes: int = 8 * 1024 * 1024
    max_requests: int = 6
    min_interval_s: float = 2.0
    max_messages: int = 200
    max_msg_body_chars: int = 20000
    max_urls_per_message: int = 20
    max_pow_seconds: float = 20.0
    robots_max_bytes: int = 65536


def validate_bug_id(value) -> int:
    """Accept only a strict decimal bug id (int or digit string) in
    ``1..BUG_ID_MAX``. bool/float/leading zeros/non-numeric values are
    rejected with ``UsageError`` -- URLs are always rebuilt by
    the application from the returned number."""

    if isinstance(value, bool):
        raise UsageError("bug_id must be numeric, got bool")
    if isinstance(value, int):
        bug_id = value
    elif isinstance(value, str):
        if not BUG_ID_RE.match(value or ""):
            raise UsageError(
                f"bug_id must be digits without leading zeros "
                f"(1..{BUG_ID_MAX}); got {value!r}")
        bug_id = int(value)
    else:
        raise UsageError(
            f"bug_id must be an int or numeric string; "
            f"got {type(value).__name__}")
    if not (1 <= bug_id <= BUG_ID_MAX):
        raise UsageError(f"bug_id out of range 1..{BUG_ID_MAX}: {bug_id}")
    return bug_id


def build_mbox_url(bug_id: int) -> str:
    """Official mbox view URL, assembled by the application only
    (no caller-supplied host or path)."""

    bug_id = validate_bug_id(bug_id)
    return f"{DEFAULT_BTS_WEB_BASE}{BTS_MBOX_PATH}?bug={bug_id}&mbox=yes"


def build_soap_url() -> str:
    """Official Debbugs/SOAP endpoint URL (fixed constant)."""

    return DEFAULT_BTS_WEB_BASE + BTS_SOAP_PATH


def build_soap_get_bug_log(bug_id: int) -> bytes:
    """SOAP envelope for ``get_bug_log`` (application-side template;
    the bug id is inserted as digits only)."""

    bug_id = validate_bug_id(bug_id)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'soap:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<soap:Body>'
        '<ns1:get_bug_log xmlns:ns1="urn:Debbugs/SOAP">'
        f'<bug_number xsi:type="xsd:int">{bug_id}</bug_number>'
        '</ns1:get_bug_log>'
        '</soap:Body></soap:Envelope>'
    ).encode("utf-8")


def bug_page_url(bug_id: int) -> str:
    """Human-readable bug page URL (returned as provenance only; this
    tool never fetches the web interface on its own initiative)."""

    bug_id = validate_bug_id(bug_id)
    return f"{DEFAULT_BTS_WEB_BASE}/{bug_id}"


def policy_dict(policy: BtsPolicy | None) -> dict:
    """Internal dictionary view of a policy for the request
    and parsing layers."""

    merged = dict(DEFAULT_POLICY)
    if policy is not None:
        merged.update(asdict(policy))
    return merged
