# Gleam / 荧 研究总览

**项目**：Gleam / 荧（前名 spring）。Debian QA 包上游身份与活动追踪；CLI 与仓库命名空间未定（见 [11](11-decisions-and-open-questions.md)）。
**时间口径**：数据快照 2026-10-29T06:44:28Z；81 包压测 2026-10-29；社区调查 2026-10-30；本总览更新 2026-11-01。

## 现状（截至 2026-11-01）

- **最小可运行实现已落地（2026-11-01）**：`gleam/` Python 包提供 CLI 工作流（快照加载、有界只读 HTTP 观测、追加式证据与基线比较、可选本地模型分诊），用法与边界见仓库根 [README.md](../../README.md)。此为最小基线，不改变本页其余"未定稿"结论，也不标志任何提案被接受。
- 数据快照是特定时刻的抓取，不是全部上游的普查；范围是快照时点 Maintainer=QA Group 的包集，不是"所有孤儿项目"。
- 文中候选计数（31 native / 59 FakeWatch / 50 git-like / 7 multi-stanza 等）出自先前 agent 一次性统计，**未重算**，需可复现重算后才可引用（见 [07](07-package-identity-taxonomy.md)）。
- 新方向记录（2026-10-31）：全档案上游发现与身份解析方向，方法记录见 [12](12-upstream-identity-resolution.md)；QA 监控仍是主线优先级，现有快照的范围与口径不变，方法均未选型、未测试。
- 相似度方向（2026-10-31）：不采用嵌入模型做相似度判断；非模型比较器仍在考察、待实测、未选型，见 [13](13-similarity-comparators.md)。
- 相似度与信号实测（2026-10-31）：名称比较器基准与信号定位器的解释含修正，无规则被采纳，见 [14](14-similarity-and-signal-tests.md)。
- Jev 决策模型调研（2026-11-01）：用小模型（jev-1.14.0）做 QA 分诊的提案记录为 provisional 研究，未采纳、未评估，见 [15](15-jev-decision-model.md)。
- 调查栈选型与工具调研（2026-11-02）：调查栈 = 自托管 SearXNG + Gleam 安全 Fetcher + trafilatura + GitHub API（选型已接受，不构成一般基准证明）；服务商比较为定性、未做基准，见 [16](16-search-fetch-agent-tools.md)。

## 问题定义

Debian sid 中由 QA Group 维护的源码包是本项目的候选总体：2026-10-29 记录的快照含 1,078 个 distinct source（先前计数）。目的：检测每个包的身份与上游活动状态；不预设"多数上游已死"，死活由证据判定。早期观察到的形态之一是"沉寂多年后突然有新版本/新 commit"（即"诈尸"）。需求：任何生命迹象的持续监控与推送。

注意：先前"专用诈尸检测器不存在（全行业空白）"的表述已收窄为**有界结论**：在已审阅的公开工具范围内未找到完整直接适配方案；边界与横向模型见 [08](08-related-tools-and-models.md) 与 [10](10-debian-community-practices.md)。

## 阅读路径

[06 框架概览](06-framework-overview.md)（已接受）→ [07 身份分类](07-package-identity-taxonomy.md) → [08 模型参考](08-related-tools-and-models.md) / [09 探针与 AI](09-discovery-probes-and-ai.md) / [10 社区实践](10-debian-community-practices.md) → [11 决定与未决](11-decisions-and-open-questions.md)。历史背景：[01](01-upstream-activity-tools.md) ~ [05](05-survey-findings.md)。

## 文档索引

