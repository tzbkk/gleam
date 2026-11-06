"""Live UDD 包事实源：QA cohort 枚举 + 逐包事实抓取 + PackageFacts 适配
（+ 显式 opt-in 的 facts 缓存，供诚实 offline 模式使用）。

这是活跃 CLI（inspect / run / investigate）的包选择与包事实输入。
仓库根 ``raw/`` 的历史快照不再是运行时输入（见 gleam/snapshot.py 的
归档说明）；本模块绝不静默回退读取它。

安全模型完全复用 gleam.udd.UddClient（固定 SQL 模板 + psql 变量、argv
列表无 shell、单只读事务 + PGOPTIONS statement_timeout/
default_transaction_read_only、LIMIT limit+1 截断探测、stdout/stderr
有界读取、墙上超时）。cohort 查询的 maintainer 是应用侧常量
（udd.QA_MAINTAINER，validate_maintainer 拒绝其他值）；release 走
udd.RELEASES 允许清单（默认 udd.QA_RELEASE="sid"，即 QA cohort 口径）。

出处（provenance）语义：每包事实带 template_version（udd:sources 等表的
固定模板版本）、fetched_at（客户端抓取时刻）与 response_sha256（psql
原始 CSV 输出的 SHA-256）；Fact 行号是确定性的（source/upstream/metadata
由模板 ORDER BY 保证；packaging 模板 v2 无 ORDER BY——字节冻结不可改
——由适配层按固定展示列稳定排序后编号），出处字符串形如
``udd:sources#row=1``，仅在本次抓取实际返回的行集合内
有效——finish_investigation 的 fact_evidence 按此集合校验，伪造或
快照时代的 ``sources.csv#row=N`` 引用一律拒绝。

QA cohort 成员判定（与历史 pull_udd.sh 同口径）：release 过滤后的
sources 行中存在 maintainer == QA_MAINTAINER 的行。0 行 = unknown-package
（不臆断），有行但非 QA 维护 = not-in-cohort（监控范围外）——都拒绝
提供事实，绝不编造。

缓存（--facts-cache DIR，操作员显式 opt-in）：
- 写：live 抓取成功（status ok 且 cohort 成员判定通过）后按 source 名
  原子写一个 JSON 文件（含完整结果与 written_at）；
- 读：**仅 --offline 模式**（此时绝不触网）；缓存未命中 = 诚实失败
  （不回退 live、不编造）。缓存条目披露原始 fetched_at 与写入时间，
  绝不冒充新鲜数据。非 offline 运行即使配置了缓存也总是 live 抓取
  （缓存只写不读），保证活跃路径的事实永远是 live 的。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass

from . import udd as udd_mod
from .facts import LIVE_TABLES, Fact, PackageFacts
from .store import utc_now

# 事实组：候选/报告派生所需的四张表（bugs/history/wnpp 属 fetch_udd 工具
# 的调查扩展，不属于事实输入）
FACTS_GROUPS = ("source", "upstream", "metadata", "packaging")

# live UDD 组名 → facts.py 逻辑表键
_GROUP_TO_TABLE_KEY = {
    "source": "sources",
    "upstream": "upstream",
    "metadata": "metadata",
    "packaging": "packaging",
}

CACHE_SCHEMA = "gleam-udd-facts-cache-v1"


class LiveFactsError(Exception):
    """带 kind 的 live 事实源错误；kind 词表：

    argument-invalid / unknown-package / not-in-cohort /
    cache-miss / cache-corrupt / 以及 UddClient 透传的
    connection-error / timeout / query-error / output-too-large /
    parse-error / budget-exhausted / psql-unavailable
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


@dataclass
class LiveFacts:
    """一次成功的逐包事实抓取（或缓存读取）的适配结果。"""

    source: str
    facts: PackageFacts
    provenance: dict
    result: dict
    raw_csv: bytes


def dedupe_names(names: list[str]) -> list[str]:
    """去重保序：重复 --package 不得重复观测同一包。"""

    seen, out = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


# legacy packaging 模板无 ORDER BY（v2 字节冻结）→ 行号前按展示列稳定
# 排序，编号与服务器返回顺序无关
_PACKAGING_SORT_KEYS = ("vcs", "url", "browser", "status", "commit_id")


def _packaging_sort_key(row: dict) -> tuple:
    return tuple(str(row.get(k) or "") for k in _PACKAGING_SORT_KEYS)


