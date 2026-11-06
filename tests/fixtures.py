"""测试合成数据：历史快照微型 CSV（多行字段、重复 source 行）与 live UDD
合成 psql 输出（FakeUddRunner，含 datasets= 新路径）。全部仅用于测试；
仓库 raw/ 永不触碰。"""

import os
import re
import textwrap
from unittest import mock

from gleam import udd as udd_mod

QA_MAINTAINER = "Debian QA Group <packages@qa.debian.org>"

SOURCES_CSV = textwrap.dedent("""\
    source,version,homepage,vcs_type,vcs_url,vcs_browser,format,files
    pkgone,1.0-1,https://example.org/pkgone/,Git,https://salsa.debian.org/debian/pkgone.git,https://salsa.debian.org/debian/pkgone,3.0 (quilt),"abc123 100 pkgone_1.0-1.dsc
    def456 9000 pkgone_1.0.orig.tar.gz"
    pkgone,1.1-1,https://example.org/pkgone/,Git,https://salsa.debian.org/debian/pkgone.git,https://salsa.debian.org/debian/pkgone,3.0 (quilt),"ghi789 100 pkgone_1.1-1.dsc"
    pkgtwo,2.0-1,,,Git,https://salsa.debian.org/debian/pkgtwo.git,https://salsa.debian.org/debian/pkgtwo,3.0 (native),
    pkgthree,3.0-1,https://example.net/three/,Git,https://salsa.debian.org/debian/pkgthree.git,https://salsa.debian.org/debian/pkgthree,3.0 (quilt),
    """)

UPSTREAM_CSV = textwrap.dedent("""\
    source,version,distribution,release,component,watch_file,metadata,debian_uversion,debian_mangled_uversion,upstream_version,upstream_url,errors,warnings,status,last_check
    pkgone,1.0-1,debian,sid,main,"version=4
    https://example.org/pkgone/pkgone-(\\d\\.\\d)\\.tar\\.gz
    ",,1.0,1.0,1.0,https://example.org/pkgone/pkgone-1.0.tar.gz,,,up to date,2026-10-28 00:21:54
    pkgtwo,2.0-1,debian,sid,main,,,,,,,"In debian/watch no matching files",,error,2026-10-27 00:25:24
    pkgthree,3.0-1,debian,sid,main,,,"Repository: https://gitlab.example.net/pkgthree.git
    Repository-Browse: https://gitlab.example.net/pkgthree",,,3.0,https://example.net/three/pkgthree-3.0.tar.xz,,,up to date,2026-10-26 00:22:12
    """)

UPSTREAM_METADATA_CSV = textwrap.dedent("""\
    source,key,value
    pkgone,Repository,https://git.example.org/pkgone.git
    pkgone,Repository-Browse,https://git.example.org/pkgone
    pkgtwo,Name,pkgtwo
    pkgthree,Repository-Browse,https://gitlab.example.net/pkgthree
    """)

VCSWATCH_CSV = textwrap.dedent("""\
    source,version,vcs,url,branch,browser,status
    pkgone,1.0-1,Git,https://salsa.debian.org/debian/pkgone.git,debian/main,https://salsa.debian.org/debian/pkgone,OK
    """)

MANIFEST = textwrap.dedent("""\
    # UDD QA metadata snapshot (synthetic fixture)
    pulled_at:   2026-10-29T06:44:28Z
    qa_packages: 3
    """)


def write_fixture(root: str) -> str:
    os.makedirs(root, exist_ok=True)
    for name, content in (
        ("sources.csv", SOURCES_CSV),
        ("upstream.csv", UPSTREAM_CSV),
        ("upstream_metadata.csv", UPSTREAM_METADATA_CSV),
        ("vcswatch.csv", VCSWATCH_CSV),
        ("_manifest.txt", MANIFEST),
    ):
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write(content)
    return root


# ---- live UDD 合成输出（固定模板 v2 列序） -----------------------------------

def csv_text(*statements):
    """statements: 每条 = (header_cols, rows)；合成多语句 psql --csv 输出。"""

    out = []
    for header, rows in statements:
        out.append(",".join(header))
        for row in rows:
            cells = []
            for cell in row:
                cell = str(cell)
                if any(ch in cell for ch in ',"\n'):
                    cells.append('"' + cell.replace('"', '""') + '"')
                else:
                    cells.append(cell)
            out.append(",".join(cells))
    return ("\n".join(out) + "\n").encode()


UDD_META_HEADER = ["grp", "server_version", "server_now"]
UDD_META_ROW = ["meta", "PostgreSQL 17.5 (Debian)", "2026-11-03 00:00:00+00"]
UDD_SOURCE_HEADER = ["grp", "source", "version", "release", "maintainer",
                     "homepage", "bin", "vcs_type", "vcs_url", "section",
                     "priority", "build_depends"]
UDD_UPSTREAM_HEADER = ["grp", "source", "version", "release", "status",
                       "upstream_version", "upstream_url", "watch_file",
                       "errors", "last_check"]
