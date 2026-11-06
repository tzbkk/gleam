# Gleam / 荧：最小可运行监控工作流

Gleam 以 live UDD 公共只读镜像为包事实输入，实现一条最小可运行流程：对 UDD 做有界固定模板只读查询（QA cohort 枚举 + 逐包 source/upstream/metadata/packaging 事实），对少量声明的候选端点做有界只读 HTTP 观测，与上一次成功观测比较，追加式保存证据与人工审核报告，并可选调用本地模型做咨询性分诊与多轮工具调用调查。实现范围刻意保持最小：不含身份合并、状态机、自动行动或常驻守护。

研究背景与边界见 [docs/research/00-overview.md](docs/research/00-overview.md)（框架：[06-framework-overview.md](docs/research/06-framework-overview.md)，探针与安全边界：[09-discovery-probes-and-ai.md](docs/research/09-discovery-probes-and-ai.md)，UDD 手册：[02-udd-handbook.md](docs/research/02-udd-handbook.md)）。

## 快速开始

```bash
cd /path/to/gleam

# 1) 查看 live UDD 包事实与候选端点（需要 psql）
python3 -m gleam inspect --limit 3
python3 -m gleam inspect --package librcd --json

# 2) 离线试运行（不发网页请求、不调模型；包事实读显式 --facts-cache，未命中即失败）
python3 -m gleam run --offline --package librcd --facts-cache facts-cache --store runs

# 3) 列出本地模型服务上的模型（默认 http://127.0.0.1:1234/v1）
python3 -m gleam models

# 4) 真实小规模运行（含 robots 检查与基线比较）
python3 -m gleam run --package librcd --store runs
python3 -m gleam run --limit 3 --store runs          # cohort 枚举（有界）

# 5) 操作员手工候选 URL（必须绑定恰好一个 --package）
python3 -m gleam run --package librcd --url https://github.com/RusXMMS/librcd --store runs

# 6) 校验证据存储
python3 -m gleam check --store runs

# 7) 工具调用型调查代理（多轮 tools 循环；本地模型；只读）
python3 -m gleam investigate --package wizznic --store runs

# 8) 抓取一个 Debian bug 的完整公开讨论（默认 --bts-source auto）
python3 -m gleam bug-fetch --bug 620025 --store runs

# 9) UDD 公共只读镜像有界固定模板查询（live；psql 子进程）
python3 -m gleam fetch-udd --list-datasets           # 18 个数据集目录（无 DB）
python3 -m gleam fetch-udd --source librcd --store runs
python3 -m gleam fetch-udd --source cdrkit --groups source,upstream,metadata,packaging,bugs,history,wnpp --store runs

# 10) 完整调查栈（自托管 SearXNG + trafilatura，见下文对应小节）
uv venv .venv && uv pip install -r requirements.txt
scripts/searxng/start.sh
.venv/bin/python -m gleam investigate --package librcd --store runs

# 测试（合成 fixtures，不需要网络）
python3 -m unittest discover -s tests -t .
```

## 运行产物

每次运行在 `<store>/runs/<UTC 时间戳>-<uuid>/` 独占创建目录，写入 `run.json`（机器可读摘要）、`report.md`（人审报告）、`observations/*.json`（逐条观测）、`raw/*`（原始响应字节/响应头/robots 原文，含失败抓取的部分响应）、`models/*.json`（模型原始请求与响应、prompt 哈希）。历史运行只追加、不修改。`<store>/index.json` 是可重建的基线索引缓存：写入在 `index.lock`（flock）下先重读再合并，读取未命中时以全量权威扫描为准。`gleam check` 做深度校验：逐条解析观测、核对 id/run_id/outcome、重算 raw body 哈希、按内容比对 index。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 完成但存在错误（传输/模型错误、未产出受验终报、抓取或查询失败；错误分级与已取得的证据仍追加保存） |
| 2 | 用法错误（参数缺失、非法或越界，如 `--url` 未绑定 `--package`、`--offline` 无 `--facts-cache`、limit 越界） |
| 3 | live UDD 事实源/存储/模型配置致命错误（查询失败、未知包、cohort 外、缓存未命中；不创建运行目录） |

## investigate：工具调用型调查代理（实验性）

