"""UDD 实时抓取（fetch_udd）：对Ultimate Debian Database 公共只读镜像的
有界、只读、固定 SQL 模板查询。

数据源与连接（docs/research/02-udd-handbook.md，2026-10-27~29 实测；
本模块 schema 又于 2026-11-02 对实库逐列复核，见
docs/research/experiments/2026-11-02-udd-fetch/REPORT.md）：
- 端点 = 公共只读 SQL 镜像 ``postgresql://udd-mirror:udd-mirror@udd-mirror.debian.net/udd``
  （密码公开、免申请；主库 udd.debian.org 不对外开放 SQL）。
- 通过 ``psql`` 子进程连接（操作员环境显式要求；不引入新 Python 依赖）：
  ``psql -X -q --csv -w -v ON_ERROR_STOP=1 -f - DSN``，SQL 脚本经 stdin。
- 解释性决定（先例：bts.py 的 SOAP）：UDD 公共镜像本身就是 Debian 文档
  指给程序化访问的只读数据服务（非网页），robots.txt 爬虫排除协议不适用；
  该决定在报告与观测记录中如实披露。

安全模型（对齐 bts.py / search.py 的受信路径先例）：
- **DSN 是应用侧常量**（默认公共镜像），模型/工具调用方不可指定；CLI
  ``--udd-dsn`` 仅供操作员覆盖（必须 postgresql:// 形态）。
- **SQL 全部是固定模板**（应用侧常量字符串），输入只经 psql 变量
  （``:'source'`` 带引号安全插值）进入模板；``:limit`` 为无引号插值但
  在 Python 侧严格校验为 int——绝不拼接 SQL、绝不 shell=True、绝不接受
  模型提供的任意 SQL。source 包名按 Debian 策略正则校验；release 与
  groups 均按严格允许清单校验。
- **只读多层强制**：PGOPTIONS 设 ``default_transaction_read_only=on``
  （服务端拒绝任何写语句，live 验证 CREATE TABLE 即被拒）+ 单次
  ``BEGIN; ... COMMIT;`` 批量只读事务 + 公共只读镜像账号本身无写权限。
- **有界**：整个 psql 调用超时（subprocess 级 kill）；每条语句
  ``statement_timeout``（PGOPTIONS）；每条 SELECT 带 ``LIMIT :limit + 1``
  （取到 limit+1 行 ⇒ 置 more_available 并丢弃第 limit+1 行，诚实披露
  截断）；stdout/stderr 读取有字节上限（超限 kill 进程、报
  output-too-large，绝不让无界输出进内存/落盘）；单次 fetch 的语句数
  ≤ ``max_queries``（当前 9 条 ≤ 10）。
- 查询组（group 允许清单）：``source``（sources 表逐 release 行）、
  ``upstream``（uscan 状态）、``metadata``（DEP-12 upstream_metadata）、
  ``packaging``（vcswatch）、``bugs``（开放 bug，含 binaries 映射子查询）、
  可选 ``history``（upload_history）与 ``wnpp``。**逐表各自成行、不 JOIN**
  （手册 §4 popcon 三大坑：join 乘法与 sources.bin 逗号文本不可作外键）。

数据集路径（template v3，``fetch(source, datasets=[...])``，2026-11-06
设计）：18 个**平等**数据集与 ``groups=`` 旧路径并存且互斥（二者同传即
argument-invalid）；旧路径字节不变（liveudd/agent/cli 与既有测试不受影响）。
- 数据集（目录见 ``list_datasets()``/``render_catalog()``，纯函数无 DB）：
  sources/upstream/upstream_metadata/vcswatch/urls/binaries/bugs/
  bugs_count/archived_bugs/wnpp/upload_history/upload_history_closes/
  removals/testing_autoremovals/popcon_src/ci/reproducible/lintian。
- 有界：单次 ≤6 个数据集；各数据集有效行上限之和 ≤120（psql 之前按
  budget-exhausted 拒绝）；默认 limit 20、硬上限 50；重表
  （archived_bugs/lintian）有效上限 10（``LIMIT LEAST(:limit1, 11)``）。
- 每行带 ``provenance = "udd:<dataset>#row=N"``（组内 1 起、LIMIT 之后、
  裁剪之前；旧 groups= 路径不加，行字典保持原样）。
- release 过滤只作用于确实有 release 列的四张表：sources/upstream/
  binaries（packages 内层子查询）/reproducible；其余数据集忽略 release
  （目录元数据 release_scoped 如实标注）。
- 诚实语义与旧路径一致：0 行=真实空结果（removals/testing_autoremovals/
  upstream_metadata 等常见）、失败=未知、截断=more_available。

bugs 组的关联出处（如实保留、不臆断）：
- 不假设 ``bugs.package`` 恰好是源包名：匹配条件 = ``bugs.source = :source``
  （BTS 自身源码映射）OR ``bugs.package = :source``（src: 前缀伪包）OR
  ``package IN (bugs_packages WHERE source=:source)`` OR
  ``package IN (packages WHERE source=:source)``（二进制→源码映射）。
- 每行保留 ``package``、``source`` 原值与 ``via_source_mapping`` /
  ``via_bugs_packages`` 布尔（关联来源 provenance）；开放口径 =
  ``status <> 'done'``（bugs 表本身只含未归档 bug；archived_bugs 不在
  本工具范围，如实披露）。
- **不按关键词过滤标题**：命中即全量返回（受 limit 截断与 more_available
  披露约束）；bug 完整讨论不在本工具内（用 bug_fetch 工具按 id 取）。

时间戳出处（区分数据时间与抓取时间）：
- 各行时间字段（arrival/last_modified/last_check/date/server_now 等）为
  **服务端渲染的原始文本**，逐字保留；
- ``fetched_at`` 为客户端抓取时刻（UTC），两者从不混写。

如实披露的限制：
- psql ``--csv`` 下 NULL 与空字符串不可区分（均渲染为空字段）；已知
  可空字段按空串呈现，本工具不做臆断填充。
- ``upstream.watch_file`` 自模板 v2 起逐字抓回（原样保留、永不执行——
  uscan 或其中的正则/URL 绝不运行）；工具层展示有字符截断，完整原文在
  落档的原始 CSV 中。
- cohort 枚举（``fetch_cohort``）：固定 QA 口径（maintainer=Debian QA
  Group + release，默认 sid）、``DISTINCT ON (source)`` 取最高版本、
  ``ORDER BY source`` 确定性行序、``LIMIT limit+1`` 截断探测；maintainer
  是应用侧常量（``validate_maintainer`` 拒绝一切其他值）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import select
import subprocess
import time

from .store import utc_now

# ---- 常量与允许清单 ---------------------------------------------------------

DEFAULT_UDD_DSN = ("postgresql://udd-mirror:udd-mirror"
                   "@udd-mirror.debian.net/udd")
UDD_TEMPLATE_VERSION = "udd-fetch-templates-v2"       # groups= 旧路径（不变）
DATASETS_TEMPLATE_VERSION = "udd-fetch-templates-v3"  # datasets= 新路径
COHORT_TEMPLATE_VERSION = "udd-cohort-templates-v1"

# QA cohort 常量（应用侧固定；maintainer 不是模型/CLI 可配置项）
QA_MAINTAINER = "Debian QA Group <packages@qa.debian.org>"
QA_RELEASE = "sid"

DEFAULT_POLICY = {
    "timeout_s": 45.0,               # 整个 psql 调用的墙上时限
    "statement_timeout_ms": 15000,   # 每条 SQL 语句的服务端超时
    "max_output_bytes": 8 * 1024 * 1024,  # stdout/stderr 各自的读取上限
    "max_rows_default": 30,          # 每组默认行数上限（LIMIT limit+1 探测）
    "max_rows_hard": 100,            # limit 参数的硬上限
    "max_queries": 10,               # 单次 fetch 的语句数上限（含 meta）
    "field_char_cap": 600,           # 单字段展示截断（仅工具层；raw 原样）
    # datasets= 新路径限额（groups= 旧路径不受影响、沿用上面两个行数上限）
    "dataset_rows_default": 20,      # 新路径默认 limit
    "dataset_rows_hard": 50,         # 新路径 limit 硬上限
    "datasets_max": 6,               # 单次 fetch 数据集数上限（+meta=7 ≤ max_queries）
    "dataset_rows_total_max": 120,   # 单次 fetch 各数据集有效行上限之和
}

GROUPS = ("source", "upstream", "metadata", "packaging", "bugs",
          "history", "wnpp")
DEFAULT_GROUPS = ("source", "upstream", "metadata", "packaging", "bugs")

# 2026-11-02 实库 distinct release（bookworm…trixie-updates 全集）+ 套件别名
RELEASES = frozenset({
    "bookworm", "bookworm-backports", "bookworm-backports-sloppy",
    "bookworm-security", "bookworm-updates",
    "bullseye", "bullseye-security", "bullseye-updates",
    "experimental", "forky", "sid",
    "trixie", "trixie-backports", "trixie-proposed-updates",
    "trixie-security", "trixie-updates",
    # 套件别名（UDD release 列通常存代号，别名保守放行、查无行即如实空）
    "oldoldstable", "oldstable", "stable", "testing", "unstable",
})

DEBIAN_SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]+$")


class UddError(Exception):
    """带 kind 的 UDD 抓取错误；kind 词表：
    argument-invalid / connection-error / timeout / query-error /
    output-too-large / parse-error / budget-exhausted / psql-unavailable
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


