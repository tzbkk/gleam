# 10 Debian 社区侧实践：MIA、UDD 与上游跟踪生态（研究归档）

**归档日期**：2026-10-31
**性质**：研究综合与提案存档。社区侧调查（2026-10-30）为会话式研究（本仓库未存其原始记录），本页整理其结论；文中"六步"指 Debian 侧历史的不活跃贡献者处理提案，不是本项目的对外联络计划。
**更名注记**：项目此前文档中称 spring，现已更名 Gleam / 荧。
**纪律**：本页不收录 MIA 私人案例数据；不重复旧计数与运行时统计（旧数字是时点快照，要用就实时查）。

## 1. MIA 以维护者为调查主体、包级处置为输出

- 官方 Developers Reference §7.4 明确：MIA 调查以维护者为主体，允许多重证据，且明确包括可能的非 Debian 活动；具体处理有人工裁量（转引自早期调研，待刷新核验）。
- Gleam 侧可用的包级痕迹是另一类数据：package actions、WNPP 工单、Uploaders 变动、上传历史。人的证据与包的证据分开记录，不互相冒充。
- Carnivore 聚合 Debian 人的别名与身份线索，是"人身份消歧"的有用类比；但它不是上游注册表，不解决包与上游实体的绑定问题。类比止步于此。
- 私人案例数据（MIA 个案细节、私下通信）不进本项目文档与数据。

## 2. 既有工具与记录

- **mia-query、mia-tracker**：源码公开，但其运营实例与数据访问是受限的；它们不是面向公众的查询/工单门户。
- **per-maintainer mbox**：实际存放在部署的数据库中；公开仓库（salsa `qa/qa` mia 目录）记录的是其格式，不存放私人案例。
- 限度声明：没有主张称这些信道全部 exclusively 属于 Debian，也没有主张对人员覆盖完整。
- **关键限度**：在本页审阅范围内未发现整合的公开上游实体模型；这只界定审阅范围，既不证明"不存在"，也不证明"存在"。这个问题的两种答案都没有证据支持。

## 3. UDD 及周边上游跟踪生态：能力与限度

| 组件 | 它提供 | 限度 |
|---|---|---|
| UDD `upstream` 表（rimporters/upstream.rb） | source + URL + version + 最新检查结果 | 是 uscan 结果的导入，不是身份数据 |
| UDD `upstream_metadata`（DEP-12） | Repository 等 DEP-12 字段 | QA 覆盖率极低（历史快照实测 46 个有值，2026-10-28；时点数字，引用需重新查询） |
| DUCK | URL 存活 + parked/moved 启发式（`config/parked.list`） | 产出是候选状态，不是身份结论 |
| distro-tracker | 消费 UDD 数据做包页展示 | 不是独立确认源；上游数据坏了它跟着坏 |
| vcswatch | 扫描声明的打包仓库 | 仓库可能与上游物理同仓（wreport：打包分支活在上游仓库内），活动信号不核对就不能归因给"上游"或"Debian 侧" |
| Janitor | state.sql 中有 upstream 表 | 该表见于所引历史修订（afc94de18e8a...）；当前运行是否仍在使用未验证 |

Janitor 条目的两点纪律：既不能说"没人做过上游实体图"，也不能说"已有完整图"。历史修订里找到了表结构，只证明曾经设计过，不证明现状。

另两条语义警戒：
- **UDD 缺 MIA 表 ≠ MIA 数据不存在。** 只说明该数据不在 UDD 公开 schema 里。
- **仓库/服务存在 ≠ 可用管道。** net-dns-fingerprint 实证：watch 文件存在且有效，status 却为 NULL；NULL 只证明缺已记录的结果，不能证明"从未处理"。部署和数据可用是两件事。

## 4. "untrackable" 的语义

untrackable 是**跟踪不能**，不是死亡判决。它包括无版本号工件（imapcopy 的 zip 无版本串，uscan 报 untrackable，实际项目定位清晰、冻结于 2009）。注意：uscan 的 "untrackable project" 字面值不得映射进任何最终 UNTRACEABLE 状态；本框架目前没有已采纳的 UNTRACEABLE 状态语义，把字面值 1:1 映射会把定位良好的冻结项目错放进"无痕迹"类桶。