`python3 -m gleam investigate --package X` 让本地 Qwen（OpenAI 兼容原生 `tools`，非 response_format）多轮调查一个包的上游去向：模型每轮可调用工具，结果按 `tool_call_id` 回填，直到调用 `finish_investigation` 或预算耗尽。零预取：进入模型循环之前不对 UDD 做任何查询（不抓 facts、不枚举 cohort、不校验成员资格），包名只做 Debian 源名策略校验（非法 exit 2），UDD 数据只在模型调用 `fetch_udd` 时才存在。工具全部串行执行、只读、经既有安全层，共 7 个：

| 工具 | 语义 |
|---|---|
| `fetch_url` | 经安全抓取器的有界抓取（SSRF/DNS 钉扎/逐跳 robots/字节与请求限额），返回观测 ID、final URL、重定向链、可读正文与抽取链接。HTML 正文优先用已安装的 trafilatura（只喂已抓回的字节，不用其下载器），缺失/失败/为空时回退标准库抽取，结果带 `extractor` 标签（`trafilatura` / `fallback-stdlib` / `fallback-empty-stdlib`）；清理 boilerplate 之前独立捕获锚文本/绝对 URL（去重保序）、mailto、footer、版权行作出处信号；JSON/纯文本按原文返回 |
| `search_web` | 经受信任本地 SearXNG（`--search-base`，默认 `http://127.0.0.1:8888`，JSON API、GET，不跟随任何重定向）搜索；结果规范化（仅 http(s) 绝对 URL、去 fragment、去重保名次、剥 HTML 标签、逐条引擎归属）；0 结果且有引擎失败 = 显式歧义，403/429/captcha/畸形 JSON 各自显式分级，失败不读作"无结果"；搜索原始 JSON 以观测落档，snippet 属第三方摘要、弱于页面正文；请求计入共享 `--max-requests` 预算 |
| `github_api` | GitHub REST 结构化查询：`action ∈ {repo, readme, releases, commits}`；`owner/repo` 白名单字符校验、路径由应用拼接；`page/per_page` 有界（1..100），`Link rel="next"` 仅在同源同路径前缀时解析为页码、绝不自动跟随；请求走公网抓取器全套校验，附写死的 `Accept: application/vnd.github+json` 与 `X-GitHub-Api-Version: 2022-11-28`（无认证）；403/429 暴露 `x-ratelimit-*`/`retry-after`、不自动重试；404 = 未知（仓库可能私有/改名，不推断死亡或无发布）；`html_url`/`homepage` 进入候选证据集；API 记录只是托管方数据，不构成上游身份的官方证明 |
| `bug_fetch` | 抓取一个 Debian BTS bug 的完整公开讨论（auto/soap/mbox 三选与回退语义见 bug-fetch 一节；与 `fetch_udd` 之间零自动联动） |
| `fetch_udd` | UDD 只读固定模板查询（datasets= 路径，安全模型、限额与结果语义见 fetch-udd 一节）：`action=list` 返回 18 个数据集目录（纯函数、无 DB、不耗预算）；`action=fetch` 单次查询 1-6 个数据集，列式行 + 平行 `udd:<dataset>#row=N` provenance 数组；调用数/行数预算在 psql 之前强制；工具结果超 24k 字符时行感知裁剪（`trimmed`+`trim_note` 显式披露），被裁掉的行不可引用 |
| `read_evidence` | 重读本轮已存观测的正文/链接；`offset`（字符）与 `links_offset`（链接序号）分页，返回窗口元数据与 `next_offset`/`next_links_offset`，不重复返回相同前缀；越界/负数/非整数显式报错 |
| `finish_investigation` | 结构化终报（候选 URL、支撑观测 ID、逐字引用、不确定性）；应用侧对照本轮实际暴露给模型的证据校验 |

终报硬校验（防伪造）：候选 URL 必须出现在被引支撑观测自身的合格 URL 集（fetch 观测为成功抓取的 requested/final URL 加该页抽取链接；search 观测为规范化结果 URL；github_api 观测为 API 提取的 `html_url`/`homepage` 等，不含 API 端点本身）；引用必须是工具实际返回给模型的文本（首次 payload 或 read_evidence 各偏移窗口）的子串（空白归一后匹配），服务端留存但模型未见过的片段不算；观测 ID 与 fetch_udd 行引用（`udd:<dataset>#row=N`，仅实际暴露且未被裁剪丢弃的行有效）必须存在，仅凭模型记忆的 URL 一律拒绝。被拒的终报获得有界修复机会（`--max-report-repairs`，默认 2），仍失败则状态 `invalid-report`、exit 1。run.json 的 `fact_citation_index` 记录每个 `udd:<dataset>#row=N` 出现在哪些 `observation_id` 中，便于审计回溯。