def validate_source(value) -> str:
    """源包名按 Debian 策略校验（小写字母数字 + . + -，首字符字母数字）。"""

    if isinstance(value, bool) or not isinstance(value, str):
        raise UddError("argument-invalid",
                       f"source must be a string, got "
                       f"{type(value).__name__}")
    if not DEBIAN_SOURCE_NAME.match(value) or len(value) > 64:
        raise UddError(
            "argument-invalid",
            f"source must match Debian source-name policy "
            f"(^[a-z0-9][a-z0-9+.-]+$, max 64 chars); got {value!r}")
    return value


def validate_groups(groups) -> tuple[str, ...]:
    """groups 严格允许清单；去重保序。None=默认组；显式空列表拒绝。"""

    if groups is None:
        return DEFAULT_GROUPS
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    if not isinstance(groups, (list, tuple)):
        raise UddError("argument-invalid",
                        "groups must be a list of strings or a "
                        "comma-separated string")
    seen: list[str] = []
    for item in groups:
        if not isinstance(item, str) or item not in GROUPS:
            raise UddError(
                "argument-invalid",
                f"unknown group {item!r}; allowed groups: {list(GROUPS)}")
        if item not in seen:
            seen.append(item)
    if not seen:
        raise UddError(
            "argument-invalid",
            "groups must contain at least one known group name "
            f"(allowed groups: {list(GROUPS)}); an empty selection would "
            f"produce a meta-only script")
    return tuple(seen)


def validate_datasets(datasets) -> tuple[str, ...]:
    """datasets= 新路径的严格允许清单；去重保序、必须是非空列表。"""

    if isinstance(datasets, str) or not isinstance(datasets, (list, tuple)):
        raise UddError("argument-invalid",
                       "datasets must be a non-empty list of dataset "
                       "names (see list_datasets())")
    seen: list[str] = []
    for item in datasets:
        if not isinstance(item, str) or item not in DATASETS:
            raise UddError(
                "argument-invalid",
                f"unknown dataset {item!r}; allowed datasets: "
                f"{list(DATASET_NAMES)}")
        if item not in seen:
            seen.append(item)
    if not seen:
        raise UddError("argument-invalid",
                       "datasets must be a non-empty list of dataset "
                       "names (see list_datasets())")
    return tuple(seen)


def validate_release(release) -> str | None:
    if release is None or release == "":
        return None
    if not isinstance(release, str) or release not in RELEASES:
        raise UddError("argument-invalid",
                       f"unknown release {release!r}; allowed: "
                       f"{sorted(RELEASES)[:8]}... ({len(RELEASES)} total)")
    return release


