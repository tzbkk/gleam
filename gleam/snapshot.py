"""历史归档快照读取器（legacy）：读取仓库根 ``raw/`` 下的 UDD QA 快照 CSV。

**这不是活跃运行时输入**：root ``raw/`` 是 scripts/pull_udd.sh 于
2026-10-29 拉取的历史存档（过滤 maintainer='Debian QA Group
<packages@qa.debian.org>' AND release='sid'），保留在磁盘上仅供历史分析
与实验报告复核。活跃 CLI（inspect / run / investigate）的包选择与包事实
来自 live UDD（见 gleam/liveudd.py + gleam/udd.py）；本模块不被活跃路径
导入，亦绝不被静默回退使用。

注意区分两种 "raw"：
- 仓库根 ``raw/``：上述历史快照存档（本模块的唯一用途）；
- run 存储内的 ``<store>/runs/<id>/raw/``：每次运行的原始 HTTP/查询
  证据（追加式，gleam check 可校验），与快照无关、永不删除。

数据模型（Fact/Candidate/PackageFacts）已移至 gleam/facts.py 并在此
再导出以保持兼容；本模块只保留 CSV 加载逻辑。
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from dataclasses import dataclass, field

from .facts import (LIVE_TABLES, SNAPSHOT_TABLES, Candidate, Fact,  # noqa: F401
                    PackageFacts)

# Debian source 包名策略：小写字母/数字起头，仅 [a-z0-9+.-]
DEBIAN_SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]+$")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_rows(path: str) -> list[dict]:
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass
class Snapshot:
    root: str
    manifest: dict
    input_hashes: dict
    packages: dict[str, PackageFacts]
    row_counts: dict

    def select(self, names: list[str] | None = None, limit: int | None = None) -> list[PackageFacts]:
        if names:
            # 去重（保序）：重复 --package 不应重复观测同一包
            seen, uniq = set(), []
            for name in names:
                if name not in seen:
                    seen.add(name)
                    uniq.append(name)
            missing = [n for n in uniq if n not in self.packages]
            if missing:
                raise KeyError("packages not in snapshot: " + ", ".join(sorted(missing)))
            return [self.packages[n] for n in uniq]
        keys = sorted(self.packages)
        if limit is not None:
            keys = keys[:limit]
        return [self.packages[k] for k in keys]

    def provenance_summary(self) -> dict:
        return {
            "kind": "archived-snapshot",
            "raw_root": os.path.abspath(self.root),
            "manifest": self.manifest,
            "input_hashes": self.input_hashes,
            "row_counts": self.row_counts,
            "package_count": len(self.packages),
            "note": "archived 2026-10-29 UDD QA snapshot; NOT a live "
                    "runtime input (active CLIs use live UDD facts)",
        }


def _parse_manifest(path: str) -> dict:
    out = {"path": os.path.abspath(path)}
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip()
    except OSError:
        out["error"] = "unreadable"
    return out


def load_snapshot(raw_root: str) -> Snapshot:
    """加载归档快照的 sources/upstream/upstream_metadata/vcswatch 四张表。

    仅用于历史分析；活跃 CLI 不调用（见模块 docstring）。
    """

    required = ["sources.csv", "upstream.csv", "upstream_metadata.csv", "vcswatch.csv"]
    missing = [f for f in required
               if not os.path.isfile(os.path.join(raw_root, f))]
    if missing:
        raise FileNotFoundError(
            f"snapshot missing tables under {raw_root}: {', '.join(missing)}")

    manifest_path = os.path.join(raw_root, "_manifest.txt")
    input_hashes = {}
    row_counts = {}
    packages: dict[str, PackageFacts] = {}
    invalid_names: list[str] = []

    for table in required:
        path = os.path.join(raw_root, table)
        input_hashes[table] = _sha256_file(path)
        rows = _read_rows(path)
        row_counts[table] = len(rows)
        for idx, row in enumerate(rows, start=1):  # 数据行号（多行字段≠物理行）
            name = (row.get("source") or "").strip()
            if not DEBIAN_SOURCE_NAME.match(name):
                invalid_names.append(f"{table}#row={idx}:{name or '<empty>'!r}")
                continue
            fact = Fact(table=table, row_number=idx, fields=row)
            packages.setdefault(name, PackageFacts(name=name)).rows.append(fact)

    if invalid_names:
        # 明确致命而非静默丢弃：坏名字意味着快照本身可疑
        raise ValueError(
            "snapshot contains source names violating Debian policy "
            f"({'^[a-z0-9][a-z0-9+.-]+$'}); refusing to load: "
            + "; ".join(invalid_names[:5])
            + (f" (+{len(invalid_names) - 5} more)" if len(invalid_names) > 5 else ""))

    return Snapshot(
        root=raw_root,
        manifest=_parse_manifest(manifest_path),
        input_hashes=input_hashes,
        packages=packages,
        row_counts=row_counts,
    )