def _adapt_result(source: str, result: dict) -> PackageFacts:
    """把 UddClient.fetch 的 groups 行适配成 live 表族的 PackageFacts。

    行号 = 组内确定性序号（1 起）：source/upstream/metadata 由模板
    ORDER BY 保证；packaging 模板无 ORDER BY（v2 冻结）→ 客户端按
    _PACKAGING_SORT_KEYS 稳定排序后编号。每行剔除模板标记列 grp。
    """

    pkg = PackageFacts(name=source, tables=dict(LIVE_TABLES))
    groups_data = result.get("groups_data") or {}
    for group, table_key in _GROUP_TO_TABLE_KEY.items():
        rows = (groups_data.get(group) or {}).get("rows") or []
        if group == "packaging":
            rows = sorted(rows, key=_packaging_sort_key)
        for idx, row in enumerate(rows, start=1):
            fields = {k: v for k, v in row.items() if k != "grp"}
            pkg.rows.append(Fact(table=LIVE_TABLES[table_key],
                                 row_number=idx, fields=fields))
    return pkg


def _provenance_for(source: str, result: dict, *,
                    cache_info: dict | None = None) -> dict:
    row_counts = {
        group: len(((result.get("groups_data") or {}).get(group)
                    or {}).get("rows") or [])
        for group in FACTS_GROUPS}
    prov = {
        "kind": "udd-live",
        "source": source,
        "dsn_display": result.get("dsn_display"),
        "release": result.get("release"),
        "template_version": result.get("template_version"),
        "fetched_at": result.get("fetched_at"),
        "response_sha256": result.get("response_sha256"),
        "row_counts": row_counts,
        "tables": dict(LIVE_TABLES),
        "citation_format": "udd:<table>#row=N (rows of THIS fetch; "
                           "snapshot-era table#row=N citations are invalid)",
    }
    if cache_info is not None:
        prov["kind"] = "udd-cache"
        prov["cache"] = cache_info
    return prov


def fetch_live_facts(client, source, *, release=None,
                     limit: int | None = None) -> LiveFacts:
    """live 抓取单包事实并适配（含 QA cohort 成员判定）。

    release 默认 udd.QA_RELEASE（sid）；失败与未知如实抛 LiveFactsError。
    """

    try:
        source = udd_mod.validate_source(source)
        result = client.fetch(source, groups=FACTS_GROUPS,
                              release=release, limit=limit)
    except udd_mod.UddError as exc:
        raise LiveFactsError(exc.kind, exc.message) from exc
    if result.get("status") != "ok":
        error = result.get("error") or {}
        raise LiveFactsError(
            error.get("kind", "query-error"),
            f"live UDD query for {source!r} failed: "
            f"{error.get('message', 'unknown error')}")
    raw = result.pop("_raw_stdout", b"")
    result.pop("_stderr", b"")
    result["response_sha256"] = hashlib.sha256(raw).hexdigest()
    _check_cohort_membership(source, result)
    facts = _adapt_result(source, result)
    return LiveFacts(source=source, facts=facts,
                     provenance=_provenance_for(source, result),
                     result=result, raw_csv=raw)


def _check_cohort_membership(source: str, result: dict) -> None:
    """成员判定：release 过滤后的 sources 行中须有 QA 维护者行。"""

    src_rows = ((result.get("groups_data") or {}).get("source")
                or {}).get("rows") or []
    release = result.get("release") or udd_mod.QA_RELEASE
    if not src_rows:
        raise LiveFactsError(
            "unknown-package",
            f"live UDD returned 0 sources rows for {source!r} "
            f"(release={release}); unknown source name or absent from "
            f"this release — facts unavailable, refusing to fabricate")
    maintainers = sorted({(r.get("maintainer") or "") for r in src_rows})
    if udd_mod.QA_MAINTAINER not in maintainers:
        raise LiveFactsError(
            "not-in-cohort",
            f"{source!r} is maintained by {maintainers} in release "
            f"{release!r}, not by the Debian QA group — outside the "
            f"monitored QA cohort; refusing to fabricate facts")


