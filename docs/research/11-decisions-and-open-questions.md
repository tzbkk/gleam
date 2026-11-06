# 11 决定与未决问题记录

**归档日期**：2026-10-31
**性质**：决策记录。区分：已接受决定 / 命名边界 / 历史名称 / 已修正错误 / 提案 / 未决。本页不定稿活动状态、TTL、阈值、schema 枚举或数据库选型。
**更名注记**：项目前名 spring，现名 Gleam / 荧（用户 2026-10-31 选定）。历史文档保留旧名；survey/、03、raw/ 原样不动。

## 1. 已接受的决定

| 决定 | 内容 | 出处 |
|---|---|---|
| 命名 | 项目名取 gleam / 荧，显示写作"Gleam / 荧"；2026-10-31 用户选定 | 用户指令 |
| 身份根 | 以证据为中心；区分 source package / entity / endpoint / channel / observation / claim | [06](06-framework-overview.md) |
| 包与实体 | 多对多（M:N）；URL 不作实体主键 | 06 §2 |
| 端点与信道分离 | 信道是观测维度（release / tag / commit / issue），不是 URL 或主机 | 06 §1、[09](09-discovery-probes-and-ai.md) §1 |
| 证据层纪律 | 观测追加不覆盖；主张版本化、可撤回（带决策记录）；评估规则可替换、可重算 | 06 §2 L3 |
| AI 与搜索的边界 | 其输出只是假设，不是最终事实；须引用证据并过审 | 09 §5 |
| 接受范围 | 仅 06 的框架方向被接受；schema 枚举与状态语义未冻结 | 06 §6、本页 §6 |
| 相似度不用嵌入模型 | 相似度判断不采用嵌入模型（potion / MiniLM / BGE / cross-encoder 等）；非模型比较器仍在考察，未选型 | 用户指令（2026-10-31）；出处 [13](13-similarity-comparators.md) |
| 调查栈选型（窄范围） | investigate 调查栈采用**自托管 SearXNG + Gleam 安全 Fetcher + trafilatura（仅已抓回字节）+ GitHub API 工具**（全部无付费 API、无嵌入组件）。范围限定：用户选型接受，**不构成一般性基准证明**；更广评测（多包、与 Exa/Brave/Tavily 同题对比）仍未做，见 [16](16-search-fetch-agent-tools.md) | 用户指令（2026-11-02 记录）；出处 [16](16-search-fetch-agent-tools.md) |
| BTS 讨论来源三选 | `bug_fetch` 来源 `auto\|soap\|mbox`，**默认 auto**：先文档化机器接口 SOAP；仅资格错误（传输/解析/挑战类）且 robots 明确允许时回退 mbox；rate-limited / not-found / 预算耗尽不回退；robots 未知/拒绝不回退且无绕过开关。显式选择失败即失败（原 BTS 报告的"默认 source=soap"由此改为 auto，语义披露见 BTS 报告与新 UDD 报告 §8） | 用户指令（2026-11-02 记录）；出处 [experiments/2026-11-02-bts-fetch](experiments/2026-11-02-bts-fetch/REPORT.md)、[experiments/2026-11-02-udd-fetch](experiments/2026-11-02-udd-fetch/REPORT.md) |
| UDD live 查询路径 | `fetch_udd`（工具与 `fetch-udd` CLI）对**文档化公共只读镜像** udd-mirror.debian.net 做 psql 子进程有界只读固定模板查询。解释性决定（与 BTS SOAP 同型）：公共只读 SQL 镜像是 Debian 文档指给程序化访问的数据服务，robots.txt 爬虫排除协议不适用；DSN 为应用侧常量（模型不可控）；无 shell、无任意 SQL；bugs 组以 BTS source 映射/bugs_packages/packages 多路关联覆盖源包+二进制包并保留逐行关联出处 | 用户指令（2026-11-02 记录）；出处 [02](02-udd-handbook.md)、[experiments/2026-11-02-udd-fetch](experiments/2026-11-02-udd-fetch/REPORT.md) |

## 2. 命名的边界与事实

- 无自造缩写；"荧光/微光"之类的比喻是可选的联想，不作为命名理由记录在案。
- 事实记录：https://gleam.run/ 于记录日 2026-10-31 成功抓取核对，是一门已有的编程语言（Beam 生态，gleam CLI）；https://github.com/chrislusf/gleam 是另一同名项目，出自先前 agent 调研，本仓库未重新抓取。采用此名**不构成**命名空间或商标清查。
- 显示名已采用；发布的 CLI / 包命名空间仍需另行核查。不从任何 404 或页面缺失推断"Debian 内名称可用"；与既有 Debian 包的重名核查未做。
- Bug #1103795 是先前调研中关于打包意图的线索，仅此而已；若未来真正发布前需要，再复查。
- 目录 `/home/hxue/Projects/spring`、脚本与数据路径不变；不改文件系统命名，无仓库命名空间决定。

## 3. 历史名称（仅作历史）

spring（调研期用名）；Freshet（过程中出现过的候选名）。两者不再使用，仅在此存档，不维护更长的名单。

## 4. 已修正的错误与矛盾（防复发清单）

