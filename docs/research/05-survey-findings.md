# QA 包鉴定压测:状态机覆盖度与特例清单

**日期**:2026-10-29
**方法**:分层抽样 81 包(watch 状态 × 结构性嫌疑),6 个并行鉴定 agent 深挖,每包含本地 UDD 快照(raw/)+ 网络探针(GH atom feeds / SWH / Repology / SF JSON / 目录 mtime)
**原始材料**:[survey/2026-10-29/](survey/2026-10-29/)(协议、6 份分批结果、样本清单)
**状态**:证据初步,个体判定为先前 agent 临时评估、未独立复核;分类与状态语义开放(见 [06](06-framework-overview.md) §6、[11](11-decisions-and-open-questions.md))。状态机如何吸收这些发现,待商榷(见 §7)

> **编者注(2026-10-31)**:本文件为历史解读存档,正文保留原样。几点口径修正:逐包判定含 agent 标签与未核实计数,不是金标准;根节点问题现按 06 以身份表述;"6/16 隐性失明"是所选样本层的比率,不是总体盲区率;"newer available"是差分信号,不等于复活信号;§5"0 例真回滚"与 bwbar 工件被删的实例相冲突,不作为普遍结论;任何样本都不能证明状态集完备。项目现名 Gleam / 荧。

## 1. 样本设计

| 批 | 抽样层 | 数量 | 预期假设 |
|---|---|---|---|
| A_error | uscan status=error | 12 | watch 失效,期待 RESOLVING/UNTRACEABLE/MUSEUM |
| B_null | status 为空 | 12 | 零覆盖,期待 UNRESOLVED 系 |
| C_uptodate | status=up to date | 16 | 猎"隐性失明":旧址博物馆 + 他处活跃 |
| D_newer | status=newer available | 14 | 猎 REVIVED,防 NOISE |
| E_weird | only older / untrackable | 12 | 版本语义怪胎 |
| F_structural | native 版本号/词典/数据嫌疑 | 15 | 攻击实体轴 |

固定随机种子 20261029,样本清单见 sample.json。成本事实:**六批合计 GH API 仅用 4 次配额**,全部决定性证据来自 atom feeds、目录 mtime、SF best_release.json、SWH、Repology——廉价探针足够,监控架构成本预估成立。

## 2. 状态覆盖对账

原 8 状态(UNRESOLVED / RESOLVING / UNTRACEABLE / QUIET-UNCERTIFIED / DORMANT / REVIVED / NOISE / MUSEUM)全部被实例化,但无一幸免于定义修正:

| 状态 | 实例量级 | 实测问题 |
|---|---|---|
| DORMANT | 30+ | 被**假覆盖**污染:wizznic 的 watch 对着已死的 SF 报 up-to-date,真上游在 GitHub 发过 1.1(四个发行版都已跟进,Debian 独不知) |
| MUSEUM | 11 | 高产成立:kic(1996 年起冻结在原位)、efax、ctorrent、liece、wavesurfer、gnome-colors…… |
| REVIVED | 17 | 被**考古学**污染:lua-cosmo 的"新版"10.4 岁;被**归属**污染:emacs-helm-ag 的 2025 commit 是第三方看门人 |
| NOISE | 0 经典形态 | 经典正则粘连(.gz)零实例;真实噪声是**沉默形状**(watch 正则失配,libpgf/swish++/yc-el)与**反向形状**(Debian 领先上游最后一个 tag,auto-dictionary-mode) |
| QUIET-UNCERTIFIED | 过渡态 | 成立——DORMANT 前置强制外部重扫被 wizznic 案例证明必需 |
| RESOLVING | 1(couriergraph)| 成立但稀有 |
| UNTRACEABLE | 6 | 成立:search-citeseer 全网仅 Debian 存档;coolmail copyright 自注 "(URL vanished)" |
| UNRESOLVED | 仅瞬态 | 应降级为解析队列,非稳态 |

## 3. 特例目录(按对状态机的杀伤方式分组)

### 3.1 信道盲区:release 冻结、commit 活着 ⭐⭐