UDD_METADATA_HEADER = ["grp", "source", "key", "value"]
UDD_PACKAGING_HEADER = ["grp", "source", "version", "vcs", "url", "browser",
                        "status", "last_scan", "changelog_distribution",
                        "commit_id"]
UDD_COHORT_HEADER = ["grp", "source", "version", "release", "maintainer"]

PKGONE_WATCH_FILE = "version=4\nhttps://example.org/pkgone/pkgone-(\\d\\.\\d)\\.tar\\.gz\n"

# 与快照 fixture 语义一致的 live 事实（pkgone 4 候选 / pkgtwo 无候选 /
# pkgthree 有 homepage + Repository-Browse + 制品）
UDD_FACTS = {
    "pkgone": {
        "source": [["source", "pkgone", "1.1-1", "sid", QA_MAINTAINER,
                    "https://example.org/pkgone/", "pkgone-bin", "Git",
                    "https://salsa.debian.org/debian/pkgone.git", "devel",
                    "optional", ""]],
        "upstream": [["upstream", "pkgone", "1.1-1", "sid", "up to date",
                      "1.1", "https://example.org/pkgone/pkgone-1.1.tar.gz",
                      PKGONE_WATCH_FILE, "", "2026-10-28 00:21:54"]],
        "metadata": [
            ["metadata", "pkgone", "Repository",
             "https://git.example.org/pkgone.git"],
            ["metadata", "pkgone", "Repository-Browse",
             "https://git.example.org/pkgone"]],
        "packaging": [["packaging", "pkgone", "1.1-1", "Git",
                       "https://salsa.debian.org/debian/pkgone.git",
                       "https://salsa.debian.org/debian/pkgone", "ok",
                       "2026-10-30 04:00:53+00", "unstable", "abc123"]],
    },
    "pkgtwo": {
        "source": [["source", "pkgtwo", "2.0-1", "sid", QA_MAINTAINER,
                    "", "pkgtwo", "Git",
                    "https://salsa.debian.org/debian/pkgtwo.git", "devel",
                    "optional", ""]],
        "upstream": [["upstream", "pkgtwo", "2.0-1", "sid", "error", "",
                      "", "", "In debian/watch no matching files",
                      "2026-10-27 00:25:24"]],
        "metadata": [["metadata", "pkgtwo", "Name", "pkgtwo"]],
        "packaging": [],
    },
    "pkgthree": {
        "source": [["source", "pkgthree", "3.0-1", "sid", QA_MAINTAINER,
                    "https://example.net/three/", "pkgthree", "Git",
                    "https://salsa.debian.org/debian/pkgthree.git", "devel",
                    "optional", ""]],
        "upstream": [["upstream", "pkgthree", "3.0-1", "sid", "up to date",
                      "3.0", "https://example.net/three/pkgthree-3.0.tar.xz",
                      "", "", "2026-10-26 00:22:12"]],
        "metadata": [["metadata", "pkgthree", "Repository-Browse",
                      "https://gitlab.example.net/pkgthree"]],
        "packaging": [["packaging", "pkgthree", "3.0-1", "Git",
                       "https://salsa.debian.org/debian/pkgthree.git",
                       "https://salsa.debian.org/debian/pkgthree", "ok",
                       "2026-10-30 04:00:53+00", "unstable", "def456"]],
    },
}

# 由别的维护者持有（cohort 之外）的包：sources 行存在但 maintainer 不同
UDD_NON_QA_SOURCE_ROW = ["source", "keepalived", "1:2.3.4-1", "sid",
                         "Example Maintainers <pkg@example.org>", "", "",
                         "", "", "admin", "optional", ""]


def udd_facts_stdout(source, *, extra_sources=None):
    """按固定模板 v2 列序合成单包 facts 查询的 psql --csv 输出。"""

    facts = UDD_FACTS.get(source)
    statements = [(UDD_META_HEADER, [UDD_META_ROW])]
    if facts is None and extra_sources and source in extra_sources:
        facts = extra_sources[source]
    if facts is None:
        # 未知包：全组 0 行（诚实空结果）
        for header in (UDD_SOURCE_HEADER, UDD_UPSTREAM_HEADER,
                       UDD_METADATA_HEADER, UDD_PACKAGING_HEADER):
            statements.append((header, []))
    else:
        statements.append((UDD_SOURCE_HEADER, facts["source"]))
        statements.append((UDD_UPSTREAM_HEADER, facts["upstream"]))
        statements.append((UDD_METADATA_HEADER, facts["metadata"]))
        statements.append((UDD_PACKAGING_HEADER, facts["packaging"]))
    return csv_text(*statements)


def udd_cohort_stdout(names, *, more_names=()):
    """合成 QA cohort 枚举查询的 psql --csv 输出（已按 source 排序）。"""

    rows = [["cohort", name, "1.0-1", "sid", QA_MAINTAINER]
            for name in sorted(names)]
    rows += [["cohort", name, "1.0-1", "sid", QA_MAINTAINER]
             for name in sorted(more_names)]
    return csv_text((UDD_META_HEADER, [UDD_META_ROW]),
                    (UDD_COHORT_HEADER, rows))


