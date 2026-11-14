import sys

from gleam.router import dispatch


def _reconfigure_streams() -> None:
    """Pin stdout/stderr to UTF-8 with LF-only line endings.

    Byte determinism for ``--json`` output and no cp1252
    UnicodeEncodeError in human output: on Windows both streams default
    to the locale encoding and LF->CRLF translation; ``reconfigure`` is
    a no-op elsewhere. Skipped when a stream lacks ``reconfigure``
    (e.g. a replaced/None stream under an embedding host).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")


if __name__ == "__main__":
    _reconfigure_streams()
    sys.exit(dispatch(sys.argv[1:]))
