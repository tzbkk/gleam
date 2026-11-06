"""包事实数据模型：Fact（带出处字段组）、Candidate（端点候选）、PackageFacts。

该模块被两个"表族"共用：
- 快照族（SNAPSHOT_TABLES，``sources.csv`` 等）：仅供历史归档快照读取器
  ``gleam.snapshot.load_snapshot`` 使用（root ``raw/`` 是 2026-10-29 的历史
  存档，不再是运行时输入）。
- live 族（LIVE_TABLES，``udd:sources`` 等）：live UDD 适配器
  ``gleam.liveudd`` 把实时抓回的行适配成同样的 Fact/Candidate 结构。

候选语义在两个表族上完全一致（这是产品语义，不是数据源语义）：
- homepage 与 DEP-12 Repository-Browse 是 web 候选（默认抓取）；
- DEP-12 Repository 是 git-endpoint，仅登记不抓取；
- upstream_url 只登记为 upstream-artifact（可能是压缩包，策略禁止下载制品）；
- sources/vcswatch 的 VCS 字段是 Debian 打包出处（packaging provenance），
  绝不进入上游候选（上游与打包仓库可能共址，不构成上游身份证明或反证）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 表族：逻辑表键 → 实际表名（Fact.table / 出处字符串使用实际表名）
SNAPSHOT_TABLES = {
    "sources": "sources.csv",
    "upstream": "upstream.csv",
    "metadata": "upstream_metadata.csv",
    "packaging": "vcswatch.csv",
}
LIVE_TABLES = {
    "sources": "udd:sources",
    "upstream": "udd:upstream",
    "metadata": "udd:upstream_metadata",
    "packaging": "udd:vcswatch",
}


@dataclass
class Fact:
    """一条带出处的字段组：表名、数据行号（1 起）与字段值。

    row_number 是结果集内的"数据行序号"（快照 CSV：不含表头，多行字段使
    物理行号与之不同；live UDD：模板 ORDER BY 下的确定性行序）。出处写作
    ``#row=N``；live 出处的真实性由所在抓取的 fetched_at 与响应哈希背书
    （见 liveudd 的 provenance）。
    """

    table: str
    row_number: int
    fields: dict

    @property
    def provenance(self) -> str:
        return f"{self.table}#row={self.row_number}"


@dataclass
class Candidate:
    """端点候选：label 说明来源语义，probe 是否默认抓取由 kind 决定。"""

    url: str
    label: str
    kind: str          # web | git-endpoint | artifact | api
    provenance: str
    fetch: bool = True  # web 才默认抓取


@dataclass
class PackageFacts:
    name: str
    rows: list[Fact] = field(default_factory=list)
    # 表族映射（逻辑键 → 实际表名）；默认快照族，live 适配器传入 LIVE_TABLES
    tables: dict = field(default_factory=lambda: dict(SNAPSHOT_TABLES))

    def field_values(self, table: str, key: str) -> list[tuple[Fact, str]]:
        out = []
        for fact in self.rows:
            if fact.table == table:
                out.append((fact, (fact.fields.get(key) or "").strip()))
        return out

    def first(self, table: str, key: str) -> str | None:
        for fact, value in self.field_values(table, key):
            if value:
                return value
        return None

    def all_values(self, table: str, key: str) -> list[str]:
        seen, out = set(), []
        for _, value in self.field_values(table, key):
            if value and value not in seen:
                seen.add(value)
                out.append(value)
        return out

    @property
    def versions(self) -> list[str]:
        out = []
        for fact in self.rows:
            v = (fact.fields.get("version") or "").strip()
            if v and v not in out:
                out.append(v)
        return out

    def metadata_pairs(self, key: str) -> list[tuple[Fact, str]]:
        """DEP-12 upstream_metadata 是 (source,key,value) 长表：按 key 行匹配。"""

        out = []
        for fact in self.rows:
            if fact.table != self.tables["metadata"]:
                continue
            if (fact.fields.get("key") or "").strip() != key:
                continue
            value = (fact.fields.get("value") or "").strip()
            if value:
                out.append((fact, value))
        return out

    def upstream_candidates(self) -> list[Candidate]:
        """Homepage 与 DEP-12 Repository / Repository-Browse 分别打标签。

        DEP-12 事实取 upstream_metadata 长表行；upstream_url 只登记为
        artifact 候选，不抓取（可能是压缩包而非网页，且策略禁止下载制品）。
        """
        out = []
        sources_t = self.tables["sources"]
        upstream_t = self.tables["upstream"]
        for value in self.all_values(sources_t, "homepage"):
            out.append(Candidate(value, "homepage-candidate", "web",
                                 f"{self.name}:{sources_t}:homepage"))
        for key, label, kind, fetch in (
            ("Repository", "dep12-repository", "git-endpoint", False),
            ("Repository-Browse", "dep12-repository-browse", "web", True),
        ):
            for fact, value in self.metadata_pairs(key):
                out.append(Candidate(
                    value, label, kind,
                    f"{self.name}:{self.tables['metadata']}:{key}:"
                    f"{fact.provenance}",
                    fetch=fetch))
        for value in self.all_values(upstream_t, "upstream_url"):
            out.append(Candidate(value, "upstream-artifact", "artifact",
                                 f"{self.name}:{upstream_t}:upstream_url",
                                 fetch=False))
        seen, uniq = set(), []
        for c in out:
            if (c.url, c.label) not in seen:
                seen.add((c.url, c.label))
                uniq.append(c)
        return uniq

    def packaging_vcs(self) -> list[dict]:
        """Debian 打包 VCS（salsa 等）：是打包出处事实，不是上游身份证明。

        上游与打包仓库可能共址（如 wreport 式 PACKAGING_IN）；本字段集
        既不证明也不否定端点的上游身份，仅供报告展示与人工比对。
        """

        out = []
        vcs_tables = (self.tables["sources"], self.tables["packaging"])
        for table in vcs_tables:
            for key in ("vcs_url", "vcs_browser", "url", "browser"):
                for fact, value in self.field_values(table, key):
                    if value:
                        out.append({"url": value, "field": key,
                                    "provenance": fact.provenance,
                                    "note": "debian-packaging-vcs (packaging "
                                            "provenance; not proof of upstream "
                                            "identity; may colocate)"})
        seen, uniq = set(), []
        for item in out:
            k = (item["url"], item["field"])
            if k not in seen:
                seen.add(k)
                uniq.append(item)
        return uniq

    def fact_citations(self) -> set[str]:
        """全部事实行的出处字符串（live 引用校验的合格集合）。"""

        return {fact.provenance for fact in self.rows}