预算（全部在创建运行目录之前校验，非法 exit 2）：`--max-rounds`（模型轮数，默认 8）、`--max-tool-calls`（默认 24；finish 不受限）、`--max-requests`（HTTP 请求总数含 robots 与搜索请求，默认 12）、`--max-body-chars`（单工具结果正文，默认 8000）、`--max-links`（默认 40，也是搜索结果条数上限）、`--max-context-chars`（对话序列化上限，默认 60000；超出对最长工具结果折半截断，截断结果包成合法 JSON 信封并双重披露）、`--udd-max-calls`（fetch_udd 调用数，默认 6，上限 50）、`--udd-max-rows`（暴露行数总预算，默认 200，上限 2000）、`--model-max-tokens`（每轮，默认 8192）、`--model-timeout`（默认 120s）、`--search-base`/`--search-timeout`（须 http(s) 无凭据，默认 `http://127.0.0.1:8888` / 10s）、`--github-api-base`（默认 `https://api.github.com`，操作员专用）、`--no-search-web` / `--no-github` / `--no-bts` / `--no-udd`（显式禁用；`--no-udd` 下 list 仍可用），以及 run 同款抓取参数。run.json 的 `counts.tool_calls_by_name` 保留每工具调用数与错误数。

安全与审计：每轮原始请求（含完整 messages 与 tools 数组）/响应/错误/工具调用参数按轮次 ID 落盘于 `models/inv-rNNN.json`，每轮实际工具执行结果（含被拒的 finish_investigation）另存于 `models/inv-tools-rNNN.json`，预算耗尽或终报被接受后结果也不丢失；抓取按 run 同构语义写 `observations/*.json` + `raw/*`，`gleam check` 可校验。assistant 回放只含 content 与 tool_calls，不回放 reasoning_content；`finish_reason=length` 与畸形 tool_calls 信封（缺 id/重复 id/function 非对象/name 非字符串）视为安全失败，该轮不执行任何调用；未知工具名、畸形 JSON、NaN/Infinity 参数一律拒绝执行并把错误作为工具结果回填。模型端点仅来自操作员配置；工具目标 URL 永远走公网校验，模型无法指向本地端点或内网；无 shell、无本地文件、无模型可控 base。investigate 不调用 Laya。

存储关系：investigate 不读取、不修改既有 run 产物；若与 run 共用同一 `--store`，其成功且未截断的观测会并入共享基线索引 `index.json`（同 URL 的后续 run 比较以该观测为基线）。需要完全隔离时请用独立 `--store`。

终报只是待人工复核的提案：不自动确认上游身份、不执行任何行动。状态区分 `completed` / `budget-exhausted` / `context-budget-exhausted`（折半截断后仍超上下文硬上限，停机不发请求）/ `model-error`（含截断、畸形响应/信封、端点不可达）/ `invalid-report` / `no-final-report`（模型仅输出散文，原样保留但不算发现）。

已知局限：`search_web` 依赖本地 SearXNG 容器在运行（见下文），容器未启动时搜索显式失败（connection-error），失败 = 未知，不构成"无结果"的证明；trafilatura 未安装时 `fetch_url` 走标准库回退并在结果与 run.json（`mode.extraction`）双重标注，功能可用但抽取质量降级。

## bug-fetch：Debian BTS 公开讨论抓取（实验性）

```bash
python3 -m gleam bug-fetch --bug 620025 --store runs                    # 默认 --bts-source auto
python3 -m gleam bug-fetch --bug 620025 --bts-source soap --store runs  # 显式单来源
python3 -m gleam check --store runs
```

按 bug 编号取回完整公开讨论，追加式落盘（observations + raw 原始字节 + 逐消息 `discussion.json` + run.json/report.md，`check` 可校验）。`--bug` 严格数字，URL 由应用侧从数字重建，主机白名单仅 bugs.debian.org。

