# Gleam / 荧 — 最小可运行监控工作流

以 **live UDD 公共只读镜像**为包事实输入的最小实现：对 UDD 做有界固定模板
只读查询（QA cohort 枚举 + 逐包 source/upstream/metadata/packaging 事实）
→ 对少量声明的候选端点做有界只读 HTTP 观测 → 与上一次成功观测比较 →
追加式保存证据与人工审核报告 →（可选）调用本地模型做咨询性分诊与提案。
实现范围刻意保持最小，不包含身份合并、状态机、自动行动或常驻守护。

**仓库根 `raw/` 是历史存档，不是运行时输入**：它是 scripts/pull_udd.sh 于
2026-10-29 拉取的 UDD QA 快照（过滤 maintainer='Debian QA Group
<packages@qa.debian.org>' AND release='sid'），保留在磁盘上仅供历史分析
与实验报告复核（`gleam.snapshot.load_snapshot` 是其遗留读取器，活跃 CLI
不导入、无 `--raw` 开关、绝不静默回退）。注意与 **run 存储内的
`<store>/runs/<id>/raw/`** 严格区分：后者是每次运行的原始 HTTP/查询证据
（追加式、`gleam check` 可校验、永不删除）。

研究背景与边界见 [docs/research/00-overview.md](docs/research/00-overview.md)
（框架：[06](docs/research/06-framework-overview.md)，探针与安全边界：[09](docs/research/09-discovery-probes-and-ai.md)）。

## 快速开始

```bash
cd /home/hxue/Projects/gleam

# 1) 查看 live UDD 包事实与候选端点（对 UDD 镜像的有界只读查询；需要 psql）
python3 -m gleam inspect --limit 3
python3 -m gleam inspect --package librcd --json

# 2) 离线试运行（不发网页请求、不调模型；包事实读显式 --facts-cache，
#    未命中=诚实失败——live UDD 事实是活跃模式的必需输入，不再声称完全离线）
python3 -m gleam run --offline --package librcd --facts-cache facts-cache --store runs
#   （先在线跑一次写入缓存：run --package librcd --facts-cache facts-cache …）

# 3) 列出本地模型服务上的模型
python3 -m gleam models            # 默认 http://127.0.0.1:1234/v1

# 4) 真实小规模运行（默认 cohort 前 3 个包；含 robots 检查与基线比较）
python3 -m gleam run --package librcd --store runs
python3 -m gleam run --limit 3 --store runs     # cohort 枚举（有界）

# 5) 操作员手工候选 URL（必须绑定到恰好一个 --package）
python3 -m gleam run --package librcd --url https://github.com/RusXMMS/librcd --store runs

# 6) 校验证据存储
python3 -m gleam check --store runs

# 7) 工具调用型 Qwen 调查代理（多轮 tools 循环；本地模型；只读）
python3 -m gleam investigate --package wizznic --store runs

# 8) 完整调查栈（自托管 SearXNG + trafilatura；详见"自托管搜索栈"节）
uv venv .venv && uv pip install -r requirements.txt   # trafilatura==2.3.1（最小依赖）
scripts/searxng/start.sh                              # gleam-searxng @ 127.0.0.1:8888
.venv/bin/python -m gleam investigate --package librcd --store runs \
    --search-base http://127.0.0.1:8888               # 默认即此；GitHub API 默认启用

# 9) 抓取一个 Debian bug 的完整公开讨论（文档化 SOAP 机器接口；BTS PoW 见 bug-fetch 节）
python3 -m gleam bug-fetch --bug 620025 --store runs      # 默认 --bts-source auto

# 10) 对 UDD 公共只读镜像做有界固定模板查询（live；psql 子进程）
python3 -m gleam fetch-udd --list-datasets            # 18 个数据集目录（无 DB）
python3 -m gleam fetch-udd --source librcd --store runs
python3 -m gleam fetch-udd --source librcd --datasets sources,bugs,upload_history --store runs
python3 -m gleam fetch-udd --source cdrkit --groups source,upstream,metadata,packaging,bugs,history,wnpp --store runs

# 测试（合成微型 CSV + 假连接/假 runner，不触碰 raw/，不需要网络）
python3 -m unittest discover -s tests -t .
```

每次运行在 `<store>/runs/<UTC时间戳>-<uuid>/` 独占创建目录，写入
`run.json`（机器可读摘要）、`report.md`（人审报告）、`observations/*.json`
（逐条观测）、`raw/*`（原始响应字节/响应头/robots 原文，**含失败抓取的
部分响应**）、`models/*.json`（模型原始请求与响应、prompt 哈希）。
历史运行只追加、不修改；`<store>/index.json` 是可重建的基线索引缓存：
写入在 `index.lock`（flock）下先重读再合并（防并发丢更新），读取未命中时
以全量权威扫描为准（防过期缓存丢失最新基线）。`check` 做深度校验：逐条
解析观测、核对 id/run_id/outcome、重算 raw body 哈希、按内容比对 index。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（investigate：产出通过校验的 finish_investigation 终报） |
| 1 | run：完成但存在传输错误和/或模型错误；investigate：未产出受验终报（预算耗尽 / 模型错误 / 终报被拒 / 仅散文），部分轨迹保留；bug-fetch：抓取失败（错误分级与可用证据仍追加保存）；fetch-udd：UDD 查询失败（错误分级与已取得的 stderr/部分证据仍追加保存） |
| 2 | 用法错误（如 `--url` 未绑定 `--package`、`--offline` 无 `--facts-cache`、limit 越界；investigate 的非法包名——只做 Debian 源名策略校验，不查 UDD） |
| 3 | live UDD 事实源/存储/模型配置致命错误（run/inspect 的查询失败、未知包、cohort 外、缓存未命中——均不创建运行目录、绝不编造事实；investigate **不再使用**：零预取、无 cohort 检查，未知源名由 fetch_udd 如实返回 0 行） |

## investigate：工具调用型 Qwen 调查代理（实验性；不动既有 run 产物，共享 store 时基线索引会合并）