def list_cohort(client, *, limit: int, release=None) -> dict:
    """枚举 QA cohort（确定性排序、limit+1 截断探测、诚实错误）。"""

    try:
        return client.fetch_cohort(limit=limit, release=release)
    except udd_mod.UddError as exc:
        raise LiveFactsError(exc.kind, exc.message) from exc
    # 注：fetch_cohort 的查询失败以 status="error" 返回而非抛出，由调用方
    # （select_live_packages）统一分级。


def select_live_packages(client, *, names=None, limit=None, release=None,
                         cache=None):
    """活跃 CLI 的包选择。

    - names 给定：逐包快速路径（每包一次固定模板查询；**不枚举 cohort**）；
    - 否则：先 cohort 枚举（bounded limit），再逐包取前 limit 个的事实。
    - cache 给定且非 offline：成功抓取后写缓存（读仍只发生在 offline）。

    返回 (live_facts 列表, selection_meta)；任何失败抛 LiveFactsError。
    """

    if names:
        out = []
        for name in dedupe_names(list(names)):
            lf = fetch_live_facts(client, name, release=release)
            if cache is not None:
                cache.write(name, lf.result, lf.raw_csv)
            out.append(lf)
        return out, {
            "mode": "named-fast-path",
            "cohort_queried": False,
            "release": release or udd_mod.QA_RELEASE,
        }
    limit = limit if limit is not None else 3
    cohort = list_cohort(client, limit=limit, release=release)
    if cohort.get("status") != "ok":
        error = cohort.get("error") or {}
        raise LiveFactsError(
            error.get("kind", "query-error"),
            f"QA cohort enumeration failed: "
            f"{error.get('message', 'unknown error')}")
    out = []
    for name in cohort.get("names", [])[:limit]:
        lf = fetch_live_facts(client, name, release=release)
        if cache is not None:
            cache.write(name, lf.result, lf.raw_csv)
        out.append(lf)
    meta = {
        "mode": "cohort",
        "cohort_queried": True,
        "release": cohort.get("release"),
        "limit": cohort.get("limit"),
        "returned": cohort.get("row_count"),
        "more_available": cohort.get("more_available"),
        "fetched_at": cohort.get("fetched_at"),
        "response_sha256": cohort.get("response_sha256"),
        "template_version": cohort.get("template_version"),
    }
    meta["_raw_csv"] = cohort.pop("_raw_stdout", b"")
    cohort.pop("_stderr", None)
    if cohort.get("note"):
        meta["note"] = cohort["note"]
    return out, meta


