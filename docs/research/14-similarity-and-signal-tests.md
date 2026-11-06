# 14 相似度与信号实测：名称比较器与信号定位器基准（研究归档 / provisional）

**归档日期**：2026-10-31
**性质**：两轮实测的存档。第一轮：名称比较器基准。第二轮：信号定位器、组合规则、版本流与 copyright 通道。全部 provisional，无数值阈值被采纳。解释经用户质询后修正，被修正的过度声明列于 §2；修正后的口径以本文为准，原说法不再有效。不得把本文当作已接受规则或选型引用（已接受范围见 [11](11-decisions-and-open-questions.md) §1）。
**关联**：[13](13-similarity-comparators.md) §3 比较器候选与 §4 实测计划（本文即其结果）；[12](12-upstream-identity-resolution.md) §5 评估方向；[06](06-framework-overview.md) §3 搜索命中是候选；[11](11-decisions-and-open-questions.md) §4、§5、§7。

## 1. 范围与状态（先读）

- 记录两轮实测：名称比较器基准（691 声明式正例 / 6,910 最近邻硬负例）；信号定位器实测（watch / upstream_url / homepage / duck 定位器、陷阱分组、组合规则 R1-R3、版本流、copyright 通道）。
- 全部 provisional；无数值阈值被采纳为规则，R1 / R2 / R3 均不采纳（见 §4c）。
- 解释经过用户质询后修正。被修正的过度声明见 §2；其余数字照录，解释按 §5 框架改写。
- 可复现脚本在 /tmp/opencode/gleam-signals/comp/（名称轮）与 /tmp/opencode/gleam-signals/sigloc/、combo/（信号轮）。scratch 可能不持久，关键数字以本文为准。

## 2. 已修正的过度声明（防复发）

以下五条在会话中被用户逐条质询并修正；原表述不再有效。

1. 「watch 与 uscan upstream_url 一致（84.7%）是独立佐证」：不是独立佐证。uscan 执行 watch，是同一条流水线的两步；独立信道需另行证明。
2. 「1,026/1,078 = 95.2% 有项目级定位器」是规则生成标识的覆盖率，不是有效上游覆盖率。其中含许可证页、公共目录等待清洗项，有效性未验证。
3. 「信号间 URL 不一致（15.8%）等于上游迁移」：不成立。主页/仓库分工、镜像、陈旧记录、解析错误均可造成同一现象；差异原因待逐案判定。
4. 「14 探针全对证明组合规则有效」：不能证明。7 个假对中 4 个因证据缺失而空正确；且探针混用包↔包与包↔仓库两类对象，口径不纯。
5. 「归档有 debian/upstream/metadata 而 UDD 快照缺 23/24」：两份数据不一致是事实；原因（UDD 滞后 / 导出伪影 / 时点差）未判定。

## 3. 名称比较器基准（数字保留，解释修正）

- 排名为本数据上的观察，不预定阈值；数字保留原值，解释见各条。
- 数据：691 个声明式正例（包 ↔ 自己声明上游）/ 6,910 个最近邻硬负例。
- 区分力排名（rank-biserial）：WRatio+规范化 0.9812 > TF-IDF char_wb(2,4) 0.9756-0.9776 > Indel 0.9703 > Levenshtein/Damerau 0.9627 > Jaro/JW 0.90-0.93。
- Jaro/JW 末档的解释：受负例膨胀，不代表算法本性的排序。
- 结构性观察：ratio / token_sort / token_set / Indel 在本数据逐分相等；单词 token 退化所致，四实为一。
- twoGramFingerprint 等价键：全部硬负例零误报、漏 24% 正例。这是高精度确认件的形状，不是排序器。
- sortedAcronym：退化为首字符相等（0.62），弃。
- 探针悖论（照录）：除 WRatio 外，所有连续比较器把假对 netkit-telnet↔netkit-telnet-ssl（fork）打得比真对 tcmu↔tcmu-runner 高（JW +0.08、Levenshtein +0.40）。
- 同批探针：真对 lua-bit32↔lua-compat-5.2 仅 0.52-0.55，假对 mha4mysql 却达 0.77。任何阈值不可救。
- 解释框架：字符串相似度测的是命名谱系（名字是否抄改自同源），不是对象同一。
- fork 抄名（名字同源、身份分裂）与重描述（身份同、命名独立）两类必然脱钩。
- 正确产出单位是带类型的候选关联，不是身份结论。

