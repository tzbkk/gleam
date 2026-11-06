"""Fixed UDD dataset catalog, SQL templates, and statement assembly.

Single source of the 18-dataset registry: every
dataset keeps its frozen physical table, filter, deterministic ORDER
BY, column list, limits, and caveats. SQL is a fixed application-side
template; caller input only ever enters through named parameters
(``:source``, ``:release``, ``:limit1``), never by string
concatenation.

Deliberately absent: the group query path
and the maintainer-based package enumeration mode; neither template
family exists. This module is pure data plus statement assembly --
no child process, no I/O, no clock.
"""

from __future__ import annotations

import re

__all__ = [
    "DATASETS",
    "DATASETS_TEMPLATE_VERSION",
    "DATASET_NAMES",
    "DEBIAN_SOURCE_NAME",
    "MAX_DATASET_LIMIT",
    "RELEASES",
    "build_statements",
    "list_datasets",
]

#: Template family identifier carried in results for provenance.
DATASETS_TEMPLATE_VERSION = "udd-fetch-templates"

#: Debian source-name policy: lowercase letters, digits, and
#: ``+ . -`` after a leading letter or digit, 64 characters at most.
DEBIAN_SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]+$")

#: Registry-wide per-dataset hard row limit (every dataset has
#: ``max_limit = 50``).
MAX_DATASET_LIMIT = 50

# Distinct release values observed on the live mirror
# (bookworm...trixie-updates) plus suite aliases (aliases are let
# through conservatively; an unknown-to-the-DB alias is an honest
# empty result).
RELEASES = frozenset({
    "bookworm", "bookworm-backports", "bookworm-backports-sloppy",
    "bookworm-security", "bookworm-updates",
    "bullseye", "bullseye-security", "bullseye-updates",
    "experimental", "forky", "sid",
    "trixie", "trixie-backports", "trixie-proposed-updates",
    "trixie-security", "trixie-updates",
    "oldoldstable", "oldstable", "stable", "testing", "unstable",
})

# ---- Fixed SQL templates (application-side constants) ------------------------
#
# Common conventions: every column is explicitly aliased (driver rows
# are read by column name; coalesce must be aliased or the name would
# read "coalesce"); timestamps are rendered server-side with
# ::text and kept verbatim. Templates end with ORDER BY; the LIMIT
# clause is appended per dataset (plain
# ``LIMIT :limit1`` or ``LIMIT LEAST(:limit1, cap+1)`` for heavy
# tables).

_RELEASE_FILTER = "AND (:release = '' OR release = :release)"

_T_META = (
    "SELECT version() AS server_version, "
    "now()::text AS server_now;")

_DS_SOURCES = (
    "SELECT source, version::text AS version, release,\n"
    "       coalesce(maintainer,'') AS maintainer,\n"
    "       coalesce(homepage,'') AS homepage, coalesce(bin,'') AS bin,\n"
    "       coalesce(vcs_type,'') AS vcs_type, coalesce(vcs_url,'') AS vcs_url,\n"
    "       coalesce(vcs_browser,'') AS vcs_browser,\n"
    "       coalesce(section,'') AS section, coalesce(priority,'') AS priority\n"
    "FROM sources WHERE source = :source " + _RELEASE_FILTER + "\n"
    "ORDER BY release")

_DS_UPSTREAM = (
    "SELECT source, coalesce(version::text,'') AS version,\n"
    "       release, coalesce(status,'') AS status,\n"
    "       coalesce(upstream_version,'') AS upstream_version,\n"
    "       coalesce(upstream_url,'') AS upstream_url,\n"
    "       coalesce(watch_file,'') AS watch_file,\n"
    "       coalesce(errors,'') AS errors,\n"
    "       last_check::text AS last_check\n"
    "FROM upstream WHERE source = :source " + _RELEASE_FILTER + "\n"
    "ORDER BY release")

