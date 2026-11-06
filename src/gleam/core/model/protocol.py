"""Frozen investigate tools protocol.

The investigation tool schemas are frozen contract data: do not redefine or alter
them here. The ``fetch_udd`` dataset enum is part of the frozen schema, so
it is spelled out as a literal (``tests/test_model.py`` cross-checks the
literal against the live ``gleam.core.sources.udd`` catalog so drift is
caught by tests, not by a hidden import-time dependency).

Design notes:

- The ``github`` action enum includes ``forks`` (fork list sorted by
  most recently pushed,
  ``/repos/{owner}/{repo}/forks?sort=newest&per_page=100``).
- Operator-owned robots.txt exemption -- a domain-granular hostname
  list (``robots-exempt.txt``, key ``GLEAM_ROBOTS_EXEMPT_FILE``) read
  once at startup through the config layer and injected into the public
  fetcher; listed hosts skip the robots pre-fetch entirely and every
  exempted fetch is audit-marked robots ``"exempt"``
  (startup-validated, disclosed in every fetch policy record; all other
  safety layers and the full robots behavior for non-listed hosts are
  unchanged). Rationale: a target site's robots.txt may
  blanket-disallow all crawlers (repology.org) while its own API terms
  (https://repology.org/api) authorize programmatic access at <=1 rps
  -- specific permission beats the general prohibition.
- The investigate system-prompt rubric's rule (8) lineage duties are
  unconditional -- they bind every run before ANY final verdict. Rule
  (8) additionally carries two unconditional duties: (c) read the
  README of every repository identified as a candidate presence
  (upstream, fork, mirror, or alternative hosting) via
  ``github action=readme`` or ``fetch_url``, recording
  succession/inheritance/'passed away'/hand-over/abandonment
  declarations (blocked fetches are recorded honestly, never silently
  skipped); (d) search the web for the person or team behind every
  candidate upstream (identity and status news -- passing,
  retirement, hand-over, inactivity). Fork-exclusion reasons must be
  grounded in a liveness comparison against the parent; "it is a fork,
  not the canonical upstream" is explicitly not an exclusion reason.
- Finish-time uncovered-GitHub-repository gate + quotable-text
  lifeline -- the investigate final-report validation rejects a
  report while any GitHub repository observed in tool results exposed
  to the model on the two discovery surfaces (search_web result entry
  URLs, fetch_url page links/anchors) was never engaged by the run
  (no github call of any action for that owner/repo, and no fetch of
  a github.com/{owner}/{repo}... URL); github list payloads (forks
  lists) deliberately never count as discovery. Each uncovered repo
  surfaces one repair-loop validation error. The system-prompt rubric
  rule (3) additionally states the quotable-text lifeline: quotes may
  be verbatim substrings of any tool-returned text (fetched page
  bodies, search snippets, UDD table rows); on a failed page fetch the
  search snippet or UDD row observation is cited instead, never a
  quote from memory.
- The 8th managed tool is ``repology`` (actions ``search``/``project``
  against the operator-configured Repology base, default
  https://repology.org), and the 7th CLI module ``gleam repology``
  ships the same digests human-driven. Four closed-table keys
  (GLEAM_REPOLOGY_MAX_REQUESTS 6 / GLEAM_REPOLOGY_MIN_INTERVAL 1.1 /
  GLEAM_REPOLOGY_TIMEOUT 15 / GLEAM_REPOLOGY_BASE, 61 keys total)
  follow the BTS budget precedent, and every repology request
  reserves from the investigation's shared HTTP budget. The repology
  client carries a tool-intrinsic robots.txt exemption for the host of
  its configured base (the same API-terms rationale: Repology's own
  API terms authorize programmatic access on that API surface at <=1
  rps -- the tool IS the specific permission): the client's fetcher is
  constructed with exempt_hosts = operator_set UNION {base host},
  while the investigation's main fetcher keeps exactly the
  operator-file exemption set, so manual fetch_url of the same host
  stays governed by the operator file. Two fetcher configurations
  coexist by design; each fetch's audit record discloses its own
  effective robots_exempt list.
- Quote verification delegates to the verbatimeter library (MIT;
  deterministic, offline, bundled vocabulary) instead of ad-hoc string
  normalization: a quote passes when every word appears in the cited
  observation's exposed text, tolerating case, punctuation and markup
  formatting (a substituted, paraphrased or never-returned quote
  fails). Quotes below the library's 3-word floor (URLs, identifiers)
  fall back to exact containment. GitHub README bodies are rendered to
  plain text at ingestion (markdown-it-py) so the model quotes prose,
  not markup. The ``finish_investigation`` quote description and
  system-prompt rubric rule (3) state the tolerance.
 - A discovered GitHub link the model judges wrong (a topic or user
   page, an unrelated project) may be declared in the report's
   optional ``dismissed_links`` array with a required reason instead
   of being fetched; the uncovered-repository gate then treats it as
   handled. Dismissals must reference a GitHub repository URL a tool
   result actually showed in this run (memory URLs stay invalid) and
   are rendered for the human reviewer.
 - The investigation exposes the live GitHub discovery/coverage ledger
   through ``inspect_report_state`` and permits repeated non-final
   ``validate_report_draft`` previews. Draft previews run the same
   report validator without consuming a final-report repair; rejected
   ``finish_investigation`` calls also return the ledger.
"""