`--bts-source` 取 `auto`（默认）| `soap` | `mbox`：auto 先走文档化机器接口 Debbugs/SOAP `get_bug_log`，仅当以资格错误（transport/timeout/http/soap-fault/parse/redirect/challenge 类）失败且 robots 明确允许 mbox 端点（如 404 = 无限制）时才回退官方 mbox 视图；rate-limited、not-found、预算耗尽不回退；robots 未知或拒绝不回退且无绕过开关。显式 `soap`/`mbox` 失败即失败。bugs.debian.org 的 robots 对 `User-agent: *` 全站 Disallow，网页接口（HTML/mbox 视图）默认不抓；SOAP 是 Debian 文档指给程序的 API，每次运行的 robots 原文事实仍入档。mbox 的 robots 门槛：明确 disallow 即拒绝，状态未知时默认拒绝，仅 `--bts-web-anyway` 显式接受未知态才抓（该开关在 auto 模式被忽略）。成功结果带逐来源 `attempts` 与 `source_selection` provenance，回退共享同一请求预算。错误分级：not-found / rate-limited / challenge / challenge-unknown / robots-disallow / redirect / timeout / truncated。

BTS 可能返回 PoW 挑战（Varnish 302 到 `/.internal/challenge.html`，`pow_challenge` cookie，SHA256(challenge;tries) 首字节为 0，`pow_nonce` cookie 回答）：客户端在 CPU 秒数与请求预算内按文档化流程有界求解，未识别形态显式 `challenge-unknown`；挑战会话 cookie 只存内存，落盘响应头脱敏。逐消息保留 message-id/date/from/subject/正文（字符集/QP/base64 解码、sha256 出处），消息内 URL 进入该观测合格证据集；附件只登记元数据（文件名/类型/大小/摘要），不单独提取/写出/执行，编码后的附件原文仍随留存的原始响应字节保留。

## fetch-udd：UDD 公共只读镜像查询（独立 CLI + investigate 工具）

```bash
python3 -m gleam fetch-udd --list-datasets              # 18 个数据集目录
python3 -m gleam fetch-udd --source librcd --store runs # legacy groups=（默认五组）
python3 -m gleam fetch-udd --source librcd \
    --datasets sources,upstream,upstream_metadata,vcswatch,bugs,wnpp \
    --release sid --limit 20 --store runs               # datasets=（template v3）
python3 -m gleam check --store runs
```

`--list-datasets` 打印 18 个数据集目录后退出（无 DB 查询、不创建运行目录）；`--datasets`（逗号分隔）与 `--groups` 互斥（同传 exit 2），都不给时默认 legacy 五组；`--source` 除 `--list-datasets` 外必填。目标为文档化公共只读镜像 `postgresql://udd-mirror.debian.net/udd`（凭据公开，见 [docs/research/02-udd-handbook.md](docs/research/02-udd-handbook.md)）。

安全模型（两条路径一致）：psql 子进程（argv 列表、无 shell），SQL 脚本经 stdin（`-c` 不做 psql 变量插值），输入只经 psql 变量进入固定模板（`:'source'`/`:'release'` 带引号插值，limit 经 Python 侧严格 int 校验）；包名按 Debian 策略正则、groups/datasets/release 按严格允许清单，DSN 形态校验（`--udd-dsn`/`GLEAM_UDD_DSN` 仅供操作员，模型不可控）。只读多层强制：`PGOPTIONS` `statement_timeout=15s` + `default_transaction_read_only=on`、单 `BEGIN;...COMMIT;` 事务、镜像账号无写权限。有界：≤9 条语句/次（datasets= 路径 meta+6 数据集 ≤7 条）、每组/数据集 `LIMIT limit+1` 截断探测（more_available 显式披露）、stdout/stderr 各 8 MiB 有界读取（超限 kill 并报 output-too-large）、整个调用 45s 墙上超时。

查询组（legacy `--groups`，template v2）：`source`（sources 表逐 release 多行保留）/`upstream`（uscan 状态）/`metadata`（DEP-12）/`packaging`（vcswatch）/`bugs` 默认，`history`（upload_history）/`wnpp` 可选；逐表各自成行、不 JOIN（避免 join 乘法与 sources.bin 逗号文本问题）。`bugs` 组覆盖源包与映射二进制包的开放报告（status<>'done'，匹配 BTS source 映射 / src: 伪包 / bugs_packages / packages 二进制→源码四种关联，每行带 `via_source_mapping`/`via_bugs_packages` 出处布尔），标题不做关键词过滤；`bugs` 组恒附带 `binaries` 子行组（二进制清单）；完整讨论不在此工具内（按 id 另调 `bug_fetch`，二者零自动联动）。