def _dataset_facts(source, extra_sources):
    facts = UDD_FACTS.get(source)
    if facts is None and extra_sources and source in extra_sources:
        facts = extra_sources[source]
    return facts


def udd_datasets_stdout(datasets, source, extra_sources=None):
    """按 datasets= 新路径（template v3）列序合成 psql --csv 输出。

    sources/upstream/upstream_metadata/vcswatch 由 UDD_FACTS（或注入的
    extra_sources）语义等价映射合成（缺列填 ""）；其余数据集只给表头
    + 0 行（诚实空结果）。未知源包：全部 0 行。
    """

    facts = _dataset_facts(source, extra_sources)
    statements = [(UDD_META_HEADER, [UDD_META_ROW])]
    for name in datasets:
        columns = list(udd_mod.DATASETS[name]["columns"])
        header = ["grp"] + columns
        rows = []
        if facts is not None:
            if name == "sources":
                # legacy source 行：grp,source,version,release,maintainer,
                # homepage,bin,vcs_type,vcs_url,section,priority,
                # build_depends → 插入空的 vcs_browser
                for row in facts["source"]:
                    rows.append(["sources", row[1], row[2], row[3], row[4],
                                 row[5], row[6], row[7], row[8], "",
                                 row[9], row[10]])
            elif name == "upstream":
                for row in facts["upstream"]:
                    rows.append(["upstream"] + list(row[1:]))
            elif name == "upstream_metadata":
                for row in facts["metadata"]:
                    rows.append(["upstream_metadata", row[1], row[2],
                                 row[3]])
            elif name == "vcswatch":
                # legacy packaging 行：grp,source,version,vcs,url,browser,
                # status,last_scan,changelog_distribution,commit_id →
                # 插入空 changelog_version，error/ci_status/issues/
                # merge_requests 填 ""
                for row in facts["packaging"]:
                    rows.append(["vcswatch", row[1], row[2], row[3], row[4],
                                 row[5], row[6], row[7], "", row[8], row[9],
                                 "", "", "", ""])
        statements.append((header, rows))
    return csv_text(*statements)


def _argv_var(argv, name):
    prefix = name + "="
    for item in argv:
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


_GRP_MARKER_RE = re.compile(r"'([a-z0-9_]+)' AS grp")


def _script_datasets(script: str):
    """datasets= 新路径脚本识别：按出现序返回请求的数据集名，legacy 返回
    None。判据 = 全部 grp 标记（除 meta）都是数据集名——legacy 脚本必含
    'source'/'metadata'/'packaging'（非数据集名），不会误判；upstream/
    bugs/wnpp/binaries 在两条路径同名，靠"全标记 ⊆ DATASET_NAMES"区分。
    """

    names: list[str] = []
    for marker in _GRP_MARKER_RE.findall(script):
        if marker in ("meta", "cohort") or marker in names:
            continue
        names.append(marker)
    if names and all(n in udd_mod.DATASET_NAMES for n in names):
        return names
    return None


class FakeUddRunner:
    """离线假 psql：按 stdin 脚本区分 cohort / datasets= 新路径 / legacy
    facts 查询并回放合成 CSV。

    记录全部调用（argv/stdin）供断言（如"investigate 零预取：模型首次
    fetch_udd 之前零调用"）。unknown_sources 可注入额外包（两条路径共用）；
    error=('conn', msg) 可模拟连接失败。
    """

    def __init__(self, cohort_names=("pkgone", "pkgthree"),
                 cohort_more_names=(), extra_sources=None, error=None):
        self.cohort_names = tuple(cohort_names)
        self.cohort_more_names = tuple(cohort_more_names)
        self.extra_sources = extra_sources or {}
        self.error = error
        self.calls = []

    def __call__(self, argv, stdin_bytes, *, timeout_s,
                 max_output_bytes, env=None):
        self.calls.append({"argv": argv, "stdin": stdin_bytes})
        if self.error is not None:
            kind, message = self.error
            stderr = message if isinstance(message, bytes) \
                else str(message).encode()
            return b"", stderr, 2, None
        script = stdin_bytes.decode()
        if "'cohort' AS grp" in script:
            return (udd_cohort_stdout(self.cohort_names,
                                      more_names=self.cohort_more_names),
                    b"", 0, None)
        source = _argv_var(argv, "source")
        datasets = _script_datasets(script)
        if datasets is not None:
            return (udd_datasets_stdout(datasets, source,
                                        extra_sources=self.extra_sources),
                    b"", 0, None)
        return (udd_facts_stdout(source, extra_sources=self.extra_sources),
                b"", 0, None)


def patch_udd_client(runner):
    """构造把 gleam.udd.UddClient 替换为假客户端的 mock.patch（with 使用）。"""

    from gleam import udd as udd_mod
    real_client = udd_mod.UddClient

    def factory(*args, **kwargs):
        kwargs.pop("policy", None)
        return real_client(runner=runner,
                           dsn=kwargs.get("dsn", udd_mod.DEFAULT_UDD_DSN),
                           psql_path=kwargs.get("psql_path", "psql"))

    return mock.patch("gleam.udd.UddClient", factory)
