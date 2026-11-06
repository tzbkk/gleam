# 16 搜索、抓取与代理工具：调研归档与选型记录

**归档日期**：2026-11-02。
**性质**：研究归档 + 选型记录。区分三类信息：已实现并实测、已实现但未实测、
仅厂商标称（未购买、未验证）。服务商比较只做定性，不复述未经核实的价格与免费
额度数字；文中引用的厂商页面数字未经独立复核，不作为事实引用。

## 1. 选型（已接受，范围有限）

选定的调查栈（2026-11-02 记录）：**自托管 SearXNG
（localhost 隔离）+ Gleam 既有安全 Fetcher + trafilatura（仅喂已抓回字节）+
GitHub API 工具**，全部在既有 investigate 工具循环内。

接受口径：这是选型接受记录，不构成"该栈在一般基准上优于其他方案"的证明；
横向比较未做基准测试，见 §6 未决。

已实现并实测的部分：

- SearXNG 容器（仅回环绑定、JSON 格式）查询可用，失败引擎透传。
- investigate 全栈 live run（librcd，本地模型）已跑通：终报通过应用侧硬校验，
  证据存储校验通过。
- trafilatura 2.3.1 真实生效（`mode.extraction` 入档）；未安装时
  标准库回退并在结果中标注。
- `github_api` 工具（repo/readme/releases/commits，无认证、白名单路径、限流头
  透传、404=未知）实测可用。

已实现但未实测：`--github-api-base` 指向非默认端点、Laya 参与调查等。

## 2. 实测发现

以下每条均为实测观察，非推断：

- **导航噪声链接**：抓回页面的抽取链接集包含大量页头/菜单/页脚导航链接。终报
  硬校验只认"被引观测自身的合格 URL 集"，模型引用噪声链接会被拒；但人工复核
  时仍需在噪声中甄别真候选。
- **截断与部分 body 隐藏**：失败或超限的抓取只保留部分响应（含空 body 的超时
  200），此时该页正文与链接证据对模型不可见；终报只能以间接证据陈述并标注
  不确定性。
- **搜索 robots 失败不做全局推断**：robots 抓取超时/失败按单次弃权（ABSTAIN）
  处理，只代表"该次该目标未知"；不得推断目标站永久不可用，更不得推断整类
  站点大面积宕机。
- **抽取可能丢弃 footer/作者署名**：trafilatura 清理会移除 footer 与版权块，
  作者、mailto、版权行可能随之下沉。已实现的对策是在清理**之前**独立捕获锚文本/
  mailto/footer/版权行作为出处信号；归属判断与正文抽取分开，不依赖抽取后正文。
- **搜索摘要弱于页面正文**：search 观测（含 SearXNG 返回的第三方摘要）作为弱
  证据类入档；引用可校验，但候选身份须有正文或 API 级观测支撑。duckduckgo-lite
  类公共抓取路径的结果质量与稳定性弱于自托管 SearXNG 主路径。
- **浏览器回退未实现**：对 JS 渲染/反爬页面的浏览器兜底抓取只是提案，无任何
  代码、无任何测试、不在本栈承诺内（见 §6）。
- **无付费 API 配置**：整条栈默认无任何 API key；SearXNG 与 GitHub API（无认证
  60 次/小时量级，以响应头实时读数为准）即全部外部依赖。
- **本地嵌入模型不采用**：相似度/判定不用嵌入模型是既有决定（[13](13-similarity-comparators.md)、[14](14-similarity-and-signal-tests.md)），
  本调研未推翻它，选型中无嵌入组件。

## 3. 搜索服务商定性比较（非基准）

| 方案 | 定性定位 | 与本栈关系 |
|---|---|---|
| SearXNG 自托管 | 聚合上游引擎、无 key、数据经本地；上游引擎可见服务器 IP，引擎健康随网络出口波动 | **已选**，实测可用 |
| Exa | 语义检索 + 自有索引全文，托管 | 未选未测 |
| Brave Search API | 托管传统搜索，摘要 + 时效元数据 | 未选未测 |
| Tavily | 面向 LLM 的搜索 + 内容分块/可选全文 | 未选未测 |
| Jina Reader / Firecrawl | 托管抓取/渲染层（非搜索），注意默认缓存对溯源的影响 | 未选未测 |
| DDG HTML 抓取 | robots 与条款不合、实测存在封锁，仅理论兜底 | 排除为主路径 |

价格、免费额度、限速数字均未购买核实，本文一律不复述；需要时以各家官方页为准。
受信端点纪律不变：搜索只指向操作员配置的本地 SearXNG，绝不被模型或网页数据指向。

官方文档（口径以官网为准）：

- SearXNG JSON API：https://docs.searxng.org/dev/search_api.html
- trafilatura Python 用法：https://trafilatura.readthedocs.io/en/latest/usage-python.html
- GitHub REST 限流：https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- smolagents（搜索/抓取工具的最小形态参考）：https://github.com/huggingface/smolagents
- crawl4ai（抓取、内容过滤与封锁分类的模式参考）：https://github.com/unclecode/crawl4ai
- Exa：https://docs.exa.ai ；Brave：https://brave.com/search/api/ ；
  Tavily：https://docs.tavily.com ；Jina Reader：https://jina.ai/reader ；
  Firecrawl：https://docs.firecrawl.dev

## 4. 抽取与代理模式（源码级参考，未采纳为依赖）

调研核对了若干真实项目的实现模式，仅作设计参考，未引入任何
框架依赖：smolagents 的"搜索与抓取分离、截断显式标注"、crawl4ai 的"封锁检测
分层（状态码 + 结构特征）与缓存模式枚举"、browser-use/OpenHands 的"预算计数器
与循环检测"。Gleam 现实现（有界 Fetcher、预算在运行前校验、终报硬校验）已覆盖
其中与安全相关的核心；差距主要是 §6 的浏览器回退与缓存策略。

## 5. 案例状态（防误读）

- **wizznic**：investigate 冒烟已跑通。它是
  **已知答案案例的诊断性冒烟**，证明管道能走通、终报硬校验能拦住伪造；**不是
  heldout 验证**，不能据此宣称对未知包的发现能力。
- **librcd**：全栈冒烟终报给出 SourceForge 项目页（注册 homepage 的重定向落点）
  与 `RusXMMS/librcd` 两个候选。后续独立深查的
  结论是：该仓库与原作者**强关联**（fork 关系已排除），但它是**上游主库还是
  作者自建镜像之一仍未决**；作者另有的 suren.me cgit 当日不可达，仅有搜索快照
  支撑。任何"官方仓库已确认"的读法都不成立。

## 6. 未决与下一步（无承诺）

- 选型栈的**更广评测**未做：多包、多形态上游、与 Exa/Brave/Tavily 的同题对比
  基准均未运行；当前"可用"结论仅限已实测场景。
- 浏览器回退：保持提案状态；若未来实现，须先定义触发条件（何种封锁分类才升级）
  与证据溯源（浏览器渲染结果的证据等级）。
- 抽取质量：footer/作者下沉问题已有清理前捕获对策，但多站点下的召回率未量化。
- 搜索引擎子集依赖网络出口（部署验证时观察到的引擎矩阵是当日出网现实，不是永久清单）。

关联：[11 选型记录](11-decisions-and-open-questions.md)、[13](13-similarity-comparators.md)、
[14](14-similarity-and-signal-tests.md)；实现语义见仓库根 README 的 investigate 与
自托管搜索栈两节。