| 文件 | 内容 | 一句话 |
|---|---|---|
| [01](01-upstream-activity-tools.md) | 活动工具调研（历史） | 早期工具全景；末节为历史草案 |
| [02](02-udd-handbook.md) | UDD 实战手册 | 连接、表结构、已验证 SQL（含历史启发式一节） |
| [03](03-libgltf-case-study.md) | libgltf 案例 | 一个包 12 年全程解剖 |
| [04](04-debian-removal-process.md) | RM 流程 | 埋葬流程与监控接口；末节为历史草图 |
| [05](05-survey-findings.md) | 81 包压测 | 状态覆盖对账与特例（历史解读） |
| [06](06-framework-overview.md) | 框架概览 | 已接受的证据中心实体框架 |
| [07](07-package-identity-taxonomy.md) | 包身份分类 | 多标签轴、案例落轴、未重算计数 |
| [08](08-related-tools-and-models.md) | 工具与模型 | 有界结论、schema 支持与关键修正 |
| [09](09-discovery-probes-and-ai.md) | 探针与 AI | 管道、安全边界、AI 分诊提案 |
| [10](10-debian-community-practices.md) | 社区实践 | MIA、UDD 生态、联络边界 |
| [11](11-decisions-and-open-questions.md) | 决定与未决 | 决策记录与防复发清单 |
| [12](12-upstream-identity-resolution.md) | 身份解析方法记录 | 候选方法与证据循环；未选型未实现 |
| [13](13-similarity-comparators.md) | 相似度比较器归档 | 不用嵌入模型的决定与比较器候选；未选型，实测进行中 |
| [14](14-similarity-and-signal-tests.md) | 相似度与信号实测 | 两轮实测数字与修正解释；全部 provisional，无规则采纳 |
| [15](15-jev-decision-model.md) | Jev 决策模型调研 | 小模型分诊的事实、预印本与成本情景；provisional，未采纳未付费 |
| [16](16-search-fetch-agent-tools.md) | 搜索/抓取/代理工具调研 | 选型记录（SearXNG+Fetcher+trafilatura+GitHub API）、实测发现与定性比较；wizznic/librcd 案例状态 |

## 关键数字（全部为带日期的历史观察，引用需实时复核）

- QA 孤儿包：~1,075（2026-10-28 口径）；2026-10-29 快照记 1,078 个 distinct source，另一按清单行数的口径为 1,082。这几组数字来自不同查询与计数单位，不是互相矛盾，引用时注明口径。
- watch 状态分布（2026-10-28）：up to date 519 / 已有新版 119 / error 185 / 无数据 210 / older 38 / untrackable 7。
- watch 信道覆盖缺口（同上分布）：(185+210+7)/1,078 ≈ 37%（分母取状态分布合计；[01](01-upstream-activity-tools.md) 历史文中作 /1,074，系当时另一口径，结果同为 ~37%）；这是 watch 覆盖比，不是"37% 项目失明"。
- 诈尸候选（历史 SQL 候选名单：QA + 上游有新版 + 3 年没打包）：9 个（2026-10-28 查询，名单见 [02](02-udd-handbook.md)）；是 SQL 候选，不是已确认的复活。
- 上游仓库分布（2026-10-28）：watch 指向 GitHub 281 个；DEP-12 Repository 有值 46 个（时点值，见 [10](10-debian-community-practices.md)）。
- 移除统计（截至 2026-10 快照的历史数据，非 2026 全年数字）：3,847 起中 43% 由 auto-decrufter 执行；人工 RM 中位 ~34 天（见 [04](04-debian-removal-process.md)）。
- 81 包压测（2026-10-29）：各 uscan 字面值均有失真实例；覆盖 = 位置×信道×解析×管道（见 [05](05-survey-findings.md)；个体判定为先前 agent 临时评估，未独立复核）。

## 方法论说明

- 集成性/存在性判断以 [08](08-related-tools-and-models.md)、[10](10-debian-community-practices.md) 的有界结论为准；[01](01-upstream-activity-tools.md) 的历史表述仅作背景。
- 未独立复核的事实按"先前 agent 报告"引用，不作定论（见 [09](09-discovery-probes-and-ai.md)）。
- 时间敏感数据一律以实时查询为准；文档数字只是历史快照。
