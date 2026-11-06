"""L3 sources domain: typed read-only adapters for remote evidence sources.

This package hosts the UDD, BTS, GitHub, and SearXNG adapters. Adapters
expose typed Request/Result surfaces only; CLI parsing, rendering, and
exit mapping live in the entry layer. Sources modules never
read the process environment and never import entry modules.
"""
