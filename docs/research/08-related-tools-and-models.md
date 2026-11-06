# 08 相关工具与身份数据模型：有界结论（研究归档）

**归档日期**：2026-10-31
**性质**：研究综合。本文记载的是对已审阅公开工具的有界结论与横向参考模型，不含技术选型定稿。
**更名注记**：项目此前文档中称 spring，现已更名 Gleam / 荧。
**基础**：[01-upstream-activity-tools.md](01-upstream-activity-tools.md) 的工具调查、[06-framework-overview.md](06-framework-overview.md) 的分层框架，及 2026-10-30 前后的横向核对。

## 1. 有界发现

先期调查的结论，其准确表述是：**在已审阅的公开工具范围内，没有找到完整、直接适配"Debian source package → 持久上游实体与关系图 → 可复核观测"全链需求的现成方案。**

这是对已审阅集合的有界结论，不构成"全行业不存在此类工具"的普遍断言。未审阅的闭源系统、内部工具、新发布项目随时可能改变这个结论；引用时必须保留边界。00-overview 中"专用诈尸检测器不存在"等说法应按此有界口径理解。

## 2. 逐项：提供什么，边界在哪

| 对象 | 提供什么 | 边界与注意 |
|---|---|---|
| Debtags | facet 标签描述包的用途与种类 | 作用于二进制包层级，描述"包是什么"，不表达上游实体身份 |
| Wikibase | statement + qualifier + reference + 完整修订历史；支持重定向与实体合并 | rank 是偏好排序，不是校准概率；见 §3 修正 1 |
| DataHub | entity + aspects 的元数据建模 | aspect 的拆分方式要自己定义，平台不替你回答身份问题 |
| CycloneDX | identity evidence、confidence、pedigree 等字段 | 是交换格式，不是运行时身份服务；pedigree 是单个 BOM 的谱系字段，不是通用主张修订日志，主张历史须分别保留各版本 BOM |
| SPDX 3.0 | RelationshipType 关系词汇（工件间关系类型） | 词汇表丰富 ≠ 零成本互操作；映射仍需自己做 |
| Software Heritage | 内容寻址对象模型 + provenance（起源记录） | 回答"这段内容从哪来"，不回答"哪个项目在掌管这段内容" |
| Anitya | 上游发版监测；ProjectMapping 表达发行版包 ↔ project 的映射 | 映射不局限在版本层；来源与登记方式各异，逐条质量不一，引用前需复核 |
| Repology | 跨发行版版本聚合，内置合并规则 | 合并规则是其自身策略，可作参考，不可当权威裁决 |
| upstream-ontologist | 从 Debian 元数据推断上游位置与元数据 | 产出是候选，需要审核后才可入库 |
| OpenRefine / Splink | 实体对齐与概率匹配工具/库 | 只覆盖匹配这一步；不是身份存储，不管理匹配结果的生命周期 |
| Reconciliation API | 匹配服务的接口规范 | 同上，接口规范不等于身份模型 |
| Backstage Software Catalog | 关系模型含 memberOf / hasMember、partOf / hasPart、ownedBy | schema 层已具备成员与归属（stewardship）表达；是否有现成"上游项目"语义、以及有没有被填充的数据，是另外的问题 |
| 图数据库 / 知识图谱平台 | 存储与查询能力 | 平台不自带本体；模式、关系语义、版本化都要自己定义与维护 |
| Atlas / STIX | 服务目录之外的实体-关系表达 | 次级参考，只做结构类比 |

各项主 URL 见 §5。

## 3. 关键修正（不得复用先前 agent 的错误结论）

1. **Wikibase 的 rank 是偏好排序，不是校准概率**。任何把 rank 读成可信度百分比的用法都站不住。同时，Wikibase 支持重定向与实体合并；先前某 agent 曾有"无法合并实体"的说法，该说法是错的，以此为准修正，后续不得再复用。Wikibase 属性体系可扩展，足以自建属性表达掌管关系（stewardship）；但 schema 能表达不等于有证据，也不等于数据已被填充。
2. **内容匹配只证明内容同一，不证明谱系方向或控制关系**。同 hash、同代码确立了"内容相同"这一事实；它本身不能确立谁 fork 自谁、谁在掌管（fork 与镜像可逐字节相同）。谱系与归属需要独立证据（发布公告、维护者身份、commit 权限等）。
3. **purl 标识的是包坐标**（type/namespace/name/version），不是规范项目身份。`pkg:deb/debian/foo` 说的是 Debian 包坐标，不回答"foo 的上游是哪个实体"。
4. **Built-Using 的记录范围比"全部构建依赖"窄**：它登记的是构建中需保留/引用其包含代码的源码包，不覆盖所有构建期使用；它也不是 vendoring 的独立证明，只能作候选证据。
5. **区分 schema 支持与已填充数据**：各模型"能表达什么"与"实际有没有人填、填得对不对"是两个问题；引用任何模型时必须说明在讨论哪一层。
6. 本文不收录厂商价格或维护度排名，不收录未核实统计；数字类事实以原始调查文档与实时查询为准。