from __future__ import annotations

# 18 UDD dataset names, registry insertion order, frozen as schema data.
_DATASET_ENUM = [
    "sources", "upstream", "upstream_metadata", "vcswatch", "urls",
    "binaries", "bugs", "bugs_count", "archived_bugs", "wnpp",
    "upload_history", "upload_history_closes", "removals",
    "testing_autoremovals", "popcon_src", "ci", "reproducible", "lintian",
]

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch one http(s) URL through the bounded, "
                       "robots-respecting, SSRF-protected collector. Returns "
                       "an observation id, final URL after redirects, "
                       "readable bounded body text, and links extracted from "
                       "HTML pages.",
        "parameters": {"type": "object",
                       "properties": {
                           "url": {"type": "string",
                                   "description": "absolute http(s) URL"}},
                       "required": ["url"],
                       "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "search_web",
        "description": "Web search via the operator-configured local "
                       "SearXNG instance (JSON API). Returns normalized "
                       "results (title, canonical URL, snippet, engine "
                       "attribution) plus unresponsive engines. Snippets "
                       "are third-party search summaries: weaker evidence "
                       "than fetched pages, not identity confirmation. "
                       "Failures (unreachable, forbidden, rate limited, "
                       "all engines failed) are explicit and mean unknown, "
                       "never absence of results.",
        "parameters": {"type": "object",
                       "properties": {
                           "query": {"type": "string",
                                     "description": "search query "
                                                    "(max 400 chars)"}},
                       "required": ["query"],
                       "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "github",
        "description": "Query the GitHub REST API for one repository "
                       "(action: repo = repository metadata, readme = "
                       "default-branch README, releases = release list, "
                       "commits = commit list, forks = fork list of the "
                       "repository sorted by most recently pushed). "
                       "Bounded pagination via "
                       "page/per_page; next_page is returned when more "
                       "pages exist (never followed automatically). Rate "
                       "limits (403/429) expose retry/reset headers. 404 "
                       "means unknown (private/renamed/none), not death. "
                       "API facts are not official-identity proof.",
        "parameters": {"type": "object",
                       "properties": {
                            "action": {"type": "string",
                                       "enum": ["repo", "readme",
                                                "releases", "commits",
                                                "forks"]},
                           "owner": {"type": "string",
                                     "description": "GitHub owner login"},
                           "repo": {"type": "string",
                                    "description": "repository name"},
                           "page": {"type": "integer",
                                    "description": "page number 1..100 "
                                                   "(list actions only)"},
                           "per_page": {"type": "integer",
                                        "description": "items per page "
                                                       "1..100 (default 10)"}},
                       "required": ["action", "owner", "repo"],
                       "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "read_evidence",
        "description": "Re-read a stored observation from this "
                       "investigation run (metadata plus a bounded window "
                       "of body text and links). Use offset/links_offset "
                       "to page beyond the first window instead of "
                       "re-reading the same prefix; next_offset/"
                       "next_links_offset tell you how to continue.",
        "parameters": {"type": "object",
                       "properties": {
                           "observation_id": {"type": "string",
                                              "description": "e.g. obs-0001"},
                           "offset": {"type": "integer",
                                      "description": "body text char "
                                                     "offset (>= 0)"},
                           "links_offset": {"type": "integer",
                                            "description": "links index "
                                                           "offset (>= 0)"}},
                       "required": ["observation_id"],
                       "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "fetch_bug",
        "description": "Fetch the full public discussion of one Debian "
                       "BTS bug (all log messages) through the documented "
                       "public BTS machine interface (Debbugs/SOAP "
                       "get_bug_log; bounded, read-only; robots facts "
                       "recorded). source=auto (default) tries SOAP first "
                       "and falls back to the official mbox view only for "
                       "eligible failures (transport/parse/challenge) AND "
                       "only when robots.txt clearly permits it — never on "
                       "rate-limit/not-found/budget exhaustion, never "
                       "bypassing robots; source=soap|mbox is explicit "
                       "with no fallback. Returns per-message metadata "
                       "(message-id, date, from, subject), bounded body "
                       "text with preserved message order/boundaries, URLs "
                       "found in bodies, per-source attempt provenance, "
                       "and attachment METADATA only (attachments are "
                       "never separately extracted, written, or executed; "
                       "encoded data may remain inside the preserved raw "
                       "artifact; the SOAP attachments array is not "
                       "extracted). A proof-of-work challenge, if "
                       "encountered, is reported honestly (solved or "
                       "explicit failure); errors mean unknown, never "
                       "'no discussion'.",
        "parameters": {"type": "object",
                       "properties": {
                           "bug_id": {"description": "Debian bug number "
                                                    "(strictly numeric "
                                                    "1..9999999)",
                                      "type": "integer"},
                           "source": {"description": "where to fetch the "
                                                    "discussion from "
                                                    "(default auto)",
                                      "type": "string",
                                      "enum": ["auto", "soap", "mbox"]}},
                       "required": ["bug_id"],
                       "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "fetch_udd",
        "description": "Query the live Ultimate Debian Database (UDD) "
                       "public readonly mirror for the package under "
                       "investigation with fixed, bounded, read-only SQL "
                        "templates via the built-in pg8000 driver "
                        "(no arbitrary SQL; DSN is fixed "
                       "and not controllable). action='list' returns the "
                       "18-dataset catalog (columns, filters, caveats; no "
                       "database query). action='fetch' queries 1-6 "
                       "datasets per call. Dataset triggers: upstream/urls "
                       "-> upstream alive; upload_history -> last upload; "
                       "removals/testing_autoremovals -> removal status; "
                       "bugs/bugs_count/archived_bugs -> open/closed bug "
                       "counts; sources/binaries -> versions/uploaders; "
                       "vcswatch -> packaging VCS/CI; upstream_metadata -> "
                       "DEP-12 fields; popcon_src -> popcon; ci -> CI "
                       "runs; reproducible -> reproducibility; lintian -> "
                       "lintian tags; wnpp -> WNPP/ITP/ITA; "
                       "upload_history_closes -> bug closure. Call "
                       "action='list' once for full columns/filters/"
                       "caveats. 0 rows is honest; errors mean unknown, "
                       "never absence.",
        "parameters": {"type": "object",
                       "properties": {
                           "action": {"type": "string",
                                      "enum": ["list", "fetch"],
                                      "description": "list = catalog only "
                                                     "(no DB); fetch = "
                                                     "query datasets"},
                           "datasets": {"type": "array",
                                        "items": {
                                            "type": "string",
                                            "enum": list(_DATASET_ENUM)},
                                        "description": "1-6 dataset names "
                                                       "from action=list; "
                                                       "required when "
                                                       "action=fetch"},
                           "release": {"type": "string",
                                       "description": "optional release; "
                                                      "defaults to the "
                                                      "operator --release"},
                           "limit": {"type": "integer", "minimum": 1,
                                     "maximum": 50,
                                     "description": "rows per dataset "
                                                    "(default 20)"}},
                       "required": ["action"],
                       "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "repology",
        "description": "Cross-distro package version tracker "
                       "(Repology API). action=search resolves a name "
                       "to project slugs (each row: slug, visible "
                       "name, newest version, repo count; capped at 12 "
                       "rows plus a more count; exact_match is the "
                       "case-insensitive presence of the query itself "
                       "among the returned slugs). action=project "
                       "returns the version ladder for one slug "
                       "(entries grouped by version, newest first, "
                       "capped at 15 tiers with the remainder "
                       "aggregated into one disclosed tier; each tier: "
                       "version, repo_count, repos capped at 8 names, "
                       "statuses, vulnerable_repos; debian_* "
                       "repositories carry a 'deb:' prefix and the "
                       "separate debian block lists every debian_* "
                       "repo/version pair). 200 with zero entries "
                       "means no such project; errors mean unknown, "
                       "never 'no results'. Search results are "
                       "unordered and may omit the exact slug (the "
                       "API caps results); when exact_match is false "
                       "but the name looks canonical, try "
                       "action=project with the plain name. Statuses "
                       "(newest/outdated/legacy/rolling) are relative "
                       "to the globally newest version across all "
                       "repositories. Use search first when unsure of "
                       "the slug: name collisions exist.",
        "parameters": {"type": "object",
                       "properties": {
                           "action": {"type": "string",
                                      "enum": ["search", "project"]},
                           "query": {"type": "string",
                                     "description": "name to resolve "
                                                    "(search action; "
                                                    "max 200 chars)"},
                           "slug": {"type": "string",
                                    "description": "project slug "
                                                   "(project action; "
                                                   "max 200 chars)"}},
                       "required": ["action"],
                       "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "finish_investigation",
        "description": "Submit the final structured report. Candidate URLs "
                       "must have been observed in tool results of this run; "
                       "quotes must be word-for-word from observed page "
                       "text (case/punctuation/markdown differences are "
                       "tolerated; altered or paraphrased quotes are "
                       "rejected); supporting_obs_ids must reference "
                       "observations of this run. Observed GitHub links "
                       "you judge wrong may be listed in dismissed_links "
                       "with a reason instead of being fetched.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "candidates": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "why": {"type": "string",
                                "description": "why this looks like the "
                                               "upstream presence"},
                        "supporting_obs_ids": {"type": "array",
                                               "items": {"type": "string"}},
                        "quote": {"type": "string",
                                  "description": "verbatim quote from an "
                                                 "observed page body"},
                        "uncertainty": {"type": "string"},
                    },
                    "required": ["url", "why", "supporting_obs_ids",
                                 "quote"],
                    "additionalProperties": False}},
                "uncertainties": {"type": "string"},
                "fact_evidence": {"type": "array",
                                  "items": {"type": "string"},
                                  "description": "optional live UDD fact "
                                                 "citations like "
                                                 "'udd:sources#row=1' "
                                                 "(copy exactly from the "
                                                 "provenance arrays "
                                                 "fetch_udd returned; "
                                                 "only rows actually "
                                                 "shown to you are "
                                                 "citable)"},
                "dismissed_links": {
                    "type": "array",
                    "description": "observed GitHub links you judge not "
                                   "worth engaging; each needs a reason "
                                   "and is exempt from the "
                                   "uncovered-repository check",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string",
                                    "description": "exact GitHub "
                                                   "repository URL a tool "
                                                   "result showed"},
                            "reason": {"type": "string",
                                       "description": "why this link is "
                                                      "not a repository "
                                                      "to engage"},
                        },
                        "required": ["url", "reason"],
                        "additionalProperties": False}},
            },
            "required": ["summary", "candidates", "uncertainties"],
            "additionalProperties": False},
     }},
     {"type": "function", "function": {
         "name": "inspect_report_state",
         "description": "Return the live GitHub link ledger before final "
                        "submission: every repository link exposed by a "
                        "search result or fetched page, whether this run "
                        "already covered it, and which repositories remain "
                        "uncovered. This is read-only and does not end the "
                        "investigation.",
         "parameters": {"type": "object", "properties": {},
                        "required": [],
                        "additionalProperties": False},
     }},
     {"type": "function", "function": {
         "name": "validate_report_draft",
         "description": "Preview a complete final report against the "
                        "current evidence and GitHub ledger. Revise the "
                        "draft and call this tool again as often as needed; "
                        "this never ends the investigation and does not "
                        "consume a finish_investigation repair.",
         "parameters": {"type": "object", "properties": {
             "draft": {"type": "object",
                       "description": "Complete report draft with summary, "
                                      "candidates, uncertainties, optional "
                                      "fact_evidence and dismissed_links"},
         }, "required": ["draft"], "additionalProperties": False},
     }},
]

TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOL_SPECS)
