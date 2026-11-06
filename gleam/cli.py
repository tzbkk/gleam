"""Gleam CLI：inspect / models / check / run / investigate / bug-fetch /
fetch-udd。

包选择与包事实输入 = live UDD 公共只读镜像（固定模板、有界、psql 无
shell）；仓库根 raw/ 历史快照不是运行时输入（无 --raw 开关；绝不静默
回退读取）。

用法（详见 README.md）：
  python3 -m gleam inspect [--package X]... [--limit N] [--json]
                           [UDD 参数]
  python3 -m gleam models [--model-base URL]
  python3 -m gleam check [--store DIR]
  python3 -m gleam run [--package X]... [--limit N] [--url URL] [--offline]
                       [--facts-cache DIR] [--no-model] [--store DIR]
                       [网络/模型/UDD 参数]
  python3 -m gleam investigate --package X [--store DIR] [预算/模型参数]
                               [--search-base URL] [--github-api-base URL]
                               [UDD 参数]
  python3 -m gleam bug-fetch --bug N [--store DIR]
                              [--bts-source auto|soap|mbox] [BTS 预算参数]
  python3 -m gleam fetch-udd [--list-datasets | --source NAME]
                             [--datasets a,b,c | --groups ...]
                             [--store DIR] [--release ...] [--limit N]
                             [UDD 参数]

退出码：0 成功；1 完成但存在传输错误或模型错误（run）/ 未产出受验终报
（investigate）/ 抓取失败（bug-fetch / fetch-udd，证据仍追加保存）；2 用法
错误（investigate 的包名只做 Debian 源名策略校验——不做 cohort 查询）；3 live
事实源/存储/配置致命错误（run/inspect 的查询失败、未知包、cohort 外）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys

from . import __version__
from . import agent as agent_mod
from . import bts as bts_mod
from . import extract as extract_mod
from . import github as github_mod
from . import liveudd as liveudd_mod
from . import models as models_mod
from . import observe, report
from . import search as search_mod
from . import udd as udd_mod
from .facts import Candidate
from .net import Fetcher, FetchOutcome
from .store import RunStore, utc_now, verify_store

EXIT_OK = 0
EXIT_COMPLETED_WITH_ERRORS = 1
EXIT_USAGE = 2
EXIT_FATAL = 3

DEFAULT_STORE = "runs"
DEFAULT_LIMIT = 3


def _env_or(env_key: str, default: str) -> str:
    return os.environ.get(env_key, default)


def _add_udd_args(parser, *, facts_cache: bool = False):
    """live UDD 事实源参数（操作员级；模型不可达）。"""

    parser.add_argument("--release", default=None,
                        help="事实/cohorts 查询的 release 过滤（严格允许"
                             "清单；默认 sid = QA cohort 口径）")
    parser.add_argument("--udd-dsn",
                        default=_env_or("GLEAM_UDD_DSN",
                                        udd_mod.DEFAULT_UDD_DSN),
                        help="操作员专用 DSN 覆盖（默认文档化公共只读镜像 "
                             "postgresql://udd-mirror.debian.net/udd；"
                             "必须 postgresql:// 形态；模型不可达）")
    parser.add_argument("--udd-psql", default="psql",
                        help="psql 可执行文件路径（默认 PATH 上的 psql）")
    parser.add_argument("--udd-timeout", type=float,
                        default=udd_mod.DEFAULT_POLICY["timeout_s"],
                        help="整个 psql 调用的墙上超时秒（默认 45）")
    if facts_cache:
        parser.add_argument(
            "--facts-cache", default=None,
            help="显式 facts 缓存目录（opt-in）：在线运行成功抓取的 live "
                 "事实写入该目录；--offline 模式只从此目录读（未命中=诚实"
                 "失败，绝不静默回退 live 或编造）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gleam",
        description="Gleam / 荧 — live UDD 包事实 → 有界观测 → 追加式证据 → "
                    "本地模型分诊（最小实现）")
    parser.add_argument("--version", action="version",
                        version=f"gleam {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser(
        "inspect",
        help="查看 live UDD 包事实与候选端点（对 UDD 镜像做有界只读查询）")
    _add_udd_args(p_inspect)
    p_inspect.add_argument("--package", action="append", default=[])
    p_inspect.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                           help="未指定 --package 时的 cohort 枚举上限"
                                f"（默认 {DEFAULT_LIMIT}；上限 "
                                f"{udd_mod.DEFAULT_POLICY['max_rows_hard']}）")
    p_inspect.add_argument("--json", action="store_true")

    p_models = sub.add_parser("models", help="列出本地模型服务上的模型")
    p_models.add_argument("--model-base",
                          default=_env_or("GLEAM_MODEL_BASE",
                                          models_mod.DEFAULT_MODEL_BASE))
    p_models.add_argument("--timeout", type=float, default=10.0)

    p_check = sub.add_parser("check", help="校验证据存储一致性")
    p_check.add_argument("--store", default=DEFAULT_STORE)

    p_run = sub.add_parser("run", help="执行一轮观测运行（证据追加式保存）")
    _add_udd_args(p_run, facts_cache=True)
    p_run.add_argument("--store", default=DEFAULT_STORE)
    p_run.add_argument("--package", action="append", default=[],
                       help="可重复；缺省按字母序取前 --limit 个")
    p_run.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                       help="未指定 --package 时的默认有界限额（默认 3）")
    p_run.add_argument("--url", action="append", default=[],
                       help="操作员手工候选 URL，需恰好指定一个 --package，"
                            "标记为 manual-candidate")
    p_run.add_argument("--offline", action="store_true",
                       help="离线观测：不发任何网页请求、不调模型；包事实"
                            "只读 --facts-cache（显式 opt-in，未命中=诚实"
                            "失败）——不再声称完全离线，live UDD 事实是"
                            "活跃模式的必需输入")
    p_run.add_argument("--no-model", action="store_true",
                       help="不调用模型；其余流程照常（确定性）")
    p_run.add_argument("--model", default=models_mod.DEFAULT_QWEN_MODEL)
    p_run.add_argument("--model-base",
                       default=_env_or("GLEAM_MODEL_BASE",
                                       models_mod.DEFAULT_MODEL_BASE))
    p_run.add_argument("--laya-model",
                       default=_env_or("GLEAM_LAYA_MODEL",
                                       models_mod.DEFAULT_LAYA_MODEL))
    p_run.add_argument("--laya-base",
                       default=_env_or("GLEAM_LAYA_BASE",
                                       models_mod.DEFAULT_LAYA_BASE),
                       help="Laya /v1/systemone 的独立 base（调用错误将保留"
                            "并回退 Qwen）")
    p_run.add_argument("--model-max-tokens", type=int,
                       default=models_mod.QWEN_MAX_TOKENS,
                       help=f"Qwen 单次调用输出 token 预算（默认 "
                            f"{models_mod.QWEN_MAX_TOKENS}，上限 "
                            f"{models_mod.QWEN_MAX_TOKENS_LIMIT}；reasoning "
                            f"计入预算，超限截断按错误弃权，不自动重试）")
    p_run.add_argument("--model-timeout", type=float,
                       default=models_mod.MODEL_TIMEOUT_S,
                       help=f"模型单次调用超时秒（默认 "
                            f"{models_mod.MODEL_TIMEOUT_S:g}，上限 "
                            f"{models_mod.MODEL_TIMEOUT_LIMIT_S:g}）")
    p_run.add_argument("--max-bytes", type=int, default=262144)
    p_run.add_argument("--timeout", type=float, default=15.0)
    p_run.add_argument("--max-redirects", type=int, default=3)
    p_run.add_argument("--max-requests", type=int, default=10)
    p_run.add_argument("--min-interval", type=float, default=1.0)

    p_inv = sub.add_parser(
        "investigate",
        help="工具调用型 Qwen 调查代理（多轮 tools 循环；只读；提案待人工复核）")
    _add_udd_args(p_inv)
    p_inv.add_argument("--store", default=DEFAULT_STORE)
    p_inv.add_argument("--package", action="append", required=True,
                       help="恰好一个源包名")
    p_inv.add_argument("--model", default=models_mod.DEFAULT_QWEN_MODEL)
    p_inv.add_argument("--model-base",
                       default=_env_or("GLEAM_MODEL_BASE",
                                       models_mod.DEFAULT_MODEL_BASE))
    p_inv.add_argument("--model-max-tokens", type=int,
                       default=models_mod.QWEN_MAX_TOKENS,
                       help=f"每轮模型调用输出 token 预算（默认 "
                            f"{models_mod.QWEN_MAX_TOKENS}，上限 "
                            f"{models_mod.QWEN_MAX_TOKENS_LIMIT}）")
    p_inv.add_argument("--model-timeout", type=float,
                       default=models_mod.MODEL_TIMEOUT_S,
                       help=f"每轮模型调用超时秒（默认 "
                            f"{models_mod.MODEL_TIMEOUT_S:g}，上限 "
                            f"{models_mod.MODEL_TIMEOUT_LIMIT_S:g}）")
    p_inv.add_argument("--max-rounds", type=int,
                       default=agent_mod.DEFAULT_LIMITS["max_rounds"],
                       help="模型轮数上限（默认 8）")
    p_inv.add_argument("--max-tool-calls", type=int,
                       default=agent_mod.DEFAULT_LIMITS["max_tool_calls"],
                       help="工具调用次数上限（默认 24；finish_investigation "
                            "不受此限）")
    p_inv.add_argument("--max-report-repairs", type=int,
                       default=agent_mod.DEFAULT_LIMITS["max_report_repairs"],
                       help="终报被拒后的修复机会（默认 2）")
    p_inv.add_argument("--max-body-chars", type=int,
                       default=agent_mod.DEFAULT_LIMITS["max_body_chars"],
                       help="单次工具结果内正文上限字符（默认 8000）")
    p_inv.add_argument("--max-links", type=int,
                       default=agent_mod.DEFAULT_LIMITS["max_links"],
                       help="单次工具结果内链接上限（默认 40）")
    p_inv.add_argument("--max-context-chars", type=int,
                       default=agent_mod.DEFAULT_LIMITS["max_context_chars"],
                       help="对话上下文序列化上限字符（默认 60000；超出对最"
                            "长工具结果折半截断并披露）")
    p_inv.add_argument("--max-bytes", type=int, default=262144)
    p_inv.add_argument("--timeout", type=float, default=15.0,
                       help="单次 HTTP 抓取超时秒")
    p_inv.add_argument("--max-redirects", type=int, default=3)
    p_inv.add_argument("--max-requests", type=int, default=12,
                       help="整次调查的 HTTP 请求总数上限（含 robots；默认 12）")
    p_inv.add_argument("--min-interval", type=float, default=1.0)
    p_inv.add_argument("--no-search-web", action="store_true",
                       help="禁用 search_web（工具仍在但如实报告不可用）")
    p_inv.add_argument("--search-base",
                       default=_env_or("GLEAM_SEARCH_BASE",
                                       search_mod.DEFAULT_SEARCH_BASE),
                       help="受信任本地 SearXNG base（默认 "
                            "http://127.0.0.1:8888；仅操作员配置，"
                            "模型无法指定；须 http(s) 且无凭据）")
    p_inv.add_argument("--search-timeout", type=float,
                       default=search_mod.SEARCH_TIMEOUT_S,
                       help="SearXNG 搜索请求超时秒（默认 10；上限 60）")
    p_inv.add_argument("--github-api-base",
                       default=_env_or("GLEAM_GITHUB_API_BASE",
                                       github_mod.DEFAULT_GITHUB_API_BASE),
                       help="GitHub REST API base（默认 https://api.github.com；"
                            "仅操作员配置；请求经公网 Fetcher 全套安全层）")
    p_inv.add_argument("--no-github", action="store_true",
                       help="禁用 github_api 工具（如实报告不可用）")
    p_inv.add_argument("--no-bts", action="store_true",
                       help="禁用 bug_fetch 工具（如实报告不可用）")
    p_inv.add_argument("--no-udd", action="store_true",
                       help="禁用 fetch_udd 工具（action=list 目录仍可用；"
                            "action=fetch 如实报告不可用）")
    p_inv.add_argument("--udd-max-calls", type=int, default=6,
                       help="fetch_udd action=fetch 调用数上限（默认 6，"
                            "上限 50；action=list 不受限、不消耗预算）")
    p_inv.add_argument("--udd-max-rows", type=int, default=200,
                       help="fetch_udd 暴露行数总预算（默认 200，上限 "
                            "2000；超限即 budget-exhausted，绝不静默截断"
                            "调用）")

    p_bts = sub.add_parser(
        "bug-fetch",
        help="抓取一个 Debian BTS bug 的完整公开讨论（有界只读；证据追加式保存）")
    p_bts.add_argument("--bug", required=True,
                       help="Debian bug 编号（严格数字，1..9999999）")
    p_bts.add_argument("--store", default=DEFAULT_STORE)
    p_bts.add_argument("--bts-source", choices=list(bts_mod.BTS_SOURCES),
                       default="auto",
                       help="抓取来源：auto=先 SOAP，仅在资格错误"
                            "（传输/解析/挑战等）且 robots 明确允许时回退"
                            " mbox（rate-limit/not-found/预算耗尽不回退，"
                            "绝不绕过 robots；默认）；soap=文档化机器接口"
                            " get_bug_log；mbox=官方 mbox 视图（属网页"
                            "接口，受 robots.txt 门槛约束）")
    p_bts.add_argument("--bts-web-anyway", action="store_true",
                       help="mbox 来源仅在 robots 状态未知（抓不到/解析不"
                            "了）时由操作员显式接受抓取（auto 模式忽略此"
                            "开关）；robots 明确 disallow 时仍拒绝（不提"
                            "供绕过开关）")
    p_bts.add_argument("--bts-max-messages", type=int,
                       default=bts_mod.DEFAULT_POLICY["max_messages"])
    p_bts.add_argument("--bts-max-bytes", type=int,
                       default=bts_mod.DEFAULT_POLICY["max_bytes"])
    p_bts.add_argument("--bts-timeout", type=float,
                       default=bts_mod.DEFAULT_POLICY["timeout_s"])
    p_bts.add_argument("--bts-max-requests", type=int,
                       default=bts_mod.DEFAULT_POLICY["max_requests"],
                       help="客户端实例累计请求总数上限（本次运行一次"
                            "抓取即一个实例；含 robots 与挑战重试；auto "
                            "回退共享同一预算；默认 6）")
    p_bts.add_argument("--bts-min-interval", type=float,
                       default=bts_mod.DEFAULT_POLICY["min_interval_s"])

    p_udd = sub.add_parser(
        "fetch-udd",
        help="对 UDD 公共只读镜像做有界只读固定模板查询（证据追加式保存）")
    p_udd.add_argument("--source", default=None,
                       help="Debian 源包名（按 Debian 策略校验；"
                            "--list-datasets 时可省略）")
    p_udd.add_argument("--store", default=DEFAULT_STORE)
    p_udd.add_argument("--list-datasets", action="store_true",
                       help="打印 18 个数据集目录（纯函数，无 DB 查询、"
                            "不建运行目录）后退出")
    p_udd.add_argument("--datasets", default=None,
                       help="逗号分隔的数据集名（datasets= 新路径，"
                            "template v3，单次 ≤6 个、行上限 50/数据集、"
                            "每行 provenance=udd:<dataset>#row=N；"
                            "与 --groups 互斥）")
    p_udd.add_argument("--groups", default=None,
                       help="逗号分隔的查询组（legacy groups= 旧路径，"
                            "template v2 字节不变；严格允许清单 "
                            f"{list(udd_mod.GROUPS)}；默认 "
                            "source,upstream,metadata,packaging,bugs；"
                            "与 --datasets 互斥）")
    p_udd.add_argument("--release", default=None,
                       help="可选 release 过滤（严格允许清单；只作用于带 "
                            "release 列的表：sources/upstream/binaries）")
    p_udd.add_argument("--limit", type=int, default=None,
                       help="每组/每数据集行数上限（LIMIT limit+1 探测截断；"
                            "默认：groups 路径 30、datasets 路径 20；"
                            "上限：groups 100、datasets 50）")
    p_udd.add_argument("--udd-dsn",
                       default=_env_or("GLEAM_UDD_DSN",
                                       udd_mod.DEFAULT_UDD_DSN),
                       help="操作员专用 DSN 覆盖（默认文档化公共只读镜像 "
                            "postgresql://udd-mirror.debian.net/udd；"
                            "必须 postgresql:// 形态；模型不可达）")
    p_udd.add_argument("--udd-psql", default="psql",
                       help="psql 可执行文件路径（默认 PATH 上的 psql）")
    p_udd.add_argument("--udd-timeout", type=float,
                       default=udd_mod.DEFAULT_POLICY["timeout_s"],
                       help="整个 psql 调用的墙上超时秒（默认 45）")
    return parser


def _resolve_live_input(args):
    """校验 UDD 参数并构造事实客户端；返回 (client, release, problem)。"""

    try:
        release = udd_mod.validate_release(args.release)
        dsn = udd_mod.validate_dsn(args.udd_dsn)
    except udd_mod.UddError as exc:
        return None, None, str(exc)
    if not math.isfinite(args.udd_timeout) \
            or not (1.0 <= args.udd_timeout <= 180.0):
        return None, None, "--udd-timeout 必须在 [1, 180] 秒内"
    client = udd_mod.UddClient(
        dsn=dsn, psql_path=args.udd_psql,
        policy={"timeout_s": args.udd_timeout})
    return client, release, None


def _live_fatal(prefix: str, exc: liveudd_mod.LiveFactsError) -> int:
    print(f"gleam: fatal: {prefix}: {exc.kind}: {exc.message}",
          file=sys.stderr)
    return EXIT_FATAL


def cmd_inspect(args) -> int:
    if args.limit < 1:
        print("gleam: --limit 必须 >= 1", file=sys.stderr)
        return EXIT_USAGE
    if args.limit > udd_mod.DEFAULT_POLICY["max_rows_hard"]:
        print(f"gleam: --limit 必须 <= "
              f"{udd_mod.DEFAULT_POLICY['max_rows_hard']}"
              f"（cohort 枚举有界）", file=sys.stderr)
        return EXIT_USAGE
    client, release, problem = _resolve_live_input(args)
    if problem:
        print(f"gleam: {problem}", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.package:
            live_facts, selection = liveudd_mod.select_live_packages(
                client, names=args.package, release=release)
        else:
            live_facts, selection = liveudd_mod.select_live_packages(
                client, limit=args.limit, release=release)
    except liveudd_mod.LiveFactsError as exc:
        return _live_fatal("live UDD facts", exc)
    # inspect 不建 run 目录、不落证据：丢弃原始 CSV 字节（仅保留摘要出处）
    selection.pop("_raw_csv", None)
    payload = {
        "provenance": liveudd_mod.input_provenance_summary(
            live_facts, release=release, selection_meta=selection,
            dsn_display=udd_mod.dsn_display(client.dsn)),
        "packages": []
    }
    for lf in live_facts:
        pkg = lf.facts
        view = observe.package_facts_view(pkg)
        view["name"] = pkg.name
        view["candidates"] = [
            {"url": c.url, "label": c.label, "kind": c.kind,
             "fetch": c.fetch, "origin": c.provenance}
            for c in pkg.upstream_candidates()]
        view["fact_rows"] = [
            {"table": f.table, "row": f.row_number,
             "version": f.fields.get("version")}
            for f in pkg.rows]
        payload["packages"].append(view)
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        prov = payload["provenance"]
        print(f"live UDD: {prov['dsn_display']} "
              f"(release={prov['release']}, QA cohort filter "
              f"{prov['qa_filter']})")
        selection = prov.get("selection") or {}
        if selection.get("mode") == "cohort" \
                and selection.get("more_available"):
            print(f"note: cohort truncated at {selection.get('limit')} "
                  f"(more available; raise --limit)")
        for lf in live_facts:
            view = next(v for v in payload["packages"]
                        if v["name"] == lf.source)
            p = lf.provenance
            print(f"\n== {view['name']} "
                  f"(versions: {', '.join(view['versions'])}; "
                  f"fetched_at {p['fetched_at']})")
            for cand in view["candidates"]:
                mark = "FETCH" if cand["fetch"] else "register-only"
                print(f"  [{mark}] {cand['label']:<28} {cand['url']}")
            for vcs in view["debian_packaging_vcs"]:
                print(f"  [packaging, not upstream] {vcs['field']}: "
                      f"{vcs['url']}")
            if view["watch_file_retained_raw"]:
                print("  watch_file: retained raw (never executed)")
    return EXIT_OK


def cmd_models(args) -> int:
    # 模型 base 形态属模型配置致命错误（README exit 3），先于任何请求
    base_problem = models_mod.validate_model_base(args.model_base)
    if base_problem:
        print(f"gleam: fatal: --model-base 非法: {base_problem}",
              file=sys.stderr)
        return EXIT_FATAL
    ids, call = models_mod.list_models(args.model_base, args.timeout)
    if call.error:
        print(f"gleam: model listing failed: {call.error['kind']}: "
              f"{call.error['message']}", file=sys.stderr)
        print(f"  endpoint: {call.endpoint}", file=sys.stderr)
        return EXIT_FATAL
    print(f"model base: {args.model_base}")
    for mid in ids:
        print(f"  - {mid}")
    expected = [models_mod.DEFAULT_QWEN_MODEL, models_mod.DEFAULT_LAYA_MODEL]
    for name in expected:
        state = "present" if name in ids else "ABSENT"
        print(f"  expected {name}: {state}")
    return EXIT_OK


def cmd_check(args) -> int:
    problems = verify_store(args.store)
    store = RunStore(args.store)
    runs = store.list_runs()
    print(f"store: {store.root} ({len(runs)} runs)")
    for problem in problems:
        print(f"  - {problem}")
    return EXIT_OK if not problems or problems == [
        "no runs found (store is empty or missing)"] else EXIT_COMPLETED_WITH_ERRORS


def cmd_run(args) -> int:
    if args.url and len(args.package) != 1:
        print("gleam: --url 需要恰好一个 --package（手工候选必须绑定到选定包）",
              file=sys.stderr)
        return EXIT_USAGE
    if args.limit < 1:
        print("gleam: --limit 必须 >= 1", file=sys.stderr)
        return EXIT_USAGE
    if args.limit > udd_mod.DEFAULT_POLICY["max_rows_hard"]:
        print(f"gleam: --limit 必须 <= "
              f"{udd_mod.DEFAULT_POLICY['max_rows_hard']}"
              f"（cohort 枚举有界）", file=sys.stderr)
        return EXIT_USAGE
    if args.offline and not args.facts_cache:
        print("gleam: --offline 需要显式 --facts-cache DIR（live UDD 事实"
              "的本地缓存；无缓存即诚实失败，绝不静默回退）", file=sys.stderr)
        return EXIT_USAGE
    if args.offline and not args.package:
        print("gleam: --offline 需要显式 --package（cohort 枚举需要 live "
              "UDD 查询，离线不可用）", file=sys.stderr)
        return EXIT_USAGE
    if not (1 <= args.model_max_tokens
            <= models_mod.QWEN_MAX_TOKENS_LIMIT):
        print(f"gleam: --model-max-tokens 必须在 [1, "
              f"{models_mod.QWEN_MAX_TOKENS_LIMIT}] 内", file=sys.stderr)
        return EXIT_USAGE
    if not math.isfinite(args.model_timeout) or not (
            0 < args.model_timeout <= models_mod.MODEL_TIMEOUT_LIMIT_S):
        print(f"gleam: --model-timeout 必须为有限正数且不超过 "
              f"{models_mod.MODEL_TIMEOUT_LIMIT_S:g} 秒", file=sys.stderr)
        return EXIT_USAGE
    # 模型 base 形态属模型配置致命错误（README exit 3）：必须在
    # create_run 之前拒绝，否则会留下缺 run.json 的运行目录
    if not args.offline and not args.no_model:
        for flag, base in (("--model-base", args.model_base),
                           ("--laya-base", args.laya_base)):
            problem = models_mod.validate_model_base(base)
            if problem:
                print(f"gleam: fatal: {flag} 非法: {problem}", file=sys.stderr)
                return EXIT_FATAL
    # 网络限额必须在创建任何运行目录之前完成校验
    net_limit_problems = []
    if args.max_bytes < 1:
        net_limit_problems.append("--max-bytes 必须 >= 1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        net_limit_problems.append("--timeout 必须为有限正数")
    if args.max_redirects < 0:
        net_limit_problems.append("--max-redirects 必须 >= 0")
    if args.max_requests < 1:
        net_limit_problems.append("--max-requests 必须 >= 1")
    if not math.isfinite(args.min_interval) or args.min_interval < 0:
        net_limit_problems.append("--min-interval 必须为有限非负数")
    if net_limit_problems:
        for problem in net_limit_problems:
            print(f"gleam: {problem}", file=sys.stderr)
        return EXIT_USAGE
    client, release, udd_problem = _resolve_live_input(args)
    if udd_problem:
        print(f"gleam: {udd_problem}", file=sys.stderr)
        return EXIT_USAGE
    cache = (liveudd_mod.FactsCache(args.facts_cache)
             if args.facts_cache else None)
    if args.offline:
        try:
            live_facts_list = liveudd_mod.offline_facts(cache, args.package)
        except liveudd_mod.LiveFactsError as exc:
            return _live_fatal("offline facts cache", exc)
        selection = {"mode": "offline-cache", "cohort_queried": False,
                     "release": release or udd_mod.QA_RELEASE}
    else:
        try:
            live_facts_list, selection = liveudd_mod.select_live_packages(
                client, names=args.package or None, limit=args.limit,
                release=release, cache=cache)
        except liveudd_mod.LiveFactsError as exc:
            return _live_fatal("live UDD facts", exc)
    packages = [lf.facts for lf in live_facts_list]

    manual_urls = {args.package[0]: list(args.url)} if args.url else {}
    store = RunStore(args.store)
    try:
        run_id = store.create_run()
    except OSError as exc:
        print(f"gleam: fatal: cannot create run dir: {exc}", file=sys.stderr)
        return EXIT_FATAL

    # 事实输入的原始 UDD CSV 落盘为证据：provenance 的 response_sha256 以此为对应原始件
    cohort_raw = selection.pop("_raw_csv", None)
    if cohort_raw:
        selection["cohort_raw_ref"] = store.write_raw(
            "udd-cohort.csv", cohort_raw)
    for lf in live_facts_list:
        lf.provenance["raw_ref"] = store.write_raw(
            f"udd-facts-{lf.source}.csv", lf.raw_csv)
    input_provenance = liveudd_mod.input_provenance_summary(
        live_facts_list, release=release, offline=args.offline,
        cache=cache, selection_meta=selection,
        dsn_display=(udd_mod.dsn_display(client.dsn)
                     if not args.offline else None))

    started_at = utc_now()
    fetcher = None
    if not args.offline:
        fetcher = Fetcher(policy={
            "max_bytes": args.max_bytes,
            "timeout_s": args.timeout,
            "max_redirects": args.max_redirects,
            "max_requests": args.max_requests,
            "min_interval_s": args.min_interval,
        })

    records = observe.collect_observations(
        store, packages, fetcher, manual_urls,
        offline=args.offline, run_id=run_id)

    model_calls = []
    models_note = None
    if args.offline:
        models_note = "（offline 模式：不调用任何模型）"
    elif args.no_model:
        models_note = "（--no-model：确定性运行，未调用模型）"
    else:
        by_pkg = {pkg.name: [r for r in records if r["package"] == pkg.name]
                  for pkg in packages}
        for pkg in packages:
            pkg_records = by_pkg[pkg.name]
            if not pkg_records:
                continue
            ev_text = observe.evidence_text(pkg, pkg_records)
            laya_call = models_mod.laya_triage(
                args.laya_base, args.laya_model, ev_text,
                call_id=f"laya-{pkg.name}")
            laya_call.raw["package"] = pkg.name
            if laya_call.error:
                laya_call.raw["fallback_note"] = (
                    "laya endpoint error retained; qwen fallback engaged "
                    "(advisory-only, nothing skipped)")
            model_calls.append(_model_record(store, laya_call, pkg.name))
            qwen_call = models_mod.qwen_analyze(
                args.model_base, args.model, ev_text,
                valid_obs_ids=[r["observation_id"] for r in pkg_records],
                call_id=f"qwen-{pkg.name}",
                max_tokens=args.model_max_tokens,
                timeout=args.model_timeout)
            qwen_call.raw["package"] = pkg.name
            model_calls.append(_model_record(store, qwen_call, pkg.name))

    store.update_index(records)
    counts = observe.summary_counts(records, packages, manual_urls)
    model_errors = [c for c in model_calls if c.get("error")]
    if not args.offline and not args.no_model and not model_calls:
        models_note = models_note or "（所选包无观测记录，未调用模型）"
    finished_at = utc_now()

    mode = {
        "offline": args.offline,
        "no_model": args.no_model,
        "model": args.model,
        "model_base": args.model_base,
        "laya_model": args.laya_model,
        "laya_base": args.laya_base,
        "models_note": models_note,
    }
    parameters = {
        "package": args.package,
        "limit": args.limit,
        "url": args.url,
        "fetch_policy": (dict(fetcher.policy) if fetcher else None),
    }
    package_views = []
    for pkg in packages:
        view = observe.package_facts_view(pkg)
        view["name"] = pkg.name
        view["observation_ids"] = [r["observation_id"] for r in records
                                   if r["package"] == pkg.name]
        package_views.append(view)

    summary = report.build_summary(
        run_id=run_id, started_at=started_at, finished_at=finished_at,
        mode=mode, parameters=parameters,
        input_provenance=input_provenance,
        packages=package_views, records=records, counts=counts,
        model_calls=model_calls)
    markdown = report.render_markdown(summary, records, model_calls)
    try:
        store.write_summary(summary, markdown)
    except OSError as exc:
        print(f"gleam: fatal: cannot write summary: {exc}", file=sys.stderr)
        return EXIT_FATAL

    print(f"run {run_id}: {counts['observations']} observations, "
          f"{len(model_calls)} model calls "
          f"({len(model_errors)} model errors)")
    print(f"report: {store.run_dir}/report.md")
    print(f"summary: {store.run_dir}/run.json")
    if counts.get("transport_error_count"):
        print(f"note: {counts['transport_error_count']} transport errors "
              "(recorded as unknown; never inferred as dead)")
    if model_errors:
        print("note: model errors were recorded and are visible in the report")
        return EXIT_COMPLETED_WITH_ERRORS
    if counts.get("transport_error_count"):
        return EXIT_COMPLETED_WITH_ERRORS
    return EXIT_OK


def cmd_investigate(args) -> int:
    if len(args.package) != 1:
        print("gleam: investigate 需要恰好一个 --package", file=sys.stderr)
        return EXIT_USAGE
    if not (1 <= args.model_max_tokens
            <= models_mod.QWEN_MAX_TOKENS_LIMIT):
        print(f"gleam: --model-max-tokens 必须在 [1, "
              f"{models_mod.QWEN_MAX_TOKENS_LIMIT}] 内", file=sys.stderr)
        return EXIT_USAGE
    if not math.isfinite(args.model_timeout) or not (
            0 < args.model_timeout <= models_mod.MODEL_TIMEOUT_LIMIT_S):
        print(f"gleam: --model-timeout 必须为有限正数且不超过 "
              f"{models_mod.MODEL_TIMEOUT_LIMIT_S:g} 秒", file=sys.stderr)
        return EXIT_USAGE
    # 模型 base 形态属模型配置致命错误（README exit 3）：必须在
    # create_run 之前拒绝，否则会留下缺 run.json 的运行目录
    model_base_problem = models_mod.validate_model_base(args.model_base)
    if model_base_problem:
        print(f"gleam: fatal: --model-base 非法: {model_base_problem}",
              file=sys.stderr)
        return EXIT_FATAL
    problems = []
    for key in ("max_rounds", "max_tool_calls", "max_report_repairs",
                "max_body_chars", "max_links", "max_context_chars"):
        value = getattr(args, key)
        low, high = agent_mod.LIMITS_BOUNDS[key]
        if not isinstance(value, int) or not (low <= value <= high):
            problems.append(f"--{key.replace('_', '-')} 必须在 [{low}, "
                            f"{high}] 内")
    if args.max_bytes < 1:
        problems.append("--max-bytes 必须 >= 1")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        problems.append("--timeout 必须为有限正数")
    if args.max_redirects < 0:
        problems.append("--max-redirects 必须 >= 0")
    if args.max_requests < 1:
        problems.append("--max-requests 必须 >= 1")
    if not math.isfinite(args.min_interval) or args.min_interval < 0:
        problems.append("--min-interval 必须为有限非负数")
    if not math.isfinite(args.search_timeout) \
            or not (0 < args.search_timeout <= 60):
        problems.append("--search-timeout 必须为有限正数且不超过 60 秒")
    if not (1 <= args.udd_max_calls <= 50):
        problems.append("--udd-max-calls 必须在 [1, 50] 内")
    if not (1 <= args.udd_max_rows <= 2000):
        problems.append("--udd-max-rows 必须在 [1, 2000] 内")
    # 受信任 base（搜索 / GitHub API）校验：必须在创建任何运行目录之前；
    # 仅操作员配置，绝不来自模型参数
    search_base_problem = (None if args.no_search_web else
                           search_mod.validate_search_base(args.search_base))
    if search_base_problem:
        problems.append(f"--search-base 非法: {search_base_problem}")
    github_base_problem = (None if args.no_github else
                           github_mod.validate_api_base(
                               args.github_api_base))
    if github_base_problem:
        problems.append(f"--github-api-base 非法: {github_base_problem}")
    if problems:
        for problem in problems:
            print(f"gleam: {problem}", file=sys.stderr)
        return EXIT_USAGE

    # 零预取：包名只做 Debian 源名策略校验（不查 UDD、不做 cohort 成员
    # 检查——未知源名在 fetch_udd 里得到诚实的 0 行结果，而不是 exit 3）
    try:
        udd_mod.validate_source(args.package[0])
    except udd_mod.UddError as exc:
        print(f"gleam: --package 非法: {exc}", file=sys.stderr)
        return EXIT_USAGE
    client, release, udd_problem = _resolve_live_input(args)
    if udd_problem:
        print(f"gleam: {udd_problem}", file=sys.stderr)
        return EXIT_USAGE

    store = RunStore(args.store)
    try:
        run_id = store.create_run()
    except OSError as exc:
        print(f"gleam: fatal: cannot create run dir: {exc}", file=sys.stderr)
        return EXIT_FATAL

    started_at = utc_now()
    fetcher = Fetcher(policy={
        "max_bytes": args.max_bytes,
        "timeout_s": args.timeout,
        "max_redirects": args.max_redirects,
        "max_requests": args.max_requests,
        "min_interval_s": args.min_interval,
    })
    limits = {key: getattr(args, key) for key in agent_mod.DEFAULT_LIMITS}
    search_client = None
    if not args.no_search_web:
        try:
            search_client = search_mod.SearxngSearch(
                args.search_base, timeout_s=args.search_timeout)
        except ValueError as exc:
            print(f"gleam: fatal: --search-base 非法: {exc}", file=sys.stderr)
            return EXIT_FATAL
    github_base = None if args.no_github else args.github_api_base
    result = agent_mod.run_investigation(
        package=args.package[0],
        udd_client=(None if args.no_udd else client),
        default_release=release,
        store=store, run_id=run_id,
        model_base=args.model_base, model=args.model,
        max_tokens=args.model_max_tokens, timeout_s=args.model_timeout,
        fetcher=fetcher, limits=limits,
        search_enabled=not args.no_search_web,
        search_client=search_client, github_base=github_base,
        bts_enabled=not args.no_bts,
        udd_enabled=not args.no_udd,
        udd_max_calls=args.udd_max_calls,
        udd_max_rows=args.udd_max_rows)
    finished_at = utc_now()

    outcome_counts: dict[str, int] = {}
    for rec in result["records"]:
        outcome_counts[rec["outcome"]] = (
            outcome_counts.get(rec["outcome"], 0) + 1)
    summary = {
        "run_id": run_id,
        "kind": "investigate",
        "package": args.package[0],
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": {
            "model": args.model,
            "model_base": args.model_base,
            "search_engine": (None if args.no_search_web
                              else f"{agent_mod.SEARCH_ENGINE}@"
                                   f"{args.search_base}"),
            "github_api_base": github_base,
            "extraction": (
                f"trafilatura {extract_mod.TRAFILATURA_VERSION}"
                if extract_mod.HAVE_TRAFILATURA
                else "stdlib-fallback (trafilatura not installed)"),
            "laya": "not called in investigate mode",
            "udd": ("live readonly mirror queries (no startup prefetch; "
                    "fixed SQL templates via psql)"
                    if not args.no_udd else "fetch_udd disabled this run"),
        },
        "parameters": {
            "package": args.package[0],
            "limits": limits,
            "fetch_policy": dict(fetcher.policy),
            "udd_max_calls": args.udd_max_calls,
            "udd_max_rows": args.udd_max_rows,
        },
        "status": result["status"],
        "status_detail": result["status_detail"],
        "counts": {
            "rounds": len(result["rounds"]),
            "tool_calls": result["tool_call_count"],
            "tool_calls_by_name": result.get("tool_calls_by_name", {}),
            "http_requests": result["request_count"],
            "observations": len(result["records"]),
            "by_outcome": outcome_counts,
            "model_errors": sum(1 for r in result["rounds"]
                                if r.get("error")),
        },
        "rounds": result["rounds"],
        "observation_ids": result["observation_ids"],
        "final_report": result["final_report"],
        "final_report_validation": result["final_report_validation"],
        "fallback_final_prose": result["fallback_final_prose"],
        "context_truncations": result["context_truncations"],
        "fact_citation_index": result.get("fact_citation_index", {}),
        "errors": result["errors"],
        "notes": [
            "model output is a proposal pending human review; identity is "
            "never confirmed automatically",
            "zero UDD prefetch: no UDD facts are read at startup; UDD "
            "data exists only after the model calls fetch_udd "
            "(action='list' is catalog-only, no database query)",
            "no cohort membership check is performed for investigate: a "
            "valid Debian source name is required at the CLI (usage error "
            "otherwise); unknown source names yield honest 0-row results "
            "from fetch_udd, never fabricated facts",
            "candidate URLs and quotes are validated against evidence "
            "actually exposed to the model in this run",
            "all model requests/responses/tool results are stored under "
            "models/ with unique round ids; observations and raw bodies "
            "are append-only",
            "search_web targets the operator-configured local SearXNG "
            "instance; raw provider JSON is stored as an observation with "
            "source kind third-party-search-results; snippets are weaker "
            "evidence than fetched pages; failures (unreachable/forbidden/"
            "rate-limited/all-engines-failed) are explicit, never read as "
            "'no results'",
            "github_api goes through the public Fetcher (SSRF/DNS pinning/"
            "robots/byte and request budgets); 404 means unknown (not "
            "death, not 'no releases' proof); rate-limit headers are "
            "exposed verbatim; pagination is explicit and bounded",
        ],
    }
    markdown = report.render_investigation_markdown(summary,
                                                    result["records"])
    try:
        store.write_summary(summary, markdown)
    except OSError as exc:
        print(f"gleam: fatal: cannot write summary: {exc}", file=sys.stderr)
        return EXIT_FATAL
    store.update_index(result["records"])

    final = summary["final_report"]
    n_candidates = len(final["candidates"]) if final else 0
    print(f"investigate {run_id}: status={summary['status']} "
          f"({len(result['rounds'])} rounds, "
          f"{result['tool_call_count']} tool calls, "
          f"{result['request_count']} http requests, "
          f"{len(result['records'])} observations, "
          f"{n_candidates} candidate(s))")
    if summary["status"] != "completed":
        print(f"  detail: {summary['status_detail']}")
    print(f"report: {store.run_dir}/report.md")
    print(f"summary: {store.run_dir}/run.json")
    print("note: report is a proposal pending human review; nothing was "
          "executed")
    if summary["errors"]:
        print(f"note: {len(summary['errors'])} error(s) recorded and "
              f"visible in the report")
    return EXIT_OK if summary["status"] == "completed" \
        else EXIT_COMPLETED_WITH_ERRORS


def _bts_headers_text(redacted: dict, status, url) -> str:
    lines = [f"> {url}", f"< status: {status}"]
    for key in sorted(redacted):
        for value in redacted[key]:
            lines.append(f"< {key}: {value}")
    return "\n".join(lines) + "\n"


def cmd_bug_fetch(args) -> int:
    try:
        bts_mod.validate_bug_id(args.bug)
    except bts_mod.BtsError as exc:
        print(f"gleam: {exc}", file=sys.stderr)
        return EXIT_USAGE
    bounds = (("--bts-max-messages", args.bts_max_messages, 1, 10000),
              ("--bts-max-bytes", args.bts_max_bytes, 1024,
               64 * 1024 * 1024),
              ("--bts-max-requests", args.bts_max_requests, 1, 20),
              ("--bts-timeout", args.bts_timeout, 1.0, 120.0),
              ("--bts-min-interval", args.bts_min_interval, 0.0, 60.0))
    problems = []
    for name, value, low, high in bounds:
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not math.isfinite(value) or not (low <= value <= high):
            problems.append(f"{name} 必须在 [{low}, {high}] 内")
    if problems:
        for problem in problems:
            print(f"gleam: {problem}", file=sys.stderr)
        return EXIT_USAGE

    store = RunStore(args.store)
    try:
        run_id = store.create_run()
    except OSError as exc:
        print(f"gleam: fatal: cannot create run dir: {exc}", file=sys.stderr)
        return EXIT_FATAL

    client = bts_mod.BtsClient(policy={
        "max_messages": args.bts_max_messages,
        "max_bytes": args.bts_max_bytes,
        "timeout_s": args.bts_timeout,
        "max_requests": args.bts_max_requests,
        "min_interval_s": args.bts_min_interval,
    })
    started_at = utc_now()
    try:
        result = client.fetch_bug(
            args.bug, source=args.bts_source,
            allow_robots_disallowed_mbox=args.bts_web_anyway)
        error = None
    except bts_mod.BtsError as exc:
        result, error = None, {"kind": exc.kind, "message": exc.message}
        if exc.attempts:
            error["attempts"] = exc.attempts
    finished_at = utc_now()

    # 证据落盘：robots 原文、（部分）响应体与脱敏响应头、结构化讨论
    raw_refs: dict = {}
    robots_fact = client.robots_fact or {}
    robots_body = robots_fact.get("raw")
    if robots_body is not None:
        raw_refs["robots"] = {
            bts_mod.DEFAULT_BTS_WEB_BASE: store.write_raw(
                "robots-bugs-debian-org.txt", robots_body.encode("utf-8"))}
    headers_text, body_bytes = "", b""
    if result is not None:
        headers_text = "robots: " + json.dumps(
            {k: v for k, v in result["robots"].items()}, ensure_ascii=False)
        headers_text += _bts_headers_text(
            result["headers_redacted"], result["status"],
            result["url"])
        body_bytes = result.pop("body_bytes")
    outcome = "OK" if result is not None else (
        "ABSTAIN" if error and error["kind"] in
        ("robots-disallow", "budget-exhausted", "unsafe-destination")
        else "TRANSPORT_ERROR")
    fetch_outcome = FetchOutcome(
        ok=result is not None,
        status=(result["status"] if result else None),
        final_url=(result["url"] if result else client.base),
        body=body_bytes,
        error=error)
    candidate = Candidate(
        url=(result["url"] if result else
             (bts_mod.build_soap_url(client.base)
              if args.bts_source in ("soap", "auto")
              else bts_mod.build_mbox_url(
                  int(args.bug), client.base))),
        label=f"bts-{args.bts_source}", kind="api",
        provenance=f"operator:bug-fetch:{args.bug}")
    record = observe.observation_record(
        run_id=run_id, seq=1, package=f"bts-{args.bug}",
        candidate=candidate, result=fetch_outcome, outcome=outcome,
        comparison=None, baseline=None,
        reason=("bug-fetch experiment (documented public BTS interface)"
                if result is not None else error["message"]),
        observed_at=finished_at, raw_refs=raw_refs,
        policy=dict(client.policy))
    if body_bytes or result is not None:
        record["raw_refs"]["body"] = store.write_raw(
            "obs-0001.body", body_bytes)
        record["raw_refs"]["headers"] = store.write_raw(
            "obs-0001.headers", headers_text.encode("utf-8"))
    if result is not None:
        discussion = {k: v for k, v in result.items()}
        record["raw_refs"]["discussion"] = store.write_raw(
            f"bug-{args.bug}.discussion.json",
            json.dumps(discussion, ensure_ascii=False, indent=2,
                       sort_keys=True).encode("utf-8"))
    store.write_observation(record)

    summary = {
        "run_id": run_id,
        "kind": "bug-fetch",
        "bug": int(args.bug),
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": {
            "source": args.bts_source,
            "source_selection": (result["source_selection"]
                                 if result is not None else None),
            "attempts": (result["attempts"] if result is not None
                         else error.get("attempts")),
            "bts_base": client.base,
            "user_agent": client.policy["user_agent"],
        },
        "parameters": {"policy": dict(client.policy)},
        "status": "completed" if result is not None else "failed",
        "status_detail": (None if result is not None else error),
        "counts": {
            "http_requests": client.request_count,
            "messages": (result["messages_count"] if result else 0),
            "attachments_metadata_recorded": (
                sum(len(m["attachments"]) for m in result["messages"])
                if result else 0),
        },
        "challenge": (result["challenge"] if result else {
            "encountered": False, "solved": False,
            "trace": client.challenge_trace,
            "note": "fetch failed before/at challenge; see status_detail"}),
        "robots": (result["robots"] if result else
                   {k: v for k, v in robots_fact.items()
                    if k != "parser"}),
        "observation_ids": [record["observation_id"]],
        "notes": [
            "bug discussions are public Debian BTS records; fetches are "
            "bounded read-only; attachments are recorded as metadata "
            "only (never separately extracted, written, or executed; "
            "encoded attachment data may remain inside the preserved "
            "raw response artifact; the SOAP attachments array is not "
            "extracted)",
            "challenge session cookies (if any) stay in client memory and "
            "are never written to artifacts; response headers are stored "
            "redacted",
        ],
    }
    markdown = _render_bug_fetch_markdown(summary, result)
    try:
        store.write_summary(summary, markdown)
    except OSError as exc:
        print(f"gleam: fatal: cannot write summary: {exc}", file=sys.stderr)
        return EXIT_FATAL
    store.update_index([record] if result is not None else [])

    if result is not None:
        selection = result.get("source_selection") or {}
        if selection.get("fallback_used"):
            via = f"{result['attempts'][0]['source']}->{result['source']}"
        else:
            via = result["source"]
        print(f"bug-fetch {run_id}: bug #{args.bug} via {via}: "
              f"{result['messages_count']} message(s), "
              f"{client.request_count} request(s), "
              f"challenge_encountered={result['challenge']['encountered']} "
              f"challenge_solved={result['challenge']['solved']}")
    else:
        print(f"bug-fetch {run_id}: bug #{args.bug} failed: "
              f"{error['kind']}: {error['message']}")
    print(f"report: {store.run_dir}/report.md")
    print(f"summary: {store.run_dir}/run.json")
    return EXIT_OK if result is not None else EXIT_COMPLETED_WITH_ERRORS


def _render_bug_fetch_markdown(summary: dict, result: dict | None) -> str:
    lines = [
        f"# bug-fetch #{summary['bug']}",
        "",
        f"- run: `{summary['run_id']}`（kind=bug-fetch）",
        f"- source: {summary['mode']['source']}"
        + (f"（实际 {summary['mode']['source_selection']['tried'][-1]}"
           f"{('，回退自 ' + summary['mode']['source_selection']['tried'][0])
              if summary['mode']['source_selection'].get('fallback_used')
              else ''}）"
           if summary['mode'].get('source_selection') else ""),
        f"- status: {summary['status']}"
        + ("" if result else f"（{summary['status_detail']['kind']}: "
                            f"{summary['status_detail']['message']}）"),
        f"- http requests: {summary['counts']['http_requests']}",
    ]
    if result is not None:
        ch = result["challenge"]
        lines.append(
            f"- PoW challenge: encountered={ch['encountered']} "
            f"solved={ch['solved']}"
            + (f"（trace: {json.dumps(ch['trace'], ensure_ascii=False)}）"
               if ch["trace"] else "（本次会话未遇到挑战）"))
        lines.append(f"- messages: {result['messages_count']}"
                     f"（source_format={result['parse_meta']['source_format']}）")
        lines.append("")
        lines.append("| # | date | from | subject | msg-id | attach(urls) |")
        lines.append("|---|------|------|---------|--------|----|")
        for m in result["messages"]:
            lines.append(
                f"| {m['index']} | {m.get('date_iso') or m.get('date') or '?'} "
                f"| {m.get('from') or '?'} | {m.get('subject') or '?'} "
                f"| {m.get('message_id') or '?'} "
                f"| {len(m.get('attachments', []))} att, {len(m.get('urls', []))} urls |")
        lines.append("")
        lines.append("- 附件仅登记元数据（文件名/类型/大小/摘要），不单独提取/写出/执行；"
                     "编码后的附件原文仍留在原始响应字节内（mbox 路径）；SOAP attachments "
                     "数组未提取（如实披露）。")
        lines.append("- 原始响应字节与逐消息边界/摘要在本 run 的 raw/ 下（追加式）。")
    else:
        lines.append("")
        lines.append("诚实失败：未取得讨论内容；错误细节见 run.json 的 "
                     "status_detail，原始可用证据仍已追加保存。")
    return "\n".join(lines) + "\n"


def cmd_fetch_udd(args) -> int:
    if args.datasets is not None and args.groups is not None:
        print("gleam: --datasets 与 --groups 互斥（datasets= 是 template "
              "v3 新路径，groups= 是 legacy v2 路径；二选一）",
              file=sys.stderr)
        return EXIT_USAGE
    if args.list_datasets:
        # 纯函数目录（无 DB、无运行目录）；仅用法互斥已在上方拒绝，
        # 其余校验按纯函数语义跳过
        sys.stdout.write(udd_mod.render_catalog())
        return EXIT_OK
    if not args.source:
        print("gleam: fetch-udd 需要 --source（或用 --list-datasets 查看"
              "数据集目录）", file=sys.stderr)
        return EXIT_USAGE
    try:
        udd_mod.validate_source(args.source)
        release = udd_mod.validate_release(args.release)
        dsn = udd_mod.validate_dsn(args.udd_dsn)
        datasets = (
            udd_mod.validate_datasets(
                [item.strip() for item in args.datasets.split(",")
                 if item.strip()])
            if args.datasets is not None else None)
        groups = (udd_mod.validate_groups(args.groups)
                  if datasets is None else None)
        if datasets is None:
            limit = udd_mod.validate_limit(
                args.limit if args.limit is not None
                else udd_mod.DEFAULT_POLICY["max_rows_default"])
        else:
            limit = args.limit  # None → udd 默认 20；显式值走 datasets 硬上限
            if limit is not None:
                udd_mod.validate_limit(
                    limit,
                    hard=int(udd_mod.DEFAULT_POLICY["dataset_rows_hard"]))
    except udd_mod.UddError as exc:
        print(f"gleam: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not math.isfinite(args.udd_timeout) \
            or not (1.0 <= args.udd_timeout <= 180.0):
        print("gleam: --udd-timeout 必须在 [1, 180] 秒内", file=sys.stderr)
        return EXIT_USAGE
    if shutil.which(args.udd_psql) is None \
            and not os.path.isfile(args.udd_psql):
        print(f"gleam: psql 不可用: {args.udd_psql!r}（用 --udd-psql "
              f"指定路径）", file=sys.stderr)
        return EXIT_FATAL
    if datasets is not None:
        policy = udd_mod.DEFAULT_POLICY
        req_limit = (limit if limit is not None
                     else int(policy["dataset_rows_default"]))
        effective = sum(
            min(req_limit, udd_mod.DATASETS[name]["row_cap"])
            if udd_mod.DATASETS[name]["row_cap"] is not None
            else req_limit
            for name in datasets)
        if len(datasets) > int(policy["datasets_max"]):
            print(f"gleam: {len(datasets)} 个 dataset 超过单次上限 "
                  f"{policy['datasets_max']}", file=sys.stderr)
            return EXIT_USAGE
        if effective > int(policy["dataset_rows_total_max"]):
            print(f"gleam: 单次有效行数 {effective} 超过上限 "
                  f"{policy['dataset_rows_total_max']}", file=sys.stderr)
            return EXIT_USAGE

    store = RunStore(args.store)
    try:
        run_id = store.create_run()
    except OSError as exc:
        print(f"gleam: fatal: cannot create run dir: {exc}", file=sys.stderr)
        return EXIT_FATAL

    client = udd_mod.UddClient(
        dsn=dsn, psql_path=args.udd_psql,
        policy={"timeout_s": args.udd_timeout})
    started_at = utc_now()
    try:
        if datasets is not None:
            result = client.fetch(args.source, datasets=list(datasets),
                                  release=release, limit=limit)
        else:
            result = client.fetch(args.source, groups=groups,
                                  release=release, limit=limit)
    except udd_mod.UddError as exc:
        result = {"status": "error",
                  "error": {"kind": exc.kind, "message": exc.message},
                  "groups_data": {}, "source": args.source,
                  "release": release, "fetched_at": utc_now(),
                  "limit": limit, "_raw_stdout": b"",
                  "_stderr": str(exc).encode()}
        if datasets is not None:
            result["datasets"] = list(datasets)
        else:
            result["groups"] = list(groups)
    finished_at = utc_now()
    ok = result.get("status") == "ok"
    raw_stdout = result.pop("_raw_stdout", b"")
    stderr_bytes = result.pop("_stderr", b"")

    raw_refs: dict = {}
    if raw_stdout:
        raw_refs["csv"] = store.write_raw("obs-0001.udd.csv", raw_stdout)
    if stderr_bytes:
        raw_refs["stderr"] = store.write_raw("obs-0001.udd.stderr",
                                             stderr_bytes)
    outcome = "OK" if ok else (
        "ABSTAIN" if (result.get("error") or {}).get("kind")
        in ("argument-invalid", "budget-exhausted") else "TRANSPORT_ERROR")
    fetch_outcome = FetchOutcome(
        ok=ok, status=None,
        final_url=result.get("dsn_display") or udd_mod.dsn_display(dsn),
        body=raw_stdout,
        error=(None if ok else result.get("error")))
    candidate = Candidate(
        url=result.get("dsn_display") or udd_mod.dsn_display(dsn),
        label="udd-fetch", kind="api",
        provenance=f"operator:fetch-udd:{args.source}")
    record = observe.observation_record(
        run_id=run_id, seq=1, package=args.source,
        candidate=candidate, result=fetch_outcome, outcome=outcome,
        comparison=None, baseline=None,
        reason=("UDD mirror query (fixed read-only SQL templates via psql)"
                if ok else (result.get("error") or {}).get("message")),
        observed_at=finished_at, raw_refs=raw_refs,
        policy=dict(client.policy))
    record["method"] = "PSQL-READONLY"
    store.write_observation(record)

    total_rows = sum(g.get("row_count", 0)
                     for g in result.get("groups_data", {}).values())
    truncated_groups = sorted(
        g for g, data in result.get("groups_data", {}).items()
        if data.get("more_available"))
    mode_block = {
        "dsn_display": result.get("dsn_display"),
        "psql": args.udd_psql,
        "release": result.get("release"),
        "template_version": result.get("template_version"),
    }
    counts_block = {
        "queries": result.get("query_count"),
        "rows": total_rows,
        "truncated_groups": truncated_groups,
    }
    if datasets is not None:
        mode_block["datasets"] = result.get("datasets", list(datasets))
        counts_block["datasets"] = len(result.get("groups_data", {}))
    else:
        mode_block["groups"] = result.get("groups", list(groups))
        counts_block["groups"] = len(result.get("groups_data", {}))
    summary = {
        "run_id": run_id,
        "kind": "udd-fetch",
        "source": args.source,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": mode_block,
        "parameters": {
            "policy": dict(client.policy),
            "limit": result.get("limit", limit),
            "inputs": {"source": args.source,
                       "release": result.get("release"),
                       "limit": result.get("limit", limit)},
        },
        "status": "completed" if ok else "failed",
        "status_detail": (None if ok else result.get("error")),
        "server": result.get("server"),
        "fetched_at": result.get("fetched_at"),
        "counts": counts_block,
        "groups_data": result.get("groups_data"),
        "notes": result.get("notes", []) + [
            "rows carry server-rendered source timestamps verbatim; "
            "fetched_at is the client-side retrieval time; the two are "
            "never conflated",
            "queries are fixed read-only SQL templates executed via psql "
            "variables in a single read-only transaction; the exact "
            "template set is identified by template_version",
        ],
        "observation_ids": [record["observation_id"]],
    }
    markdown = _render_udd_markdown(summary)
    try:
        store.write_summary(summary, markdown)
    except OSError as exc:
        print(f"gleam: fatal: cannot write summary: {exc}", file=sys.stderr)
        return EXIT_FATAL
    store.update_index([record] if ok else [])

    unit = "dataset" if datasets is not None else "group"
    unit_count = (counts_block.get("datasets")
                  if datasets is not None else counts_block.get("groups"))
    if ok:
        print(f"fetch-udd {run_id}: source {args.source}: "
              f"{total_rows} row(s) across {unit_count} {unit}(s), "
              f"{result.get('query_count')} quer(y|ies)"
              + (f", truncated: {', '.join(truncated_groups)}"
                 if truncated_groups else ""))
    else:
        error = result.get("error") or {}
        print(f"fetch-udd {run_id}: source {args.source} failed: "
              f"{error.get('kind')}: {error.get('message')}")
    print(f"report: {store.run_dir}/report.md")
    print(f"summary: {store.run_dir}/run.json")
    return EXIT_OK if ok else EXIT_COMPLETED_WITH_ERRORS


def _render_udd_markdown(summary: dict) -> str:
    mode = summary["mode"]
    # datasets= 新路径与 groups= legacy 路径共用渲染：仅单位标签不同
    label = "dataset" if "datasets" in mode else "group"
    names = mode.get("datasets") if "datasets" in mode else mode.get("groups")
    lines = [
        f"# fetch-udd {summary['source']}",
        "",
        f"- run: `{summary['run_id']}`（kind=udd-fetch）",
        f"- endpoint: {mode['dsn_display']}"
        f"（psql {mode['psql']}）",
        f"- {label}s: {', '.join(names or [])}"
        + (f"（release 过滤: {mode['release']}）"
           if mode.get("release") else ""),
        f"- template version: {mode.get('template_version')}",
        f"- status: {summary['status']}"
        + ("" if summary["status"] == "completed"
           else f"（{summary['status_detail']['kind']}: "
                f"{summary['status_detail']['message']}）"),
        f"- fetched_at: {summary.get('fetched_at')}"
        + (f"（server_now: {summary['server']['server_now']}）"
           if summary.get("server") else ""),
    ]
    groups_data = summary.get("groups_data") or {}
    for group in sorted(groups_data):
        data = groups_data[group]
        more = "，more_available=True（显式截断）" \
            if data.get("more_available") else ""
        lines.append(f"- {label} `{group}`: {data.get('row_count', 0)} 行"
                     f"{more}"
                     + (f"——{data['note']}" if data.get("note") else ""))
    if not groups_data and summary["status"] != "completed":
        lines.append("- 无组数据：抓取失败（错误见上）")
    lines.append("")
    lines.append("- 行内时间戳为服务端渲染原文（数据时间），与 fetched_at"
                 "（抓取时间）严格区分。")
    lines.append("- SQL 为应用侧固定模板 + psql 变量（只读事务）；原始 CSV "
                 "输出与本 run 的 raw/ 一致（追加式）。")
    return "\n".join(lines) + "\n"


def _model_record(store: RunStore, call, package: str) -> dict:
    record = {
        "call_id": call.call_id,
        "kind": call.kind,
        "package": package,
        "model": call.model,
        "endpoint": call.endpoint,
        "prompt_sha256": call.prompt_sha256,
        "request_payload": call.request_payload,
        "response_status": call.response_status,
        "response_body_raw": call.response_body,
        "error": call.error,
        "validation": call.validation,
        "parsed": call.parsed,
        "extra": call.raw,
    }
    store.write_model_call(record)
    record["raw_file"] = f"models/{call.call_id}.json"
    return record


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "models":
        return cmd_models(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "investigate":
        return cmd_investigate(args)
    if args.command == "bug-fetch":
        return cmd_bug_fetch(args)
    if args.command == "fetch-udd":
        return cmd_fetch_udd(args)
    parser.error(f"unknown command {args.command!r}")
    return EXIT_USAGE