| 包 | release 信道 | commit 信道 |
|---|---|---|
| bitlbee | tarball 2019-02 冻结 | master 2025-10(在写 purple-googlechat 新协议支持)|
| ubertooth | tag 2020-12 冻结 | master 2026-03(extcap 维护)|
| paredit-everywhere | tag 2014 冻结 | main 2024-11 |
| syncevolution | tag 2021(且写成 `2-0-0`,dpkg 比较语义碎裂)| commit 2026-05 |

**要害**:"看着的信道没有信号"≠"没有信号"。版本比较在此类包上**永久失明**,commit 时效必须独立成轴。

### 3.2 假状态:uscan 字面值无认识论含量 ⭐⭐

- **fische / webcamd**:FakeWatch(`FakeWatchNoUpstream*` + `Dversionmangle→0.No-Track`)伪造的 "up to date";webcamd 的 Homepage 字段就是 web.archive.org 快照
- **dmaths**:watch 注释自称"源码无版本,无法做能用的 watch"——实际官网明写 version 7.7.0,Debian 停在 4.4,**三个大版本被一句认输注释埋掉**
- **net-dns-fingerprint**:watch 文件存在且有效,但 status=NULL——**uscan 结果从未被处理**,管道故障冒充包属性,掩盖了一个活上游(kirei/fpdns,commit 2024-09,Fedora 已发 0.10.0)

**要害**:NULL/error/"up to date" 都必须先分解成因:无 watch 文件 / 有但未处理 / 结构性不可 watch / 假 watch / 自指元数据(coolmail 的 Homepage 指向 packages.debian.org 自己)。

### 3.3 WATCH_BLIND:目标活着,产物形态变了 ⭐

| 包 | 病因 | 后果 |
|---|---|---|
| libpgf | SF 把版本号从文件名挪进目录名,.zip 替代 .tar.gz | watch 只见 6.14.12(2010),实际 7.21.7 已发 |
| swish++ | 上游中途改名 `swishplusplus`→`swish++` | 双 pattern 的 v4 watch 自身畸形 |
| yc-el | 上游页面亲手警告"5.0.0 起格式从 .tar.gz 改回 .gz" | 真实状态是 newer,报成 older |
| simulavr | **2020 年用 6 个 .deb + 文档发布 1.1.0,无源码 tarball** | watch 永远看不见这次发版 |
| imapcopy | 无版本号 zip | uscan "untrackable" ≠ 机器 UNTRACEABLE(状态词撞名)|

**要害**:这不是 NOISE("忽略")而是"**换手段重扫之前,沉默不可信**"。E 批 11 个 "only older" 中 0 例是真回滚——该状态是可信度最低的 uscan 字面值。

### 3.4 信号归属:活着的不一定是"上游" ⭐

- **emacs-helm-ag**:作者 syohex 把全部 elisp 仓库搬进名为 `emacsattic` 的 org(redirect 仍 200!),2025 年的 commit 是第三方看门人 jcs090218 打的——活着的坟场
- **wv**:GitHub 有个活的 "wvware" org,里面是无关仓库(VR-Space-Shooter、nesemu2)——**死名被注册,冒名者陷阱**
- **wavesurfer**:Repology 的"最新版"是 Homebrew 第三方二进制重打包(1.8.8p6.1),非作者信号
- **apcupsd**:核心死于 2015,外围绕着一圈活着的 wrapper(apcupsd_exporter 180★ 2025-08 还在 push)

### 3.5 实体轴:DEBIAN_NATIVE 四物种 ⭐⭐

F 批 15 包中 8 个 native(3.0 native 格式),且**不是一个案例,是四个物种**:

