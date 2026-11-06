# UDD 实战手册（Ultimate Debian Database）

**验证日期**：2026-10-27 ~ 2026-10-29（所有连接方式、SQL、数据均本机实测）

## 1. 连接方式

```bash
# 公共只读 SQL 直连（免申请，密码是公开的）
psql "postgresql://udd-mirror:udd-mirror@udd-mirror.debian.net/udd"

# 免认证 JSON（不需要任何客户端）
curl -sSL "https://udd.debian.org/cgi-bin/upstream-status.json.cgi"   # ~12.7MB 快照

# 建议别名
alias udd="psql postgresql://udd-mirror:udd-mirror@udd-mirror.debian.net/udd"
```

坑：连接串用 `udd-mirror:udd-mirror`（不是 `udd:udd`，那会被 pg_hba 拒）。host 是 `udd-mirror.debian.net`；主库 `udd.debian.org` 不对外开放 SQL。

## 2. psql 速查

| 操作 | 命令 |
|---|---|
| 列表（带过滤） | `\dt up*` |
| 表结构 | `\d upstream` |
| 竖排宽表 | `\x` |
| 计时 | `\timing on` |
| 执行 SQL 文件 | `\i f.sql` |
| 退出 | `\q` |
| 导 CSV | `psql --csv -c "..." > f.csv` |
| 纯数据流（接管道） | `psql -At -c "..."` |

**大坑**：`GROUP BY 1,2,3` / `ORDER BY 4` 列号引用在 SELECT 列表变化后必碎（`not in select list`）——脚本里永远用真实列名。

## 3. Gleam 核心表（schema 实测确认，spring 时期采集）

| 表 | 关键列 | 用途 |
|---|---|---|
| `sources` | source, version, **maintainer**, release, homepage, **bin**, build_depends(_indep/_arch), vcs_url | 包元数据；QA 包清单 |
| `upstream` | source, **status**, upstream_version, upstream_url, watch_file, errors, last_check | **uscan 结果，30 分钟刷新**。status 值：`up to date` / `newer package available` / `error` / NULL / `only older package available` / `untrackable project` |
| `upload_history` | source, version, date, changed_by | 沉寂判断（最后一次打包时间） |
| `wnpp` | id, type(O/RFA/RFH/ITA/ITP/RFP), source, title | 孤儿化工单 |
| `upstream_metadata` | source, key, value | DEP-12（Repository 等），QA 包覆盖率极低 |
| `popcom_src` | **source**, insts, vote, olde, recent, nofiles | **按源码包聚合的 popcon**（选它！） |
| `popcon` | package(二进制), insts, vote, recent... | 二进制粒度 |
| `packages` | package, source, depends, release... | 二进制→源码映射、运行时依赖 |
| `vcswatch` | source, status, last_scan | Debian 侧 salsa 仓库活动 |
| `duck_url_status` | url, status_code, is_ok | 上游 URL 存活 |
| `bugs` / `archived_bugs` / `bugs_usertags` | id, arrival, last_modified, done, title | RM 工单生命周期 |
| `package_removal_batch` + `package_removal` | time, ftpmaster, distribution, requestor, reasons | 移除记录 |

## 4. 诈尸 SQL（已验证跑通）

```sql
-- ① QA 孤儿包清单（实测 ~1,075）
SELECT count(*) FROM sources
WHERE maintainer = 'Debian QA Group <packages@qa.debian.org>'
  AND release = 'sid';

-- ② 上游 watch 状态分布（实测 2026-10-28：520/210/187/120/38/7）
SELECT u.status, count(*)
FROM upstream u
JOIN sources s ON s.source = u.source AND s.release = 'sid'
WHERE s.maintainer = 'Debian QA Group <packages@qa.debian.org>'
GROUP BY u.status ORDER BY 2 DESC;

-- ③ 核心：诈尸名单（QA + 上游有新版 + 3 年没打包）
SELECT u.source, s.version AS debian_ver, u.upstream_version,
       max(uh.date)::date AS last_upload, p.recent
FROM upstream u
JOIN sources s ON s.source = u.source AND s.release = 'sid'
JOIN upload_history uh ON uh.source = u.source
JOIN popcon_src p ON p.source = u.source
WHERE s.maintainer = 'Debian QA Group <packages@qa.debian.org>'
  AND u.status = 'newer package available'
GROUP BY u.source, s.version, u.upstream_version, p.recent
HAVING max(uh.date) < now() - interval '3 years'
ORDER BY max(uh.date);
-- 实测结果（2026-10-28）：libgltf(2018), aspell-ga, ooo-thumbnailer,
-- herculesstudio, xtermcontrol 3.8→3.11, kytos-utils, dbtoepub, guilt, jtb

-- ④ 谁还在 build-depends / depends 某包（判"该埋"用）
SELECT source FROM sources WHERE release='sid'
  AND (build_depends ILIKE '%libgltf%' OR build_depends_indep ILIKE '%libgltf%'
    OR build_depends_arch ILIKE '%libgltf%');
SELECT package, depends FROM packages
  WHERE release='sid' AND depends ILIKE '%libgltf%';
```

**popcon 三大坑**（实测踩过）：
1. `sources.bin` 是逗号分隔文本（`'libots-dev, libots0, ots'`），**不能当外键 JOIN popcon**——多二进制包会被 INNER JOIN 无声吞掉（libgltf 就这么消失过）。用 `popcon_src` 直接按 source 关联
2. 允许缺失的场景用 `LEFT JOIN`（冷门包可能不在 popcon_src 里）
3. `packages` 表每架构一行，同名重复是正常的

## 5. 上游仓库分布（实测）

QA 孤儿包的上游住在哪（决定 commit 级监控用什么工具）：

```
watch 指向 github.com：281 个
DEP-12 Repository 元数据：仅 46 个（github 42 / gitlab 3 / 其他 1）
```

→ commit 级监控需从 watch_file / Homepage 反推仓库 URL，不能指望元数据。

## 6. popcon 时间序列接口

```
https://qa.debian.org/cgi-bin/popcon-data?packages=A+B;from_date=YYYY-MM-DD;to_date=YYYY-MM-DD
```

返回 `{包名: {日期: {vote, old, recent, no_files}}}`，免认证。`insts = 四项之和`。
判断"诈尸后接养价值"时，一条多年曲线远比 popcon_src 快照有说服力（libgltf 案例：峰值 11,717 台装机 / 3 台真实使用）。

## 7. "该埋 vs 等诈尸"分类规则（libgltf 案例总结，历史启发式）

> **编者注（2026-10-31）**：以下三条件是调研期启发式，未被采纳为 Gleam 的自动移除规则；Gleam 不自动发起 RM，建议的移除经人工复核后由人执行（Debian 侧另有其自身的自动移除机制；见 [04](04-debian-removal-process.md)、[11](11-decisions-and-open-questions.md)）。

三条件全中 → 该埋（发 RM，见 04 文档）：
1. 零 build-rdep（④ 第一条查询空）
2. 零运行时 rdep（④ 第二条只有自引用）
3. 上游官方判死（作者弃坑声明 / 仓库钉死 / 唯一下游已弃用）

否则 → 等诈尸（进监控名单）。

## 8. 练习（ CLI 教学遗留，附答案口径）

1. 沉寂阈值 3→5 年：只改 `interval '5 years'`
2. watch 指向 GitHub 的孤儿包：`upstream.watch_file ILIKE '%github.com%'`（≈281）
3. 按年统计孤儿包最后打包年份：`date_part('year', max(uh.date))` 再 GROUP BY
4. WNPP 各类型计数：`SELECT type, count(*) FROM wnpp GROUP BY 1`