_DS_UPSTREAM_METADATA = (
    "SELECT source, key, coalesce(value,'') AS value\n"
    "FROM upstream_metadata WHERE source = :source\n"
    "ORDER BY key")

# vcswatch: issues/merge_requests base types were never re-verified
# column by column; coalesce(...::text,'') is safe for int/text alike.
_DS_VCSWATCH = (
    "SELECT source, coalesce(version::text,'') AS version,\n"
    "       coalesce(vcs,'') AS vcs, coalesce(url,'') AS url,\n"
    "       coalesce(browser,'') AS browser, coalesce(status,'') AS status,\n"
    "       last_scan::text AS last_scan,\n"
    "       coalesce(changelog_version::text,'') AS changelog_version,\n"
    "       coalesce(changelog_distribution,'') AS changelog_distribution,\n"
    "       coalesce(commit_id,'') AS commit_id,\n"
    "       coalesce(error,'') AS error,\n"
    "       coalesce(ci_status,'') AS ci_status,\n"
    "       coalesce(issues::text,'') AS issues,\n"
    "       coalesce(merge_requests::text,'') AS merge_requests\n"
    "FROM vcswatch WHERE source = :source\n"
    "ORDER BY source")

# duck_urls has no primary key (live-DB fact) -> ORDER BY lists every
# display column and ends with ctid; duck_url_status is LEFT JOINed:
# absent status columns mean unknown, not dead.
_DS_URLS = (
    "SELECT d.source AS source, coalesce(d.version::text,'') AS version,\n"
    "       coalesce(d.component,'') AS component, coalesce(d.type,'') AS type,\n"
    "       coalesce(d.checkmethod,'') AS checkmethod, coalesce(d.url,'') AS url,\n"
    "       coalesce(d.filename,'') AS filename, coalesce(d.override::text,'') AS override,\n"
    "       coalesce(s.status_code::text,'') AS status_code,\n"
    "       coalesce(s.response_time_ms::text,'') AS response_time_ms,\n"
    "       coalesce(s.error_message,'') AS error_message,\n"
    "       s.last_checked::text AS last_checked,\n"
    "       coalesce(s.redirect_url,'') AS redirect_url,\n"
    "       coalesce(s.is_ok::text,'') AS is_ok\n"
    "FROM duck_urls d LEFT JOIN duck_url_status s\n"
    "       ON s.url = d.url AND s.checkmethod = d.checkmethod\n"
    "WHERE d.source = :source\n"
    "ORDER BY d.source, d.version, d.component, d.url, "
    "coalesce(d.filename,''), d.checkmethod, d.ctid")

_DS_BINARIES = (
    "SELECT package, release\n"
    "FROM (SELECT DISTINCT package, release FROM packages "
    "WHERE source = :source " + _RELEASE_FILTER + ") t\n"
    "ORDER BY package, release")

# Open bugs: no assumption that bugs.package equals the source name.
# Match = BTS source mapping OR src: pseudo-package OR bugs_packages
# multi-package association OR binary->source mapping; every row keeps
# its association provenance flags.
_DS_BUGS = (
    "SELECT b.id::text AS id, coalesce(b.package,'') AS package,\n"
    "       coalesce(b.source,'') AS source, b.status AS status,\n"
    "       coalesce(b.severity::text,'') AS severity,\n"
    "       coalesce(b.title,'') AS title,\n"
    "       b.arrival::text AS arrival, b.last_modified::text AS last_modified,\n"
    "       coalesce(b.done,'') AS done,\n"
    "       coalesce((SELECT string_agg(tag, ',') FROM bugs_tags t "
    "WHERE t.id = b.id), '') AS tags,\n"
    "       (b.source = :source)::text AS via_source_mapping,\n"
    "       EXISTS (SELECT 1 FROM bugs_packages bp WHERE bp.id = b.id "
    "AND bp.source = :source)::text AS via_bugs_packages\n"
    "FROM bugs b\n"
    "WHERE b.status <> 'done'\n"
    "  AND (b.source = :source OR b.package = :source\n"
    "       OR b.package IN (SELECT package FROM bugs_packages "
    "WHERE source = :source)\n"
    "       OR b.package IN (SELECT DISTINCT package FROM packages "
    "WHERE source = :source))\n"
    "ORDER BY b.id")