## 5. "六步"所指与边界（澄清存档）

社区调查（2026-10-30）中讨论的"六步"是 DebConf25 上讨论过的对不活跃贡献者的提醒流程：由 DAM / Keyring / FrontDesk 分步提醒直至最终移除的既议流程。它是 Debian 侧的流程讨论，**不是本项目发起的对外联络计划**。截至历史审阅时点，没有已确认的部署运行状态。本页划定的边界：

- 本项目未来的联络动作（debian-qa 设计咨询、MIA 私下信道）见下文联络边界一节，与上述 Debian 侧流程是两回事，不得混同。
- 边界表述：Gleam 不自动发起 RM；任何建议的 RM 都经人工复核后再由人执行（Debian 侧本就存在 auto-decrufter 等自动移除机制，那是 Debian 自身流程，与本框架无关）。与 [02](02-udd-handbook.md)、[04](04-debian-removal-process.md) 的纪律一致：证据先行。
- 不预决状态语义。活动状态机仍开放（见 [06](06-framework-overview.md) 未决事项）。
- 旧文档中的计数与运行时统计（包数、状态分布等）不在此复述；引用时以实时查询为准。

## 6. 联络边界（重要）

- **设计咨询**：走 debian-qa 列表，公开、可归档、可引用。
- **MIA 人案**：仅经 MIA 团队私下信道；个案细节不公开、不入库、不写进文档。
- 截至归档日：与 debian-qa、MIA 均无实际联络记录；本文档描述的是"将要做什么的边界"，不是"已经做了什么"。

## 7. 主要来源 URL

- Developers Reference §7.4（MIA；含非 Debian 活动证据与受限访问说明；转引自早期调研，待刷新核验）: https://www.debian.org/doc/manuals/developers-reference/beyond-pkging.html
- MIA 团队 wiki: https://wiki.debian.org/Teams/MIA
- salsa qa 仓库 mia 目录（per-maintainer mbox）: https://salsa.debian.org/qa/qa/-/tree/master/mia
- debian-project 列表存档（2025-07 帖）: https://lists.debian.org/debian-project/2025/07/msg00006.html
- DEP-12: https://dep-team.pages.debian.net/deps/dep12/
- UDD upstream importer 源码: https://salsa.debian.org/qa/udd/-/blob/master/rimporters/upstream.rb
- debian-qa 列表存档（2025-06 帖）: https://lists.debian.org/debian-qa/2025/06/msg00013.html
- DUCK parked.list: https://salsa.debian.org/debian/duck/-/blob/master/config/parked.list
- distro-tracker: https://salsa.debian.org/qa/distro-tracker
- Janitor state.sql（历史修订 afc94de18e8a473ff7fc6a1c06dd5336410d7a97）: https://salsa.debian.org/jelmer/janitor/blob/afc94de18e8a473ff7fc6a1c06dd5336410d7a97/state.sql
- WNPP: https://www.debian.org/devel/wnpp/

均为转述，无长段引文；帖子内容与代码结构以原始页面为准。

## 8. 与其他文档的关系及研究缺口

- [02-udd-handbook.md](02-udd-handbook.md)：UDD 表结构与已验证 SQL 的实操手册；本页讨论其生态位。
- [04-debian-removal-process.md](04-debian-removal-process.md)：RM 流程的社区侧；本页的联络边界与其"人工发起"纪律同源。
- [09-discovery-probes-and-ai.md](09-discovery-probes-and-ai.md)：证据获取的技术侧；本页是组织与人际侧。

缺口：
- Janitor 当前运行状态需要向维护者或现行部署求证，才能关闭"有没有上游实体图"的问题。
- debian-qa 对本类监控框架的态度未咨询过；提案的可行性评估没有社区输入。
- MIA 数据口径（哪些可用、哪些保密）没有官方清单，接触前只能按上文联络边界的保守边界行事。
