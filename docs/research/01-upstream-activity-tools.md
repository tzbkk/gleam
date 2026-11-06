# 上游活动追踪工具调研全景

**调研日期**：2026-10-27 ~ 2026-10-29
**问题**：追踪 Debian 孤儿包（QA 维护）上游作者活动状态，尤其是"死了很久突然诈尸"

> **编者注（2026-10-31）**：本文件是历史调研存档，不是当前运行保证。§1"专用工具不存在"是有界审阅结果，边界与修正见 [08](08-related-tools-and-models.md)、[10](10-debian-community-practices.md)，对集成方式的断言以这两份为准；§6 的"≥2 个信号才触发"等固定阈值是调研期提案，未被接受为政策；§7 为历史草案，保留原样。项目现名 Gleam / 荧。

## 1. 结论：专用工具不存在

没有名为或功能为"zombie upstream detector / orphaned package revival watcher"的现成工具。证据收敛：

- **Debian DPL Andreas Tille 亲自承认**（2025-05，Phoronix）："我们识别实际上无人维护的包的工具相当弱"
- unix.SE 同款问题（#549318 "list of packages with dead parent project"）长期无人能答，最佳答案："UDD 的 JSON 已经是自动化极限"
- Fedora 2024-02 讨论 "Watching the health of upstream projects" 至今没选出工具；ELN issue #246 还在求基础孤儿告警
- GitHub/salsa 代码与仓库名搜索（zombie/dead upstream/orphan watcher/necromancer/revival 等）零命中
- 学术圈研究诈尸（见 §6）但没有产出可部署工具

**推论：必须自组，但积木齐全。**

## 2. 先回答：uscan 不是已经知道新版本了吗？

uscan 数据确实全库采集（UDD `upstream` 表，每 30 分钟刷新），但针对 QA 孤儿包有三个致命缺陷（实测数据，2026-10-28）：

1. **~37% 完全看不见**：1,074 个 QA 包中，watch 报错 185 + 无任何 watch 数据 210 + untrackable 7。孤儿包的 watch 文件经常指向已搬家/死掉的站点
2. **只看发版**：commit 级复活（诈尸的前奏）完全不可见
3. **"知道"≠"通知"**：数据静静躺在 UDD/tracker 网页里，没有任何东西对"沉寂数年→突然有新版"做主动告警

## 3. Debian 官方设施（全部验证）

| 设施 | 上游相关能力 | 关键细节 |
|---|---|---|
| **tracker.debian.org** | 每包页面 "A new upstream version is available" 面板（来自 uscan）+ 孤儿状态 | **RSS 可靠**（`/pkg/<pkg>/rss` 实测携带上游新版本条目）；**邮件订阅不可靠**（官方文档明说新版本通知"尚未实现"）。订阅：`control@tracker.debian.org` 发 `subscribe <pkg>` |
| **UDD** | `upstream` 表（status/upstream_version/last_check，30 分钟刷新） | SQL 直连见 02 手册。DMD：`udd.debian.org/dmd/?packages@qa.debian.org` |
| **upstream-status JSON** | `https://udd.debian.org/cgi-bin/upstream-status.json.cgi` | 免认证，39,530 条，~12.7MB，快照式（自己做 diff） |
| **qa.debian.org/cgi-bin/watch?pkg=X** | 单包 uscan 按需跑 | |
| WNPP | O:/RFA:/RFH: 工单（UDD `wnpp` 表：O 1,094 / RFA 98 / RFH 48） | wnpp.debian.net 可按 "Dust"（无动静天数）排序，社区维护（hartwork），活跃 |
| how-can-i-help | 本机装的包的接养机会 | `--json` 输出、cron 邮件摘要 |
| vcswatch | **只看 Debian 侧 salsa 打包仓**，从不看上游 | COMMITS 状态可作 Debian 侧活动信号 |
| duck | 上游 URL 存活检查（Homepage/copyright 等） | 死站检测，非复活检测；结果在 UDD `duck_urls`/`duck_url_status` |
| MIA team | 只追踪 Debian 贡献者（人），不追踪上游作者 | 人案处理的进一步边界见 [10](10-debian-community-practices.md) |
| DEHS | **2011 年已死**，UDD upstream importer 接班 | |

## 4. 第三方服务