`python3 -m gleam investigate --package X` 让本地 Qwen（OpenAI 兼容原生
`tools`，非 response_format）**真正多轮**调查一个包的上游去向：模型每轮可调用
工具，工具结果按 `tool_call_id` 回填，直到调用 `finish_investigation` 或预算耗尽。
**零预取**：进入模型循环之前不对 UDD 做任何查询（不抓 facts、不枚举 cohort、
不校验成员资格，也不落 `udd-facts-<source>.csv`）——包名只做 Debian 源名策略
校验（非法 exit 2），UDD 数据只在模型调用 `fetch_udd` 时才存在。工具全部串行
执行、只读、经既有安全层，共 7 个：

| 工具 | 语义 |
|---|---|
| `fetch_url` | 经 `net.Fetcher` 的有界安全抓取（SSRF/DNS 钉扎/逐跳 robots/字节与请求限额全复用）；返回观测 ID、final URL、重定向链、可读正文与抽取链接。HTML 正文优先用**已安装的 trafilatura**（只喂给已抓回的字节，绝不用其下载器，SSRF 边界仍全在 Fetcher）；boilerplate 清理**之前**独立捕获锚文本/绝对 URL（去重保序）、mailto、footer、版权行作出处信号；trafilatura 缺失/失败/为空时回退标准库抽取，结果带 `extractor` 标签（`trafilatura` / `fallback-stdlib` / `fallback-empty-stdlib`），绝不冒充；短作者页不隐藏，正文与出处无论长短都保留。JSON/纯文本按原文返回不破坏 |
| `search_web` | 经**受信任本地 SearXNG**（`--search-base`，默认 `http://127.0.0.1:8888`，JSON API、GET）。独立于公网 Fetcher 的信任路径（操作员配置、scheme/凭据校验、有界字节/超时、**不跟随任何重定向**）；结果规范化（仅 http(s) 绝对 URL、去 fragment、按 URL 去重保名次、title/snippet 剥 HTML 标签、逐条引擎归属）；`unresponsive_engines` 原样透传；**0 结果且有引擎失败 = 显式歧义**（all-engines-failed-or-empty），403/429/captcha/畸形 JSON 各自显式分级，绝不把失败读成"无结果"，绝不伪造。搜索原始提供商 JSON 以观测落档（`source_kind: third-party-search-results`）；snippet 属第三方摘要，**弱于页面正文的证据**，引用可校验但候选需谨慎；搜索请求计入共享 `--max-requests` 预算 |
| `github_api` | 结构化 GitHub REST API 查询：`action ∈ {repo, readme, releases, commits}`，`owner/repo` 白名单字符校验、路径由应用拼接（模型不可控 base/path）；`page/per_page` 有界（1..100），`Link rel="next"` 仅在同源同路径前缀时解析为 `next_page` 页码，**绝不自动跟随**；请求经公网 Fetcher（SSRF/DNS 钉扎/robots/限额全套）并附写死的 `Accept: application/vnd.github+json` 与 `X-GitHub-Api-Version: 2022-11-28` 头（无认证、无 secret）；403/429 如实暴露 `x-ratelimit-*`/`retry-after`，不自动重试；**404 = 未知**（仓库可能私有/改名/无发布，绝不推断死亡或无发布）；README 经 `/repos/{owner}/{repo}/readme`（默认分支）取回并按 encoding 解码（base64），解码方式入 provenance 说明；repo 视图含 `html_url/homepage/archived/pushed_at/default_branch` 等，其中 html_url/homepage 进入候选证据 URL 集（可被引用为候选）；API 事实只是托管方记录，**不构成上游身份的官方证明** |
| `bug_fetch` | 取一个 Debian BTS bug 的**完整公开讨论**（如 WNPP 孤儿化/ITP bug 的全部日志）：`source` 三选 **auto（默认）| soap | mbox**——auto 先经**文档化机器接口** Debbugs/SOAP `get_bug_log`，仅当以资格错误（transport/timeout/http/soap-fault/parse/redirect/challenge 类）失败**且** robots.txt 明确允许 mbox 端点时回退官方 mbox 视图；rate-limited / not-found / 预算耗尽**不回退**；robots 未知/拒绝**不回退且无绕过开关**；显式 soap/mbox 失败即失败。逐来源 `attempts` 与 `source_selection` provenance 入档；请求预算在同一客户端实例（含回退）与 `--max-requests` 共享。BTS 专用受信路径：主机白名单 bugs.debian.org、bug_id 严格数字、URL/信封应用侧拼接、请求/字节/时间/消息/PoW CPU 预算齐全；连接层复用 net.py 的 SSRF/DNS 钉扎原语。逐消息保留 message-id/date/from/subject/正文（字符集/QP/base64 解码、边界与 sha256 出处、消息内 URL 进入该观测合格证据集）；**附件只登记元数据，不单独提取/写出/执行**（编码后的附件原文仍随留存的原始响应字节保留——mbox 路径的必然结果；SOAP attachments 数组未提取，如实披露）。bugs.debian.org robots 对 `User-agent: *` 全站 Disallow——网页接口（HTML/mbox 视图）默认不抓、明确 disallow 无绕过开关；2026-11-02 实测的 Varnish PoW 挑战（pow_challenge/pow_nonce cookie + SHA256 首字节为零）按文档化客户端流程有界求解，未识别形态显式 challenge-unknown，cookie 只存内存、落盘脱敏。详见 [docs/research/experiments/2026-11-02-bts-fetch/REPORT.md](docs/research/experiments/2026-11-02-bts-fetch/REPORT.md) |
| `fetch_udd` | 对 **UDD 公共只读镜像**（udd-mirror.debian.net，Debian 文档指给程序化访问的 SQL 服务）做**有界只读固定模板查询**（datasets= 新路径，template v3）：`action="list"` 返回 **18 个平等数据集**的目录（列/过滤/caveats；纯函数、无 DB 查询、不消耗预算）；`action="fetch"` 对调查中的包一次查询 **1-6 个数据集**（sources/upstream/upstream_metadata/vcswatch/urls/binaries/bugs/bugs_count/archived_bugs/wnpp/upload_history/upload_history_closes/removals/testing_autoremovals/popcon_src/ci/reproducible/lintian），**列式行 + 平行 `provenance` 数组**（`udd:<dataset>#row=N`），release 只作用于 release-scoped 数据集（sources/upstream/binaries/reproducible）。安全模型与旧 groups= 路径一致：psql 子进程（argv 无 shell）、输入只经 psql 变量、单只读事务 + `PGOPTIONS` 只读/超时、LIMIT limit+1 截断探测、stdout/stderr 有界。限额：单数据集默认 20 行/硬上限 50、重表（archived_bugs/lintian）有效上限 10、单次各数据集有效行合计 ≤120；调查级预算 `--udd-max-calls`（默认 6 次 fetch 调用）与 `--udd-max-rows`（默认 200 行）在 psql 之前强制（超限 = budget-exhausted 如实错误，绝不静默吞调用）；工具结果超 24k 字符时**行感知裁剪**（从最后一个仍有行的数据集丢最后一行、`trimmed`+`trim_note` 显式披露、被裁掉的行不可引用）。诚实语义：0 行=真实空结果（未知源名亦然——**没有 cohort 检查**）、失败=未知、截断=more_available；行内时间戳为服务端渲染原文，与 `fetched_at` 分离；DSN 为应用侧常量（模型不可控）。URL 字段（homepage/VCS/DEP-12/upstream_url/vcswatch url/browser）进入该观测合格候选集（数据库事实，弱于页面正文）。与 `bug_fetch` 之间零自动联动。standalone：`python3 -m gleam fetch-udd`。详见 [docs/research/experiments/2026-11-02-udd-fetch/REPORT.md](docs/research/experiments/2026-11-02-udd-fetch/REPORT.md) |
| `read_evidence` | 重读本轮已存观测的正文/链接；支持 `offset`（字符）与 `links_offset`（链接序号）分页，返回窗口元数据与 `next_offset`/`next_links_offset`——**不再重复返回相同前缀**；越界/负数/非整数 offset 显式报错 |
| `finish_investigation` | 结构化终报（候选 URL、支撑观测 ID、逐字引用、不确定性）；应用侧对照**本轮实际暴露给模型的证据**校验 |

