"""运行摘要：run.json（机器可读）与 report.md（人工审核）。

报告原则：展示观测 ID、缺失/未知/错误计数、模型提案（带证据引用）、
对上一次证据的引用；模型输出一律标注为提案/咨询，不构成行动。
"""

from __future__ import annotations

import json


def build_summary(*, run_id: str, started_at: str, finished_at: str,
                  mode: dict, parameters: dict, input_provenance: dict,
                  packages: list[dict], records: list[dict],
                  counts: dict, model_calls: list[dict]) -> dict:
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": mode,
        "parameters": parameters,
        "input_provenance": input_provenance,
        "counts": counts,
        "packages": packages,
        "observation_ids": [r["observation_id"] for r in records],
        "model_calls": [
            {"call_id": c["call_id"], "package": c.get("package"),
             "kind": c["kind"], "model": c["model"],
             "validation": c.get("validation"),
             "error": c.get("error"),
             "parsed": c.get("parsed"),
             "extra": c.get("extra"),
             "raw_file": c.get("raw_file")}
            for c in model_calls],
        "notes": [
            "observations are append-only; prior runs are never modified",
            "model output is advisory proposal only; no action is executed",
            "no identity merge / status machine is performed",
        ],
    }


def _input_provenance_lines(prov: dict) -> list[str]:
    """输入出处行（live UDD / 离线缓存两形态；未知形态如实标注）。"""

    lines = []
    kind = prov.get("kind", "unknown")
    if kind in ("udd-live", "udd-cache"):
        cache = prov.get("cache") or {}
        suffix = ""
        if kind == "udd-cache":
            suffix = "（offline：读取显式 --facts-cache，非 live；见 cache 字段）"
        lines.append(
            f"- 输入（{kind}）：`{prov.get('dsn_display')}` "
            f"(release={prov.get('release')}；QA 过滤 "
            f"`{prov.get('qa_filter')}`；固定只读模板 "
            f"{json.dumps(prov.get('template_versions') or {},
                          ensure_ascii=False)}){suffix}")
        selection = prov.get("selection") or {}
        if selection.get("mode") in ("named-fast-path", "offline-cache"):
            lines.append(f"  - 选择方式：{selection['mode']}"
                         f"（逐包快速路径，不枚举 cohort）")
        elif selection.get("mode") == "cohort":
            more = ("，more_available（显式截断）"
                    if selection.get("more_available") else "")
            lines.append(f"  - 选择方式：cohort 枚举 limit="
                         f"{selection.get('limit')} → {selection.get(
                             'returned')} 包{more} "
                         f"(cohort fetched_at {selection.get('fetched_at')})")
        if isinstance(cache, dict) and cache.get("root"):
            lines.append(f"  - facts 缓存：`{cache['root']}`"
                         f"（{cache.get('mode', '')}）")
        packages = prov.get("packages") or {}
        for name in sorted(packages):
            p = packages[name] or {}
            extra = ""
            pcache = p.get("cache") or {}
            if pcache:
                extra = (f"；离线缓存（原始 fetched_at "
                         f"{pcache.get('original_fetched_at')}，可能过期）")
            lines.append(
                f"  - {name}: fetched_at {p.get('fetched_at')} "
                f"response_sha256 `{str(p.get('response_sha256'))[:16]}…` "
                f"({p.get('template_version')}; "
                f"{json.dumps(p.get('row_counts') or {},
                              ensure_ascii=False)}){extra}")
    else:
        lines.append(f"- 输入：{json.dumps(prov, ensure_ascii=False)[:200]}"
                     "（未知形态，如实原样展示）")
    return lines


def _md_escape(text: str) -> str:
    """表格单元格转义：管道、换行一律转义；超长截断以省略号显式披露。"""

    cell = (str(text or "").replace("|", "\\|")
            .replace("\n", " ").replace("\r", " "))
    if len(cell) > 160:
        cell = cell[:160] + "…"
    return cell


