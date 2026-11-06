# Gleam / 荧 框架概览：以证据为中心的上游身份知识库

**状态**：框架方向已接受；活动状态、TTL、阈值与告警语义仍待商议。
**依据**：[81 包鉴定压测](05-survey-findings.md)、[工具与数据模型调查](01-upstream-activity-tools.md)及本地 UDD 快照。

## 1. 定位

Gleam（荧）是一套以 Debian source package 为入口、以持久上游实体及其关系为核心、以可复核证据为基础的监控框架。它不是一棵互斥分类树，也不把 uscan 的状态值当作事实。

核心数据应保存“观察到了什么”和“哪些主张由什么证据支持”；上游活动解释、状态、TTL 与通知均由可替换、可版本化的评估规则派生。更改评估规则时，应能基于已有证据重新计算，而无需改写历史观测或身份记录。

```text
Debian source package
        ↓
包身份分类与实体绑定
        ↓
上游实体、端点与关系图
        ↓
可复核的观测证据
        ↓
版本化评估规则
        ↓
行动建议与人工审核
```

```text
debian package ⇄（M:N）⇄ upstream entity
entity ── endpoints（位置）＋ channels（观测维度：release / tag / commit / issue / ...）
endpoint × channel × 时间 → observation（追加式）
observation → claim（版本化、可撤回、带决策记录）→ 评估规则（可替换、可重算）
```

信道（channel）是观测的维度，不是 URL 或主机：同一主机可承载多信道，同一信道可跨主机（如镜像）。上面的流程图是主干，本图是观测侧的展开（提案）。

## 2. 分层模型

### L0 — Debian 包登记

Debian source package 是系统入口。登记其 source 名称、版本、格式、suite/component、Maintainer、Homepage、VCS、watch、DEP-12、WNPP 与上传历史等 Debian 侧事实。

这些字段是带来源的数据，不直接等同于“上游身份已确认”“项目已死亡”或“项目已复活”。特别是 `uscan` 状态只是一项来源明确的观测。

### L1 — 包身份分类

分类回答“这个 Debian 包本身是什么”，用于描述包的性质和后续处理路径。初始候选包括：

- `NATIVE`
- `EXTERNAL_SINGLE`
- `EXTERNAL_MULTI`
- `CONTENT_OR_DATA`
- `DERIVED_OR_VENDORED`
- `UMBRELLA_MEMBER`
- `PACKAGE_INSIDE_UPSTREAM`
- `IDENTITY_UNRESOLVED`

这些候选不是已经完备、互斥的最终枚举。包可以有一个主要分类并同时带多个标签或关系。例如 `freecdb` 可以是 Debian native、quarry 且聚合多个来源；不能把这些事实强压成同一个互斥类型。标签需与状态分离，例如 `DEBIAN_ASSIGNED_VERSION`、`RELEASELESS`、`FUNCTIONALLY_OBSOLETE`、`PAPER_ORPHAN`。

打包格式 native 不自动豁免监控，也不创设新终态；是否走独立的 Debian 内部监控路径属未决状态语义（§6）。

### L2 — 上游实体与端点

包与上游实体是多对多关系：一个包可对应多个实体；一个实体也可被多个 Debian 包引用。上游实体使用独立、持久的 `entity_id`，URL 只表示一个端点，不作为实体主键。

```text
upstream_entity
├── entity_id
├── canonical_name / aliases
├── entity_kind
├── candidate_endpoints / verified_endpoints
├── external_identifiers
└── identity_history
```

端点可以是 forge 仓库、发布目录、个人网页、Wiki、归档站或 Debian 内部仓库。改名、迁移、重定向和失效都要保留历史，不静默覆盖旧地址。候选端点与已确认端点须区分。

端点迁移默认仍属同一实体：搬迁产生 `RELOCATED_TO` 端点转移与别名变更；只有独立出处证据支持时才另立实体或建立 `FORK_OF`（实体层）。转移与 FORK_OF 的 subject/object 精确 schema 待定。来源标识符（purl 等）与内容标识符（hash 等）都不充当 `project_id`；project_id 只指向实体。

实体间和包到实体间使用类型化关系，例如：

- `TRACKS`
- `FORK_OF`
- `SUCCESSOR_OF`
- `RELOCATED_TO`
- `DERIVED_FROM` / `VENDORED_FROM`
- `MEMBER_OF` / `PART_OF_MONOREPO`
- `CONSUMES`
- `MIRROR_OF`
- `PACKAGING_IN`

每条关系都是一项可审计主张，不只是无出处的图边。

### L3 — 观测与证据

网络探针、UDD、Debian 元数据、归档服务、搜索结果及人工调查首先生成观测或证据记录。观测保留来源、方法、时间、目标和原始响应引用；同一 URL 不同时间的结果追加保存，不相互覆盖。

```text
observation
├── observation_id
├── subject / endpoint
├── source / method / channel
├── observed_at
├── outcome
├── response_hash / raw_reference
└── tool_or_parser_version
```

解释性主张另行记录：

```text
claim
├── subject — predicate — object
├── supporting_evidence[]
├── confidence
├── asserted_by
├── review_status
└── valid_from / valid_until
```

至少区分原始事实、机器推导、搜索或 AI 提出的假设、人工/规则确认的决定。搜索命中、HTTP 200、DNS 成功或 AI 输出本身都不能单独证明上游身份。