| 服务 | 能力 | 诈尸相关用法 |
|---|---|---|
| **Anitya / release-monitoring.org** | 297,774 项目、32 种 backend（github/gitlab/cgit/sourceforge/gnu/任意 regex 页面）；`/api/v2/packages/?distribution=Debian` 有 **752 个 Debian 映射**（实测）；`/api/v2/versions/?project_id=N` 返回每版本 `created_on` | **唯一的免费 push 通道**：匿名 MQTT `mqtt.fedoraproject.org` 订阅主题 `anitya.project.version.update.v2`，消息体自带 `packages[]`，过滤 `distribution=="Debian"`。缺的项目可 `POST /api/v2/projects/` 注册。注意 created_on 是 Anitya 首见时间 |
| **Repology** | 跨 ~270 仓库家族的版本对比；API 无任何日期字段 | 诈尸旁证：`/api/v1/projects/?inrepo=debian_stable&outdated=1&families_newest=25` = "别的发行版都有新版就 Debian 没有"。限 1 rps，批量用 dumps.repology.org |
| **Software Heritage** | `/api/1/origin/<url>/visits/`（**URL 传裸的，别 percent-encode**）返回 {date, status, snapshot} 序列 | **快照 ID diff = 复活检测**：同 snapshot=沉寂，变了=有动静；`status:"not_found"` 还能发现上游消失。平均数天~两周重爬；`POST /api/1/origin/save/...` 可主动触发 |
| **newreleases.io**（托管） | GitHub/GitLab/Codeberg/Gitea/Bitbucket/**GNU Savannah** + 17 registries 的 releases **和 tags**；邮件/Slack/Telegram/Discord/Matrix/webhook；instant~weekly | 盯 tag-only 复活（老仓库常只打 tag 不做 release） |
| libraries.io | 活着但数据集冻结（2024-09 起不再发数据集）、仅 registry | 不作主干 |
| isitmaintained.com | 壳活着，2019 年起无人维护 | **不用** |

## 5. 自托管/CLI 工具（liveness 全部 2026-10 实测）

| 工具 | 信号 | 通道 | 状态 |
|---|---|---|---|
| **Argus**（release-argus/Argus） | GH/GL releases+tags + 任意 HTML 抓取 + Docker Hub | Gotify/Slack/Discord/ntfy/Telegram/Pushover/SMTP/webhook（shoutrrr） | 活（2026-10-27 有 push） |
| iamspido/github-release-monitor | GH/GL/Codeberg/Forgejo releases | 邮件 + Apprise 70+ | 活 |
| changedetection.io（35k★） | **任意 URL diff**（盯 /tags 页面） | Apprise 100+ | 活 |
| nvchecker | github/gitlab/gitea/cgit/regex 等插件式源 | 无内建通知，自己接 | 活 |
| Apprise / ntfy | 通知管道胶水 | 100+ 服务 | 活 |
| JanisV/release-bot | GitHub releases | Telegram（公开 bot @janisreleasebot） | 活 |

**原生 feed（免部署）**：
- GitHub：`/{o}/{r}/releases.atom` + **`/tags.atom`**（tag-only 复活必用）+ `/commits/{branch}.atom`
- Gitea/Forgejo：`/{o}/{r}.atom` **只含 commits**（releases 要走 API/newreleases）
- cgit：`/atom/?h=<branch>`；SourceForge：`/projects/NAME/rss?path=/`
- 陷阱：rss-bridge 的 GithubReleaseBridge **不含 tag-only 条目**

**GitHub 批量**：夜间批查 `pushed_at`（认证 5k req/h，1,075 个包绰绰有余）。GH Archive/BigQuery：只 select `repo.name/type/created_at` 三列极便宜；**2025 年中后 ReleaseEvent 数据退化，只能信 PushEvent**；ClickHouse github-explorer 有现成 "worst stagnation" 查询族。

**别用（已死/跑偏）**：gitwatch（盯本地文件）、freshcode.club（活但 flaky）、DEHS、CZ-NIC/release-bot（消失）、justwatchcom/github-releases-notifier（2023 停更）、dupgit/versions（2024 停更）。

## 6. 学术参考（防误报规则来源）

- **《Beyond the Grave: An Empirical Study of Dormancy and Revival in Scientific OSS》**（arXiv 2606.20966，2026）：2,984 个 dormant-revived 候选的实证研究。关键结论：**11.5% 的"复活"是 bot 或单次 commit 假阳性**；持续恢复与"沉寂期短 + 原贡献者回归"正相关。→ 告警规则：**≥2 个信号（新 tag + 非 bot commit）才触发，单 commit 不触发**
- 弃包预测数据集（arXiv 2507.21678）：115,466 repo / 57,733 确认弃养，时间线特征
- Avelino et al. 2019（ESEM'19）：**41% 被弃热门项目最终被新核心开发者复活**——弃养≠死亡，监控有理

## 7. 早期架构草案（历史记录）

本节是在 81 包实体鉴定压测之前形成的活动监控草案。它将 UDD/watch 状态转移置于核心；后续压测揭示身份与覆盖问题，因此**不再代表当前接受的框架概览**。当前框架方向见 [06-framework-overview.md](06-framework-overview.md)，压测证据与未决状态机问题见 [05-survey-findings.md](05-survey-findings.md)。

```
核心层：UDD 状态机（覆盖 63% 有 watch 的包）
  每天 diff upstream-status（SQL 或 JSON）
  告警 = 状态转移：error/null→newer available，up-to-date→newer
  过滤 = 沉寂条件（upload_history 3 年+）+ 防误报规则
  推送 = Apprise → ntfy/Telegram/邮件

推送层：Anitya MQTT（免费 push，过滤 distribution==Debian）

补盲层（37% watch 失效区）：
  GitHub 子集（281 个）：夜间批查 pushed_at
  非 GitHub：SWH visits 快照 diff + 原生 tags.atom

旁证层：Repology families_newest + tracker RSS（重点包）+ popcon 曲线

下游动作（见 04）：判"该埋"→ 提示发 RM；判"等诈尸"→ 继续盯
```

用户需求映射：全部 QA 包（UDD 一次覆盖）/ 持续监控+推送（Apprise 管道）/ 任何生命迹象（发版+commit+URL 复活三路信号）。
