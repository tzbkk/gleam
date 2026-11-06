# 07 包身份分类：轴、标签与未重算计数（研究归档）

**归档日期**：2026-10-31
**性质**：研究综合与设计提案。本文的分类轴是提案，枚举不封闭；不在此定稿任何活动状态、TTL、来源信任层级或监控豁免。压测的逐包判定属临时评估（provisional），本文未独立复核，引用时保留该性质。
**更名注记**：项目此前文档中称 spring，现已更名 Gleam / 荧。
**证据基础**：
- 81 包压测（2026-10-29），综合见 [05-survey-findings.md](05-survey-findings.md)
- 对快照数据的一次性候选计数（归档时未重算，见"未重算的候选计数"一节）

## 1. 身份根的理由：为什么六个概念必须分离

06 框架的核心纪律是让生命周期不同、证据责任不同的东西各归各位：

| 概念 | 回答什么 | 不稳定性示例 |
|---|---|---|
| source package | 登记入口 | 随上传、版本、接养而变 |
| project / entity | 持久的上游实体，独立 entity_id | 名字不可靠：wvware org 被冒名注册，gogits org 改名 gogs |
| endpoint | URL 级的具体位置 | 迁移、重定向、死亡都是端点层事件（wizznic SF→GitHub） |
| channel | 同一实体的不同观测面 | bitlbee release 信道冻结 7 年，commit 信道活跃（见 [05](05-survey-findings.md)） |
| observation | 何人何时以何方法观测到什么 | 同一 URL 不同时间追加，不覆盖 |
| claim | 哪个主张由哪些证据支持 | watch 字面值、搜索命中、AI 输出都只是观测或假设 |

混用的翻车形态在压测中被实测：把 uscan 字面值当实体属性（假覆盖案例，见 [05](05-survey-findings.md)）、把 watch 目标位置当项目现状（wizznic）、把 URL 可达当上游活着（wavesurfer 的 Homebrew 重打包信号）。

## 2. 多标签轴（非互斥枚举）

每个包可在多条轴上同时带标签。轴描述维度，标签是候选词；没有任何标签能豁免其他轴的审查，也没有标签是互斥枚举的一员。候选标签与 [06](06-framework-overview.md) L1 候选分类的关系未定稿。

### 轴 A：打包格式
- 候选标签：`native` / `non-native`；存储时保留 sources.format 的实际值（如 `3.0 (native)`、`3.0 (quilt)`），二分标签只是视图。
- 格式事实与身份推论分离：`3.0 (native)` 只是 dpkg 打包格式，不是"无上游"的证明，也不是"有上游"的证明。格式是审查触发器，不是审查结论；native 不带来自动豁免，也不创设新终态。
- 反例同格式不同物种：intltool-debian 是 native 但为 intltool 2006 快照的 fork（母体 2015 年死）；pppconfig 是 native 且真为 Debian 出生、2026 年仍在上传。压测中共整理出同格式四种物种（见 [05](05-survey-findings.md)）。

### 轴 B：内容类型
- 候选标签：`code` / `font` / `data` / `dictionary` / `meta` / `format-carrier`
- 调查现状：按抽样标准应含词典/数据/字体嫌疑的那一批压测案例实测 0 例；但 81 包整体并非零内容类样本：uzbek-wordlist（词典）、gnome-colors（图标主题数据）、play.it-adventure（数据快照集）均落此轴。该批零例说明抽样层的结构性嫌疑由实体结构解释，不说明全样本无此类型。批内最接近的类比是 freecdb（格式本身是稳定产物）与 wv（遗留 .doc 格式载体）。此轴无系统调查，覆盖不了 font/meta（见"调查空白"一节）。

### 轴 C：包与项目的基数
- 1 包 → N 项目：freecdb 聚合 4 个 DJB 谱系来源（multi-upstream / quarry）。
- N 包 → 1 入口（候选伞状）：netkit 家族约 7 个源码包的 copyright 指向同一死 FTP 目录。共享 host 既不是"全体成员同生命周期"的证明，也不是"它们同属一个上游实体"的证明；伞状实体只是候选假设。按包监控会把同一尸体发现 7 次，假设的复活会同时牵动多个包，而每个包的关联程度仍需各自验证。
- 活在大仓库里：gogits-chardet 在压测中被标为 Gogs 的 vendored 工具库（subproject）；该方向性主张（谁 vendored 谁）尚需源码级核验，暂按候选记录。wreport 的 Debian 打包分支活在上游仓库内部，以路径区分（PACKAGING_IN）。