观测一经写入即不可变（追加式）；主张可版本化、可撤回，撤回须留决策记录（decision record）。采集过程的确定性只保证"如实记录了来源"，不证明元数据内容为真。置信度暂用 high/med/low 临时刻度；正式量表与固定的来源信任层级均未定稿。

### L4 — 评估与行动

版本化评估规则读取登记数据、确认过的关系和观测证据，派生活动解释、状态快照及行动建议。派生结果可重算；它不反向改写原始证据。

行动建议可包括：复查身份、验证新端点、补充 watch、检查新版本或 commit、人工评审、报告元数据问题。告警阈值和 Debian 后续行动规则不在本文件中定稿。

## 3. 证据获取与权限边界

采用分层、按需的证据获取：

1. **本地确定性来源**：UDD、`debian/watch`、DEP-12、上传历史、VCS 与 Debian 包元数据。
2. **已登记端点的应用层探针**：HTTPS/HTTP、HTTPS Git、Atom/RSS、发布目录和平台 API；保留完整重定向链并限速。
3. **结构化外部来源**：Repology、Anitya、Software Heritage 等，作为具名来源的证据。
4. **联网搜索**：仅在身份未决、旧端点失效或搬迁可疑时触发；输出候选，不直接确认绑定。
5. **AI 辅助分诊**：整理证据、摘取原文、提出关系候选和摘要；输出须引用观测证据，并进入审核流程。
6. **人工或明确规则裁决**：确认或驳回高影响身份关系。身份合并、迁移和 fork 关系不得由搜索排名或 AI 独立落库。

不做原始 TCP/UDP 端口扫描。探针只访问已登记或经过候选审核的应用层端点；限制重定向、请求速率、响应体大小与超时，并防止 SSRF。不得把“可达性”误作“身份”。

## 4. 典型包的表达方式

| 案例 | 框架表达 |
|---|---|
| `freecdb` | 包身份为 native；记录多个 `DERIVED_FROM` 来源及聚合关系 |
| `netkit-rusers` | 包通过 `MEMBER_OF` 指向共享的 netkit 上游组 |
| `couriergraph` | 独立上游实体通过 `FORK_OF` 关联 `mailgraph` |
| `wizznic` | 保留 SourceForge 旧端点，并将 GitHub 地址作为迁移候选/经核验的新端点 |
| `wreport` | 通过 `PACKAGING_IN` 表示 Debian 打包与上游仓库共处，并区分路径 |
| `wvware` 同名仓库 | 保存身份冲突或冒名候选，不按名字自动绑定 |
| 上游身份不明 | 保留 Debian 包与已有来源，身份关系为 unresolved，不推断成“上游不存在” |

## 5. 最小实现边界

第一阶段可使用 SQLite 或 PostgreSQL，不要求先引入通用知识图谱平台。最低限度需要能保存：

```text
debian_package
package_label
upstream_entity
entity_alias
entity_endpoint
entity_relation
observation
evidence
claim
review_decision
evaluation_snapshot
action
```

实现优先级：

1. 将现有 UDD 快照导入并登记 QA source packages。
2. 依据 package format、watch、DEP-12、Homepage、版权来源等生成带出处的身份候选。
3. 建立持久实体 ID、历史端点、类型化关系、观测和主张记录。
4. 对端点运行受控应用层探针，并保存可复核结果。
5. 对残余模糊案例按需使用结构化搜索和 AI 分诊，进入人工审核。
6. 待状态语义商定后，再将评估器与通知规则作为版本化规则包接入。

## 6. 明确未定事项

本文件接受的是**框架概览和数据边界**，并未定稿活动状态机。以下仍需后续讨论：

- `ACTIVE`、`QUIET`、`REVIVED` 的定义及其相互关系；
- release、tag、commit、issue 等信道的优先级；
- `QUIET`/`DORMANT` 的认证条件、TTL 与周期；
- freshness、信号归属、信号震级如何影响状态或告警；
- native 包是否进入独立的 Debian 内部监控；
- 跨发行版信号的权重与同源校验规则；
- 各类身份关系的置信度门槛和人工审核策略。

这些语义应作为后续评估规则讨论，不应混进身份、端点或观测数据模型。

## 7. 与既有研究文档的关系

- [01-upstream-activity-tools.md](01-upstream-activity-tools.md)：活动工具调研（历史）；其中 §7 的状态机式骨架是调研阶段方案，不是当前已接受的框架定义。
- [07-package-identity-taxonomy.md](07-package-identity-taxonomy.md)：包身份分类的轴、标签与计数保留（本页 L1 的展开）。
- [08-related-tools-and-models.md](08-related-tools-and-models.md)：身份数据模型的横向参考、schema 支持与关键修正。
- [09-discovery-probes-and-ai.md](09-discovery-probes-and-ai.md)：证据获取分层、探针边界与 AI 分诊提案（本页 §3 的展开）。
- [10-debian-community-practices.md](10-debian-community-practices.md)：Debian 社区侧实践与联络边界。
- [11-decisions-and-open-questions.md](11-decisions-and-open-questions.md)：已接受决定、提案与未决清单。
- [05-survey-findings.md](05-survey-findings.md)：81 包压测证据及待商榷的状态机问题（历史解读，见其编者注）。
- [survey/2026-10-29/](survey/2026-10-29/)：分批鉴定结果、协议和样本原始材料。