数据集路径（`--datasets`，template v3）：18 个平等数据集（目录见 `--list-datasets`），单次 ≤6 个、各数据集有效行合计 ≤120、默认 limit 20 / 硬上限 50（archived_bugs/lintian 有效上限 10）；每行带 `provenance = "udd:<dataset>#row=N"`；release 过滤只作用于 release-scoped 数据集（sources/upstream/binaries/reproducible），其余忽略。

结果语义（两路径一致）：0 行 = 真实空结果（未知源名亦然，没有 cohort 检查）、失败 = 未知、截断 = more_available，三者严格区分；行内时间戳为服务端渲染原文，与 `fetched_at`（客户端时刻）分离；模板版本 + 输入 + 原始 CSV（含 sha256）入档，`gleam check` 会重算 CSV 哈希校验。与 run/investigate 共用 `--store` 时，成功的观测同样并入共享基线索引（键为脱凭据 DSN 形态，HTTP 探测永不指向该键）。已知限制：psql --csv 下 NULL 与空串不可区分；`upstream.watch_file` 逐字抓回、原样保留、从不执行（工具层展示截断，完整原文在落档 CSV 中）。URL 字段（homepage/VCS/DEP-12/upstream_url/vcswatch url/browser）进入该观测合格候选集（数据库事实，弱于页面正文）。

## 自托管搜索栈（SearXNG）+ trafilatura

调查栈 = 自托管 SearXNG（localhost 隔离）+ 既有安全抓取器 + trafilatura（仅已抓回字节）+ GitHub API 工具（默认启用），全部在既有 investigate 循环内。

```bash
cd /path/to/gleam

# 1) Python 依赖（uv；锁定 trafilatura==2.3.1，依赖刻意最小）
uv venv .venv && uv pip install -r requirements.txt   # 或 uv sync
.venv/bin/python -m gleam investigate --package librcd --store runs

# 2) SearXNG 容器（Docker 仅 docker run，无 compose/特权/socket 挂载）
scripts/searxng/start.sh   # 幂等：容器已在跑则复用；拒绝动无关容器
scripts/searxng/stop.sh    # 只停 gleam-searxng（校验容器名+镜像归属后才删）

# 3) 就绪检查
curl -s http://127.0.0.1:8888/healthz
curl -s 'http://127.0.0.1:8888/search?q=librcd+debian&format=json' | head -c 400
```

部署要点：官方 `docker.io/searxng/searxng` 镜像，`docker run -d --name gleam-searxng -p 127.0.0.1:8888:8080`，仅绑定回环，无 host networking、无特权、无 docker socket 挂载；`settings.yml` 启用 `search.formats: [html, json]`、关闭 limiter（仅本机）、保守引擎子集（keep_only 无 key、无 captcha 绕过的引擎，实际以 JSON 查询验证可用为准，`unresponsive_engines` 会如实报告）。

## 数据语义