def validate_limit(value, hard: int | None = None) -> int:
    bound = hard if hard is not None else DEFAULT_POLICY["max_rows_hard"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise UddError("argument-invalid",
                       f"limit must be an integer, got {value!r}")
    if not (1 <= value <= bound):
        raise UddError("argument-invalid",
                       f"limit must be in [1, {bound}]; got {value}")
    return value


def validate_dsn(dsn) -> str:
    """操作员级 DSN 校验（模型不可达）；必须 postgresql:// 且无空白。"""

    if not isinstance(dsn, str) or not dsn.startswith("postgresql://"):
        raise UddError("argument-invalid",
                       "DSN must start with postgresql:// (the documented "
                       "public readonly UDD mirror form)")
    if any(ch.isspace() for ch in dsn):
        raise UddError("argument-invalid", "DSN must not contain whitespace")
    return dsn


def validate_maintainer(value) -> str:
    """cohort 的 maintainer 是应用侧常量：仅接受 QA Group 字面值。

    拒绝一切其他值（含模型/调用方注入尝试）——cohort 口径不可定制。
    """

    if value != QA_MAINTAINER:
        raise UddError(
            "argument-invalid",
            "cohort maintainer is a fixed application constant "
            f"({QA_MAINTAINER!r}); it is not configurable")
    return value


def dsn_display(dsn: str) -> str:
    """展示用脱凭据形态：postgresql://host/db。"""

    rest = dsn[len("postgresql://"):]
    netloc, _, path = rest.partition("/")
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    return f"postgresql://{netloc}/{path}"


_ARGV_SECRET_PREFIXES = ("source=", "release=", "limit1=", "maintainer=")


def _redact_argv(argv: list[str], dsn: str) -> list[str]:
    """从真实 argv 派生脱敏回显（变量值与 DSN 凭据替换为占位），
    避免与 argv 构造漂移。"""

    out = [a if not a.startswith(_ARGV_SECRET_PREFIXES)
           else a.split("=", 1)[0] + "=<value>" for a in argv[:-1]]
    out.append(dsn_display(dsn))
    return out


# ---- 固定 SQL 模板（应用侧常量；输入只经 psql 变量进入）---------------------

# 各模板公共前缀：所有列显式命名别名（psql --csv 头行用列名，coalesce 必须
# 起名否则头行会变成 "coalesce"）；时间戳 ::text 服务端渲染逐字保留。
# 带 LIMIT 的旧 groups= 模板与 datasets= 注册表共享主体（*_BODY 以 ORDER BY
# 结尾、LIMIT 由 build_script 按路径追加）——同一 SQL 单一出处。
_RELEASE_FILTER = "AND (:'release' = '' OR release = :'release')"

_T_META = (
    "SELECT 'meta' AS grp, version() AS server_version, "
    "now()::text AS server_now;")

_T_SOURCE = (
    "SELECT 'source' AS grp, source, version::text AS version, release,\n"
    "       coalesce(maintainer,'') AS maintainer,\n"
    "       coalesce(homepage,'') AS homepage, coalesce(bin,'') AS bin,\n"
    "       coalesce(vcs_type,'') AS vcs_type, coalesce(vcs_url,'') AS vcs_url,\n"
    "       coalesce(section,'') AS section, coalesce(priority,'') AS priority,\n"
    "       coalesce(build_depends,'') AS build_depends\n"
    "FROM sources WHERE source = :'source' " + _RELEASE_FILTER + "\n"
    "ORDER BY release LIMIT :limit1;")

_UPSTREAM_BODY = (
    "SELECT 'upstream' AS grp, source, coalesce(version::text,'') AS version,\n"
    "       release, coalesce(status,'') AS status,\n"
    "       coalesce(upstream_version,'') AS upstream_version,\n"
    "       coalesce(upstream_url,'') AS upstream_url,\n"
    "       coalesce(watch_file,'') AS watch_file,\n"
    "       coalesce(errors,'') AS errors,\n"
    "       last_check::text AS last_check\n"
    "FROM upstream WHERE source = :'source' " + _RELEASE_FILTER + "\n"
    "ORDER BY release")

_T_UPSTREAM = _UPSTREAM_BODY + " LIMIT :limit1;"

_T_METADATA = (
    "SELECT 'metadata' AS grp, source, key, coalesce(value,'') AS value\n"
    "FROM upstream_metadata WHERE source = :'source'\n"
    "ORDER BY key LIMIT :limit1;")

_T_PACKAGING = (
    "SELECT 'packaging' AS grp, source, coalesce(version::text,'') AS version,\n"
    "       coalesce(vcs,'') AS vcs, coalesce(url,'') AS url,\n"
    "       coalesce(browser,'') AS browser, coalesce(status,'') AS status,\n"
    "       last_scan::text AS last_scan,\n"
    "       coalesce(changelog_distribution,'') AS changelog_distribution,\n"
    "       coalesce(commit_id,'') AS commit_id\n"
    "FROM vcswatch WHERE source = :'source' LIMIT :limit1;")

# bugs 组附带 binaries 子查询：packages 表逐架构重复（手册 §4 坑 3），
# DISTINCT 压扁；与 bugs 查询同事务但**独立语句**（不 JOIN，避免乘法）。
_BINARIES_BODY = (
    "SELECT 'binaries' AS grp, package, release\n"
    "FROM (SELECT DISTINCT package, release FROM packages "
    "WHERE source = :'source' " + _RELEASE_FILTER + ") t\n"
    "ORDER BY package, release")

_T_BINARIES = _BINARIES_BODY + " LIMIT :limit1;"

# 开放 bug：不假设 bugs.package == 源包名。匹配 = BTS 源码映射 OR src:伪包
# OR bugs_packages 多包关联 OR packages 二进制→源码映射；每行带关联出处。
_BUGS_BODY = (
    "SELECT 'bugs' AS grp, b.id::text AS id, coalesce(b.package,'') AS package,\n"
    "       coalesce(b.source,'') AS source, b.status AS status,\n"
    "       coalesce(b.severity::text,'') AS severity,\n"
    "       coalesce(b.title,'') AS title,\n"
    "       b.arrival::text AS arrival, b.last_modified::text AS last_modified,\n"
    "       coalesce(b.done,'') AS done,\n"
    "       coalesce((SELECT string_agg(tag, ',') FROM bugs_tags t "
    "WHERE t.id = b.id), '') AS tags,\n"
    "       (b.source = :'source')::text AS via_source_mapping,\n"
    "       EXISTS (SELECT 1 FROM bugs_packages bp WHERE bp.id = b.id "
    "AND bp.source = :'source')::text AS via_bugs_packages\n"
    "FROM bugs b\n"
    "WHERE b.status <> 'done'\n"
    "  AND (b.source = :'source' OR b.package = :'source'\n"
    "       OR b.package IN (SELECT package FROM bugs_packages "
    "WHERE source = :'source')\n"
    "       OR b.package IN (SELECT DISTINCT package FROM packages "
    "WHERE source = :'source'))\n"
    "ORDER BY b.id")

_T_BUGS = _BUGS_BODY + " LIMIT :limit1;"

_WNPP_BODY = (
    "SELECT 'wnpp' AS grp, id::text AS id, coalesce(type,'') AS type,\n"
    "       coalesce(source,'') AS source, coalesce(title,'') AS title\n"
    "FROM wnpp WHERE source = :'source' ORDER BY id")

_T_WNPP = _WNPP_BODY + " LIMIT :limit1;"

_T_HISTORY = (
    "SELECT 'history' AS grp, version::text AS version, date::text AS date,\n"
    "       coalesce(changed_by,'') AS changed_by\n"
    "FROM upload_history WHERE source = :'source'\n"
    "ORDER BY date DESC LIMIT :limit1;")

# QA cohort 枚举（固定模板）：与历史 scripts/pull_udd.sh 同一过滤口径
# （maintainer=QA Group AND release=sid）。DISTINCT ON (source) + 内层
# ORDER BY source, version DESC → 每个源包取最高版本，行序确定
# （外层 ORDER BY source），LIMIT :limit1 截断探测。maintainer/release
# 只以 psql 变量进入模板（应用侧常量，见 validate_maintainer）。
_T_COHORT = (
    "SELECT 'cohort' AS grp, source, version::text AS version, release,\n"
    "       coalesce(maintainer,'') AS maintainer\n"
    "FROM (SELECT DISTINCT ON (source) source, version, release, maintainer\n"
    "      FROM sources\n"
    "      WHERE maintainer = :'maintainer' AND release = :'release'\n"
    "      ORDER BY source, version DESC) qa_cohort\n"
    "ORDER BY source LIMIT :limit1;")

GROUP_TEMPLATES = {
    "source": _T_SOURCE,
    "upstream": _T_UPSTREAM,
    "metadata": _T_METADATA,
    "packaging": _T_PACKAGING,
    "bugs": _T_BUGS,
    "wnpp": _T_WNPP,
    "history": _T_HISTORY,
}
# bugs 组永远附带 binaries 行（关联 provenance 需要）
GROUP_EXTRA = {"bugs": (_T_BINARIES,)}

# limit+1 探测：每个模板的 LIMIT 用 :limit1 = limit + 1，取满 limit+1 行
# 即说明还有更多 → more_available=True 并丢弃第 limit+1 行（显式截断披露）。

# ---- datasets= 路径的固定模板（template v3）----------------------------------
#
# 设计事实（2026-11-06 批准的 18 数据集调查表）：全部只读、按调查源包过滤、
# 确定性 ORDER BY、LIMIT 由 build_script 追加（普通 ``LIMIT :limit1``；重表
# ``LIMIT LEAST(:limit1, cap+1)``）。列名歧义处理：survey 未逐列复核底层
# 类型的列（vcswatch.issues/merge_requests、duck_urls.type、
# duck_url_status.response_time_ms/redirect_url、testing_autoremovals.version、
# ci.duration、reproducible.release）一律按调查清单列名书写，且数值/布尔列
# 经 coalesce(col::text,'') 包裹——int/text/interval/bool 皆安全，NULL 渲染
# 为空串（与 psql --csv 的 NULL/空串不可区分事实一致）。

_DS_SOURCES = (
    "SELECT 'sources' AS grp, source, version::text AS version, release,\n"
    "       coalesce(maintainer,'') AS maintainer,\n"
    "       coalesce(homepage,'') AS homepage, coalesce(bin,'') AS bin,\n"
    "       coalesce(vcs_type,'') AS vcs_type, coalesce(vcs_url,'') AS vcs_url,\n"
    "       coalesce(vcs_browser,'') AS vcs_browser,\n"
    "       coalesce(section,'') AS section, coalesce(priority,'') AS priority\n"
    "FROM sources WHERE source = :'source' " + _RELEASE_FILTER + "\n"
    "ORDER BY release")

_DS_UPSTREAM_METADATA = (
    "SELECT 'upstream_metadata' AS grp, source, key, coalesce(value,'') AS value\n"
    "FROM upstream_metadata WHERE source = :'source'\n"
    "ORDER BY key")

# vcswatch：issues/merge_requests 的底层类型未逐列复核（调查清单列名），
# coalesce(...::text,'') 对 int/text 均安全。
_DS_VCSWATCH = (
    "SELECT 'vcswatch' AS grp, source, coalesce(version::text,'') AS version,\n"
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
    "FROM vcswatch WHERE source = :'source'\n"
    "ORDER BY source")

# duck_urls 无主键（实库事实）→ ORDER BY 含全部展示列 + ctid 收尾；
# duck_url_status 为 LEFT JOIN：缺状态行 = 未知（不是死亡）。
_DS_URLS = (
    "SELECT 'urls' AS grp, d.source AS source, coalesce(d.version::text,'') AS version,\n"
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
    "WHERE d.source = :'source'\n"
    "ORDER BY d.source, d.version, d.component, d.url, "
    "coalesce(d.filename,''), d.checkmethod, d.ctid")

_DS_BUGS_COUNT = (
    "SELECT 'bugs_count' AS grp, source, coalesce(rc_bugs::text,'') AS rc_bugs,\n"
    "       coalesce(all_bugs::text,'') AS all_bugs\n"
    "FROM bugs_count WHERE source = :'source'\n"
    "ORDER BY source")

# archived_bugs ~1M 行（重表，有效上限 10）；filter 只按 BTS 源码映射
# （b.source；binary-only 归档 bug 不匹配，如实为空）。tags 经 bugs_tags
# 相关子查询（与开放 bugs 组同一出处方式；无记录则如实为空串）。
_DS_ARCHIVED_BUGS = (
    "SELECT 'archived_bugs' AS grp, b.id::text AS id,\n"
    "       coalesce(b.package,'') AS package, coalesce(b.source,'') AS source,\n"
    "       b.status AS status, coalesce(b.severity::text,'') AS severity,\n"
    "       coalesce(b.title,'') AS title,\n"
    "       b.arrival::text AS arrival, b.last_modified::text AS last_modified,\n"
    "       coalesce(b.done,'') AS done,\n"
    "       coalesce((SELECT string_agg(tag, ',') FROM bugs_tags t "
    "WHERE t.id = b.id), '') AS tags\n"
    "FROM archived_bugs b\n"
    "WHERE b.source = :'source'\n"
    "ORDER BY b.id")

# upload_history ~1M 行；version 为 debversion（::text 展示）；nmu 布尔。
_DS_UPLOAD_HISTORY = (
    "SELECT 'upload_history' AS grp, version::text AS version, date::text AS date,\n"
    "       coalesce(changed_by,'') AS changed_by, coalesce(nmu::text,'') AS nmu,\n"
    "       coalesce(distribution,'') AS distribution, coalesce(file,'') AS file\n"
    "FROM upload_history WHERE source = :'source'\n"
    "ORDER BY date DESC, version DESC, file")

_DS_UPLOAD_HISTORY_CLOSES = (
    "SELECT 'upload_history_closes' AS grp, source, version::text AS version,\n"
    "       bug::text AS bug, coalesce(file,'') AS file\n"
    "FROM upload_history_closes WHERE source = :'source'\n"
    "ORDER BY bug DESC, version DESC, file")

# 移除记录：package_removal（逐包行；arch_array 含 'source' 即源包移除）
# LEFT JOIN package_removal_batch（批次时间/ftpmaster/原因）；arch_array 是
# text[] 经 array_to_string 展开，reasons 是 text（2026-11-06 实库复核：
# 不是数组，勿用 array_to_string）。0 行=从未被移除（合法结果）。
_DS_REMOVALS = (
    "SELECT 'removals' AS grp, r.batch_id::text AS batch_id, r.name AS name,\n"
    "       r.version::text AS version,\n"
    "       array_to_string(r.arch_array, ',') AS arch_array,\n"
    "       b.time::text AS time, coalesce(b.ftpmaster,'') AS ftpmaster,\n"
    "       coalesce(b.distribution,'') AS distribution,\n"
    "       coalesce(b.requestor,'') AS requestor,\n"
    "       coalesce(b.reasons,'') AS reasons\n"
    "FROM package_removal r "
    "LEFT JOIN package_removal_batch b ON b.id = r.batch_id\n"
    "WHERE r.name = :'source' AND 'source' = ANY(r.arch_array)\n"
    "ORDER BY r.batch_id DESC, r.name")

# testing_autoremovals 的时间列是 epoch 整数（to_timestamp(...)::text 渲染）；
# version 列名以调查清单为准。0 行=不在自动移除名单（合法结果）。
_DS_TESTING_AUTOREMOVALS = (
    "SELECT 'testing_autoremovals' AS grp, source,\n"
    "       coalesce(version::text,'') AS version,\n"
    "       to_timestamp(first_seen)::text AS first_seen,\n"
    "       to_timestamp(last_checked)::text AS last_checked,\n"
    "       to_timestamp(removal_time)::text AS removal_time,\n"
    "       coalesce(rdeps::text,'') AS rdeps,\n"
    "       coalesce(buggy_deps::text,'') AS buggy_deps,\n"
    "       coalesce(bugs_deps::text,'') AS bugs_deps,\n"
    "       coalesce(bugs::text,'') AS bugs,\n"
    "       coalesce(rdeps_popcon::text,'') AS rdeps_popcon\n"
    "FROM testing_autoremovals WHERE source = :'source'\n"
    "ORDER BY source")

_DS_POPCON_SRC = (
    "SELECT 'popcon_src' AS grp, source, insts::text AS insts,\n"
    "       vote::text AS vote, olde::text AS olde,\n"
    "       recent::text AS recent, nofiles::text AS nofiles\n"
    "FROM popcon_src WHERE source = :'source'\n"
    "ORDER BY source")

# ci（ci.debian.net 导入）；duration 底层类型未逐列复核（interval/数值皆
# 可能），coalesce(...::text,'') 两种均安全。
_DS_CI = (
    "SELECT 'ci' AS grp, coalesce(suite,'') AS suite, coalesce(arch,'') AS arch,\n"
    "       source, version::text AS version, date::text AS date,\n"
    "       run_id::text AS run_id, coalesce(status,'') AS status,\n"
    "       coalesce(previous_status,'') AS previous_status,\n"
    "       coalesce(duration::text,'') AS duration,\n"
    "       coalesce(message,'') AS message\n"
    "FROM ci WHERE source = :'source'\n"
    "ORDER BY date DESC, run_id")

# reproducible 具备 release 列（调查清单口径）→ release 过滤生效；
# version 为 debversion（2026-11-02 实库逐列复核）→ ::text 展示。
_DS_REPRODUCIBLE = (
    "SELECT 'reproducible' AS grp, source, version::text AS version,\n"
    "       release, architecture, coalesce(status,'') AS status\n"
    "FROM reproducible WHERE source = :'source' " + _RELEASE_FILTER + "\n"
    "ORDER BY release, architecture")

# lintian 是 ~1.2GB 物化视图（重表，有效上限 10）；行无唯一键 → ORDER BY
# 以 ctid 收尾保证确定性。
_DS_LINTIAN = (
    "SELECT 'lintian' AS grp, package, coalesce(tag_type::text,'') AS tag_type,\n"
    "       coalesce(package_type,'') AS package_type,\n"
    "       package_version::text AS package_version,\n"
    "       coalesce(package_arch,'') AS package_arch, coalesce(tag,'') AS tag,\n"
    "       coalesce(information,'') AS information\n"
    "FROM lintian\n"
    "WHERE package = :'source' AND package_type = 'source'\n"
    "ORDER BY tag, package_version, package_arch, ctid")

# 18 数据集注册表（单一出处；插入序 = 目录序）。row_cap=None → 有效上限
# = 请求 limit（≤50）；重表 row_cap=10 → 有效上限 = min(limit, 10)。
DATASETS: dict[str, dict] = {
    "sources": {
        "question": "Which releases carry this source package, at which "
                    "version, with which maintainer/VCS/homepage metadata?",
        "physical": "sources",
        "filter": "source = :'source' + optional release",
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
        "filter": "source = :'source' + optional release",
        "order_by": "release",
        "columns": ["source", "version", "release", "status",
                    "upstream_version", "upstream_url", "watch_file",
                    "errors", "last_check"],
        "template": _UPSTREAM_BODY,
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
        "filter": "source = :'source'",
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
        "filter": "source = :'source'",
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
        "filter": "d.source = :'source'",
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
        "filter": "source = :'source' (inner subquery) + optional release",
        "order_by": "package, release",
        "columns": ["package", "release"],
        "template": _BINARIES_BODY,
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
        "template": _BUGS_BODY,
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
        "filter": "source = :'source'",
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
        "filter": "b.source = :'source'",
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
        "filter": "source = :'source'",
        "order_by": "id",
        "columns": ["id", "type", "source", "title"],
        "template": _WNPP_BODY,
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
        "filter": "source = :'source'",
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
        "filter": "source = :'source'",
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
        "filter": "r.name = :'source' AND 'source' = ANY(r.arch_array)",
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
        "filter": "source = :'source'",
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
        "filter": "source = :'source'",
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
        "filter": "source = :'source'",
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
        "filter": "source = :'source' + optional release",
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
        "filter": "package = :'source' AND package_type = 'source'",
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
    """18 个数据集的目录（纯函数、无 DB、确定序 = 注册表插入序）。

    每条字段：name/question/physical/filter/order_by/columns/
    default_limit/max_limit/release_scoped/caveats/joins。返回拷贝，
    调用方改写不影响注册表。
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


def render_catalog() -> str:
    """数据集目录的紧凑 markdown（给模型看；纯函数、确定序）。"""

    lines = [
        "## UDD datasets for fetch_udd(datasets=[...])",
        "",
        "Bounds: max 6 datasets per call; default limit 20, hard limit "
        "50; total effective rows per call <= 120; archived_bugs and "
        "lintian are capped at 10 rows each. The release filter applies "
        "ONLY to release-scoped datasets (sources, upstream, binaries, "
        "reproducible); all other datasets ignore release.",
        "",
    ]
    for spec in list_datasets():
        line = (f"- **{spec['name']}** ({spec['physical']}): "
                f"{spec['question']} Columns: "
                f"{', '.join(spec['columns'])}. Order: "
                f"{spec['order_by']}. Limit "
                f"{spec['default_limit']}/{spec['max_limit']}. "
                f"Release-scoped: "
                f"{'yes' if spec['release_scoped'] else 'no'}.")
        if spec["joins"] != "none":
            line += f" Joins: {spec['joins']}."
        if spec["caveats"]:
            line += " Caveats: " + "; ".join(spec["caveats"]) + "."
        lines.append(line)
    lines += [
        "",
        "Shared caveats: in psql --csv output NULL and empty string are "
        "indistinguishable (both render empty); 0 rows is a valid honest "
        "result; errors mean unknown, never absence. Every returned row "
        "carries provenance `udd:<dataset>#row=N` (1-based within that "
        "dataset's rows of THIS fetch).",
    ]
    return "\n".join(lines) + "\n"


def _dataset_limit_clause(cap: int | None) -> str:
    """datasets= 路径的 LIMIT 子句：普通 = LIMIT :limit1（limit+1 截断
    探测）；重表 = LIMIT LEAST(:limit1, cap+1)。cap 只接受 1..硬上限 的
    int（来自应用侧常量与已校验的 limit，绝不来自模型输入）。"""

    if cap is None:
        return "LIMIT :limit1;"
    if isinstance(cap, bool) or not isinstance(cap, int) \
            or not (1 <= cap <= int(DEFAULT_POLICY["dataset_rows_hard"])):
        raise UddError(
            "argument-invalid",
            f"dataset row cap must be an int in [1, "
            f"{DEFAULT_POLICY['dataset_rows_hard']}]; got {cap!r}")
    return f"LIMIT LEAST(:limit1, {cap + 1});"


def build_script(groups: tuple[str, ...] | None = None,
                 release: str | None = None, *, datasets=None,
                 dataset_caps: dict | None = None) -> str:
    """把固定模板拼成单个只读事务脚本（BEGIN; ... COMMIT;）。

    groups= 旧路径：模板自带 ``LIMIT :limit1``（字节不变）。datasets= 新
    路径：模板以 ORDER BY 结尾，LIMIT 由本函数按每数据集有效上限追加
    （dataset_caps 只含重表条目；缺省即普通 LIMIT）。
    """

    if datasets is not None:
        caps = dataset_caps or {}
        parts = [_T_META]
        for name in datasets:
            parts.append(DATASETS[name]["template"] + " "
                         + _dataset_limit_clause(caps.get(name)))
        return "BEGIN;\n" + "\n".join(parts) + "\nCOMMIT;\n"
    parts = [_T_META]
    for group in groups:
        parts.append(GROUP_TEMPLATES[group])
        parts.extend(GROUP_EXTRA.get(group, ()))
    return "BEGIN;\n" + "\n".join(parts) + "\nCOMMIT;\n"


def build_cohort_script() -> str:
    """QA cohort 枚举的只读事务脚本（meta + cohort，共 2 条语句）。"""

    return "BEGIN;\n" + _T_META + "\n" + _T_COHORT + "\nCOMMIT;\n"


# ---- 有界 psql 子进程 --------------------------------------------------------

def _run_bounded(argv: list[str], stdin_bytes: bytes, *, timeout_s: float,
                 max_output_bytes: int, env: dict | None = None
                 ) -> tuple[bytes, bytes, int | None, str | None]:
    """运行子进程并**有界**读取 stdout/stderr（select 循环 + 字节上限 +
    墙上超时，超限/超时 kill）。返回 (stdout, stderr, exit_code, kill_kind)；
    kill_kind ∈ {timeout, output-too-large, None}。绝无 shell=True。"""

    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
    except FileNotFoundError:
        raise UddError("psql-unavailable",
                       f"psql executable not found: {argv[0]!r}")
    except OSError as exc:
        raise UddError("psql-unavailable",
                       f"cannot spawn {argv[0]!r}: {type(exc).__name__}: {exc}")
    out: bytearray = bytearray()
    err: bytearray = bytearray()
    kill_kind: str | None = None
    deadline = time.monotonic() + float(timeout_s)
    try:
        # 脚本为固定模板（~4KB，远小于 64KB 管道缓冲），整写 stdin 不会
        # 阻塞——这是固定模板不变式保证的；若模板显著增长需改为分块写
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # 对方早退（如连接失败）：继续读输出拿错误信息
        fd_map = {proc.stdout.fileno(): out, proc.stderr.fileno(): err}
        fds = list(fd_map)
        while fds:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_kind = "timeout"
                break
            try:
                ready, _, _ = select.select(fds, [], [],
                                            min(remaining, 1.0))
            except InterruptedError:
                continue
            if not ready:
                continue  # 超时窗口到点后重查剩余时间
            for fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    fds.remove(fd)
                    continue
                fd_map[fd].extend(chunk)
                if len(out) >= max_output_bytes \
                        or len(err) >= max_output_bytes:
                    kill_kind = "output-too-large"
                    break
            if kill_kind:
                break
        # 单次 os.read 最多越过一个块；截到上限内保证返回值也有界
        if len(out) > max_output_bytes:
            del out[max_output_bytes:]
        if len(err) > max_output_bytes:
            del err[max_output_bytes:]
        if kill_kind:
            proc.kill()
            proc.wait()
            return bytes(out), bytes(err), None, kill_kind
        # 双管道 EOF 后子进程可能仍挂着（关输出再 hang）：wait 同样受
        # 剩余墙上时限约束，超时 kill + timeout，兑现全程有界承诺
        while True:
            code = proc.poll()
            if code is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                return bytes(out), bytes(err), None, "timeout"
            time.sleep(min(0.05, remaining))
        return bytes(out), bytes(err), code, None
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait()
            except OSError:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def parse_csv_output(text: str) -> tuple[dict[str, list[dict]], dict,
                                            list[list[str]]]:
    """解析多语句 psql --csv 输出：每个语句一个头行 + 数据行，首列 grp
    标记归属。返回 ({grp: rows}, meta_row, headers)；headers 按语句顺序
    保留每个头行（用于校验 SQL 列与数据集注册表一致）。头行本身
    grp=='grp' 被跳过；多行字段（引号内换行）由 csv 模块透明处理。"""

    groups: dict[str, list[dict]] = {}
    header: list[str] | None = None
    headers: list[list[str]] = []
    meta: dict = {}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        first = row[0]
        # 数据行的首列只会是固定组名（meta/source/bugs/…），头行首列是
        # 字面 "grp"——固定模板保证二者不会混淆
        if first == "grp":
            header = row
            headers.append(row)
            continue
        if header is None:
            raise UddError("parse-error",
                           f"csv output has data before any header: {row!r}")
        entry = dict(zip(header, row))
        if first == "meta":
            meta = entry
            continue
        groups.setdefault(first, []).append(entry)
    return groups, meta, headers


# ---- 客户端 ------------------------------------------------------------------

def _psql_env(policy: dict) -> dict:
    """构造 psql 子进程环境：只读 PGOPTIONS + 固定错误语言。"""

    env = dict(os.environ)
    env["PGOPTIONS"] = (
        f"-c statement_timeout={int(policy['statement_timeout_ms'])}"
        f" -c default_transaction_read_only=on")
    # LC_ALL=C：psql 错误消息语言固定（错误分级按英文 stderr 模式
    # 匹配，不受操作员 locale 影响；live 复审发现 zh_CN 下连接拒绝
    # 会被误分为 query-error）
    env["LC_ALL"] = "C"
    return env


def _classify_failure(code, kill_kind, stderr: bytes,
                      policy: dict) -> dict | None:
    """把 (exit_code, kill_kind, stderr) 分级为诚实错误或 None（成功）。"""

    if kill_kind == "timeout":
        return {
            "kind": "timeout",
            "message": f"psql exceeded {policy['timeout_s']:g}s "
                       f"wall clock and was killed",
        }
    if kill_kind == "output-too-large":
        return {
            "kind": "output-too-large",
            "message": f"psql output exceeded "
                       f"{policy['max_output_bytes']} bytes and the "
                       f"process was killed; no content parsed",
        }
    if code != 0:
        err_text = stderr.decode("utf-8", "replace").strip()
        # 服务端 statement_timeout 取消：非零退出 + stderr "canceling
        # statement..."——分级为 timeout（时限事实），非笼统 query-error
        if "statement timeout" in err_text \
                or "canceling statement" in err_text:
            return {"kind": "timeout",
                    "message": (f"psql exited with code {code}: "
                                f"{err_text[-1000:] or 'no stderr'}")}
        # psql 各版本连接失败措辞：旧 "could not connect to server" /
        # 新 "connection to server at ... failed"（LC_ALL=C 固定英文）
        if code is not None and (
                "could not connect" in err_text
                or "connection to server at" in err_text):
            return {"kind": "connection-error",
                    "message": err_text[-1000:] or "psql could not connect"}
        return {
            "kind": "query-error",
            "message": (f"psql exited with code {code}: "
                        f"{err_text[-1000:] or 'no stderr'}"),
        }
    return None


class UddClient:
    """UDD 有界只读客户端。一次 fetch() = 一个 psql 进程 = 一个只读事务 =
    ≤ max_queries 条固定模板语句。runner 可注入（测试用假子进程）。"""

    def __init__(self, dsn: str = DEFAULT_UDD_DSN, psql_path: str = "psql",
                 policy: dict | None = None, runner=None):
        self.dsn = validate_dsn(dsn)
        self.psql_path = psql_path
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)
        self._runner = runner or _run_bounded

    def _run(self, script: str, argv: list[str]):
        """跑一个固定模板脚本（有界子进程 + 只读/超时 PGOPTIONS）。"""

        return self._runner(
            argv, script.encode("utf-8"),
            timeout_s=float(self.policy["timeout_s"]),
            max_output_bytes=int(self.policy["max_output_bytes"]),
            env=_psql_env(self.policy))

    def fetch_cohort(self, limit: int | None = None, *,
                     maintainer: str = QA_MAINTAINER,
                     release: str | None = QA_RELEASE) -> dict:
        """QA cohort 枚举（固定模板；与 fetch 同一安全模型与错误分级）。

        返回 names（确定性按 source 排序，≤ limit 个）+ more_available
        截断探测 + fetched_at/response_sha256 出处。maintainer 仅接受
        应用侧常量；release 走允许清单。
        """

        limit = validate_limit(
            limit if limit is not None
            else self.policy["max_rows_default"])
        maintainer = validate_maintainer(maintainer)
        if release is None:
            release = QA_RELEASE
        release = validate_release(release)

        script = build_cohort_script()
        argv = [self.psql_path, "-X", "-q", "--csv", "-w",
                "-v", "ON_ERROR_STOP=1",
                "-v", f"maintainer={maintainer}",
                "-v", f"release={release}",
                "-v", f"limit1={limit + 1}",
                "-f", "-", self.dsn]
        fetched_at = utc_now()
        started = time.monotonic()
        stdout, stderr, code, kill_kind = self._run(script, argv)
        duration = round(time.monotonic() - started, 3)
        result = {
            "cohort": "qa-maintainer-sid",
            "maintainer": maintainer,
            "release": release,
            "limit": limit,
            "dsn_display": dsn_display(self.dsn),
            "template_version": COHORT_TEMPLATE_VERSION,
            "fetched_at": fetched_at,
            "duration_s": duration,
            "query_count": 2,
            "psql_argv_safe_form": _redact_argv(argv, self.dsn),
            "response_sha256": hashlib.sha256(stdout).hexdigest(),
        }
        kill_reason = _classify_failure(code, kill_kind, stderr,
                                        self.policy)
        if kill_reason is not None:
            result.update(status="error", error=kill_reason, names=[],
                          rows=[], row_count=0, more_available=None,
                          server=None, _raw_stdout=stdout, _stderr=stderr)
            return result
        try:
            parsed_groups, meta, _ = parse_csv_output(
                stdout.decode("utf-8", "replace"))
        except UddError as exc:
            result.update(status="error",
                          error={"kind": exc.kind, "message": exc.message},
                          names=[], rows=[], row_count=0,
                          more_available=None, server=None,
                          _raw_stdout=stdout, _stderr=stderr)
            return result
        rows = parsed_groups.get("cohort", [])
        more = len(rows) > limit
        shown = rows[:limit]
        names = [row.get("source", "") for row in shown]
        result.update(
            status="ok", error=None, rows=shown, names=names,
            row_count=len(shown), more_available=more,
            hidden_row_count=max(0, len(rows) - len(shown)),
            server={"server_version": meta.get("server_version"),
                    "server_now": meta.get("server_now")},
            _raw_stdout=stdout, _stderr=stderr)
        if not shown:
            result["note"] = ("0 cohort rows returned — a real empty "
                              "result, not a failure (no sources match "
                              "the QA maintainer filter in this release)")
        elif more:
            result["note"] = (f"cohort truncated at {limit} names; more "
                              f"exist (raise the limit for more)")
        return result

    def fetch(self, source, groups=None, release=None,
              limit: int | None = None, datasets=None) -> dict:
        """单包 UDD 查询（一次调用 = 一个 psql 进程 = 一个只读事务）。

        两条互斥路径：
        - ``groups=`` 旧路径（默认 DEFAULT_GROUPS）：7 个旧组名，行字典
          原样（无 provenance 键），默认 limit 30 / 硬上限 100；
        - ``datasets=`` 新路径（18 个数据集名，见 list_datasets()）：≤6
          个/次、有效行合计 ≤120、默认 limit 20 / 硬上限 50、重表
          （archived_bugs/lintian）有效上限 10；每行带
          ``provenance = "udd:<dataset>#row=N"``；结果键 ``datasets``
          （非 ``groups``）+ ``groups_data[dataset]``。

        二者同传 = argument-invalid。校验/预算错误抛 UddError（psql 之前）；
        查询失败以 status="error" 结果返回（失败 = 未知，非"无数据"）。
        """

        source = validate_source(source)
        if datasets is not None:
            if groups is not None:
                raise UddError(
                    "argument-invalid",
                    "datasets and groups are mutually exclusive; pass "
                    "one of the two (datasets= lists dataset names, "
                    "groups= is the legacy group path)")
            return self._fetch_datasets(source, datasets, release, limit)
        group_list = validate_groups(groups)
        release = validate_release(release)
        limit = validate_limit(
            limit if limit is not None
            else self.policy["max_rows_default"])

        script = build_script(group_list, release)
        statement_count = 1 + sum(
            1 + len(GROUP_EXTRA.get(g, ())) for g in group_list)
        if statement_count > int(self.policy["max_queries"]):
            raise UddError(
                "budget-exhausted",
                f"{statement_count} statements exceed per-fetch query cap "
                f"{self.policy['max_queries']}")

        argv = [self.psql_path, "-X", "-q", "--csv", "-w",
                "-v", "ON_ERROR_STOP=1",
                "-v", f"source={source}",
                "-v", f"release={release or ''}",
                "-v", f"limit1={limit + 1}",
                "-f", "-", self.dsn]
        fetched_at = utc_now()
        started = time.monotonic()
        stdout, stderr, code, kill_kind = self._run(script, argv)
        duration = round(time.monotonic() - started, 3)
        result = {
            "source": source,
            "groups": list(group_list),
            "release": release,
            "limit": limit,
            "dsn_display": dsn_display(self.dsn),
            "template_version": UDD_TEMPLATE_VERSION,
            "fetched_at": fetched_at,
            "duration_s": duration,
            "query_count": statement_count,
            "psql_argv_safe_form": _redact_argv(argv, self.dsn),
        }
        kill_reason = _classify_failure(code, kill_kind, stderr,
                                        self.policy)
        if kill_reason is not None:
            result.update(status="error", error=kill_reason,
                          server=None, groups_data={},
                          _raw_stdout=stdout, _stderr=stderr)
            return result
        try:
            parsed_groups, meta, _ = parse_csv_output(
                stdout.decode("utf-8", "replace"))
        except UddError as exc:
            result.update(status="error",
                          error={"kind": exc.kind, "message": exc.message},
                          server=None, groups_data={},
                          _raw_stdout=stdout, _stderr=stderr)
            return result

        groups_data: dict[str, dict] = {}
        expected = {g for g in group_list}
        expected |= {"binaries"} if "bugs" in group_list else set()
        for group in sorted(parsed_groups):
            rows = parsed_groups[group]
            row_cap = limit
            more = len(rows) > row_cap
            shown = rows[:row_cap]
            entry = {
                "status": "ok",
                "rows": shown,
                "row_count": len(shown),
                "limit": limit,
                "more_available": more,
                "hidden_row_count": max(0, len(rows) - len(shown)),
            }
            if group == "bugs":
                for row in shown:
                    for flag in ("via_source_mapping",
                                 "via_bugs_packages"):
                        if row.get(flag) == "true":
                            row[flag] = True
                        elif row.get(flag) == "false":
                            row[flag] = False
                entry["association_note"] = (
                    "match provenance per row: package/source kept verbatim; "
                    "via_source_mapping = BTS source mapping equals the "
                    "queried source; via_bugs_packages = listed in "
                    "bugs_packages for this source; binary matches came via "
                    "packages/binaries mapping. Open = status <> 'done' "
                    "(archived_bugs not covered). No keyword filtering of "
                    "titles.")
            if not shown:
                entry["note"] = ("0 rows returned for this group — a real "
                                 "empty result, not a failure (unknown "
                                 "source name or no matching rows)")
            elif more:
                entry["note"] = (f"truncated at {limit} rows; more rows "
                                 f"exist (re-fetch with a higher limit for "
                                 f"more; explicit pagination not provided)")
            if group not in expected:
                entry["unexpected"] = True
            groups_data[group] = entry
        for group in sorted(expected):
            if group not in groups_data:
                groups_data[group] = {
                    "status": "ok", "rows": [], "row_count": 0,
                    "limit": limit, "more_available": False,
                    "note": "0 rows returned for this group — a real "
                            "empty result, not a failure (statement ran "
                            "and matched nothing)",
                }
        result.update(
            status="ok", error=None,
            server={"server_version": meta.get("server_version"),
                    "server_now": meta.get("server_now")},
            groups_data=groups_data,
            notes=[
                "rows are live UDD facts fetched via bounded read-only "
                "psql queries; source-side timestamps are server-rendered "
                "verbatim and are distinct from fetched_at",
                "in psql --csv output NULL and empty string are "
                "indistinguishable (both empty); no speculative filling",
                "upstream.watch_file is fetched verbatim and retained "
                "raw; it is never executed (uscan or its regexes are "
                "never run)",
                "bugs group covers open (status<>'done') reports matched "
                "via BTS source mapping, src: pseudo-package, "
                "bugs_packages association, or binary->source mapping; "
                "titles are not keyword-filtered; full discussions need "
                "bug_fetch per bug id",
            ],
            _raw_stdout=stdout, _stderr=stderr)
        return result

    def _fetch_datasets(self, source: str, datasets, release,
                        limit: int | None) -> dict:
        """datasets= 新路径（见 fetch 的公共 API 说明）。"""

        dataset_list = validate_datasets(datasets)
        release = validate_release(release)
        limit = validate_limit(
            limit if limit is not None
            else int(self.policy["dataset_rows_default"]),
            hard=int(self.policy["dataset_rows_hard"]))
        if len(dataset_list) > int(self.policy["datasets_max"]):
            raise UddError(
                "budget-exhausted",
                f"{len(dataset_list)} datasets exceed per-fetch dataset "
                f"cap {self.policy['datasets_max']}")
        caps = {
            name: (min(limit, DATASETS[name]["row_cap"])
                   if DATASETS[name]["row_cap"] is not None else limit)
            for name in dataset_list}
        total_rows = sum(caps.values())
        if total_rows > int(self.policy["dataset_rows_total_max"]):
            raise UddError(
                "budget-exhausted",
                f"requested effective rows {total_rows} exceed per-fetch "
                f"total row cap {self.policy['dataset_rows_total_max']} "
                f"(datasets={list(dataset_list)}, limit={limit})")
        statement_count = 1 + len(dataset_list)
        if statement_count > int(self.policy["max_queries"]):
            raise UddError(
                "budget-exhausted",
                f"{statement_count} statements exceed per-fetch query "
                f"cap {self.policy['max_queries']}")
        heavy_caps = {name: caps[name] for name in dataset_list
                      if DATASETS[name]["row_cap"] is not None}

        script = build_script(datasets=dataset_list,
                              dataset_caps=heavy_caps)
        argv = [self.psql_path, "-X", "-q", "--csv", "-w",
                "-v", "ON_ERROR_STOP=1",
                "-v", f"source={source}",
                "-v", f"release={release or ''}",
                "-v", f"limit1={limit + 1}",
                "-f", "-", self.dsn]
        fetched_at = utc_now()
        started = time.monotonic()
        stdout, stderr, code, kill_kind = self._run(script, argv)
        duration = round(time.monotonic() - started, 3)
        result = {
            "source": source,
            "datasets": list(dataset_list),
            "release": release,
            "limit": limit,
            "dsn_display": dsn_display(self.dsn),
            "template_version": DATASETS_TEMPLATE_VERSION,
            "fetched_at": fetched_at,
            "duration_s": duration,
            "query_count": statement_count,
            "psql_argv_safe_form": _redact_argv(argv, self.dsn),
        }
        kill_reason = _classify_failure(code, kill_kind, stderr,
                                        self.policy)
        if kill_reason is not None:
            result.update(status="error", error=kill_reason,
                          server=None, groups_data={},
                          _raw_stdout=stdout, _stderr=stderr)
            return result
        try:
            parsed_groups, meta, headers = parse_csv_output(
                stdout.decode("utf-8", "replace"))
        except UddError as exc:
            result.update(status="error",
                          error={"kind": exc.kind,
                                 "message": exc.message},
                          server=None, groups_data={},
                          _raw_stdout=stdout, _stderr=stderr)
            return result

        expected_headers = [
            ["grp"] + list(DATASETS[name]["columns"])
            for name in dataset_list]
        if headers[1:] != expected_headers:
            result.update(
                status="error",
                error={"kind": "parse-error",
                       "message": (
                           "dataset CSV header does not match the "
                           "registered columns; expected "
                           f"{expected_headers!r}, got {headers[1:]!r}")},
                server=None, groups_data={},
                _raw_stdout=stdout, _stderr=stderr)
            return result

        datasets_data: dict[str, dict] = {}
        for dataset in sorted(parsed_groups):
            rows = parsed_groups[dataset]
            cap = caps.get(dataset, limit)
            more = len(rows) > cap
            shown = rows[:cap]
            for idx, row in enumerate(shown, start=1):
                row["provenance"] = f"udd:{dataset}#row={idx}"
            entry = {
                "status": "ok",
                "rows": shown,
                "columns": list(DATASETS[dataset]["columns"])
                if dataset in DATASETS else [],
                "row_count": len(shown),
                "limit": cap,
                "more_available": more,
                "hidden_row_count": max(0, len(rows) - len(shown)),
            }
            if not shown:
                entry["note"] = (
                    "0 rows returned for this dataset — a real empty "
                    "result, not a failure (unknown source name or no "
                    "matching rows)")
            elif more:
                note = (f"truncated at {cap} rows; more rows exist "
                        f"(re-fetch with a higher limit for more; "
                        f"explicit pagination not provided)")
                if dataset in DATASETS \
                        and DATASETS[dataset]["row_cap"] is not None:
                    note += (f"; heavy dataset effective cap "
                             f"{DATASETS[dataset]['row_cap']} rows "
                             f"applies even at higher limits")
                entry["note"] = note
            if dataset not in caps:
                entry["unexpected"] = True
            datasets_data[dataset] = entry
        for dataset in dataset_list:
            if dataset not in datasets_data:
                datasets_data[dataset] = {
                    "status": "ok", "rows": [],
                    "columns": list(DATASETS[dataset]["columns"]),
                    "row_count": 0, "limit": caps[dataset],
                    "more_available": False,
                    "note": "0 rows returned for this dataset — a real "
                            "empty result, not a failure (statement ran "
                            "and matched nothing)",
                }
        result.update(
            status="ok", error=None,
            server={"server_version": meta.get("server_version"),
                    "server_now": meta.get("server_now")},
            groups_data=datasets_data,
            notes=[
                "rows are live UDD facts fetched via bounded read-only "
                "psql queries; source-side timestamps are server-rendered "
                "verbatim and are distinct from fetched_at",
                "each row carries provenance "
                '"udd:<dataset>#row=N" — the 1-based row number within '
                "that dataset as ordered by its fixed ORDER BY, valid "
                "only for rows of THIS fetch",
                "in psql --csv output NULL and empty string are "
                "indistinguishable (both empty); no speculative filling",
                "the release filter is applied only to release-scoped "
                "datasets (sources, upstream, binaries, reproducible); "
                "other datasets ignore the release parameter",
                "upstream.watch_file is fetched verbatim and retained "
                "raw; it is never executed (uscan or its regexes are "
                "never run)",
                "0 rows is a valid honest result (common for removals, "
                "testing_autoremovals, upstream_metadata); errors mean "
                "unknown, never absence of data",
            ],
            _raw_stdout=stdout, _stderr=stderr)
        return result


def extract_candidate_urls(result: dict) -> list[str]:
    """从 UDD 行里抽取可作为调查候选的 URL（数据库出处、弱于页面正文；
    排序去重）。只认 http(s) 绝对 URL。

    同时接受 groups= 旧路径键（source/upstream/metadata/packaging）与
    datasets= 新路径键（sources/upstream_metadata/vcswatch/urls）。urls
    （duck_urls/duck_url_status）的 URL **不**进入候选：那是 DUCK 的检查
    目标清单（混有打包 VCS/版权等 URL），不构成上游身份证明。
    """

    urls: list[str] = []
    groups = result.get("groups_data") or {}

    def add(value):
        if isinstance(value, str) and value.startswith(("http://",
                                                        "https://")):
            if value not in urls:
                urls.append(value)

    for name in ("source", "sources"):
        for row in groups.get(name, {}).get("rows", []):
            add(row.get("homepage"))
            add(row.get("vcs_url"))
            add(row.get("vcs_browser"))
    for row in groups.get("upstream", {}).get("rows", []):
        add(row.get("upstream_url"))
    for name in ("packaging", "vcswatch"):
        for row in groups.get(name, {}).get("rows", []):
            add(row.get("url"))
            add(row.get("browser"))
    for name in ("metadata", "upstream_metadata"):
        for row in groups.get(name, {}).get("rows", []):
            if row.get("key") in ("Repository", "Repository-Browse",
                                  "Homepage"):
                add(row.get("value"))
    return urls