| 先前错误说法 | 修正口径 | 落点 |
|---|---|---|
| native = 无上游 | native 是打包格式，非身份结论，非豁免，不创设新终态 | [07](07-package-identity-taxonomy.md) 轴 A |
| HTTP 200 = 上游活跃 | 200 只是传输成功；活动须经解释与归属核对 | [09](09-discovery-probes-and-ai.md) §6 |
| 内容 hash 相同 = fork/镜像关系成立 | hash 只证内容同一；方向与控制需独立证据 | [08](08-related-tools-and-models.md) §3 |
| 搜索命中 = 身份确认 | 命中是候选；身份须独立证据 | 06 §3、09 |
| MIA 源码公开 = 个案数据可访问 | 源码公开，运营数据受限；私人案例不引用 | [10](10-debian-community-practices.md) §2 |
| Wikibase rank = 可信度概率 | rank 是偏好排序；支持合并与重定向 | 08 §3 |
| 31/59/50/7 等计数可直接引用 | 未重算候选；需可复现重算 | 07 §4 |
| QA 快照可证"包未被使用" | 该数据集无全档案 rdeps，推论不成立 | 07 §5 |
| "0 例真回滚"普遍成立 | 与 bwbar 工件被删实例冲突，不作普遍结论 | [05](05-survey-findings.md) 编者注 |
| NULL = 管道从未运行 | 只证明缺已记录结果 | 09 §6、10 §3 |
| "六步" = 本项目联络计划 | 是 DebConf25 议过的 DAM/Keyring/FrontDesk 提醒流程 | 10 §5 |
| Anitya 映射只是版本层、纯启发 | ProjectMapping 是发行版包 ↔ project 映射，逐条质量不一 | 08 §2 |
| CycloneDX pedigree = 主张修订日志 | 是 BOM 谱系字段；历史须分别保留 BOM 版本 | 08 §2 |
| Built-Using = vendoring 证明 | 范围窄于全部构建依赖；仅候选证据 | 08 §3 |
| Backstage 等模型不能表达成员/归属 | 官方关系模型含 memberOf/hasMember、partOf/hasPart、ownedBy；Wikibase 属性可扩展 | 08 §2、§4 |
| 先前 agent 的任何"最新事实" | 临时评估，无集中请求日志；引用须保留 provisional | 07、09 |
| watch 与 uscan 结果一致 = 独立佐证 | uscan 执行 watch，同一流水线两步；独立信道另证 | [14](14-similarity-and-signal-tests.md) §2 |
| 定位器覆盖率 = 有效上游覆盖 | 规则生成标识，含许可证页/目录；有效性未证 | 14 §2 |
| URL 不一致 = 上游迁移 | 分工/镜像/陈旧/解析错误皆可；原因待判 | 14 §2 |

## 5. 提案（未接受，待讨论）

- 五层发现管道与 outcome 示意词表（09 §1、§6）。
- AI 消融实验设计与 gold set 流程（09 §5）。
- 字段 → 候选 → 限度映射（07 §5）。
- 与 debian-qa 的设计咨询、MIA 私下信道边界（10 §6）。
- 上游身份解析方法组合：概率匹配 + 冲突约束 + 增量修正（多轮阻断、Fellegi-Sunter 打分、冲突否决合并、增量 merge/split/move、Dedupe 主动学习）。仅记录；组合未选定、未验证、未写实现，详见 [12](12-upstream-identity-resolution.md)。
- 相似度比较器组合候选与分级模式（rapidfuzz 各算法、Splink 分级阈值、dedupe blocking 谓词等，见 [13](13-similarity-comparators.md) §3；实测结果已归档于 [14](14-similarity-and-signal-tests.md)，未选型）。
- 信号组合需以带类型关系判定为单位重新设计（R1 / R2 / R3 均不采纳为规则，见 [14](14-similarity-and-signal-tests.md) §4）。
- Jev 决策模型分诊：语义页面 diff、迁移公告、候选排序、升级 GLM；脚本处理精确字段变更 / HTTP / 新 commit；仅记录为 provisional，未采纳、未付费推理、未评估，见 [15](15-jev-decision-model.md)。

## 6. 未决（开放问题）

- 活动状态机：ACTIVE / QUIET / REVIVED 定义、TTL、信道优先级、震级（05 §7、06 §6）。
- schema：关系 subject/object 精确形态、置信量表、来源信任层级。
- 存储选型（06 §5 只约束起步边界）。
- 命名空间：CLI 名与仓库名（目录名不变）。
- native 包是否进入独立的 Debian 内部监控（05 §7 Q2）。

## 7. 研究缺口与下一步（无自动 RM 承诺，无全量实现承诺）

- 在 raw/ 快照上重算候选计数，留下可复现脚本。
- native 候选逐包归类。
- 复核 81 包压测结论，建立 gold set。
- debian-qa 设计咨询（未发生；边界见 10 §6）。
- 身份解析标注集尚不存在：需经独立复核的正例对、困难反例、未决/多上游案例；81 包压测只是临时种子，与候选方法的比较未做（[12](12-upstream-identity-resolution.md)）。
- 比较器在真实包数据上的区分力实测（已完成并归档，见 [14](14-similarity-and-signal-tests.md)；无数值阈值被采纳）。
- 关系类型判定方法（同一 / 分叉 / 插件 / 家族 / 巧合）未建立；独立信道判定标准未定（[14](14-similarity-and-signal-tests.md) §5）。
- Jev 分诊的 heldout 验证未执行：需重要变更 / 噪声 / 证据不足三类用例，计假阴 / 假阳 / 升级率 / 成本，抽样审计被拒负例，先锁 jev-1.14 再谈阈值（[15](15-jev-decision-model.md) §6）。
- 调查栈的更广评测未执行（2026-11-02）：当前选型（SearXNG+Fetcher+trafilatura+GitHub API）只有两次单包冒烟实测；多包、多形态上游、与托管搜索服务商的同题对比基准均未运行；wizznic 案例是诊断不是 heldout；librcd 候选 primary vs 镜像未决（[16](16-search-fetch-agent-tools.md) §5、§7）。

关联：[07](07-package-identity-taxonomy.md)、[08](08-related-tools-and-models.md)、[09](09-discovery-probes-and-ai.md)、[10](10-debian-community-practices.md)；原始材料 [survey/2026-10-29/](survey/2026-10-29/) 与 `raw/`。