_DS_BUGS_COUNT = (
    "SELECT source, coalesce(rc_bugs::text,'') AS rc_bugs,\n"
    "       coalesce(all_bugs::text,'') AS all_bugs\n"
    "FROM bugs_count WHERE source = :source\n"
    "ORDER BY source")

# archived_bugs ~1M rows (heavy table, effective cap 10); the filter
# only matches the BTS source mapping (binary-only archived bugs do
# not match and stay an honest empty result).
_DS_ARCHIVED_BUGS = (
    "SELECT b.id::text AS id,\n"
    "       coalesce(b.package,'') AS package, coalesce(b.source,'') AS source,\n"
    "       b.status AS status, coalesce(b.severity::text,'') AS severity,\n"
    "       coalesce(b.title,'') AS title,\n"
    "       b.arrival::text AS arrival, b.last_modified::text AS last_modified,\n"
    "       coalesce(b.done,'') AS done,\n"
    "       coalesce((SELECT string_agg(tag, ',') FROM bugs_tags t "
    "WHERE t.id = b.id), '') AS tags\n"
    "FROM archived_bugs b\n"
    "WHERE b.source = :source\n"
    "ORDER BY b.id")

# upload_history ~1M rows; version is debversion (shown via ::text);
# nmu is a boolean.
_DS_UPLOAD_HISTORY = (
    "SELECT version::text AS version, date::text AS date,\n"
    "       coalesce(changed_by,'') AS changed_by, coalesce(nmu::text,'') AS nmu,\n"
    "       coalesce(distribution,'') AS distribution, coalesce(file,'') AS file\n"
    "FROM upload_history WHERE source = :source\n"
    "ORDER BY date DESC, version DESC, file")

_DS_UPLOAD_HISTORY_CLOSES = (
    "SELECT source, version::text AS version,\n"
    "       bug::text AS bug, coalesce(file,'') AS file\n"
    "FROM upload_history_closes WHERE source = :source\n"
    "ORDER BY bug DESC, version DESC, file")

# Removal records: package_removal (one row per package; a removal of
# the source package means 'source' appears in arch_array) LEFT JOINed
# with package_removal_batch (batch time/ftpmaster/reasons). 0 rows =
# never removed (a valid result).
_DS_REMOVALS = (
    "SELECT r.batch_id::text AS batch_id, r.name AS name,\n"
    "       r.version::text AS version,\n"
    "       array_to_string(r.arch_array, ',') AS arch_array,\n"
    "       b.time::text AS time, coalesce(b.ftpmaster,'') AS ftpmaster,\n"
    "       coalesce(b.distribution,'') AS distribution,\n"
    "       coalesce(b.requestor,'') AS requestor,\n"
    "       coalesce(b.reasons,'') AS reasons\n"
    "FROM package_removal r "
    "LEFT JOIN package_removal_batch b ON b.id = r.batch_id\n"
    "WHERE r.name = :source AND 'source' = ANY(r.arch_array)\n"
    "ORDER BY r.batch_id DESC, r.name")

# testing_autoremovals time columns are epoch integers (rendered via
# to_timestamp(...)::text). 0 rows = not pending autoremoval.
_DS_TESTING_AUTOREMOVALS = (
    "SELECT source,\n"
    "       coalesce(version::text,'') AS version,\n"
    "       to_timestamp(first_seen)::text AS first_seen,\n"
    "       to_timestamp(last_checked)::text AS last_checked,\n"
    "       to_timestamp(removal_time)::text AS removal_time,\n"
    "       coalesce(rdeps::text,'') AS rdeps,\n"
    "       coalesce(buggy_deps::text,'') AS buggy_deps,\n"
    "       coalesce(bugs_deps::text,'') AS bugs_deps,\n"
    "       coalesce(bugs::text,'') AS bugs,\n"
    "       coalesce(rdeps_popcon::text,'') AS rdeps_popcon\n"
    "FROM testing_autoremovals WHERE source = :source\n"
    "ORDER BY source")