终报硬校验（防伪造）：候选 URL 必须出现在**被引支撑观测自身**的合格 URL 集——
fetch 观测为其成功抓取的 requested/final URL + 该页抽取链接；**search 观测为规范
化的结果 URL**（第三方搜索，弱证据）；**github_api 观测为 API 提取的 html_url/
homepage 等**（不含 API 端点本身——那是数据源地址不是项目页面）；失败抓取仅回显
的 requested URL、以及其他未被引用观测中的 URL 一律不算——仅凭模型记忆或张冠李戴
的 URL 均拒绝；引用必须是**工具实际返回给模型的文本**（fetch/search/github 首次
返回的 payload，或 read_evidence 各偏移窗口逐步返回的部分；此后对话变长导致的
上下文截断另行披露、不影响引用有效性）的子串（空白归一后匹配）——服务端留存但
模型未见过的全文片段不算；观测 ID 与 **fetch_udd 行引用**（`udd:<dataset>#row=N`，
取自 fetch_udd 返回的 provenance 数组；仅本次调查实际暴露且未被 24k 裁剪丢弃的
行有效）必须存在——**快照时代的 `sources.csv#row=N` 引用无效**（快照非运行时
输入；零预取下没有 fetch_udd 调用就没有任何可引用行），旧 `snapshot_evidence`
字段也已是未知字段。被拒的终报获得有界修复机会
（`--max-report-repairs`，默认 2），仍失败则状态 `invalid-report`、exit 1。
run.json 的 `fact_citation_index` 记录每个 `udd:<dataset>#row=N` 出现在哪些
`observation_id` 中（同一 dataset 以不同 release 多次抓取时行号可能对应不同
行，审计按该索引回溯到具体观测与原始 CSV）。

预算（全部在创建运行目录之前校验，非法 exit 2）：`--max-rounds`（模型轮数，默认
8）、`--max-tool-calls`（默认 24；finish 不受限）、`--max-requests`（HTTP 请求
总数含 robots **和 SearXNG 搜索请求**，默认 12）、`--max-body-chars`（单工具结果
正文，默认 8000）、`--max-links`（默认 40，也用作搜索结果条数上限）、
`--max-context-chars`（对话序列化上限，默认 60000；超出对最长工具结果折半截断
——截断结果包成**合法 JSON 信封**（`truncated`/`note`/`kept_chars`/`text`），
工具消息永远是可解析 JSON——并在带内与 run.json 双重披露）、
`--udd-max-calls`（fetch_udd action=fetch 调用数，默认 6，上限 50）、
`--udd-max-rows`（fetch_udd 暴露行数总预算，默认 200，上限 2000；两项均在
psql 之前强制，超限 = budget-exhausted）、`--model-max-tokens`（每轮，默认
8192）、`--model-timeout`（默认 120s）、`--search-base`/`--search-timeout`
（受信任搜索 base 与超时，须 http(s) 无凭据，默认 `http://127.0.0.1:8888` /
10s）、`--github-api-base`（默认 `https://api.github.com`，操作员专用）、
`--no-search-web` / `--no-github` / `--no-bts` / `--no-udd`（如实禁用；
`--no-udd` 下 action=list 目录仍可用）、
以及 run 同款的抓取参数。
run.json 的 `counts.tool_calls_by_name` 保留每工具的调用数与错误数，
`parameters.udd_max_calls`/`parameters.udd_max_rows` 记录 UDD 预算。

安全与审计语义：每轮原始请求（含完整 messages 与 tools 数组）/响应/错误/工具调用
参数按唯一轮次 ID 落盘于 `models/inv-rNNN.json`；**每轮实际工具执行结果（成功/
失败/被拒的 finish_investigation 均含）另存于 `models/inv-tools-rNNN.json`**——
即使预算耗尽或终报被接受后没有下一轮请求回放，结果也不丢失；每次抓取按 run 同构
语义写 `observations/*.json` + `raw/*`（含失败的部分响应），`gleam check` 可深度
校验；assistant 回放只含 content 与 tool_calls（**绝不回放 reasoning_content**）；
`finish_reason=length` 与**畸形 tool_calls 信封**（缺 id/重复 id/function 非
对象/name 非字符串）均视为安全失败——该轮**不执行任何调用**；未知工具名 /
畸形 JSON / NaN、Infinity 参数一律拒绝执行并把错误作为工具结果回填（有界修复）。
模型端点仅来自操作员配置；工具目标 URL 永远走公网校验（模型无法指向本地端点/内
网）；无 shell、无本地文件、无模型可控 base。investigate 不调用 Laya。