def render_markdown(summary: dict, records: list[dict],
                    model_calls: list[dict]) -> str:
    mode = summary["mode"]
    counts = summary["counts"]
    lines = []
    lines.append(f"# Gleam 观测报告 `{summary['run_id']}`")
    lines.append("")
    lines.append(f"- 时间：{summary['started_at']} → {summary['finished_at']}")
    lines.append(f"- 模式：offline={mode.get('offline')} no_model={mode.get('no_model')} "
                 f"model={mode.get('model')} laya={mode.get('laya_model')} "
                 f"(laya_base={mode.get('laya_base')})")
    prov = summary["input_provenance"]
    lines.extend(_input_provenance_lines(prov))
    lines.append("")
    lines.append("## 计数")
    lines.append("")
    lines.append(f"- 选定包：{counts.get('packages_selected')}"
                 f"；观测：{counts.get('observations')}")
    by = counts.get("by_outcome") or {}
    lines.append(f"- outcome 分布：{json.dumps(by, ensure_ascii=False)}")
    if counts.get("packages_without_any_candidate"):
        lines.append(f"- **缺失候选**（快照中无任何上游候选）："
                     f"{', '.join(counts['packages_without_any_candidate'])}")
    if counts.get("transport_error_count"):
        lines.append(f"- **未知**（传输错误，按 09 §6 不构成死亡结论）："
                     f"{counts['transport_error_count']} 条")
    lines.append("")

    lines.append("## 观测明细")
    lines.append("")
    lines.append("| 观测 ID | 包 | 标签 | URL | outcome | 状态码 | 对比 | baseline |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for rec in records:
        base = rec.get("baseline") or {}
        base_ref = (f"{base.get('observation_id')}@{base.get('run_id', '')[:15]}"
                    if base else "—")
        reason = f" ({rec['reason']})" if rec.get("reason") else ""
        err = f" err={rec['error']['kind']}" if rec.get("error") else ""
        # 包名/标签/URL 均来自不可信快照数据，单元格一律转义
        lines.append(
            f"| `{rec['observation_id']}` | {_md_escape(rec['package'])} "
            f"| {_md_escape(rec['endpoint']['label'])} "
            f"| {_md_escape(rec['endpoint']['url'])} "
            f"| {rec['outcome']}{reason}{err} | {rec.get('status_code')} "
            f"| {rec.get('comparison') or '—'} | {base_ref} |")
    lines.append("")

    lines.append("## 模型结果（提案/咨询，不构成行动）")
    lines.append("")
    if not model_calls:
        lines.append(mode.get("models_note", "（未启用模型）"))
    else:
        for call in model_calls:
            lines.append(f"### `{call['call_id']}` — {_md_escape(call.get('package'))} / {call['kind']} "
                         f"({_md_escape(call.get('model'))})")
            if call.get("error"):
                lines.append(f"- **模型错误（如实展示）**：`{call['error']['kind']}` "
                             f"{_md_escape(call['error'].get('message', ''))}")
            if call.get("kind") == "laya-triage" and call.get("parsed"):
                p = call["parsed"]
                lines.append(f"- Laya 咨询（advisory-only，不据此跳过任何包）："
                             f"choice={p.get('choice')}，probabilities="
                             f"{json.dumps(p.get('probabilities'), ensure_ascii=False)}")
            if call.get("kind") == "qwen-analysis" and call.get("parsed"):
                p = call["parsed"]
                abstain = p.get("abstain")
                lines.append(f"- Qwen abstain={abstain}；摘要：{_md_escape(p.get('summary', ''))}")
                for prop in p.get("proposals", []):
                    ids = ", ".join(f"`{i}`" for i in prop.get("evidence_obs_ids", []))
                    lines.append(f"  - 提案 `{prop.get('action')}`（证据 {ids}）："
                                 f"{_md_escape(prop.get('rationale', ''))}")
            if call.get("raw_file"):
                lines.append(f"- 原始请求/响应：`{call['raw_file']}`（prompt_sha256 "
                             f"{(call.get('prompt_sha256') or '')[:16]}…）")
            lines.append("")
    lines.append("## 边界声明")
    lines.append("")
    lines.append("- 探针错误 = 未知，绝不推断上游死亡；字节哈希变化仅为表示层变化。")
    lines.append("- 候选仅来自 live UDD 事实字段与操作员显式 `--url`；无自动开放网页搜索。")
    lines.append("- 输入事实是 live UDD 数据库行（Debian 打包元数据）；sources/vcswatch 的 "
                 "VCS 字段是 Debian 打包出处（packaging provenance），不构成上游身份的"
                 "证明或反证；上游与打包仓库可能共址。")
    lines.append("- watch_file 原样保留（live UDD 逐字抓回），从不执行 uscan 或其正则。")
    lines.append("- 仓库根 `raw/` 是历史归档快照，不是本次运行的输入；本 run 的原始证据"
                 "在 `<store>/runs/<id>/raw/`（追加式）。")
    lines.append("- 模型仅访问操作员配置的本地端点；其输出需人工复核后才可能成为行动。")
    return "\n".join(lines) + "\n"


def render_investigation_markdown(summary: dict, records: list[dict]) -> str:
    """investigate 模式的人审报告：轮次轨迹、观测、受验终报与边界声明。"""

    lines = []
    lines.append(f"# Gleam 调查报告 `{summary['run_id']}` "
                 f"（package: {summary['package']}）")
    lines.append("")
    lines.append(f"- 时间：{summary['started_at']} → {summary['finished_at']}")
    lines.append(f"- 状态：**{summary['status']}** — {summary['status_detail']}")
    lines.append(f"- 模式：investigate（model={summary['mode']['model']}，"
                 f"search={summary['mode'].get('search_engine') or 'disabled'}，"
                 f"Laya 未参与）")
    if summary["mode"].get("udd"):
        lines.append(f"- UDD：{summary['mode']['udd']}")
    # investigate 模式零预取：run.json 不含 input_provenance（无启动事实
    # 抓取）；run 模式照旧渲染
    prov = summary.get("input_provenance")
    if prov:
        lines.extend(_input_provenance_lines(prov))
    counts = summary["counts"]
    lines.append(f"- 预算消耗：{counts['rounds']} 轮 / "
                 f"{counts['tool_calls']} 次工具调用 / "
                 f"{counts['http_requests']} 次 HTTP 请求 / "
                 f"{counts['observations']} 条观测")
    lim = summary["parameters"]["limits"]
    budget = (f"- 预算上限：rounds={lim['max_rounds']} "
              f"tool_calls={lim['max_tool_calls']} "
              f"requests={summary['parameters']['fetch_policy']['max_requests']} "
              f"context_chars={lim['max_context_chars']}")
    udd_calls = summary["parameters"].get("udd_max_calls")
    udd_rows = summary["parameters"].get("udd_max_rows")
    if udd_calls is not None and udd_rows is not None:
        budget += f" udd_calls={udd_calls} udd_rows={udd_rows}"
    lines.append(budget)
    if summary.get("context_truncations"):
        n = len(summary["context_truncations"])
        lines.append(f"- **上下文截断披露**：{n} 次工具结果因上下文预算被折半缩短"
                     "（原始内容完整保留在本运行 models/ 与 raw/ 中）")
    lines.append("")

    lines.append("## 轮次轨迹")
    lines.append("")
    lines.append("| 轮 | 调用 ID | finish_reason | 工具调用 |")
    lines.append("|---|---|---|---|")
    for rnd in summary["rounds"]:
        calls = "; ".join(
            f"`{_md_escape(tc.get('name'))}`"
            + ("（拒绝）" if tc.get("error") else "")
            for tc in rnd.get("tool_calls", []))
        err = f" err={rnd['error']['kind']}" if rnd.get("error") else ""
        lines.append(f"| {rnd['round']} | `{rnd['call_id']}` "
                     f"| {_md_escape(rnd.get('finish_reason'))}{err} "
                     f"| {calls or '—'} |")
    lines.append("")

    if records:
        lines.append("## 观测明细")
        lines.append("")
        lines.append("| 观测 ID | 标签 | URL | outcome | 状态码 |")
        lines.append("|---|---|---|---|---|")
        for rec in records:
            err = f" err={rec['error']['kind']}" if rec.get("error") else ""
            lines.append(f"| `{rec['observation_id']}` "
                         f"| {_md_escape(rec['endpoint']['label'])} "
                         f"| {_md_escape(rec['endpoint']['url'])} "
                         f"| {rec['outcome']}{err} "
                         f"| {rec.get('status_code')} |")
        lines.append("")

    lines.append("## 终报（提案，待人工复核；不构成身份确认）")
    lines.append("")
    final = summary.get("final_report")
    if final:
        lines.append(f"- 摘要：{_md_escape(final.get('summary', ''))}")
        for i, cand in enumerate(final.get("candidates", [])):
            ids = ", ".join(f"`{o}`" for o in cand.get(
                "supporting_obs_ids", []))
            lines.append(f"### 候选 {i + 1}：`{_md_escape(cand.get('url'))}`")
            lines.append(f"- 理由：{_md_escape(cand.get('why', ''))}")
            lines.append(f"- 支撑观测：{ids}")
            lines.append(f"- 引用：> {_md_escape(cand.get('quote', ''))}")
            if cand.get("uncertainty"):
                lines.append(f"- 不确定性：{_md_escape(cand['uncertainty'])}")
        lines.append(f"- 总体不确定性：{_md_escape(final.get('uncertainties', ''))}")
        if final.get("fact_evidence"):
            lines.append(f"- live UDD 事实引用（数据库出处，区别于 live 页面观测）："
                         f"{_md_escape(', '.join(final['fact_evidence']))}")
        lines.append(f"- 校验：`{summary.get('final_report_validation')}`"
                     "（URL 与引用均对照本轮实际暴露给模型的证据验证）")
    else:
        lines.append("（未产出受验终报）")
        if summary.get("fallback_final_prose"):
            lines.append("")
            lines.append("模型最终散文（原样保留，不视为已发现的结论）：")
            lines.append("")
            lines.append("> " + _md_escape(summary["fallback_final_prose"]))
        if summary.get("final_report_validation"):
            lines.append("")
            lines.append("最后一次 finish_investigation 被拒的问题：")
            for problem in summary["final_report_validation"][:10]:
                lines.append(f"- {_md_escape(problem)}")
    lines.append("")

    if summary.get("errors"):
        lines.append("## 错误（如实展示）")
        lines.append("")
        for err in summary["errors"][:30]:
            lines.append(f"- {_md_escape(err)}")
        lines.append("")

    lines.append("## 边界声明")
    lines.append("")
    lines.append("- 本报告是**待人工复核的提案**：绝不自动确认上游身份，不执行任何行动。")
    lines.append("- 候选 URL 与引用已对照本轮证据校验：仅凭模型记忆的 URL、非观测正文"
                 "子串的引用一律拒绝。")
    lines.append("- 探针错误 = 未知，绝不推断上游死亡；robots 拒绝 / 超时 / 网络不可达"
                 "按原样呈现。")
    lines.append("- search_web 指向操作员自配置的本地 SearXNG（JSON API）；"
                  "原始提供商 JSON 以观测留档（source kind = 第三方搜索结果），"
                  "snippet 属弱证据；其失败（不可达/被拒/限流/全引擎失败）如实"
                  "返回，绝不伪造结果、绝不解读为“确认无结果”。")
    lines.append("- github_api 经公网 Fetcher（SSRF/DNS 钉扎/robots/限额全套"
                  "复用）；404 = 未知（不证明死亡、不证明无发布）；限流头原样"
                  "暴露；分页有界、绝不自动跟随。")
    lines.append("- 模型每轮原始请求/响应/工具结果存于 `models/inv-r*.json`；"
                 "抓取观测与原始字节追加式存于 `observations/` 与 `raw/`。")
    return "\n".join(lines) + "\n"