_DS_POPCON_SRC = (
    "SELECT source, insts::text AS insts,\n"
    "       vote::text AS vote, olde::text AS olde,\n"
    "       recent::text AS recent, nofiles::text AS nofiles\n"
    "FROM popcon_src WHERE source = :source\n"
    "ORDER BY source")

# ci (ci.debian.net import); duration base type not re-verified
# (interval or numeric), coalesce(...::text,'') is safe for both.
_DS_CI = (
    "SELECT coalesce(suite,'') AS suite, coalesce(arch,'') AS arch,\n"
    "       source, version::text AS version, date::text AS date,\n"
    "       run_id::text AS run_id, coalesce(status,'') AS status,\n"
    "       coalesce(previous_status,'') AS previous_status,\n"
    "       coalesce(duration::text,'') AS duration,\n"
    "       coalesce(message,'') AS message\n"
    "FROM ci WHERE source = :source\n"
    "ORDER BY date DESC, run_id")

# reproducible has a release column -> the release filter applies;
# version is debversion (live-DB column check).
_DS_REPRODUCIBLE = (
    "SELECT source, version::text AS version,\n"
    "       release, architecture, coalesce(status,'') AS status\n"
    "FROM reproducible WHERE source = :source " + _RELEASE_FILTER + "\n"
    "ORDER BY release, architecture")

# lintian is a ~1.2GB materialized view (heavy table, effective cap
# 10); rows have no unique key -> ORDER BY ends with ctid so the row
# order stays deterministic.
_DS_LINTIAN = (
    "SELECT package, coalesce(tag_type::text,'') AS tag_type,\n"
    "       coalesce(package_type,'') AS package_type,\n"
    "       package_version::text AS package_version,\n"
    "       coalesce(package_arch,'') AS package_arch, coalesce(tag,'') AS tag,\n"
    "       coalesce(information,'') AS information\n"
    "FROM lintian\n"
    "WHERE package = :source AND package_type = 'source'\n"
    "ORDER BY tag, package_version, package_arch, ctid")

_DS_WNPP = (
    "SELECT id::text AS id, coalesce(type,'') AS type,\n"
    "       coalesce(source,'') AS source, coalesce(title,'') AS title\n"
    "FROM wnpp WHERE source = :source ORDER BY id")