**与 run 的存储关系（如实披露）**：investigate 不读取、不修改任何既有 run 产物
（追加式），但若与 run 共用同一 `--store`，其成功且未截断的观测会并入共享基线
索引 `index.json`——同 URL 的后续 run 比较将以该观测为基线（与 run→run 的基线
语义完全一致）。需要完全隔离时请用独立 `--store`。

**终报只是待人工复核的提案**：绝不自动确认上游身份、不执行任何行动；状态与退出码
区分 `completed` / `budget-exhausted` / `context-budget-exhausted`（折半截断后仍
超上下文硬上限，停机不发请求）/ `model-error`（含截断、畸形响应/畸形 tool_calls
信封、端点不可达）/ `invalid-report` / `no-final-report`（模型仅输出散文——原样
保留但不算发现）。

实际局限（如实声明）：`search_web` 依赖**本地 SearXNG 容器在运行**（见下节"自托管
搜索栈"）；容器未启动时搜索显式失败（connection-error），失败 = 未知，不构成"无结
果"的证明。trafilatura 未安装时 fetch_url 走标准库回退并在结果与 run.json
（`mode.extraction`）双重标注——功能可用但抽取质量降级，绝不冒充。

## bug-fetch：Debian BTS 公开讨论抓取（实验性，独立于 investigate 循环）

```bash
python3 -m gleam bug-fetch --bug 620025 --store runs        # --bts-source auto（默认）
python3 -m gleam bug-fetch --bug 620025 --bts-source soap --store runs   # 显式单来源
python3 -m gleam bug-fetch --bug 1103795 --store runs       # 完整讨论（多消息）
python3 -m gleam check --store runs                         # 证据深度校验
```

按 bug 编号取回完整公开讨论，追加式落盘（observations + raw 原始字节 +
逐消息结构化 discussion.json + run.json/report.md；与 run/investigate 同构，
`check` 可校验）。`--bug` 严格数字；URL 由应用侧从数字重建（主机白名单仅
bugs.debian.org）。**来源三选语义**：`--bts-source` 取
`auto（默认）| soap | mbox`。auto 先走文档化机器接口 Debbugs/SOAP
`get_bug_log`（robots.txt 是爬虫排除协议，对 `User-agent: *` 全站
Disallow 的部分是网页接口——SOAP 是文档明确指给程序的 API，每次运行的
robots 原文事实仍如实入档）；SOAP 仅以**资格错误**（transport/timeout/
http/soap-fault/parse/redirect/challenge 类）失败**且** robots 明确允许
mbox 端点（如 robots 404=无限制）时才回退 `mbox=yes` 视图——rate-limited /
not-found / 预算耗尽**不回退**；robots 未知或拒绝**不回退、绝不绕过**
（`--bts-web-anyway` 在 auto 模式被忽略）。显式 `soap`/`mbox` 失败即失败
（无回退）；mbox 的 robots 门槛语义不变：明确 disallow 即拒绝（无绕过
开关），robots 状态未知时默认拒绝、仅 `--bts-web-anyway` 显式接受未知态
才抓。成功结果带逐来源 `attempts` 与 `source_selection`（requested/tried/
fallback_used/fallback_reason）；auto 全失败时错误取最后失败来源的分级、
`attempts` 保留完整轨迹；回退共享同一客户端请求预算。错误诚实分级：
not-found / rate-limited / challenge / challenge-unknown / robots-disallow /
redirect / timeout / truncated 各自可辨。
PoW 挑战（2026-11-02 实测：Varnish 302 → /.internal/challenge.html，
pow_challenge cookie，SHA256(challenge;tries) 首字节为 0，pow_nonce cookie 回答）
按文档化客户端流程有界求解（CPU 秒数/请求预算内），求解 live 证据见实验报告。
附件只登记元数据（文件名/类型/大小/摘要），不单独提取/写出/执行——编码后的
附件原文仍随留存的原始响应字节保留（mbox 路径的必然结果）；SOAP attachments
数组未提取（如实披露，不做投机解析）；挑战会话
cookie 只存内存，落盘响应头脱敏。

## fetch-udd：UDD 公共只读镜像 live 查询（独立 CLI + investigate 工具）

```bash
python3 -m gleam fetch-udd --list-datasets              # 目录（无 DB、无 run 目录）
python3 -m gleam fetch-udd --source librcd --store runs # legacy groups=（默认五组，v2）
python3 -m gleam fetch-udd --source librcd \
    --datasets sources,upstream,upstream_metadata,vcswatch,bugs,wnpp \
    --release sid --limit 20 --store runs               # datasets=（template v3）
python3 -m gleam fetch-udd --source cdrkit \
    --groups source,upstream,metadata,packaging,bugs,history,wnpp \
    --release sid --limit 50 --store runs               # legacy 全组（v2 字节不变）
python3 -m gleam check --store runs
```

`--list-datasets` 打印 18 个数据集目录后退出（纯函数：无 DB 查询、不创建
运行目录）；`--datasets`（逗号分隔）与 `--groups` **互斥**（同传 exit 2），
两者都不给时默认 legacy 五组。`--source` 除 `--list-datasets` 外必填。

对文档化公共只读镜像 `postgresql://udd-mirror.debian.net/udd`（凭据公开，
见 docs/research/02-udd-handbook.md）做**有界只读固定模板查询**：psql
子进程（argv 列表、无 shell；`-X -q --csv -w -v ON_ERROR_STOP=1`，SQL 脚本
经 stdin——`-c` 不做 psql 变量插值，live 验证后弃用）；输入只经 psql 变量
进入模板（`:'source'`/`:'release'` 带引号安全插值；limit 为 Python 侧
严格 int 校验后的插值），包名按 Debian 策略正则、groups/datasets/release
按严格允许清单、DSN 形态校验（`--udd-dsn`/`GLEAM_UDD_DSN` 仅供操作员；模型
不可控）。只读多层强制：`PGOPTIONS` `statement_timeout=15s` +
`default_transaction_read_only=on`（live 验证 CREATE 被拒）+ 单
`BEGIN;...COMMIT;` 事务 + 镜像账号无写权限。有界：≤9 条语句/次（datasets=
路径 meta+6 数据集 ≤ 7 条）、每组/数据集 `LIMIT limit+1` 截断探测
（more_available 显式披露）、stdout/stderr 各 8 MiB select 循环有界读取
（超限 kill + output-too-large 诚实报错）、整个调用 45s 墙上超时。

