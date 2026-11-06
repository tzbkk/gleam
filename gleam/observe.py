"""观测引擎：候选端点 → 有界抓取 → 与上次成功观测比较 → 追加式落盘。

outcome 词表沿用 docs/research/09 §6 的示意（未冻结），外加非主张词：
- NOT_PROBED：候选因策略不抓取（git 端点 / 制品），只是登记。
- HTTP_ERROR：4xx——HTTP 交换完成但目标报错；保留响应，不作死亡推断，
  不成为基线。
- 基线只取同一端点上一次"成功"观测（OK/NOT_MODIFIED/REPRESENTATION_CHANGED
  且 body 未被截断）；失败的扫描绝不成为 baseline。哈希变化只是表示层变化，
  不是活性结论。
- 304 Not Modified：有效哈希沿用基线哈希，绝不用空 body 哈希覆盖好哈希。
- 截断（本次或基线任一侧）时 comparison 记 unknown-truncated-body。
"""

from __future__ import annotations

import hashlib

from .facts import PackageFacts
from .net import Fetcher, FetchOutcome, origin_key, robots_raw_name
from .store import RunStore, utc_now

ABSTAIN_KINDS = {"robots-disallow", "robots-unknown", "budget-exhausted",
                 "unsafe-destination", "offline"}


def _outcome_for(result: FetchOutcome, status: int | None, comparison: str | None) -> str:
    if not result.ok:
        kind = (result.error or {}).get("kind", "transport-error")
        if kind in ABSTAIN_KINDS:
            return "ABSTAIN"
        return "TRANSPORT_ERROR"
    if status == 304:
        return "NOT_MODIFIED"
    if status is not None and 400 <= status < 500:
        return "HTTP_ERROR"
    if comparison == "unchanged":
        return "NOT_MODIFIED"
    if comparison == "changed":
        return "REPRESENTATION_CHANGED"
    return "OK"


def _headers_text(hops) -> str:
    lines = []
    for hop in hops:
        lines.append(f"> GET {hop.url}")
        lines.append(f"< status: {hop.status} (pinned_ip: {hop.pinned_ip})")
        for key, values in sorted(hop.headers.items()):
            for value in values:
                lines.append(f"< {key}: {value}")
        lines.append("")
    return "\n".join(lines)


def observation_record(*, run_id: str, seq: int, package: str, candidate,
                       result: FetchOutcome | None, outcome: str,
                       comparison: str | None, baseline: dict | None,
                       reason: str | None, observed_at: str,
                       raw_refs: dict, policy: dict,
                       effective_hash: str | None = None) -> dict:
    content_sha = effective_hash if effective_hash is not None else (
        result.content_sha256 if result else None)
    return {
        "observation_id": f"obs-{seq:04d}",
        "run_id": run_id,
        "package": package,
        "endpoint": {
            "url": candidate.url,
            "label": candidate.label,
            "kind": candidate.kind,
            "origin": candidate.provenance,
        },
        "observed_at": observed_at,
        "method": "GET",
        "outcome": outcome,
        "reason": reason,
        "status_code": (result.status if result else None),
        "final_url": (result.final_url if result else None),
        "redirect_chain": (result.redirect_chain if result else []),
        "content_sha256": content_sha,
        "content_bytes": (result.content_bytes if result else None),
        "body_truncated": (result.body_truncated if result else None),
        "effective_hash_from_baseline": bool(
            effective_hash is not None and result is not None
            and result.content_sha256 is not None
            and effective_hash != result.content_sha256),
        "error": (result.error if result else None),
        "robots": (result.robots if result else None),
        "baseline": baseline,
        "comparison": comparison,
        "raw_refs": raw_refs,
        "policy": policy,
        "interpretation_notes": [
            "hash change = representation change only, not proof of life",
            "error/timeout/HTTP error = unknown, never proof of death",
        ],
    }


def package_facts_view(pkg: PackageFacts) -> dict:
    sources_t = pkg.tables["sources"]
    upstream_t = pkg.tables["upstream"]
    metadata_t = pkg.tables["metadata"]
    watch = pkg.first(upstream_t, "watch_file")
    return {
        "versions": pkg.versions,
        "homepage": pkg.all_values(sources_t, "homepage"),
        "watch_file_retained_raw": bool(watch and watch.strip()),
        "watch_status": pkg.all_values(upstream_t, "status"),
        "upstream_version": pkg.all_values(upstream_t, "upstream_version"),
        "upstream_artifact_urls": pkg.all_values(upstream_t, "upstream_url"),
        "dep12_keys": sorted({(fact.fields.get("key") or "").strip()
                              for fact in pkg.rows
                              if fact.table == metadata_t
                              and (fact.fields.get("key") or "").strip()}),
        "debian_packaging_vcs": pkg.packaging_vcs(),
    }


def evidence_text(pkg: PackageFacts, records: list[dict]) -> str:
    """给模型的小型英文证据清单（网页内容一律不带出，只有元数据）。"""

    lines = [f"package: {pkg.name}",
             "versions (all snapshot rows): " + ", ".join(pkg.versions)]
    for rec in records:
        lines.append(
            f"[{rec['observation_id']}] {rec['endpoint']['label']} "
            f"{rec['endpoint']['url']} -> outcome={rec['outcome']} "
            f"status={rec['status_code']} comparison={rec.get('comparison')} "
            f"hash={str(rec.get('content_sha256'))[:12]}")
    vcs = pkg.packaging_vcs()
    if vcs:
        lines.append("note: packaging VCS (NOT upstream): "
                     + ", ".join(v["url"] for v in vcs[:2]))
    lines.append("reminder: hash change is representation change only; "
                 "errors are unknown, not death.")
    return "\n".join(lines)