# The 18-dataset registry (single source of truth; insertion order is
# the catalog order). row_cap=None -> the effective cap equals the
# requested limit (<= 50); heavy tables carry row_cap=10 -> the
# effective cap is min(limit, 10) even at higher limits.
DATASETS: dict[str, dict] = {
    "sources": {
        "question": "Which releases carry this source package, at which "
                    "version, with which maintainer/VCS/homepage metadata?",
        "physical": "sources",
        "filter": "source = :source + optional release",
        "order_by": "release",
        "columns": ["source", "version", "release", "maintainer",
                    "homepage", "bin", "vcs_type", "vcs_url",
                    "vcs_browser", "section", "priority"],
        "template": _DS_SOURCES,
        "joins": "none",
        "release_scoped": True,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["one row per release is kept (no dedupe to one "
                    "version)"],
    },
    "upstream": {
        "question": "What did uscan last report for this source "
                    "(upstream version, watch status, errors)?",
        "physical": "upstream",
        "filter": "source = :source + optional release",
        "order_by": "release",
        "columns": ["source", "version", "release", "status",
                    "upstream_version", "upstream_url", "watch_file",
                    "errors", "last_check"],
        "template": _DS_UPSTREAM,
        "joins": "none",
        "release_scoped": True,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["watch_file is fetched verbatim and never executed"],
    },
    "upstream_metadata": {
        "question": "Which DEP-12 upstream-metadata keys (Repository, "
                    "Homepage, ...) are declared for this source?",
        "physical": "upstream_metadata",
        "filter": "source = :source",
        "order_by": "key",
        "columns": ["source", "key", "value"],
        "template": _DS_UPSTREAM_METADATA,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["0 rows is a valid result (DEP-12 coverage is low)"],
    },
    "vcswatch": {
        "question": "What does VcsWatch last know about the packaging VCS "
                    "(scan status, changelog version hints, CI)?",
        "physical": "vcswatch",
        "filter": "source = :source",
        "order_by": "source",
        "columns": ["source", "version", "vcs", "url", "browser",
                    "status", "last_scan", "changelog_version",
                    "changelog_distribution", "commit_id", "error",
                    "ci_status", "issues", "merge_requests"],
        "template": _DS_VCSWATCH,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["packaging VCS facts, not upstream identity proof"],
    },
    "urls": {
        "question": "Which URLs does DUCK track for this source, and what "
                    "did the last reachability check show?",
        "physical": "duck_urls d LEFT JOIN duck_url_status s "
                    "ON s.url = d.url AND s.checkmethod = d.checkmethod",
        "filter": "d.source = :source",
        "order_by": "d.source, d.version, d.component, d.url, "
                    "coalesce(d.filename,''), d.checkmethod, d.ctid",
        "columns": ["source", "version", "component", "type",
                    "checkmethod", "url", "filename", "override",
                    "status_code", "response_time_ms", "error_message",
                    "last_checked", "redirect_url", "is_ok"],
        "template": _DS_URLS,
        "joins": "duck_urls LEFT JOIN duck_url_status on "
                 "url+checkmethod (status columns may be absent)",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["LEFT JOIN: absent status columns mean unknown, not "
                    "dead",
                    "duck_urls has no primary key; ORDER BY is finalized "
                    "with ctid"],
    },
    "binaries": {
        "question": "Which binary packages (per release) are built from "
                    "this source?",
        "physical": "packages (DISTINCT package, release)",
        "filter": "source = :source (inner subquery) + optional release",
        "order_by": "package, release",
        "columns": ["package", "release"],
        "template": _DS_BINARIES,
        "joins": "none (DISTINCT subquery over per-architecture rows)",
        "release_scoped": True,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["one row per package x release (packages repeats per "
                    "architecture, deduplicated here)"],
    },
    "bugs": {
        "question": "Which open bugs match this source via BTS mappings "
                    "(with per-row association provenance)?",
        "physical": "bugs b (+ bugs_tags / bugs_packages / packages "
                    "subqueries)",
        "filter": "existing 4-way match (BTS source mapping OR src: "
                  "pseudo-package OR bugs_packages OR packages mapping)",
        "order_by": "b.id",
        "columns": ["id", "package", "source", "status", "severity",
                    "title", "arrival", "last_modified", "done", "tags",
                    "via_source_mapping", "via_bugs_packages"],
        "template": _DS_BUGS,
        "joins": "none (correlated bugs_tags subquery + bugs_packages/"
                 "packages IN subqueries)",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["open bugs only (status <> 'done')",
                    "association provenance per row via "
                    "via_source_mapping / via_bugs_packages"],
    },
    "bugs_count": {
        "question": "How many release-critical and total open bugs are "
                    "filed against this source?",
        "physical": "bugs_count (view)",
        "filter": "source = :source",
        "order_by": "source",
        "columns": ["source", "rc_bugs", "all_bugs"],
        "template": _DS_BUGS_COUNT,
        "joins": "none (view)",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": [],
    },
    "archived_bugs": {
        "question": "Which archived (done) bugs are filed against this "
                    "source via the BTS source mapping?",
        "physical": "archived_bugs b (+ bugs_tags subquery)",
        "filter": "b.source = :source",
        "order_by": "b.id",
        "columns": ["id", "package", "source", "status", "severity",
                    "title", "arrival", "last_modified", "done", "tags"],
        "template": _DS_ARCHIVED_BUGS,
        "joins": "none (correlated bugs_tags subquery)",
        "release_scoped": False,
        "default_limit": 10,
        "max_limit": 50,
        "row_cap": 10,
        "caveats": ["heavy: ~1M rows; effective cap is 10 rows even when "
                    "a higher limit is requested"],
    },
    "wnpp": {
        "question": "Is there a WNPP work request (O/RFA/RFH/ITA/ITP/RFP) "
                    "for this source?",
        "physical": "wnpp (view)",
        "filter": "source = :source",
        "order_by": "id",
        "columns": ["id", "type", "source", "title"],
        "template": _DS_WNPP,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["source may be NULL when the title parsing fails; "
                    "such rows cannot match the filter"],
    },
    "upload_history": {
        "question": "When was this source uploaded, by whom, to which "
                    "distribution (newest first)?",
        "physical": "upload_history",
        "filter": "source = :source",
        "order_by": "date DESC, version DESC, file",
        "columns": ["version", "date", "changed_by", "nmu",
                    "distribution", "file"],
        "template": _DS_UPLOAD_HISTORY,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["~1M rows; newest uploads come first (date DESC)"],
    },
    "upload_history_closes": {
        "question": "Which bugs were closed by which uploads of this "
                    "source?",
        "physical": "upload_history_closes",
        "filter": "source = :source",
        "order_by": "bug DESC, version DESC, file",
        "columns": ["source", "version", "bug", "file"],
        "template": _DS_UPLOAD_HISTORY_CLOSES,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": [],
    },
    "removals": {
        "question": "Was this source package ever removed from a suite, "
                    "when, by whom, and why?",
        "physical": "package_removal r LEFT JOIN package_removal_batch b "
                    "ON b.id = r.batch_id",
        "filter": "r.name = :source AND 'source' = ANY(r.arch_array)",
        "order_by": "r.batch_id DESC, r.name",
        "columns": ["batch_id", "name", "version", "arch_array", "time",
                    "ftpmaster", "distribution", "requestor", "reasons"],
        "template": _DS_REMOVALS,
        "joins": "package_removal LEFT JOIN package_removal_batch on "
                 "batch_id = id (batch columns may be absent)",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["0 rows is a valid result (the source was never "
                    "removed)"],
    },
    "testing_autoremovals": {
        "question": "Is this source pending autoremoval from testing, "
                    "with which rdeps and triggering bugs?",
        "physical": "testing_autoremovals",
        "filter": "source = :source",
        "order_by": "source",
        "columns": ["source", "version", "first_seen", "last_checked",
                    "removal_time", "rdeps", "buggy_deps", "bugs_deps",
                    "bugs", "rdeps_popcon"],
        "template": _DS_TESTING_AUTOREMOVALS,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["0 rows is a valid result (not pending autoremoval)",
                    "first_seen/last_checked/removal_time are epoch "
                    "integers rendered via to_timestamp()::text"],
    },
    "popcon_src": {
        "question": "How popular is this source package per popcon "
                    "(insts/vote/olde/recent/nofiles)?",
        "physical": "popcon_src",
        "filter": "source = :source",
        "order_by": "source",
        "columns": ["source", "insts", "vote", "olde", "recent",
                    "nofiles"],
        "template": _DS_POPCON_SRC,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": ["0 rows is a valid result (absent from popcon)"],
    },
    "ci": {
        "question": "What do the latest ci.debian.net autopkgtest runs "
                    "show per suite/arch (newest first)?",
        "physical": "ci",
        "filter": "source = :source",
        "order_by": "date DESC, run_id",
        "columns": ["suite", "arch", "source", "version", "date",
                    "run_id", "status", "previous_status", "duration",
                    "message"],
        "template": _DS_CI,
        "joins": "none",
        "release_scoped": False,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": [],
    },
    "reproducible": {
        "question": "What is the reproducible-builds status per release "
                    "and architecture?",
        "physical": "reproducible",
        "filter": "source = :source + optional release",
        "order_by": "release, architecture",
        "columns": ["source", "version", "release", "architecture",
                    "status"],
        "template": _DS_REPRODUCIBLE,
        "joins": "none",
        "release_scoped": True,
        "default_limit": 20,
        "max_limit": 50,
        "row_cap": None,
        "caveats": [],
    },
    "lintian": {
        "question": "Which lintian tags are currently reported for this "
                    "source package?",
        "physical": "lintian (materialized view)",
        "filter": "package = :source AND package_type = 'source'",
        "order_by": "tag, package_version, package_arch, ctid",
        "columns": ["package", "tag_type", "package_type",
                    "package_version", "package_arch", "tag",
                    "information"],
        "template": _DS_LINTIAN,
        "joins": "none (materialized view)",
        "release_scoped": False,
        "default_limit": 10,
        "max_limit": 50,
        "row_cap": 10,
        "caveats": ["heavy: ~1.2GB materialized view; effective cap is "
                    "10 rows even when a higher limit is requested",
                    "rows have no unique key; ORDER BY is finalized with "
                    "ctid"],
    },
}