查询组（legacy `--groups`，template v2，字节不变）：`source`（sources 表逐
release 多行保留）/`upstream`（uscan 状态）/`metadata`（DEP-12）/`packaging`
（vcswatch）/`bugs` 默认，`history`（upload_history）/`wnpp` 可选；**逐表各自
成行、不 JOIN**（手册 §4 的 join 乘法/sources.bin 逗号文本坑）。`bugs` 组覆盖
**源包与映射二进制包**的开放报告（status<>'done'）：匹配 = BTS source 映射 OR
src: 伪包 OR bugs_packages 多包关联 OR packages 二进制→源码映射；每行保留
package/source 原值与 `via_source_mapping`/`via_bugs_packages` 关联出处布尔；
id/标题/状态/严重度/tags/arrival/last_modified/done 齐全，标题**不做关键词
过滤**；完整讨论不在此工具内（按 id 另调 `bug_fetch`，二者零自动联动）。
`bugs` 组恒附带 `binaries` 子行组（packages 表的二进制清单，逐 package×release
去重，作为 bug 关联出处展示）。

数据集路径（`--datasets`，template v3）：**18 个平等数据集**（无核心/可选之分，
目录见 `--list-datasets`），单次 ≤6 个、各数据集有效行上限合计 ≤120、默认
limit 20 / 硬上限 50（重表 archived_bugs/lintian 有效上限 10）；**每行带
`provenance = "udd:<dataset>#row=N"`**；release 过滤只作用于 release-scoped
数据集（sources/upstream/binaries/reproducible），其余忽略 release；0 行=
真实空结果（removals/testing_autoremovals/upstream_metadata 等常见，未知源名
亦然）、失败=未知、截断=more_available，三者严格区分。

诚实语义（两路径一致）：0 行=真空结果（note 明示）、失败=未知（绝不读作
"无数据"）、截断=more_available；行内时间戳为服务端渲染原文、与 `fetched_at`
（客户端时刻）严格分离；模板版本 + 输入 + 原始 CSV（含 sha256，经观测载荷
哈希由 `gleam check` 重算校验——body 与 csv 引用同等待遇）入档。
与 run/investigate 共用 `--store` 时，成功的 fetch-udd 观测同样并入共享
基线索引（键为脱凭据 DSN 形态；HTTP 探测永不指向该键，语义无害但如实
披露）。如实限制：psql --csv 下 NULL 与空串不可区分；
`upstream.watch_file` 自模板 v2 起逐字抓回（原样保留、永不执行；工具层
展示截断，完整原文在落档原始 CSV 中）。investigate 工具版走 datasets= 路径
（action=list|fetch；`--no-udd` 下 list 仍可用、fetch 如实禁用；release 默认
操作员 `--release`、limit ≤50；调用数/行数预算见 investigate 节），URL 字段
（homepage/VCS/DEP-12/upstream_url/vcswatch url/browser）进入该观测合格
候选集（数据库事实，弱于页面正文）。live 证据（cdrkit 88 行含二进制包
bug #267233 关联出处、librcd 空 bugs 如实空、check 通过）见
[docs/research/experiments/2026-11-02-udd-fetch/REPORT.md](docs/research/experiments/2026-11-02-udd-fetch/REPORT.md)。

## 自托管搜索栈（SearXNG）+ trafilatura：最小部署

用户选定的调查栈 = 自托管 SearXNG（localhost 隔离）+ 既有安全 Fetcher +
trafilatura（仅已抓回字节）+ GitHub API 工具，全部在既有 investigate 循环内。

```bash
# 1) Python 依赖（uv；不碰系统 pip）。锁定 trafilatura==2.3.1，依赖刻意最小
cd /home/hxue/Projects/gleam
uv venv .venv && uv pip install -r requirements.txt   # 或 uv sync
.venv/bin/python -m gleam investigate --package librcd --store runs

# 2) SearXNG 容器（操作员脚本；Docker 仅 docker run，无 compose/特权/socket 挂载）
scripts/searxng/start.sh    # 幂等：容器已在跑则复用；检查 127.0.0.1:8888 占用与
                            # 父容器归属，拒绝动无关容器；secret 生成进
                            # scripts/searxng/.searxng.env（git 忽略，不入库）
scripts/searxng/stop.sh     # 只停 gleam-searxng（校验容器名+镜像归属后才删）

# 3) 就绪与真实查询冒烟（部署侧执行，证据存 /tmp/opencode/gleam-stack-smoke）
curl -s http://127.0.0.1:8888/healthz
curl -s 'http://127.0.0.1:8888/search?q=librcd+debian&format=json' | head -c 400
```

部署要点：官方 `docker.io/searxng/searxng` 镜像（`docker run -d --name gleam-searxng
-p 127.0.0.1:8888:8080`，**仅绑定回环**，无 host networking、无特权、无 docker
socket 挂载；同机 8080 的无关容器 originblame-demo 不受影响）；`settings.yml`
启用 `search.formats: [html, json]`、关闭 limiter（仅本机）、保守引擎子集
（keep_only：无 key、无 captcha 绕过的引擎；实际以 JSON 查询验证可用为准，
`unresponsive_engines` 会如实报告）；当前运行的镜像 digest 记录在
`scripts/searxng/digest.txt`（git 忽略）与部署冒烟证据中。既有 `runs/` 历史证据
只追加、不修改；冒烟使用隔离 store `/tmp/opencode/gleam-stack-smoke`。
实时部署状态、resolved digest 与 librcd 实测调查证据由部署冒烟步骤写入
`/tmp/opencode/gleam-stack-smoke/`（命令与结果原样留存）。


## 数据语义（重要）

- **包事实来自 live UDD 固定模板查询**（`--release` 默认 `sid`=QA cohort
  口径；QA 过滤与历史 pull_udd.sh 同为 maintainer='Debian QA Group
  <packages@qa.debian.org>' AND release='sid'）。选择方式两种：显式
  `--package` = 逐包快速路径（不枚举 cohort）；仅 `--limit N` = cohort
  枚举（`DISTINCT ON (source)` 取最高版本、`ORDER BY source` 确定性行序、
  `LIMIT N+1` 截断探测、N 有界 ≤100）。包名按 Debian 策略
  （`^[a-z0-9][a-z0-9+.-]+$`）校验；未知包（0 行）与非 QA 维护包分别以
  unknown-package / not-in-cohort 致命拒绝（exit 3、不创建运行目录、
  绝不编造事实）。