## 4. 对 Gleam 的参考意义（提案层，选型未定）

可借鉴的层面：
- Wikibase 的 reference + 修订历史：每条 statement 带出处、可追溯变更，与 06 的 claim/observation 结构同构。
- DataHub 的 aspect 拆分：把不同关注点（身份、观测、评估）拆成可独立演进的 aspect，避免单表大杂烩。
- CycloneDX 的 evidence + confidence 字段：身份主张附证据类型与置信的表达方式（主张级历史须分别保留各版本 BOM；pedigree 不是修订日志）。
- SPDX 的关系词汇：与 06 L2 类型化关系清单（`FORK_OF`、`VENDORED_FROM` 等）对照补漏。
- SWH 的 provenance：内容级起源证据，适合做端点失效后的考古通道。
- Repology / Anitya / upstream-ontologist：映射与推断的工程经验，候选生产者。

不应照搬的层面：
- 把任何一个平台当作身份的唯一事实源；各模型都只覆盖 06 分层中的一层或一角。
- 把平台自带的置信语义直接当活动状态阈值；置信与状态在 06 中是分离的，状态语义本身还开放。

06 §5 已给出最小实现边界（SQLite/PostgreSQL 起步即可）。本文不改变该边界，也不预选任何平台。

### 表达能力速记（schema 支持 ≠ 已有数据）

| 关注点 | schema 层支持 | 数据层注意 |
|---|---|---|
| 成员/归属 | Backstage memberOf / hasMember、partOf / hasPart、ownedBy（官方关系模型）；Wikibase 可自建属性 | 有字段 ≠ 有人填；把 ownedBy 映射到"上游掌管"需另行定义证据要求 |
| 工件间谱系 | SPDX relationshipType；CycloneDX pedigree（BOM 谱系字段） | 表达关系类型；方向与控制仍需独立证据 |
| 发行版包 ↔ 上游项目 | Anitya ProjectMapping | 是映射记录，不自带证据链 |
| 内容起源 | SWH provenance | 回答内容来源，不回答掌管者 |
| 主张 + 出处 + 历史 | Wikibase statement / qualifier / reference + 修订历史 | rank 是偏好；合并与重定向受支持；历史语义靠自己的约定 |

结论：在 schema 层，"成员与掌管"在 Backstage、Wikibase 等模型中已有可借用的表达；不能声称"所有模型都无法表达 stewardship"。真正缺的是被审核、被证据引用支撑的填充数据，这正是 Gleam 要自建的部分。本表只讨论表达能力，不构成任何数据库选型。

## 5. 主要来源 URL

- Wikibase DataModel: https://www.mediawiki.org/wiki/Wikibase/DataModel
- DataHub metadata model: https://docs.datahub.com/docs/metadata-modeling/metadata-model
- CycloneDX 1.6 JSON schema: https://cyclonedx.org/docs/1.6/json/
- SPDX 3.0.1 RelationshipType: https://spdx.github.io/spdx-spec/v3.0.1/model/Core/Vocabularies/RelationshipType/
- SWH data model: https://docs.softwareheritage.org/devel/swh-model/data-model.html
- Anitya docs: https://anitya.readthedocs.io/en/latest/
- Repology docs: https://repology.org/docs/about
- upstream-ontologist: https://github.com/jelmer/upstream-ontologist
- Debtags wiki: https://wiki.debian.org/Debtags
- Reconciliation API spec: https://github.com/reconciliation-api/specs
- Splink: https://github.com/moj-analytical-services/splink
- Backstage Software Catalog 关系模型: https://backstage.io/docs/features/software-catalog/well-known-relations/ （2026-10-31 独立抓取核对：含 memberOf/hasMember、partOf/hasPart、ownedBy）

以上为文档与规范出处，本文均为转述与评论，不含长段引文。

## 6. 与其他文档的关系

- [01-upstream-activity-tools.md](01-upstream-activity-tools.md)：早期的活动监控工具全景与积木清单，本文在此之上补横向身份模型。
- [06-framework-overview.md](06-framework-overview.md)：本文各模型映射进的分层框架；状态语义不在任何一份模型中，也不在本文定稿。
- [09-discovery-probes-and-ai.md](09-discovery-probes-and-ai.md)：证据如何经探针与 AI 分诊进入这些结构。
- 研究缺口：各模型对"历史端点 + 迁移链"的表达能力未逐一验证；fork 与镜像的身份区分字段设计未做；这些留待后续讨论稿。