DATASET_NAMES: tuple[str, ...] = tuple(DATASETS)


def list_datasets() -> list[dict]:
    """Catalog of the 18 datasets (pure function: no database, no
    budget consumption).

    Deterministic order = registry insertion order. Each entry carries
    name/question/physical/filter/order_by/columns/default_limit/
    max_limit/release_scoped/caveats/joins. Fresh copies: callers may
    rewrite the returned structures without touching the registry.
    """

    out: list[dict] = []
    for name in DATASET_NAMES:
        spec = DATASETS[name]
        out.append({
            "name": name,
            "question": spec["question"],
            "physical": spec["physical"],
            "filter": spec["filter"],
            "order_by": spec["order_by"],
            "columns": list(spec["columns"]),
            "default_limit": spec["default_limit"],
            "max_limit": spec["max_limit"],
            "release_scoped": spec["release_scoped"],
            "caveats": list(spec["caveats"]),
            "joins": spec["joins"],
        })
    return out


def _dataset_limit_clause(cap: int | None) -> str:
    """LIMIT clause for the datasets path: plain ``LIMIT :limit1``
    (the limit+1 truncation probe) for regular tables and
    ``LIMIT LEAST(:limit1, cap+1)`` for heavy tables. ``cap`` only
    accepts ints in [1, MAX_DATASET_LIMIT] and comes exclusively from
    application-side constants plus an already-validated limit."""

    if cap is None:
        return "LIMIT :limit1;"
    if isinstance(cap, bool) or not isinstance(cap, int) \
            or not (1 <= cap <= MAX_DATASET_LIMIT):
        raise ValueError(
            f"dataset row cap must be an int in [1, {MAX_DATASET_LIMIT}]; "
            f"got {cap!r}")
    return f"LIMIT LEAST(:limit1, {cap + 1});"


def build_statements(datasets: tuple[str, ...],
                     dataset_caps: dict | None = None
                     ) -> list[tuple[str | None, str]]:
    """Assemble the fixed templates into standalone statements for the
    in-process driver.

    Each element is ``(owner, sql)``: ``owner`` is the dataset name
    that owns the statement's rows, or ``None`` for the leading meta
    statement. No ``BEGIN``/``COMMIT`` text is included -- the adapter
    owns the transaction. ``dataset_caps`` carries only the
    heavy-table entries; datasets without an entry get the plain
    limit+1 probe clause.
    """

    caps = dataset_caps or {}
    statements: list[tuple[str | None, str]] = [(None, _T_META)]
    for name in datasets:
        statements.append(
            (name, DATASETS[name]["template"] + " "
             + _dataset_limit_clause(caps.get(name))))
    return statements