- 包事实来自 live UDD 固定模板查询（`--release` 默认 `sid`；QA 过滤为 maintainer='Debian QA Group <packages@qa.debian.org>' AND release='sid'）。选择方式两种：显式 `--package` = 逐包快速路径（不枚举 cohort）；仅 `--limit N` = cohort 枚举（`DISTINCT ON (source)` 取最高版本、`ORDER BY source` 确定性行序、`LIMIT N+1` 截断探测、N 有界 ≤100）。包名按 Debian 策略（`^[a-z0-9][a-z0-9+.-]+$`）校验；未知包与非 QA 维护包分别以 unknown-package / not-in-cohort 致命拒绝（exit 3，不创建运行目录）。
- 候选端点只来自 live 事实字段与操作员 `--url`：`homepage-candidate`（udd:sources.homepage）、`dep12-repository` / `dep12-repository-browse`（DEP-12 长表，分别标注）；`upstream_url` 只登记为 `upstream-artifact`，不抓取（可能是压缩包，策略禁止下载制品）。
- sources / vcswatch 的 VCS 字段是 Debian 打包出处：不构成上游身份的证明或反证（上游与打包仓库可能共址），报告中按 `debian-packaging-vcs` 展示，不进入上游候选。
- live 事实行出处为 `udd:<table>#row=N`（模板 ORDER BY 下的确定性行号），绑定该次抓取的 `template_version` + `fetched_at` + 原始 CSV 响应 SHA-256（记入 run.json 的 input_provenance）。同名多版本行全部保留（不按 source 去重覆盖）；重复 `--package` 自动去重。
- `watch_file` 由 live 模板逐字抓回，原样保留在事实中，从不执行 uscan 或其中的正则/URL。
- 离线语义：`--offline` = 不发网页请求、不调模型，包事实只读显式 `--facts-cache DIR`（先在线运行写入；未命中 = 致命失败，不回退 live）。活跃模式需要 live UDD 可达（psql + 网络），本项目不是完全离线系统；缓存条目披露原始 fetched_at，可能过期。
- outcome 词表（示意，未冻结）：`OK` / `NOT_MODIFIED` / `REPRESENTATION_CHANGED` / `TRANSPORT_ERROR` / `ABSTAIN`，另有 `NOT_PROBED`（登记但不抓取的候选）与 `HTTP_ERROR`（4xx：HTTP 交换完成但目标报错；保留响应、不作死亡推断、不成为基线）。探针错误 = 未知，不推断上游死亡；字节哈希变化仅为表示层变化，不是活性结论。
- 基线只与同端点上一次成功且未截断观测比较；失败扫描、HTTP_ERROR 与截断 body 都不成为基线。304 Not Modified 沿用基线哈希（不用空 body 哈希覆盖好哈希）；本次或基线任一侧截断时 comparison 记 `unknown-truncated-body`。

## 网络安全边界

- 仅 http(s)；拒绝 URL 内凭据与畸形 URL（含坏 IPv6 字面量、非数字/越界端口，统一按 unsafe-destination 弃权，单条坏端点不中断整轮）；每一跳重定向都重新走完整校验。
- SSRF 防护：DNS 解析结果必须为公网单播地址（拒绝环回/内网/链路本地/保留/组播，含 IPv4-mapped IPv6 拆包），随后 `http.client` 连接钉住的 IP，TLS 仍按原主机名做 SNI 与证书校验（防 DNS 重绑定）。不使用代理环境变量。
- 上限可配：`--max-bytes`（默认 256 KiB）、`--timeout`（15s）、`--max-redirects`（3）、`--max-requests`（每次运行 10）、同主机最小间隔（默认 1s）；一切限额在创建任何运行目录之前校验，非法 exit 2。
- robots.txt：每一跳（含重定向目标，同主机换路径也算）在 GET 前逐一判定；规则按 origin 解析并缓存（robots 抓取自身不做 robots 检查）；disallow / 401/403/5xx / 抓取中途失败 / 其他非 2xx 一律弃权（ABSTAIN）；404/410 视为无限制；robots 原文按 origin 哈希命名留存（IDN 安全），在观测 `raw_refs.robots` 中引用。目标主机级失败（DNS 解析失败、连接拒绝、地址不安全）按原始传输错误呈现，不误标为 robots 弃权。
- 失败抓取同样保留已取得的响应头与（部分）body（`raw_refs.body/headers`）。
- run 模式无任何自动开放网页搜索，模型输出与网页内容都不会触发抓取（investigate 模式例外：模型通过工具显式发起，每次仍走本节全部安全层；search_web 走受信任本地 SearXNG 的独立代码路径）。
- 受信任端点共三个，全部仅来自操作员配置、与不可信探针 URL 严格分路：本地模型端点（`--model-base` / `--laya-base`）、本地 SearXNG 搜索（`--search-base`，有界、不跟随重定向）、GitHub API base（`--github-api-base`，其请求本身仍走公网抓取器全套校验）。三者绝不来自网页或 CSV 数据。

## 模型（可选）