### 轴 D：谱系
- fork：intltool-debian（死母体的活 fork）；couriergraph 是活体 mailgraph 的死 fork，复活路径是谱系的不是地点的。
- quarried：freecdb、ifenslave（其"上游文件"后来被内核自己删掉）。
- vendored：chardet ← Gogs（方向待源码核验，见轴 C）。
- 纪律：URL 相似度、名字相似度不得直接推出父项目关系；谱系关系必须带独立证据并过审。

### 轴 E：端点基底
- 候选基底：forge / 个人主页 / wiki / 归档站 / Debian 自有基建 / 发行版档案。
- key-chord-el：上游是 EmacsWiki 文件，无 VCS、无 tarball、无版本串；任何人可编辑，信号归属不可靠。
- libapache2-mod-auth-plain：其"上游位置"是 Debian 自有且已退役的 alioth.debian.org，外部位置全部失效。这类案例说明端点基底影响解释路径；"forge 死 = 上游死"是单例描述，不上升为普遍规律。
- ifile / bwbar：Debian 池成了全球最新版档案馆；追踪器生态本身是上游产物空间的一部分。

### 轴 F：版本出处
- Debian 自造版本：evil-paredit 0.0.2、emacs-openwith 0.8g 是只存在于 Debian 侧的版本号。
- 快照版本：`+git` / `~git` 后缀（persp-projectile、auto-dictionary-mode）。
- 外来实体的版本：libapache2-mod-auth-plain 的 2.0.54 继承自 Apache 的版本。
- 复合版本：ctorrent `1.3.4.dnh3.3.2` = 原始发行 + 分支 + patchlevel，对应三代谱系。
- 对照：freecdb 的 0.78.x 悬在多个母版本之上，但没有证据表明它是复合版本；它是 Debian 自身的版本序列，与四个母版本无已证对应。
- 含义：任何"Debian 版本 vs 上游版本"的比较必须先按版本出处条件化，否则方向无意义（压测中的横向观察）。

### 轴 G：信号归属
- 候选归属：作者 / 看门人 / 第三方 / 重打包 / 冒名者。
- emacs-helm-ag：2025 年 commit 出自第三方看门人 jcs090218；emacsattic org 重定向仍 200，活着的坟场。
- wavesurfer：Repology 的"最新版"是 Homebrew 第三方二进制重打包，非作者信号。
- wv：`wvware` org 挂着无关仓库（VR-Space-Shooter、nesemu2），死名被注册。
- gogits→gogs：存量 URL 全靠 silent redirect 续命，身份绑定必须跟到重定向终点再核。

## 3. 案例落轴速查

| 案例 | 涉及主轴 |
|---|---|
| freecdb | A native + B 格式稳定 + C 多来源 + D quarry（0.78.x 为 Debian 自身版本序列，非已证复合版本） |
| intltool-debian | A native + D fork |
| ifenslave | A native + D quarry（内核衍生） |
| wreport | C 共仓路径分离 + 信号归属（上游作者是 DD，包侧却孤儿） |
| netkit-rusers | C umbrella（共享 host ≠ 共享生命周期） |
| wizznic | E 迁移：旧端点保留历史，新端点作为候选/核验 |
| key-chord-el | E wiki + F 无上游版本 + G 归属不可靠 |
| ctorrent | D 三代谱系 + E 个人 ISP 空间 + F 复合版本 |

以上落轴均出自 81 包压测（2026-10-29），综合解读见 [05-survey-findings.md](05-survey-findings.md)。

## 4. 未重算的候选计数（关键保留）

对快照数据做过一次性候选计数，归档时未重算。只能当候选清单规模参考，不能当分类结果，更不能当监控豁免依据：

