#!/usr/bin/env bash
# Usage: ./scripts/pull_udd.sh
# Source: UDD public mirror (upstream table refreshes every ~30min).
# Exports all QA-orphan package metadata to raw/ as CSV.
#
# ARCHIVED-STATUS NOTE (2026-11-03): the repository-root raw/ produced here is
# an ARCHIVED historical snapshot (pulled 2026-10-29). It is NO LONGER a
# runtime input of the active CLIs (inspect/run/investigate now query live UDD
# via gleam/udd.py fixed templates; there is no --raw flag). This script is
# kept for historical reproduction only. Do NOT confuse it with per-run
# evidence raw/ directories under <store>/runs/<id>/raw/ (append-only evidence,
# never deleted).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UDD="postgresql://udd-mirror:udd-mirror@udd-mirror.debian.net/udd"
OUT="$ROOT/raw"
mkdir -p "$OUT"

psql "$UDD" -v ON_ERROR_STOP=1 <<SQL
SET statement_timeout='600000';
CREATE TEMP TABLE qa_sources (source text PRIMARY KEY);
INSERT INTO qa_sources
  SELECT DISTINCT source FROM sources
  WHERE maintainer='Debian QA Group <packages@qa.debian.org>' AND release='sid';
ANALYZE qa_sources;

\copy (SELECT s.* FROM sources s JOIN qa_sources q ON q.source=s.source WHERE s.release='sid' AND s.maintainer='Debian QA Group <packages@qa.debian.org>' ORDER BY s.source) TO '$OUT/sources.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT p.* FROM packages p JOIN qa_sources q ON q.source=p.source WHERE p.release='sid' ORDER BY p.source, p.package, p.architecture) TO '$OUT/packages.csv' WITH (FORMAT csv, HEADER)

\copy (SELECT u.* FROM upstream u JOIN qa_sources q ON q.source=u.source WHERE u.release='sid' ORDER BY u.source) TO '$OUT/upstream.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT m.* FROM upstream_metadata m JOIN qa_sources q ON q.source=m.source ORDER BY m.source, m.key) TO '$OUT/upstream_metadata.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT v.* FROM vcswatch v JOIN qa_sources q ON q.source=v.source ORDER BY v.source) TO '$OUT/vcswatch.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT d.* FROM duck_urls d JOIN qa_sources q ON q.source=d.source ORDER BY d.source, d.url) TO '$OUT/duck_urls.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT t.* FROM duck_url_status t JOIN duck_urls d ON d.url=t.url JOIN qa_sources q ON q.source=d.source ORDER BY t.url) TO '$OUT/duck_url_status.csv' WITH (FORMAT csv, HEADER)

\copy (SELECT h.* FROM upload_history h JOIN qa_sources q ON q.source=h.source ORDER BY h.source, h.date) TO '$OUT/upload_history.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT w.* FROM wnpp w JOIN qa_sources q ON q.source=w.source ORDER BY w.source, w.type) TO '$OUT/wnpp.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT b.* FROM bugs b JOIN qa_sources q ON q.source=b.source ORDER BY b.source, b.id) TO '$OUT/bugs.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT a.* FROM archived_bugs a JOIN qa_sources q ON q.source=a.source ORDER BY a.source, a.id) TO '$OUT/archived_bugs.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT t.* FROM bugs_usertags t JOIN (SELECT id FROM bugs b JOIN qa_sources q ON q.source=b.source UNION SELECT id FROM archived_bugs a JOIN qa_sources q ON q.source=a.source) ids ON ids.id=t.id ORDER BY t.id) TO '$OUT/bugs_usertags.csv' WITH (FORMAT csv, HEADER)

\copy (SELECT p.* FROM package_removal p JOIN qa_sources q ON q.source=p.name ORDER BY p.batch_id, p.name) TO '$OUT/package_removal.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT DISTINCT b.* FROM package_removal_batch b JOIN package_removal p ON p.batch_id=b.id JOIN qa_sources q ON q.source=p.name ORDER BY b.time) TO '$OUT/package_removal_batch.csv' WITH (FORMAT csv, HEADER)

\copy (SELECT c.* FROM popcon_src c JOIN qa_sources q ON q.source=c.source ORDER BY c.source) TO '$OUT/popcon_src.csv' WITH (FORMAT csv, HEADER)
SQL

rowcount() { python3 -c "import csv,sys;print(sum(1 for _ in csv.reader(open(sys.argv[1])))-1)" "$1"; }

{
  echo "# UDD QA metadata snapshot"
  echo "pulled_at:   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "udd_host:    udd-mirror.debian.net"
  echo "qa_filter:   maintainer='Debian QA Group <packages@qa.debian.org>' AND release='sid'"
  echo "qa_packages: $(rowcount "$OUT/sources.csv")"
} > "$OUT/_manifest.txt"

for f in "$OUT"/*.csv; do
  printf '%-28s %8s rows  %s\n' "$(basename "$f")" "$(rowcount "$f")" "$(du -h "$f" | cut -f1)" | tee -a "$OUT/_manifest.txt"
done
echo "== done =="
