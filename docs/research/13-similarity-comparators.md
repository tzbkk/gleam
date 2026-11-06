# 13 相似度比较器：模型排除决定与候选清单（研究归档 / 未选型）

**归档日期**：2026-10-31
**性质**：调研存档与决定记录。用户决定（2026-10-31）：相似度不采用嵌入模型；比较器仍在考察，未选型。小模型调研结论仅作存档，不进入实现。不得把本文当作选型或已接受设计引用（已接受范围见 [11](11-decisions-and-open-questions.md) §1）。
**关联**：[12](12-upstream-identity-resolution.md) §3 候选方法、§5 评估；[06](06-framework-overview.md) §3 搜索命中是候选；[07](07-package-identity-taxonomy.md) 轴 C/D；[11](11-decisions-and-open-questions.md) §1、§5、§7；81 包压测见 [05](05-survey-findings.md) 与 [survey/2026-10-29/](survey/2026-10-29/)。

## 1. 范围与决定（先读）

- **决定（用户 2026-10-31）**：相似度判断不采用嵌入模型，包括 potion、MiniLM、BGE、cross-encoder 等语义模型（「归档，那就不用模型」）。
- 决定理由（会话中记录，未独立复核）：IdBench 显示嵌入在标识符上不可靠；无公开可引用的零样本基准；余弦相似度阈值不稳定。
- **排除边界**：排除的是嵌入/语义模型；规则、编辑距离、TF-IDF、语音键等非模型方法不在排除之列，但仍须实测。
- **比较器仍在考察**：rapidfuzz 类非模型算法是候选方向，选择待实测；本文不选型、不预定阈值。
- 与 [12](12-upstream-identity-resolution.md) 的分工：12 记录身份解析的方法循环（未选型）；本文只记录相似度这一层的排除决定与比较器候选。
- 小模型调研结论仅作存档，不进入实现；不得据此引用任何「已选定算法」。

## 2. 调研要点（provisional；先前 agent 记录，归档日未重新核验）

生产系统的否定结果：模糊匹配在包名上普遍不用于自动合并。

- Repology：零模糊，纯规则 + effname 精确匹配；repo 内 grep 验证（@fce35eb）。
- libraries.io：精确匹配 + PEP 503 名称规范化。
- ORT：无模糊匹配。
- Anitya：unique(name, homepage) 约束；pg_trgm ≥ 0.3 仅用于搜索建议（@6f61cb2）。
- deps.dev：以元数据与证明做关联；模糊仅产「相似名」警告（同形字 + Levenshtein ≤ 1、仅热门包）。
- 共同模式：模糊只产候选或警告，从不自动合并；与 [12](12-upstream-identity-resolution.md) §2 的证据循环一致。

## 3. 候选比较器清单（待实测，未选型）

- rapidfuzz（MIT）：JaroWinkler、Levenshtein、DamerauLevenshtein、token_set_ratio、process.cdist。
- jellyfish：只考虑其语音键部分（Codeberg jpt/jellyfish）。
- Splink v5 分级比较模式：exact / d≤1 / d≤2 三档；NameComparison 默认 exact + Jaro-Winkler 0.92 / 0.88 / 0.70 三阈值。模式可借鉴，不引入框架。
- dedupe 的 blocking 谓词：字符前缀、1-2gram 指纹、sortedAcronym、DoubleMetaphone、TF-IDF canopy。
- scikit-learn：char_wb(2,4) TF-IDF + cosine。
- URL 纪律：url-normalize 规范化后按 host / owner / repo 逐段精确比较，永不模糊。
- thefuzz 与 Levenshtein（PyPI 包）均为 rapidfuzz 的壳，不单列。
- Cohen、Ravikumar、Fienberg（2003）：短名上仿射缺口编辑距离最强；有语料统计可用时 TF-IDF 系获胜（此前会话口误作「Cohen-Pientka」，在此更正）。
- 以上全部为候选；没有任何一项被选定，实测（§4）完成前不作排序。

## 4. 实测计划（已完成，结果见 14）

在 raw/ 快照上比较各比较器的区分力：声明式正例 = 包 ↔ 自己的 watch / DEP-12 声明上游；负例 = 跨包近邻 + 已知误导案例。结果见 [14](14-similarity-and-signal-tests.md)，不预定阈值；本文不收录实验数字。

## 5. 存档边界

- 本文只存档决定与调研要点，不含实现、不含实验结果、不含阈值。
- 后续实测结果另存；若实测推翻候选，以新文档为准，本文不改写，只可加勘误。

## 6. 来源（2026-10-31 会话中记录，归档日未重新独立核验）

- [rapidfuzz](https://github.com/rapidfuzz/rapidfuzz)
- [jellyfish](https://codeberg.org/jpt/jellyfish)
- [Splink comparison library](https://moj-analytical-services.github.io/splink/api_docs/comparison_library.html)
- [dedupe](https://github.com/dedupeio/dedupe)
- [scikit-learn feature extraction](https://scikit-learn.org/stable/modules/feature_extraction.html)
- [anitya](https://github.com/fedora-infra/anitya)（models.py 的 pg_trgm / unique 约束）
- [repology](https://github.com/repology/repology)（transformer/actions.py 的 setname）
- [deps.dev similar names](https://blog.deps.dev/similar-names/)
- [url-normalize](https://github.com/niksite/url-normalize)
- [Cohen、Ravikumar、Fienberg 2003](https://ar5iv.labs.arxiv.org/html/2003.03471v1)
- [IdBench](https://arxiv.org/html/1910.05177v2)