- **候选端点只来自 live 事实字段与操作员 `--url`**：`homepage-candidate`
  （udd:sources.homepage）、`dep12-repository` / `dep12-repository-browse`
  （udd:upstream_metadata 的 DEP-12 长表，分别标注）；`upstream_url` 只登记
  为 `upstream-artifact`，**不抓取**（可能是压缩包，策略禁止下载制品）。
- **sources / vcswatch 的 VCS 字段是 Debian 打包出处（packaging provenance）**：
  不构成上游身份的证明或反证（上游与打包仓库可能共址），报告中按
  `debian-packaging-vcs` 展示，绝不进入上游候选。
- live 事实行出处为 `udd:<table>#row=N`（模板 ORDER BY 下的确定性行号），
  绑定该次抓取的 `template_version` + `fetched_at` + 原始 CSV 响应 SHA-256
  （记入 run.json 的 input_provenance；investigate 的 fetch_udd 数据集行则用
  `udd:<dataset>#row=N` provenance 数组、只经终报 fact_evidence 引用）。
  同名多版本行全部保留（不按 source 去重覆盖）；重复 `--package` 自动去重。
- `watch_file` 由 live 模板 v2 逐字抓回，原样保留在事实中，**从不执行**
  uscan 或其中的正则/URL。
- **离线语义（如实声明）**：`--offline` = 不发网页请求、不调模型，包事实
  **只读显式 `--facts-cache DIR`**（先在线运行写入；未命中=诚实致命失败，
  绝不回退 live、绝不编造）。活跃模式需要 live UDD 可达（psql + 网络），
  本项目**不声称完全离线**可用；缓存条目披露原始 fetched_at，可能过期。
- outcome 词表沿用 09 §6（示意，未冻结）：`OK` / `NOT_MODIFIED` /
  `REPRESENTATION_CHANGED` / `TRANSPORT_ERROR` / `ABSTAIN`，另有 `NOT_PROBED`
  （登记但不抓取的候选）与 `HTTP_ERROR`（4xx：HTTP 交换完成但目标报错；
  保留响应、不作死亡推断、不成为基线）。**探针错误 = 未知，绝不推断上游死亡；
  字节哈希变化仅为表示层变化，不是活性结论。**
- 基线只与同端点**上一次成功且未截断**观测比较；失败扫描、HTTP_ERROR 与
  截断 body 都不成为基线。**304 Not Modified 沿用基线哈希**（绝不用空 body
  哈希覆盖好哈希）；本次或基线任一侧截断时 comparison 记
  `unknown-truncated-body`。

## 网络安全边界

- 仅 http(s)；拒绝 URL 内凭据与畸形 URL（含坏 IPv6 字面量、非数字/越界端口——
  统一按 unsafe-destination 弃权，单条坏端点不中断整轮）；每一跳重定向都重新
  走完整校验。
- SSRF 防护：DNS 解析结果必须为公网单播地址（拒绝环回/内网/链路本地/保留/组播，
  含 IPv4-mapped IPv6 拆包），随后 `http.client` 连接**钉住的 IP**，TLS 仍按原主机名
  做 SNI 与证书校验（防 DNS 重绑定）。不使用代理环境变量。
- 上限可配：`--max-bytes`（默认 256 KiB）、`--timeout`（15s）、`--max-redirects`（3）、
  `--max-requests`（每次运行 10，与内部默认一致）、同主机最小间隔（默认 1s）；
  一切限额（含 NaN/负数/超界）在创建任何运行目录之前校验，非法 exit 2。
- robots.txt：**每一跳（含重定向目标，同主机换路径也算）在 GET 前逐一判定**；
  规则按 origin 解析并缓存（同 origin 新路径不发新请求，robots 抓取自身不做
  robots 检查、无递归）；disallow / 401/403/5xx / 抓取中途失败 / 其他非 2xx 状态
  一律弃权（ABSTAIN）；404/410 视为无限制；robots 原文按 origin 哈希命名留存
  （IDN 安全），并在观测 `raw_refs.robots` 中引用。目标主机级失败（DNS 解析失败、
  连接拒绝、地址不安全）按原始传输错误呈现，不误标为 robots 弃权。
- 失败抓取同样保留已取得的响应头与（部分）body（`raw_refs.body/headers`）。
- **run 模式**无任何自动开放网页搜索；模型输出、网页内容都不会触发抓取
  （**investigate 模式例外**：模型通过 fetch_url / github_api 工具显式发起抓取，
  每次仍走本节全部安全层；search_web 走**受信任本地 SearXNG** 的第三条独立
  代码路径——见下条，见 investigate 一节）。
- 受信任端点共有三个，全部仅来自操作员配置、与不可信探针 URL 严格分路：本地
  模型端点（`--model-base` / `--laya-base`）、本地 SearXNG 搜索（`--search-base`，
  有界、不跟随重定向、绝不被模型参数指向）、GitHub API base（`--github-api-base`，
  但其**请求本身**走公网 Fetcher 全套 SSRF/robots/限额）。三者绝不来自网页或
  CSV 数据。

## 模型（可选）

- **live UDD 事实输入**（inspect/run 共用；investigate 零预取、不经此路径——
  其 UDD 访问只发生在模型调用 fetch_udd 时，原始 CSV 以该工具观测的
  `raw/<obs_id>.udd.csv` 落档）：对文档化公共只读
  镜像的 psql 子进程查询——argv 列表无 shell、固定 SQL 模板 + psql 变量
  （`:'source'`/`:'maintainer'`/`:'release'` 带引号插值，limit 为 Python 侧
  严格 int）、单 `BEGIN;...COMMIT;` 只读事务 + `PGOPTIONS`
  statement_timeout/default_transaction_read_only、LIMIT limit+1 截断探测、
  stdout/stderr 有界读取与墙上超时。DSN 仅操作员可配（`--udd-dsn`/
  `GLEAM_UDD_DSN`）；cohort 的 maintainer 是应用侧常量（不可配置）；
  release 走严格允许清单。**每次 run 把事实输入的原始 UDD
  CSV 追加落盘**为该次运行的 `raw/udd-facts-<source>.csv`（cohort 枚举
  另存 `raw/udd-cohort.csv`），provenance 中的 `response_sha256` 即其
  SHA-256（offline 缓存读取同样落盘对应缓存内容，哈希链一致）；inspect
  为查看型命令、不建运行目录、不落证据。