def _last_response_body(hops) -> bytes:
    """取最近一个已取得响应的 hop 的 body（失败抓取也保留其部分 body）。"""

    for hop in reversed(hops):
        if hop.status is not None:
            return hop.body
    return b""


def collect_observations(store: RunStore,
                         packages: list[PackageFacts],
                         fetcher: Fetcher | None, manual_urls: dict[str, list],
                         offline: bool, run_id: str) -> list[dict]:
    records = []
    seq = 0
    policy_used = dict(fetcher.policy) if fetcher else {}
    robots_written: dict[str, str] = {}

    def flush_robots() -> None:
        for key, body in getattr(fetcher, "_robots_bodies", {}).items():
            if key not in robots_written:
                name = robots_raw_name(key)
                robots_written[key] = store.write_raw(name, body)

    for pkg in packages:
        candidates = pkg.upstream_candidates() + [
            _manual_candidate(url, pkg.name)
            for url in manual_urls.get(pkg.name, [])]
        for candidate in candidates:
            seq += 1
            observed_at = utc_now()
            if not candidate.fetch or offline or fetcher is None:
                if offline:
                    reason = "offline-mode: no network by policy"
                elif not candidate.fetch:
                    reason = f"policy: {candidate.kind} endpoints are registered but not probed"
                else:
                    reason = "no fetcher"
                rec = observation_record(
                    run_id=run_id, seq=seq, package=pkg.name,
                    candidate=candidate, result=None, outcome="ABSTAIN"
                    if offline else "NOT_PROBED",
                    comparison=None, baseline=None, reason=reason,
                    observed_at=observed_at, raw_refs={},
                    policy=policy_used)
                store.write_observation(rec)
                records.append(rec)
                continue

            baseline = store.find_baseline(candidate.url)
            result = fetcher.fetch(candidate.url)
            flush_robots()

            comparison = None
            effective_hash = None
            if result.ok:
                if result.status == 304:
                    # 304：有效表示哈希沿用基线，不用空 body 哈希覆盖
                    if baseline and baseline.get("content_sha256"):
                        comparison = "unchanged"
                        effective_hash = baseline["content_sha256"]
                    else:
                        comparison = None
                elif result.body_truncated or (
                        baseline and baseline.get("body_truncated")):
                    # 任一侧截断：无法断言异同
                    comparison = "unknown-truncated-body"
                elif baseline and baseline.get("content_sha256"):
                    if result.content_sha256 == baseline["content_sha256"]:
                        comparison = "unchanged"
                    else:
                        comparison = "changed"
                else:
                    comparison = "first-observation"
            outcome = _outcome_for(result, result.status, comparison)

            raw_refs = {}
            got_response = result.status is not None or any(
                h.status is not None for h in result.hops)
            if got_response:
                # 失败抓取同样保留响应头与已取得的（部分）body
                obs_id = f"obs-{seq:04d}"
                raw_refs["body"] = store.write_raw(
                    f"{obs_id}.body",
                    _last_response_body(result.hops) or result.body)
                raw_refs["headers"] = store.write_raw(
                    f"{obs_id}.headers",
                    _headers_text(result.hops).encode("utf-8"))
            robots_refs = {}
            for url in [candidate.url] + [hop["to"] for hop in result.redirect_chain]:
                key = origin_key(url)
                if key and key in robots_written:
                    robots_refs[key] = robots_written[key]
            if robots_refs:
                raw_refs["robots"] = robots_refs
            rec = observation_record(
                run_id=run_id, seq=seq, package=pkg.name,
                candidate=candidate, result=result, outcome=outcome,
                comparison=comparison,
                baseline=({"run_id": baseline["run_id"],
                           "observation_id": baseline["observation_id"],
                           "observed_at": baseline["observed_at"],
                           "content_sha256": baseline["content_sha256"]}
                          if baseline else None),
                reason=None, observed_at=observed_at, raw_refs=raw_refs,
                policy=policy_used, effective_hash=effective_hash)
            store.write_observation(rec)
            records.append(rec)
    return records


def _manual_candidate(url: str, package: str):
    from .facts import Candidate
    return Candidate(url, "manual-candidate", "web",
                     f"{package}:operator:--url")


def summary_counts(records: list[dict], packages: list[PackageFacts],
                   manual_urls: dict) -> dict:
    by_outcome: dict[str, int] = {}
    for rec in records:
        by_outcome[rec["outcome"]] = by_outcome.get(rec["outcome"], 0) + 1
    missing = [p.name for p in packages
               if not p.upstream_candidates()
               and not manual_urls.get(p.name)]
    return {
        "packages_selected": len(packages),
        "observations": len(records),
        "by_outcome": by_outcome,
        "packages_without_any_candidate": missing,
        "transport_error_count": by_outcome.get("TRANSPORT_ERROR", 0),
        "abstain_count": by_outcome.get("ABSTAIN", 0),
    }