1. **Debian 出生且活着**:pppconfig(2026-05 有上传)、snmptrapfmt、debian-installer-launcher(v48 在 pipeline)——监控"上游"是范畴错误,salsa 就是上游
2. **采石场**:freecdb 聚合 4 个 DJB 遗迹 tarball(Debian 0.78 版号线漂浮在所有死去的父版本之上);ifenslave 的"上游文件"连内核自己都删了;mdetect 的 copyright 明写"**我不打算追踪上游**"(Caldera 遗腹子,法律上都放射性)
3. **死物 fork**:intltool-debian = intltool 2006 快照,母体 2015 死亡,fork 是唯一活体
4. **上游死于 Debian 自家基建**:libapache2-mod-auth-plain 的"上游"是已退役的 alioth.debian.org——forge 死了,上游就死了

### 3.6 谱系关系:复活概率在包外

- **couriergraph**:死 fork,母体 mailgraph 活得好好的(1.14 各发行版在发)——复活路径是谱系的不是地点的
- **efax**:死上游,消费者 efax-gtk 活着
- **netkit-\***:一个死 FTP 目录撑着 ~7 个 Debian 源码包——按包监控会把同一尸体发现 7 次
- **gogits-chardet**:GitHub org 改名 gogits→gogs,一切存量 URL 靠 silent redirect 续命

### 3.7 杂项奇观(单例但 Each 一课)

| 包 | 现象 | 教训 |
|---|---|---|
| ifile / bwbar | Debian 池就是全球最新版档案馆;kernel.org 目录被回滚到 1.2.2(Debian 独有 1.2.3)| 追踪器生态本身是上游产物空间的一部分 |
| splitvt | 对着 1999 年的 tarball 报 up-to-date(27 年)| 真空但不衰——"该软件写完了" |
| kic | uscan 跟随 http→https 重定向把 host+path 粘成 `www.wrcad.comftp` | 监控真确度受限于 watcher 自身 bug |
| imagination | SF 项目被删,301 跳目录搜索页,**伪装成 200** | 项目删除是可检测事件(SF 墓碑签名)|
| play.it-adventure | 维护者=上游作者,"孤儿"是纸面的;下载站挂着 example.com 证书 | 假孤儿检测(changed_by vs 版权人)|
| gwaei | 11.5 年沉默后恰好 2 个 commit(2025-06)| 复活有"震级"问题,弱信号会再衰减 |
| xtrs | **作者本人 2025 年回归** + Gentoo 交叉佐证 | 完美复活样本:作者身份+跨发行版佐证双锚点 |
| meterec | 上游两次发版间隔 7 年(2013→2020)| 沉寂 TTL 必须 cadence-normalized |
| wreport | Debian 打包分支活在**上游仓库内部**(Zini 是 DD),10 月刚"孤儿" | 孤儿标志与现实倒挂 |
| golang-gogits-chardet | 无 watch 文件纯属 2017 打包惯例 | "无传感器"≠"无信号" |

## 4. 根节点的实证修正

"覆盖闭合"作为根被验证(每个翻车案例都是覆盖某维断裂),但覆盖不是一个维度,是四个:

```
覆盖 = 位置真(wizznic:watch 看着尸体产地)
     × 信道真(bitlbee:看 release,活在 commit)
     × 解析真(libpgf:URL 对,正则失配)
     × 管道真(net-dns-fingerprint:watch 存在,从未被处理)
```

**"无信号可采信"当且仅当四维同时闭合。**这是压测后修正的根节点定义。

## 5. 数字侧写

- C 批("up to date"):**6/16 打破假设**(2 假 watch、2 commit 活、2 健康误入)→ 隐性失明率实测 ≈ 37.5%(该层)
- D 批("newer"):8 真复活 / 3 落后 / 2 考古 / 1 博物馆 → **"newer available" 是差分信号,不是复活信号**
- E 批("only older"):11 例中 **0 例真回滚**,约一半是 Debian 侧版本串伪影,2 例WATCH_BLIND 掩盖了真实 "newer"
- B 批("null"):四种不同含义(无文件/未处理/不可 watch/自指元数据)
- F 批:结构性嫌疑(native 版本号)100% 由实体结构解释;**实体轴完全预测哪些包永远离不开 DORMANT**

## 6. 分批覆盖度判定汇总