## 4. 信号定位器实测（数字保留，解释修正）

### a. 证据链与冲突率

- 证据链结构：upstream_metadata ⊂ metadata blob ⊂ duck。这是抄写链，不是三票独立。
- 信号间 URL 不一致率 15.8%。作为待解释差异记录，不判迁移。
- 例子照录：django-maintenancemode 的 watch 指旧 repo，uscan 却跟到 fork 新 owner；6tunnel 的 watch 仓库 vs homepage 项目页。

### b. 陷阱表

- 11 组已知陷阱，朴素基址分组的误合并总数：watch 610 / upstream_url 800 / homepage 24 / duck 1,592。
- 规范化 host/owner/project 精确匹配后：watch 1、其余 0。
- 唯一残留源是 duck_url（628，其中 gnu.org/licenses 409 包的许可证巨型组）。duck_url 的 URL 行不得作上游定位器。
- 剔除 duck_url 后，全信号并集仅 1 个假对：mha4mysql 的 `@PACKAGE@` 模板占位符。
- 假设证伪：yp.to/unifoundry 类陷阱死于 host+段级，各工具有独立路径，并未幸存。
- 实际幸存的陷阱类：host-only 主页（fizmo 8）；波浪号用户目录（~esr / ~roderick / ~eagle）；家族/目录段（ftp.gnu.org/aspell 6 词典、os.ghalkes.nl/dist）。
- 25 个声明同上游组（63 包）的构成：约 13 family / 10 规则伪组 / 2 待判。
- 唯一 forge 组是 granite。伪组全出自 host+段分支：门户类（cpan.org/modules、metacpan.org/release、pypi.python.org/packages、alioth cgi-bin）；目录塌缩类（aspell dict/）。

### c. 组合规则与版本流（全部不采纳）

- R1（任一信号规范化命中）：14/14，但含空正确（§2 第 4 条），不作证据。
- R2（≥2 类型 + 字节独立性卫兵）：丢真对 granite/tegaki。真同仓对的 watch 目标 URL 本应逐字节相同；字节相同对真对恰是同一性证据，不是抄写证据。对 libextractor 又因机械展开差异误放行。同时过严过松。
- R3：在缺信号探针上无定义。
- 版本流：25 组中同 major 9 vs 不同 16（granite 6.x/7.x、libextractor 1.0.0/0.6、citadel 916/917）。
- 解释：版本一致证流不证项目；版本分叉不可否决同上游关联（granite 反例）。

### d. copyright 通道与勘误

- copyright 通道（40 包，sources.debian.org 零失败）：Source 与 watch 目标严格一致 65.8%。
- SourceForge 两种形态（路径式 vs `/projects/` 式）判为不等价后，一致率 78.9%；真不一致 15.8%（逐案原因未判）。
- UDD 快照对比：归档有 debian/upstream/metadata 的包中 23/24 在 UDD 快照缺失；两份不一致是事实，原因未判（§2 第 5 条）。
- 勘误：FakeWatch 实为 59 包而非 35（24 个 v5 `Source:` 型旧法不可见）。
- 规则缺口（照录，未修补；防按结果调参）：SF 路径式 vs `/projects/` 式；svn.code.sf.net；web.archive.org 未解包；非 forge 分支目录粒度；模板占位符（`@PACKAGE@`、`[^/]+`）。

## 5. 修正后的读法

两轮测的都是指向关系：名字抄改同源、URL 规范化后同址、版本同流、出处同址。指向不是身份同一。每种信号只回答自己那部分，互相之间也不因同源抄写而升级置信度。从指向到同一还需关系类型判定（同一 / 分叉 / 插件 / 家族 / 巧合），该判定方法未建立；这是遗留问题（见 [11](11-decisions-and-open-questions.md) §7）。

## 6. 来源与复现

- 全部为本地快照与 sources.debian.org API 实测；无外部文献需引用。
- API 操作注记：`/api/src/<pkg>/` 取版本列表；原始文件走 `/data/main/<分桶>/<pkg>/<版>/<路径>`；HTTP 200 且 body 为 `{"error":404}` 表示文件不存在。
- 与 [13](13-similarity-comparators.md) 的分工：13 存决定与候选，本文存实测数字与修正解释。后续实测若推翻本文，以新文档为准，本文只加勘误。