| 计数 | 候选量级 | 说明 |
|---|---|---|
| native 打包格式 | ~31 | sources.format / version 形态候选；格式事实候选，身份分类必须逐包做 |
| FakeWatch 字面匹配 | ~59 | watch_file 字段的字面 `FakeWatch*` 匹配；people.debian.org 源只是线索，单独不足以归入此类；按 uscan 状态分层前先单列并打标（segregate/tag），不据此删除或排除包本身（压测中 fische/webcamd 两例实证其污染效应） |
| git-like 版本串 | ~50 | 指 sources.version 中形如 git 快照的语法（`+git` 等），不是 watch 文本分类，与身份无关 |
| multi-stanza watch | ~7 | 多 stanza 既不证明单一上游也不证明多上游；wayfire 三 stanza 属同一上游只是压测的临时判定（未独立复核），须逐案例归因 |

附加保留：
1. 同一次快照内两种计数单位存在差异：去重后 1,078 个 distinct source，清单行数记 1,082。这是行数 vs 去重 source 名的差异，不是快照时点漂移；差值 4 需检查重复 source 行或其他列重复后再解释。
2. 这些计数不构成任何监控豁免：native 候选逐包分类后才可能进入 Debian 内部监控路径，而该路径语义仍待商定（[06](06-framework-overview.md) 未决事项、[05](05-survey-findings.md) 待商榷问题）。
3. 上述计数未经独立复核，引用时必须保留"未重算"标注。

## 5. 本地字段 → 候选 → 限度（提案映射）

| 字段 | 可产出的候选 | 限度 |
|---|---|---|
| sources.format | 轴 A 标签（native / non-native） | 格式 ≠ 身份；native 非豁免 |
| sources.version | git-like 版本串候选（~50）；轴 F 版本出处 | 语法形状 ≠ 上游版本存在 |
| packages.section / description | 轴 B 内容类型候选 | 按 source 去重后使用；二进制包的角色可能与源码包不同 |
| upstream.watch_file | FakeWatch 字面匹配（~59）、multi-stanza（~7）候选 | 字面形态 ≠ 身份结论 |
| upstream.status | uscan 字面值观测 | NULL 且有 watch 只证明"缺已记录结果"，不证明"从未运行"；error 同理不产生身份主张 |
| upstream_metadata（DEP-12 各字段） | 位置候选（Repository 等） | QA 覆盖率低；字段存在 ≠ 端点有效 |
| duck_urls / duck_url_status | 端点存活候选 | 存活 ≠ 身份；parked/moved 启发式只是候选 |
| vcswatch | Debian 侧打包仓库活动候选 | 打包仓库可能与上游物理同仓，归因需核对 |
| upload_history | Debian 侧活动度量（最后上传） | 度量 Debian 侧活动，不是上游时钟；binNMU 占比"约 8%"为早期近似值，未独立核实 |
| wnpp | 孤儿化 / 接养事件候选 | 簿记状态可与现实倒挂（wreport 实证） |

反向依赖注意：本快照仅覆盖 Maintainer=QA Group 的包集，不含全档案反向依赖；任何"零反向依赖 → 未使用"的推论在此数据上不成立。

## 6. 调查空白

- 内容类型轴缺系统调查：抽样批内零实抽；全样本仅 uzbek-wordlist、gnome-colors、play.it-adventure 三个散点，覆盖不了 font/meta。
- 31 个 native 候选未逐包人工归类；压测中 8 例是唯一的深挖样本。
- 标签到 06 L1 候选（`NATIVE` / `EXTERNAL_SINGLE` / `EXTERNAL_MULTI` 等）的映射未定。
- multi-stanza 样本仅 7 例，per-component 状态语义还缺数据（见 [05](05-survey-findings.md)）。

## 7. 证据来源与下一步

- 81 包压测（2026-10-29）：逐包判定的综合解读见 [05-survey-findings.md](05-survey-findings.md)。压测按案例类型分组：结构类（native 四物种、freecdb、ifenslave、intltool-debian、wreport、wizznic、wv、wcd）、空结果类（netkit、key-chord-el、ctorrent、evil-paredit、emacs-openwith、net-dns-fingerprint）、版本出处与信道盲区类（gnome-colors 等）、结构性 watch 失配类（uzbek-wordlist）、新版本类（play.it-adventure）。
- 社区调查（2026-10-30）为会话式研究，各文档只引用其结论。
- 本文只做综合并标注局限。

下一步研究优先项：native 候选逐包归类、内容类型轴的实际抽样、标签映射讨论稿。