- live UDD 事实输入（inspect/run 共用；investigate 零预取、不经此路径）：对 UDD 只读镜像的固定模板 psql 查询，安全模型见 fetch-udd 一节。每次 run 把事实输入的原始 UDD CSV 追加落盘为 `raw/udd-facts-<source>.csv`（cohort 枚举另存 `raw/udd-cohort.csv`），provenance 的 `response_sha256` 即其 SHA-256；inspect 为查看型命令，不建运行目录、不落证据。
- `--offline`：见数据语义；缓存条目含 result 与原始 CSV 的双重 SHA-256 校验，篡改/损坏一律 `cache-corrupt` 拒绝。`--no-model`：确定性运行，不调模型（事实仍为 live）。
- Qwen（默认 `qwen3.8-27b`，`/v1/chat/completions` + JSON structured output）：只产出枚举 action 的提案与摘要，必须引用本次运行的观测 ID，应用侧校验引用与 schema；截断（finish_reason=length）、空内容、畸形 JSON、引用不合规一律按错误弃权并保留原始响应；提示词含 distrust-web-data 指令。输出预算与超时可配：`--model-max-tokens`（默认 8192、上限 65536）、`--model-timeout`（默认 120s、上限 600s），非法值按用法错误拒绝（exit 2）。单次调用、无自动重试；若仍截断，报告标注 truncated 并以 exit 1 显式失败。
- Laya（`--laya-base`，默认 `http://127.0.0.1:1234`，`POST /v1/systemone`，载荷 `{model, state, questions.triage}`，期望 `answers.triage.{choice, probabilities}`）：仅咨询（advisory-only），任何选择都不跳过任何包，Qwen 照常复核；状态文本截断会披露。错误路径：原始错误完整保留在 `models/laya-*.json`，运行继续用 Qwen（fallback 记录在案），退出码非 0；接口可用性以当次调用为准。
- 两次调用的原始请求/响应、模型 ID、prompt SHA-256 全部落盘；模型输出只是待人工复核的提案，不执行任何动作。

## 局限（明确不做）

- 无自动身份合并 / 实体图谱 / 状态机 / TTL / 阈值；无邮件、MR 或任何自动行动。
- 无常驻守护；如需周期运行可自行 cron，例如：`17 3 * * * cd /path/to/gleam && python3 -m gleam run --limit 3 --store runs >> runs/cron.log 2>&1`
- run 模式不做 GitHub API 探测（investigate 的 `github_api` 工具提供结构化 repo/readme/releases/commits 查询；run 保持纯 HTTP 观测语义，不把 API 记录或 HTML 变化解读为代码活动）。
- 活跃事实源 = live UDD 的 source/upstream/metadata/packaging 四组固定模板（其余 UDD 表与完整实体 schema 不在事实输入内；fetch-udd 的 bugs/history/wnpp 等属调查扩展，不回写任何事实）。
- 每次运行需 UDD 镜像可达（psql + 网络）；`--offline` 只覆盖网页观测与模型，不是完全离线系统。
- outcome 词表未定稿，本实现按"非主张"口径记录。
- Laya 的 probabilities 是自评分、未校准，不可作为阈值依据。

## 布局

```
gleam/            # CLI 包：python3 -m gleam
  facts.py        # 包事实数据模型（Fact/Candidate/PackageFacts）
  liveudd.py      # live UDD 事实源：QA cohort 枚举 + 逐包 facts 适配 + 离线缓存
  udd.py          # UDD live 查询（psql 固定模板：groups= v2 + datasets= v3）
  snapshot.py     # 历史归档快照读取器（legacy；活跃 CLI 不使用）
  net.py          # 有界安全抓取器（SSRF/DNS 钉扎/重定向/robots/限额）
  store.py        # 追加式证据存储与基线索引
  observe.py      # 观测引擎（outcome 判定、基线比较、原始留存）
  models.py       # Qwen / Laya 客户端（校验、弃权、原始保留）
  agent.py        # investigate：工具调用型多轮调查代理
  search.py       # 受信任本地 SearXNG 适配器
  extract.py      # trafilatura 抽取（仅已抓回字节）+ 标准库回退
  github.py       # GitHub API 工具辅助（路径构造/分页/限流头）
  bts.py          # Debian BTS 讨论抓取（auto/soap/mbox、PoW 客户端）
  report.py       # run.json + report.md 渲染
  cli.py          # inspect / models / check / run / investigate / bug-fetch / fetch-udd
tests/            # unittest + 合成 fixtures（不需要网络）
raw/              # 本地历史快照存档（git 忽略；非运行时输入）
runs/             # 默认证据存储（git 忽略；追加式）
scripts/searxng/  # SearXNG 自托管脚本与 settings.yml
pyproject.toml    # uv 项目：依赖刻意最小（trafilatura==2.3.1）
.venv/            # uv venv（git 忽略）
```

Python 3.14（3.11+ 可运行）。核心仅标准库；第三方依赖刻意最小，仅 trafilatura（未安装时 `fetch_url` 走标准库回退并在结果中标注，测试在有无 trafilatura 两种环境都可跑）。