- `--offline`：不发网页请求、不调模型；包事实只读显式 `--facts-cache`
  （缓存条目含 result 与原始 CSV 的双重 SHA-256 校验，篡改/损坏一律
  `cache-corrupt` 拒绝，绝不冒用原始 fetched_at）。
  `--no-model`：确定性运行，不调模型（事实仍为 live）。
- **Qwen**（默认 `qwen3.8-27b`，`/v1/chat/completions` + JSON structured output）：
  只产出枚举 action 的提案与摘要，必须引用本次运行的观测 ID，应用侧校验引用与
  schema；截断（finish_reason=length）、空内容、畸形 JSON、引用不合规一律按错误
  弃权并保留原始响应。提示词含 distrust-web-data 指令。
  - 输出预算与超时可配：`--model-max-tokens`（默认 8192、上限 65536）、
    `--model-timeout`（默认 120s、上限 600s）；非法值（0/负数/NaN/Inf/超上限）
    按用法错误拒绝（exit 2）。**单次调用、无自动重试**；有效预算同时记录在
    `models/qwen-*.json` 的 `extra` 与请求载荷中。
  - 预算依据（2026-11-01 实测）：2048 预算下 qwen3.8-27b 的 reasoning 耗尽
    completion_tokens（reasoning_tokens=2007/2048），content 为空且
    finish_reason=length（证据：独立冒烟
    `/tmp/opencode/gleam-independent-smoke` 同日运行）；故默认提高至 8192。
    若仍截断，报告会如实标注 truncated 并以 exit 1 显式失败。
- **Laya**（独立可配 `--laya-base`，默认 `http://127.0.0.1:1234`，
  `POST /v1/systemone`，`{model, state, questions.triage}`，期望
  `answers.triage.{choice, probabilities}`）：仅咨询（advisory-only），**任何选择都
  不跳过任何包**，Qwen 照常复核。状态文本截断会披露。模型无元数据修订信息可核。
  - 实测注记（2026-11-01）：按文档载荷的多轮本地调用均成功返回 choice +
    probabilities（含 librcd 两次独立冒烟）；早前另一次曾返回
    `Unexpected endpoint or method`。错误路径已实现并有测试覆盖：
    原始错误完整保留在 `models/laya-*.json`，运行继续用 Qwen（fallback 记录在案），
    退出码非 0。接口可用性以当次调用为准，帮助文案不写死状态。
- 两次调用的原始请求/响应、模型 ID、prompt SHA-256 全部落盘；模型输出只是待人工
  复核的提案，不执行任何动作。

## 局限（明确不做）

- 无自动身份合并 / 实体图谱 / 状态机 / TTL / 阈值；无邮件、MR 或任何自动行动。
- 无常驻守护；如需周期运行可自行 cron，例如：
  `17 3 * * * cd /path/to/gleam && python3 -m gleam run --limit 3 --store runs >> runs/cron.log 2>&1`
- run 模式仍不做 GitHub API 探测（investigate 的 `github_api` 工具已实现结构化
  repo/readme/releases/commits 查询；run 保持纯 HTTP 观测语义，不把 API 记录或
  HTML 变化解读为代码活动）。
- 活跃事实源 = live UDD 的 source/upstream/metadata/packaging 四组固定模板
  （其余 UDD 表与完整实体 schema 不在事实输入内；fetch_udd 工具/fetch-udd CLI
  的 bugs/history/wnpp 组属调查扩展，不回写任何事实）。仓库根 `raw/` 归档
  快照仅供历史分析，活跃路径不读取、也不回写。
- 每次运行需 UDD 镜像可达（psql + 网络）；`--offline` 只覆盖网页观测与模型，
  事实依赖显式缓存——**不是完全离线系统**（如实声明）。
- 观测语义与词汇表（09 §6）未定稿；本实现按"非主张"口径记录。
- Laya 的 probabilities 未校准、不可作为阈值依据（09 §5：自评分不是校准概率）。

## 布局

```
gleam/            # CLI 包：python3 -m gleam
  facts.py        # 包事实数据模型（Fact/Candidate/PackageFacts；快照与 live 两表族共用）
  liveudd.py      # live UDD 事实源：QA cohort 枚举 + 逐包 facts 抓取适配 + 显式离线缓存
  udd.py          # UDD 公共只读镜像 live 查询（psql 固定模板：groups= v2 +
                  # datasets= v3（18 数据集/目录/逐行 provenance）、cohort
                  # 固定口径、只读事务、有界子进程读取器）
  snapshot.py     # 历史归档快照读取器（legacy；root raw/ 仅供历史分析，非运行时输入）
  net.py          # 有界安全抓取器（SSRF/DNS 钉扎/重定向/robots/限额；支持工具层写死的附加头）
  store.py        # 追加式证据存储与基线索引
  observe.py      # 观测引擎（outcome 判定、基线比较、原始留存）
  models.py       # Qwen / Laya 客户端（校验、弃权、原始保留）
  agent.py        # investigate：工具调用型多轮调查代理（tools 循环）
  search.py       # 受信任本地 SearXNG 适配器（独立信任路径：不跟随重定向、诚实分级）
  extract.py      # trafilatura 抽取（仅已抓回字节）+ 标准库回退 + 清理前出处捕获
  github.py       # GitHub API 工具辅助（白名单路径构造/整形/Link 分页/限流头）
  bts.py          # Debian BTS 讨论抓取（auto/soap/mbox 三选、Varnish PoW 客户端、附件仅元数据）
  report.py       # run.json + report.md 渲染（run 与 investigate 两式；live UDD 出处）
  cli.py          # inspect / models / check / run / investigate / bug-fetch / fetch-udd
tests/            # unittest + 合成 fixtures（快照微型 CSV + live UDD 假 psql；不依赖网络与 raw/）
raw/              # 历史归档 UDD 快照（2026-10-29；非运行时输入；scripts/pull_udd.sh 产出）
runs/             # 默认证据存储（运行产物，git 忽略；runs/<id>/raw/ 是追加式证据）
scripts/searxng/  # SearXNG 自托管：settings.yml（JSON 格式+保守引擎，无 secret 入库）
                  # 与幂等 start/stop 脚本（部署侧；.searxng.env/digest.txt git 忽略）
pyproject.toml    # uv 项目：依赖刻意最小（trafilatura==2.3.1）；requirements.txt 同步锁定
.venv/            # uv venv（git 忽略）；CLI：.venv/bin/python -m gleam investigate
```