class FactsCache:
    """显式 facts 缓存（--facts-cache DIR；一个 source 一个 JSON 文件）。

    写仅在 live 抓取成功后发生；读仅在 offline 模式发生（见模块
    docstring）。文件名经 SAFE_ID 式校验（source 名本就受 Debian 策略
    正则约束），路径双保险防逃逸。
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _path(self, source: str) -> str:
        try:
            udd_mod.validate_source(source)
        except udd_mod.UddError as exc:
            raise LiveFactsError("argument-invalid",
                                 f"invalid source name for cache: "
                                 f"{exc.message}") from exc
        path = os.path.join(self.root, f"{source}.json")
        if os.path.dirname(os.path.abspath(path)) != self.root:
            raise LiveFactsError("argument-invalid",
                                 f"cache path escapes root: {source!r}")
        return path

    @staticmethod
    def _digest_json(value: dict) -> str:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def write(self, source: str, result: dict, raw_csv: bytes) -> None:
        """live 抓取成功后落缓存（原子替换；目录按需创建）。"""

        path = self._path(source)
        os.makedirs(self.root, exist_ok=True)
        payload = {
            "schema": CACHE_SCHEMA,
            "source": source,
            "written_at": utc_now(),
            "result": result,
            "result_sha256": self._digest_json(result),
            "raw_csv_b64": base64.b64encode(raw_csv).decode("ascii"),
            "raw_csv_sha256": hashlib.sha256(raw_csv).hexdigest(),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2,
                      sort_keys=True, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, path)

    def read(self, source: str) -> dict | None:
        """读缓存；未命中返回 None，损坏（schema/source 不符/坏 JSON）抛错。"""

        path = self._path(source)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveFactsError(
                "cache-corrupt",
                f"facts cache entry for {source!r} is unreadable: "
                f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(payload, dict) \
                or payload.get("schema") != CACHE_SCHEMA \
                or payload.get("source") != source \
                or not isinstance(payload.get("result"), dict):
            raise LiveFactsError(
                "cache-corrupt",
                f"facts cache entry for {source!r} has unexpected "
                f"schema/content (expected {CACHE_SCHEMA})")
        if payload.get("result_sha256") != self._digest_json(
                payload["result"]):
            raise LiveFactsError(
                "cache-corrupt",
                f"facts cache result checksum mismatch for {source!r}")
        encoded = payload.get("raw_csv_b64")
        if not isinstance(encoded, str):
            raise LiveFactsError(
                "cache-corrupt",
                f"facts cache raw CSV is missing for {source!r}")
        try:
            raw_csv = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise LiveFactsError(
                "cache-corrupt",
                f"facts cache raw CSV encoding is invalid for {source!r}: "
                f"{exc}") from exc
        raw_digest = hashlib.sha256(raw_csv).hexdigest()
        if payload.get("raw_csv_sha256") != raw_digest \
                or payload["result"].get("response_sha256") != raw_digest:
            raise LiveFactsError(
                "cache-corrupt",
                f"facts cache raw CSV checksum mismatch for {source!r}")
        return payload


def facts_from_cache(cache: FactsCache, source: str) -> LiveFacts:
    """从缓存构造 LiveFacts（offline 模式；未命中=诚实失败）。"""

    source = udd_mod.validate_source(source)
    payload = cache.read(source)
    if payload is None:
        raise LiveFactsError(
            "cache-miss",
            f"--offline 需要 {source!r} 的已缓存 facts（--facts-cache "
            f"{cache.root}），但缓存未命中；先在在线模式运行一次写入"
            f"缓存，或去掉 --offline 使用 live UDD（绝不静默回退）")
    result = payload["result"]
    if result.get("status") != "ok":
        raise LiveFactsError(
            "cache-corrupt",
            f"cached entry for {source!r} is not a successful fetch")
    facts = _adapt_result(source, result)
    cache_info = {
        "root": cache.root,
        "written_at": payload.get("written_at"),
        "original_fetched_at": result.get("fetched_at"),
        "read_at": utc_now(),
        "note": "facts read from the explicit offline cache; they reflect "
                "the original live fetch time above and may be stale",
    }
    return LiveFacts(source=source, facts=facts,
                     provenance=_provenance_for(source, result,
                                                cache_info=cache_info),
                     result=result,
                     raw_csv=base64.b64decode(payload["raw_csv_b64"],
                                              validate=True))


def offline_facts(cache: FactsCache, names: list[str]) -> list[LiveFacts]:
    """offline 模式的包事实输入：仅显式 --package + 仅缓存。"""

    return [facts_from_cache(cache, name)
            for name in dedupe_names(list(names))]


def input_provenance_summary(live_facts: list[LiveFacts], *,
                             release: str | None, offline: bool = False,
                             cache: "FactsCache | None" = None,
                             selection_meta: dict | None = None,
                             dsn_display: str | None = None) -> dict:
    """run/investigate 的 input_provenance（live UDD 出处汇总）。"""

    qa_release = release or udd_mod.QA_RELEASE
    summary = {
        "kind": "udd-cache" if offline else "udd-live",
        "dsn_display": dsn_display
        or (live_facts[0].provenance.get("dsn_display") if live_facts
            else udd_mod.dsn_display(udd_mod.DEFAULT_UDD_DSN)),
        "release": qa_release,
        "qa_filter": (f"maintainer='{udd_mod.QA_MAINTAINER}' "
                      f"AND release='{qa_release}'"),
        "offline": offline,
        "template_versions": {
            "facts": udd_mod.UDD_TEMPLATE_VERSION,
            "cohort": udd_mod.COHORT_TEMPLATE_VERSION,
        },
        "selection": selection_meta,
        "packages": {lf.source: lf.provenance for lf in live_facts},
        "notes": [
            "package facts come from bounded read-only live UDD queries "
            "(fixed SQL templates via psql variables); facts are Debian "
            "packaging metadata, not live web observations",
            "the repository-root raw/ directory is an archived 2026-10-29 "
            "snapshot and is NOT an input of this run; per-run raw/ under "
            "the store remains append-only evidence",
        ],
    }
    if cache is not None:
        summary["cache"] = {
            "root": cache.root,
            "mode": "read-only (offline)" if offline
            else "write-through (live fetches cached for future offline "
                 "runs; reads never happen while online)",
        }
    return summary
