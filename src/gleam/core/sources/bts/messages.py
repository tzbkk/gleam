"""Message structuring and mbox/SOAP parsing with frozen semantics.

Every message becomes a structured record: message-id / date /
from / subject / body plus a sha256 provenance digest over the raw
message bytes; URLs are extracted from the decoded body text.
Attachments are METADATA ONLY (filename, content type, size, transfer
encoding, sha256) -- they are never separately extracted, written, or
executed; the encoded attachment bytes remain only inside the preserved
raw response artifact held by the caller's evidence store.

Body text gets charset decoding and transfer-encoding decoding only.
The SOAP ``attachments`` array inside ``get_bug_log`` items is NOT
parsed (no documented structure; speculative parsing is refused) --
its presence is disclosed in the parse meta notes instead.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.header
import email.parser
import email.policy
import email.utils
import hashlib
import re
import xml.etree.ElementTree as ET

from .errors import BtsError

__all__ = [
    "parse_mbox_messages",
    "parse_soap_bug_log",
    "soap_fault_string",
    "split_mbox",
    "structure_message",
]

_MBOX_FROM_RE = re.compile(
    rb"^From \S+ +(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    rb"[ 0-9]\d \d{2}:\d{2}:\d{2} \d{4}", re.M)

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def split_mbox(data: bytes) -> tuple[list[tuple[int, int]], bool]:
    """Split an mbox payload on ``From `` separator lines; returns
    ``[(offset, length)]`` and whether multiple blocks were found.

    Only the "From sender weekday month day time year" shape counts as
    a separator (the BTS mbox is observed to be mboxo of this shape).
    With no recognizable separator the payload is treated as one block
    (the caller then labels boundary=blob, preserving the fact that
    message boundaries are unknown rather than inventing them)."""

    starts = [m.start() for m in _MBOX_FROM_RE.finditer(data)]
    if not starts:
        return [(0, len(data))], False
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(data)
        blocks.append((start, end - start))
    return blocks, True


def _decode_rfc2047(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        parts = email.header.decode_header(raw)
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", "replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception:
        return raw


def _looks_like_base64_body(text: str) -> bool:
    """True when the whole ``text`` is one base64 block.

    The SOAP interface returns debbugs-flattened bodies: the
    Content-Transfer-Encoding header is gone while the payload stays
    encoded, so the stdlib cannot decode it. Sniffing is restricted to
    a body that is *entirely* base64 (whitespace aside), which prose
    containing a pasted blob never is."""

    compact = "".join(text.split())
    if len(compact) < 64:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact))


def _base64_body_text(text: str) -> str | None:
    """Decode an entirely-base64 body to text, or ``None``."""
    if not _looks_like_base64_body(text):
        return None
    compact = "".join(text.split())
    try:
        raw = base64.b64decode(compact + "=" * (-len(compact) % 4),
                               validate=True)
    except (binascii.Error, ValueError):
        return None
    for attempt in ("utf-8", "cp1252", "latin-1"):
        try:
            decoded = raw.decode(attempt)
        except (UnicodeDecodeError, LookupError):
            continue
        return decoded
    return None


def _part_text(part) -> str | None:
    """Decode one text/* part to str; transfer encodings (QP/base64)
    are decoded by the stdlib, charsets follow the declared -> utf-8 ->
    cp1252/latin-1 replacement chain (the charset actually used is
    recorded by the caller). A body that arrives entirely base64
    without its Content-Transfer-Encoding header (the SOAP
    flattening) is decoded here instead of being shown encoded."""

    try:
        raw = part.get_payload(decode=True)
    except Exception:
        return None
    if raw is None:
        raw = (part.get_payload() or "").encode("utf-8", "replace")
    charset = (part.get_content_charset() or "").strip().lower()
    for attempt in (charset, "utf-8", "cp1252", "latin-1"):
        if not attempt:
            continue
        try:
            text = raw.decode(attempt)
        except (UnicodeDecodeError, LookupError):
            continue
        return _base64_body_text(text) or text
    text = raw.decode("utf-8", "replace")
    return _base64_body_text(text) or text


def _strip_html_bounded(html_text: str, cap: int) -> str:
    """Bounded HTML -> text (used for readability only when a message
    carries nothing but text/html; the record is flagged html_only).
    No full HTML semantic parsing: conservative tag removal plus entity
    dereferencing, with bounded length. The input is pre-truncated to
    4*cap before the regexes: callers pass the fully decoded body (up
    to max_bytes) and unclosed <script/<style would otherwise trigger
    O(k*n) backtracking."""

    if len(html_text) > 4 * cap:
        html_text = (html_text[:4 * cap]
                     + f" [...html truncated at {4 * cap} chars]")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'"))
    text = " ".join(text.split())
    return text[:cap]


def structure_message(raw: bytes, *, index: int, offset: int,
                      source_url: str, policy: dict,
                      msg_num: int | None = None) -> dict:
    """Structure one raw RFC822 message: headers / body / attachment
    metadata / provenance.

    Attachments are never separately extracted, written, or executed --
    only metadata is registered (filename, type, size, transfer
    encoding, sha256 digest); the encoded original stays inside the
    preserved raw response artifact (an inevitable property of the mbox
    path). The displayed body is bounded and honestly labelled when
    truncated."""

    cap = int(policy["max_msg_body_chars"])
    url_cap = int(policy["max_urls_per_message"])
    # The mbox From_ separator line is not a header; strip it before
    # feeding the parser (boundary information is carried by offset)
    if raw.startswith(b"From "):
        nl = raw.find(b"\n")
        raw_msg = raw[nl + 1:] if nl >= 0 else raw
    else:
        raw_msg = raw
    # Deeply nested MIME explodes both the stdlib parser and the walk
    # recursion below: convert it to an honest parse-error at the entry
    # point instead of crashing with a bare RecursionError
    try:
        msg = email.parser.BytesParser(
            policy=email.policy.compat32).parsebytes(raw_msg)
    except RecursionError as exc:
        raise BtsError("parse-error",
                       "MIME nesting too deep for the stdlib parser "
                       "(RecursionError); honest parse failure") from exc

    subject_raw = msg.get("Subject")
    from_raw = msg.get("From")
    date_raw = msg.get("Date")
    date_iso = None
    if date_raw:
        try:
            dt = email.utils.parsedate_to_datetime(date_raw)
            date_iso = dt.isoformat()
        except (TypeError, ValueError):
            date_iso = None

    body_text, charsets, html_only, attach_meta = "", [], False, []

    def walk(part):
        nonlocal body_text, html_only
        if part.is_multipart():
            for sub in part.get_payload():
                walk(sub)
            return
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        ctype = (part.get_content_type() or "text/plain").lower()
        is_attachment = ("attachment" in disp or bool(filename))
        if is_attachment:
            # Attachment: not separately extracted/written/executed;
            # the digest is computed over the payload already read into
            # memory (the whole response is bounded anyway). The encoded
            # original stays inside the preserved raw response artifact.
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            attach_meta.append({
                "filename": filename,
                "content_type": ctype,
                "bytes": len(payload),
                "transfer_encoding":
                    (part.get("Content-Transfer-Encoding") or "").strip(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "note": "not separately extracted, written, or executed; "
                        "encoded data may remain inside the preserved raw "
                        "response artifact",
            })
            return
        if ctype == "text/plain" and not body_text:
            body_text = _part_text(part) or ""
            cs = (part.get_content_charset() or "undetected")
            if cs not in charsets:
                charsets.append(cs)
        elif ctype == "text/html":
            if not body_text:
                html_text = _part_text(part) or ""
                body_text = _strip_html_bounded(html_text, cap)
                html_only = True
                cs = part.get_content_charset() or "undetected"
                if cs not in charsets:
                    charsets.append(cs)

    try:
        walk(msg)
    except RecursionError as exc:
        raise BtsError("parse-error",
                       "MIME nesting too deep to walk "
                       "(RecursionError); honest parse failure") from exc
    if not body_text and not attach_meta:
        # The SOAP body field is debbugs-flattened text, not the
        # original MIME (multipart/signed boundary lines are absent --
        # an honestly disclosed limitation). The parser reports
        # StartBoundaryNotFoundDefect and does not split -- fall back
        # to decoding the top-level text.
        payload = None
        try:
            payload = msg.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            payload = (msg.get_payload() or "").encode("utf-8", "replace")
        charset = msg.get_content_charset() or ""
        for attempt in (charset, "utf-8", "cp1252", "latin-1"):
            if not attempt:
                continue
            try:
                body_text = payload.decode(attempt)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            body_text = payload.decode("utf-8", "replace")
        if charset and charset not in charsets:
            charsets.append(charset)
    defects = [type(d).__name__ for d in getattr(msg, "defects", [])]
    truncated = len(body_text) > cap
    body_shown = body_text[:cap]
    if truncated:
        body_shown += f" [...body truncated at {cap} chars]"
    urls, seen = [], set()
    for m in _URL_RE.finditer(body_text):
        u = m.group(0).rstrip(".,;")
        if u not in seen:
            seen.add(u)
            urls.append(u)
        if len(urls) >= url_cap:
            break
    return {
        "index": index,
        "msg_num": msg_num,
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "date": date_raw,
        "date_iso": date_iso,
        "from": from_raw,
        "subject": subject_raw,
        "subject_decoded": _decode_rfc2047(subject_raw),
        "body_text": body_shown,
        "body_chars": len(body_text),
        "body_truncated": truncated,
        "html_only": html_only,
        "charsets": charsets,
        "attachments": attach_meta,
        "parse_defects": defects,
        "urls": urls,
        "source_url": source_url,
        "raw": {
            "offset": offset,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "boundary": "mbox-from-line" if raw.startswith(b"From ")
            else "soap-header-body",
        },
        "provenance_note": (
            "parsed from preserved raw artifact bytes at offset "
            f"{offset}; full raw response stored as this run's "
            f"per-source document"),
    }


def parse_mbox_messages(data: bytes, *, source_url: str, policy: dict
                        ) -> tuple[list[dict], dict]:
    blocks, separated = split_mbox(data)
    notes = []
    if not separated:
        notes.append("no mbox From_ separator recognized; parsed as one "
                     "blob (boundary information not available)")
    messages = []
    max_messages = int(policy["max_messages"])
    for index, (offset, length) in enumerate(blocks):
        if len(messages) >= max_messages:
            notes.append(f"message cap {max_messages} reached; "
                         f"{len(blocks) - max_messages} more message(s) "
                         f"not parsed")
            break
        messages.append(structure_message(
            data[offset:offset + length], index=index, offset=offset,
            source_url=source_url, policy=policy))
    return messages, {"source_format": "mbox", "notes": notes,
                      "messages_total": len(blocks)}


def parse_soap_bug_log(data: bytes, *, source_url: str, policy: dict
                       ) -> tuple[list[dict], dict]:
    """Parse a ``get_bug_log`` response: per-item header / body /
    msg_num.

    XML is handled by the stdlib ElementTree only (no external
    entities), and entities are decoded EXACTLY ONCE -- literal
    ``&amp;``-style text in a body must survive verbatim (locked by a
    regression test). The BTS ``attachments`` array inside items is NOT
    parsed (no documented structure; no speculative parser): a
    non-empty array is only disclosed in meta.notes. Non-2xx semantics
    are graded by the caller first; SOAP Faults are graded by the
    client."""

    notes = []
    attachments_array_items = 0
    messages = []
    max_messages = int(policy["max_messages"])
    try:
        root = ET.fromstring(data.decode("utf-8", "replace"))
    except ET.ParseError as exc:
        raise BtsError("parse-error",
                       f"SOAP response is not well-formed XML: {exc}") from exc
    fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        faultstring = ""
        for child in fault:
            if child.tag.endswith("faultstring"):
                faultstring = (child.text or "").strip()
        # faultstring comes from the server (up to max_bytes): bound the
        # error message to 200 characters
        raise BtsError("soap-fault",
                       f"SOAP Fault from BTS: "
                       f"{faultstring[:200] or 'unknown'}")
    # Loose namespace matching: item/header/body/msg_num appear in
    # urn:Debbugs/SOAP responses; nested items inside an attachments
    # array are not messages
    parent_map = {child: parent for parent in root.iter() for child in parent}

    def under_attachments(el) -> bool:
        node = parent_map.get(el)
        while node is not None:
            if node.tag.rsplit("}", 1)[-1] == "attachments":
                return True
            node = parent_map.get(node)
        return False

    items = [el for el in root.iter()
             if (el.tag.endswith("}item") or el.tag == "item")
             and not under_attachments(el)]
    if not items:
        raise BtsError("parse-error",
                       "get_bug_log response contains no message items")
    for index, item in enumerate(items):
        if len(messages) >= max_messages:
            notes.append(f"message cap {max_messages} reached; "
                         f"{len(items) - max_messages} more message(s) "
                         f"not parsed")
            break
        header_text = body_text = ""
        msg_num = None
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "header":
                # itertext performs exactly one (and only one) XML
                # entity decode; never decode again
                header_text = "".join(child.itertext())
            elif tag == "body":
                body_text = "".join(child.itertext())
            elif tag == "msg_num":
                try:
                    msg_num = int((child.text or "").strip())
                except ValueError:
                    msg_num = None
            elif tag == "attachments":
                if len(child) > 0:
                    attachments_array_items += 1
        header_text = _strip_mbox_from_lines(header_text)
        raw_msg = (header_text + "\n\n" + body_text).encode("utf-8", "replace")
        messages.append(structure_message(
            raw_msg, index=index, offset=-1, source_url=source_url,
            policy=policy, msg_num=msg_num))
    if attachments_array_items:
        notes.append(
            f"{attachments_array_items} item(s) carry a BTS attachments "
            f"array in the SOAP response which this parser does NOT "
            f"extract (structure undocumented here); attachment metadata "
            f"unavailable via this path -- honest disclosure, no "
            f"speculative parsing")
    return messages, {"source_format": "soap-get-bug-log", "notes": notes,
                      "messages_total": len(items)}


def _strip_mbox_from_lines(header_text: str) -> str:
    """SOAP header strings carry stray mbox From_ lines (observed in
    production); strip them so the email parser does not treat them as
    malformed headers."""

    out = []
    for line in header_text.splitlines():
        if line.startswith("From ") and _MBOX_FROM_RE.match(
                line.encode("utf-8", "replace")):
            continue
        out.append(line)
    return "\n".join(out)


def soap_fault_string(data: bytes) -> str | None:
    """Extract a SOAP Fault string from a payload, or ``None`` when the
    payload is not a fault-bearing envelope (used to grade HTTP 500
    responses)."""

    try:
        root = ET.fromstring(data.decode("utf-8", "replace"))
    except ET.ParseError:
        return None
    fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is None:
        return None
    for child in fault:
        if child.tag.endswith("faultstring"):
            return (child.text or "").strip()
    return ""