Python 3.14（3.11+ 可运行）。核心仅标准库；第三方依赖刻意最小：仅 trafilatura
（未安装时 fetch_url 走标准库回退并如实标注，测试在两种环境都可跑）。测试：
`python3 -m unittest discover -s tests -t .`（420+ 个用例，其中 2 个真实
trafilatura 抽取用例在未安装时自动 skip；覆盖 CSV 多行/重复行、VCS≠上游、
追加式与基线语义、SSRF/钉扎/重定向/字节上限、robots、模型畸形输出与引用校验、
模型预算/超时校验与透传、Laya 错误回退、offline（缓存写/离线读回环/未命中
诚实失败/离线零查询）/no-model、CLI 参数错误与帮助文案回归，以及 investigate
的合成多轮工具循环、tool_call_id 回填、未知工具/
畸形与非有限 JSON 参数、截断不执行、预算耗尽轨迹保留、伪造引用/记忆 URL/未知
观测拒绝、fetch_udd 行引用（`udd:<dataset>#row=N` 有效——仅实际暴露行、
伪造行号与被 24k 裁剪丢弃的行拒绝、快照时代
`sources.csv#row=N` 及旧 `snapshot_evidence` 字段拒绝）、**零预取**
（模型首次 fetch_udd 之前 FakeUddRunner 零调用、run.json 无
input_provenance）、action=list 无 DB 无观测、未知/缺失数据集与 >6 数据集
拒绝、调用数/行数预算在 psql 之前强制、列式行 + 平行 provenance、24k 裁剪
保持合法 JSON、上下文截断披露、
搜索能力诚实失效（不可达/403/429/重定向/畸形 JSON/
全引擎失败歧义/去重保名次/引擎归属/共享请求预算）、github_api（白名单路径/
固定头/分页有界与 Link 同源校验/404=未知/限流头透传/证据 URL 集成/禁用如实
报告/非法 owner 不触网）、trafilatura 抽取与回退标注/清理前出处捕获（锚点/
mailto/footer/版权行）/链接去重、read_evidence 偏移翻页与越界拒绝、
counts.tool_calls_by_name 保留、存储 check 通过、畸形 tool_calls 信封（缺/
重复 id、function 非对象）整轮不执行、unhashable 引用元素拒绝、上下文硬上限
停机、URL/引用绑定到被引观测自身与实际暴露文本、每轮工具结果独立落盘；
**live UDD 事实源**（udd.fetch_cohort 固定口径：maintainer 常量不可配置/
release 允许清单/limit 严格整数有界/确定性行序/limit+1 截断探测/只读事务
脚本无写语句/argv 无 shell/PGOPTIONS 只读/凭据脱敏/连接与超时诚实分级/
空 cohort=如实空；liveudd 适配器：homepage/Repository-Browse=web 候选、
Repository=仅登记、upstream_url=制品仅登记、打包 VCS 绝不进上游候选、
watch_file 逐字保留、`udd:*#row=N` 出处；成员判定 unknown-package/
not-in-cohort 拒绝且不编造；选择：指定包名快速路径不枚举 cohort、cohort
limit 有界且披露 more_available；FactsCache：写后离线读回环/未命中=诚实
失败/损坏拒绝/在线配置缓存仍 live 抓取；**no-root-raw**：--raw 旗标移除
（parse 拒绝）、load_snapshot 炸弹补丁下 inspect/run 全流程通过、cli 不再
暴露快照加载器、live 事实不混入 .csv 表名）；run 报告 live 出处渲染
（无"输入快照"声称、归档 raw 与 run raw 区分声明、离线缓存披露）；
bug_fetch/BTS（严格数字 bug_id、应用侧拼 URL/主机白名单、mbox/SOAP 解析
含 multipart/字符集/QP/base64/附件仅元数据与"编码原文仍留原始件"披露/
SOAP attachments 数组不解析且嵌套 item 不算消息/SOAP 实体单次解码回归/
解析缺陷披露、Debian Varnish
PoW 与 Anubis 挑战的检测/有界求解/cookie 不落盘/未识别形态诚实失败、
robots 门槛与无绕过开关、429/404/redirect/soap-fault→not-found 分级、
共享请求预算、CLI 落盘与 check 通过、禁用如实报告、**auto 来源回退矩阵**
（SOAP 成功不回退/资格错误且 robots 允许才回退并留 attempts 轨迹/
rate-limit、not-found、预算耗尽不回退/robots 拒绝与未知不回退且无绕过
开关/auto 双失败带双来源轨迹/显式选择无回退语义/非法 source 拒绝））；
fetch_udd/UDD（包名/groups/release/limit/DSN 严格允许清单与注入串拒绝且
不触网、固定模板单只读事务脚本、多语句 CSV 解析（多行字段/逗号引号/
meta）、逐 release 多行保留、空 sources/空 bugs=如实空结果、limit+1
more_available 截断、bugs 关联出处（via_* 布尔、package≠source 原样、
时间戳原文）、psql 错误分级（connection/query/timeout/output-too-large/
psql 缺失/头行前数据；LC_ALL=C 固定错误语言与 psql 18 措辞回归）、有界
子进程读取器（真本地子进程的正常/超时 kill/
输出超限 kill/stdin 送达/spawn 失败）、候选 URL 抽取去重、CLI 落盘与
原始 CSV 原样留存 + check 通过、check 对 csv 引用重算哈希（篡改检测）、
fetch-udd CLI 的 --list-datasets（无 run 目录）/--datasets（v3，datasets
键与 provenance 行）/legacy --groups（v2 默认值不变）/互斥与缺 --source
拒绝、investigate fetch_udd 工具（禁用与错误如实/字段截断仅在展示层/
证据 URL 注册/预算强制/列式输出/裁剪语义））。
