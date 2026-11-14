"""Entry shells: thin parser -> bind -> execute -> render wiring.

Every entry module is a shell over the core capability domains: it owns
only the flag surface (via ``gleam.core.config.bind`` and the
declarative ``MODULE_SPECS``), mode checks, rendering, and the exit
code. All business logic lives in ``gleam.core``.
"""
