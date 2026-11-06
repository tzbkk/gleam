# Debian RM（包移除）流程全景

**验证日期**：2026-10-28 ~ 2026-10-29（removals.txt 全量解析、UDD bugs/package_removal 实测、官方文档）

## 1. 一句话

任何人都能发 RM（FTP team 的 Scott Kitterman 原话："*we're all QA*"）→ FTP team 用 `dak rm` 执行（中位 ~34 天）→ testing 次日自动跟进 → 尸体进 morgue/snapshot 永生。**2026 年 43% 的移除无人类参与**（DAK's auto-decrufter）。

## 2. 发工单

```
收件人: submit@bugs.debian.org
伪包:   ftp.debian.org
主题:   RM: <包名> [<版本>] -- <理由>       ← ftp-master 官方格式
User: ftp.debian.org@packages.debian.org    ← 新 reportbug 自动带（另一套 QA 惯例：packages@qa.debian.org）
Usertags: remove
severity: normal
```

testing/stable 移除 → 改发 `release.debian.org` 伪包 + usertag `rm`。

## 3. 动词词表（官方语义 × 2026 removals.txt 实际用量）

| 缩写 | 含义 | 2026 用量 |
|---|---|---|
| ROM / RoM | Request of Maintainer（维护者请求） | **803** |
| RoQA | Request of QA（任何人以 QA 名义） | **620** |
| ROTeamM | 团队请求 | 137 |
| NBS | Not Built from Source | 42 |
| ANAIS | NBS 自动移除系统署名 | 23 |
| NVIU / NVIT | Newer Version In Unstable/Testing | 自动 |
| RoSRM | Stable 发布经理请求 | 3 |

流行理由句式（活例）：`dead upstream, low popcon, RC-buggy` / `orphaned; depends on db5.3` / `unused; successor available` / `unmaintained, depends on gtk2`

## 4. 处理：谁、多快、怎么被拦

- **执行者**：FTP team（Alteholz / Kitterman / crusoe / bage / jbicha…），命令 `dak rm -D -p -d <bug> -R -C package -m "理由"`（removals.html 公示命令预览）
- **速度**（UDD，2023+ 已关闭 5,196 张）：**中位 33.7 天，IQR 30–45**；史上 18,628 张，现存 open 273
- **拦截**：`-R` rdep 检查——有包依赖就标 `moreinfo` 挂起（python-lockfile 挂 2.5 年）
- **救援路径**（三个实证）：
  - rsnapshot：用户工单喊"别删"→ 有人接养
  - markdown：RM 挂 moreinfo 期间被 Andrea Pappacoda 接养，changelog `Closes: #1072958`，包被 testing 踢出后又 migrate 回来
  - 官方口径（Kitterman）：真删错了 → 重新上传走 NEW 队列

## 5. 自动葬礼（监控重点）

- **dak auto-decrufter**（dinstall 期间跑）：unstable 的 NBS+无rdep、experimental 的 NVIU、t-p-u 的 NVIT。签名 `[ftpmaster: DAK's auto-decrufter]`，**2026 年 3,847 起中占 1,669（43%）**，理由格式 `[auto-cruft] NBS (no longer built by X, no reverse dependencies)`
- **testing autoremoval**（RC bug 驱动，另一套）：UDD `testing_autoremovals` 当前 2,738 行待埋，`first_seen + ~30 天`执行，机读 `udd.debian.org/cgi-bin/autoremovals.yaml.cgi`
- **射程盲区**（Gleam 的机会）："活着但无用"的包（构建健康、零 rdep、零用户，如 libgltf）不在机器人射程内，只能人工 RM

## 6. 移除之后

1. **传播**：unstable 删除 → britney 以 `Package not in unstable` 次日踢出 testing（"Debian testing watch" 自动邮件）
2. **pool 文件**：stay-of-execution 缓冲 → ftp-master 内部 morgue（`morgue/pool/YYYY/MM/DD/`，非公开）→ 物理删除
3. **snapshot.debian.org 永生**：除非法律原因（对伪包 snapshot.debian.org 发工单）
4. **BTS**：`dak rm -D` 自动关包的 bug；WNPP O: 工单一并关（ramond O: #1070874 由 ftpmaster 关闭，实证）；漏网由 `udd.debian.org` `bugs-removed-packages.cgi` 兜底
5. **stable**：原则上不删（SRM / point release / 安全更新路径）
6. 已发布套件冻结副本不动，跟发行版到 EOL 后进 archive.debian.org

## 7. 监控接口（Gleam 集成清单；历史调研）

| 接口 | 内容 | 更新 |
|---|---|---|
| `ftp-master.debian.org/removals.txt` | 全量移除日志（结构化文本；2026 年 4.9MB/3,847 起；条目格式见下） | dinstall |
| `ftp-master.debian.org/rss/removals.rss` | RSS 1.0，标题即"包名: 理由" | 小时级 |
| `ftp-master.debian.org/removals-YYYY.txt` / `.822` | 按年归档 + RFC822 版 | 年度 |
| `ftp-master.debian.org/removals.html` | **pending 视图**（open RM + moreinfo 状态 + dak 命令预览） | dinstall |
| UDD `package_removal_batch` ⋈ `package_removal` | time / ftpmaster / distribution / requestor / reasons / (batch_id, name, version, arch) | 随 UDD |
| UDD `bugs` + `bugs_usertags(tag='remove')` | RM 工单生命周期（arrival/last_modified/done） | 随 UDD |
| `udd.debian.org/cgi-bin/autoremovals.yaml.cgi` | testing 待自动移除（机读） | 日 |

**没有邮件列表广播**：`dak rm` 只发 bug-close 邮件 + 可选 `-C` 抄送。

removals.txt 条目格式（实测样本）：

```
=========================================================================
[Date: Wed, 28 Oct 2026 13:59:46 -0000] [ftpmaster: DAK's auto-decrufter]
Removed the following packages from experimental:
   pkg | version | archs
------------------- Reason -------------------
[auto-cruft] NVIU
----------------------------------------------
Also closing bug(s): NNNNNN
Also closing WNPP bug(s):
```

## 8. 下游动作闭环（历史草图，未实现）

> **编者注（2026-10-31）**：本节是调研期的下游动作草图，非已实现功能，也不代表当前框架（[06](06-framework-overview.md)）。仅"上游沉寂"不支持任何移除建议；Gleam 不自动发起 RM，建议的移除经人工复核后由人执行（Debian 侧的 auto-decrufter 是 Debian 自身机制）。当前决定与未决清单见 [11](11-decisions-and-open-questions.md)。

```
UDD 诈尸 SQL（02 手册 ③）
  → 三条件判死（03 案例 §6）→ "该埋"类 → 提示发 RM（模板见 03 §9，中位 34 天入土）
  → "等诈尸"类 → 继续盯 upstream 信号（01 文档 §7 架构）
  → RM 已发 → watch removals.txt 出现包名 = 葬礼确认
  → RM 被救（接养/撤回）→ 包回归，转入正常监控
```

## 9. 参考

- 格式规范：ftp-master.debian.org/removals.html 页顶
- 流程 howto：wiki.debian.org/ftpmaster_Removals；qa.debian.org/howto-remove.html（RoQA 管道）
- Developers Reference 5.11.2（Removing packages）、5.16.2.2（Removals from testing）
- dak 源码：salsa.debian.org/ftp-team/dak（dak/rm.py、dak/auto_decruft.py、templates/rm.bug-close）
- 讨论：debian-devel 2024-08 "Removing more packages from unstable"（Kitterman 关于 RoQA 流程与删错恢复路径的说明）
