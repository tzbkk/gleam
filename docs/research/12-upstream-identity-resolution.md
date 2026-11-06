# 12 上游身份解析：候选方法与证据循环（研究归档 / 未选型）

**归档日期**：2026-10-31
**性质**：方法记录，不是选型决定。本文调研上游身份解析的候选方法，不选定、不实现任何算法，不含实现日期与性能承诺。不得把本文当作已接受设计引用（已接受范围见 [11](11-decisions-and-open-questions.md) 的选型记录）。
**关联**：[06](06-framework-overview.md) 分层模型、[07](07-package-identity-taxonomy.md) 身份分类轴 C/D、[09](09-discovery-probes-and-ai.md) 发现管道、[11](11-decisions-and-open-questions.md) 研究缺口；81 包压测见 [05](05-survey-findings.md)。

## 1. 范围与状态（先读）

- 记录 ≠ 选型：本文是记录，不是选定或实现算法。所有方法均为未测试提案。
- 范围方向：全档案（archive-wide）上游发现与身份解析是新增方向；QA 监控仍是主线优先级。监控套件清单与历史枚举仍未定，现有 QA 快照的范围与口径不变。
- 两个不同的问题，不互相替代：
  - **上游身份记录去重**：同一上游实体在多条线索下被拆成多条记录，要不要合并成一条。
  - **source package → 上游实体指派**：一个包聚合多个上游、多个包共享一个上游的多对多关系（[07](07-package-identity-taxonomy.md) 轴 C）。前者不解决后者；后者还需要逐包证据，不是去重的副产品。

## 2. 证据循环（候选假设的生成与撤销）

- 全部 source package 都贡献线索 → 线索汇成候选关联（相似度只是候选，不是结论）→ 人工证据审查、逐案立档 → 出现新证据或既有决定被修订时，受影响的案例重开。
- 不做重复自证：同一方法在同一份证据上的重复确认不升级置信度；被复制传播的证据不算独立证据。
- 未决案例保留未决状态及复查依据：缺证据不等于已解决，不把"查不出"归档为"没有"。

## 3. 候选方法（全部为未测试提案）

综合方向（未选型、未验证）：概率匹配 + 冲突约束 + 增量修正；不是"名字相似就合并"。

### a. 多轮阻断 + Fellegi-Sunter 概率打分

- 多轮阻断（blocking）缩小字段比较空间；Fellegi-Sunter / EM 估计字段权重，Splink 可作为可解释打分基线。
- 已知保留：条件独立假设在真实数据上常被违反；分数须在独立复核的样本上检验，不能默认是已校准概率。被抄录复制的证据不是独立证据，会虚增匹配分。
- 参考：[Fellegi-Sunter 理论（Splink topic guide）](https://moj-analytical-services.github.io/splink/topic_guides/theory/fellegi_sunter.html)、[阻断规则（Splink topic guide）](https://moj-analytical-services.github.io/splink/topic_guides/blocking/blocking_rules.html)。

### b. 冲突约束下的合并

- 保守基线：相似度达标才提议合并，任何已验证冲突一票否决（merge with conflict veto）。
- 对照方向是受限相关性聚类，两者优劣未比较。硬 cannot-link ≠ 软负边：硬约束是"禁止合并"，负权重边只是"降分"。经典 Pivot 算法不保证满足硬约束，也不保证对任意稀疏加权图给出质量保证。

### c. 增量记录链接

- Gruenheid / Dong / Srivastava（PVLDB 2014）：新证据到达时做 merge / split / move，避免全量重算。
- 保留：基于依赖的重算可能扩散到直接邻居之外；扩散边界多大，本文不设定，留待实验。

### d. Dedupe 主动学习

- Dedupe 的主动学习可在有了经人工复核的标签之后可选启用；没有复核标签就不启用。

## 4. 相似度边的纪律（为什么不做传递闭包）

- 假想案例：A~B 相似、B~C 相似，但 A≠C 已被验证。对相似度边做传递闭包会把 A、C 假合并；相似度边不是等价关系，闭包只适用于已验证的同一性陈述。
- 以下各项单独都不足以证明同一上游：同 org、同 URL、同 host、同 repo、共享内容 hash、fork 关系、monorepo 成员关系（[07](07-package-identity-taxonomy.md) 轴 C/D 与 [08](08-related-tools-and-models.md) 的既有保留继续适用）。
- 反向同理：Debian 源码包名不同、release track 不同，都不证明上游实体不同。
- 修正历史时保留实体身份与原始观测：合并、拆分是主张层的修订，旧观测不删不改（[06](06-framework-overview.md) 分层模型中主张层的追加纪律）。
- 签名只证明它所附的具体陈述，不构成对整个项目身份的背书。

## 5. 评估与数据（未建立，仅有方向）

- 缺标注集：需经独立复核的正例对 + 困难反例 + 未决/多上游案例。现有 81 包压测只是临时种子（provisional，见 [05](05-survey-findings.md) 的编者注）；watch / DEP-12 / 外部映射本身不是金标。
- 候选指标方向：候选召回、假合并及其传播影响、漏配、人工审查负担、反证后成功拆分的数量、增量与全量重算的一致性与成本。
- 数值阈值一概未定；本文不做性能声明，不引用不存在的全局基准。

## 6. 来源

- [Fellegi-Sunter 理论（Splink topic guide）](https://moj-analytical-services.github.io/splink/topic_guides/theory/fellegi_sunter.html)
- [阻断规则（Splink topic guide）](https://moj-analytical-services.github.io/splink/topic_guides/blocking/blocking_rules.html)
- [arXiv 2501.03154](https://arxiv.org/abs/2501.03154)
- [Incremental Record Linkage（Gruenheid, Dong, Srivastava，PVLDB 2014）](https://vldb.org/pvldb/vol7/p697-gruenheid.pdf)
- [Dedupe 文档](https://docs.dedupe.io/en/latest/)