| 批 | 干净拟合 | 勉强拟合 | 打破 |
|---|---|---|---|
| A_error | 7/12 | 5/12 | 0(但 "error" 字面值本身零信息)|
| B_null | 8/12 | 3 | 1(net-dns-fingerprint 管道故障)|
| C_uptodate | 10/16 | 0 | 6 |
| D_newer | 9/14 | 5 | 0 |
| E_weird | 5/12 | 4 | 3(paredit/syncevolution/simulavr)|
| F_structural | 5/15 | 2 | 8(NATIVE 族)|

## 7. 待商榷:状态机吸收方案(未定,讨论清单)

以下每条都列出证据与至少两个候选方案,**均未拍板**:

**Q1 信道盲区怎么进状态机?**
(A) 新增状态 ACTIVE-UNTAGGED(release 冻结/commit 活);(B) 不加状态,给所有状态挂正交属性 `commit_recency`,由独立轴表达。证据:§3.1 四例 + E 批三例打破。
牵连:REVIVED 定义是否要求"版本事件"?syncevolution 按"commit 也算"应为 REVIVED,按版本应为 DORMANT——**优先级规则(信道孰先)本身就是要商榷的对象**。

**Q2 NATIVE 族是终态 bypass 还是独立管道?**
(A) 实体类型判定 → NATIVE_ACTIVE/NATIVE_QUARRY 终态,抑制上游监控;(B) 完全独立的 Debian 侧管道(监控 salsa/uploads),上游状态机对 native 一票否决。证据:§3.5 四物种;31 个 native 包全量待归类。

**Q3 WATCH_BLIND 与 NOISE 的边界?**
NOISE 语义是"忽略",WATCH_BLIND 语义是"重扫"。二者都产生假 uscan 字面值,但动作相反。合并进一个"字面值无效"态 + 成因子字段,还是分开?

**Q4 信号四属性(日期/归属/震级/信道)是状态门槛还是旁注?**
version_date→FRESH/STALE/ARCHAEOLOGY 分档是否作为 REVIVED 的硬门槛(cadence 归一化怎么算)?信号归属(原作者/看门人/第三方/冒名者)是否阻断 REVIVED?震级(gwaei 2-commit)与衰减怎么参数化?

**Q5 谱系关系(PARENT_ALIVE/CONSUMER_ALIVE/UMBBRELLA_GROUP)放哪?**
影响复活概率与告警文案,但不是包自身状态。放进实体档案(旁注)还是参与状态转移(如 couriergraph 建议动作="向母体靠拢")?

**Q6 下游信号算不算复活力?**
libhbalinux:RHEL 系全员 1.0.17>Debian 1.0.16——别家发行版的版本算 REVIVED 证据吗?xtrs 的 Gentoo 佐证算加分项吗?ips 的 AUR 同名异项目算反例吗?需要一个跨发行版证据的规则(同源校验)。

**Q7 假 watch / 未处理 watch 的检测责任归属?**
监控系统自建假 watch 识别(`FakeWatch*` 正则、people.debian.org 源),还是依赖 Debian 数据质量管道修复?net-dns-fingerprint 类"管道故障"要不要由 Gleam 的对账逻辑(uscan 该跑而没跑)兜底?

**Q8 位置集合的排序与优先级?**
wcd/wizznic 证明 DEP-12 Repository 常比 watch URL 新。位置候选的优先序(watch URL / DEP-12 / Homepage / 版权 Source / SWH 发现)怎么定?redirect 跟随(gogits→gogs、emacsattic)是强制项吗?

## 8. 与既有文档的关系

- 01 §7 推荐架构的"补盲层"升级为**认证层**(全部 covered-quiet 包的周期性外部重扫),不再是 37% 盲区的专属补偿——wizznic/bandwidthd 对照实验(cdist blobandconquer)的结论被 81 包压测放大
- 02 §7 "该埋 vs 等诈尸"三条件不变;UNTRACEABLE/NATIVE_QUARRY 族成为"该埋"管道的高置信输入
- 03 libgltf 的 NOISE(.gz 粘连)是真实但**罕见**的形态——特例目录 §3.3/§3.4 的沉默形状与归属形状噪声更常见
