#!/usr/bin/env python3
"""统一四面板评测报告生成器（shopping-final-v1）。

把两路已有评测按 task_id 合并，不重跑、不调 LLM、不调 ShopSimulator、不修改任何输入：

- 路线 A（Panel 1/4）：deterministic evaluator 输出
  （reports/<run>/task_results.jsonl + summary.json + failure_breakdown.json）；
- 路线 B（Panel 2/3）：冻结 Rubric（initial_query_only_v1）+ model_trace-only LLM Judge
  的 frozen Judgment（evaluations/.../judgments/<task_id>.json）。

以 benchmark manifest.task_ids 为唯一任务集合，默认严格校验：缺/多任何一路数据即失败
（--allow-missing 降级为警告，用于调试）。

输出（--out 指定 report.json，同目录另写 summary.json / report.md）：
- report.json：完整结构化四面板报告（含每任务对象）；
- summary.json：汇总指标；
- report.md：人类可读报告（不含 gold ASIN / hidden TaskFacts）。

用法:
  python3 eval/report.py \
    --benchmark benchmarks/shopping-final-v1 \
    --run-dir runs/h0-0905-1446 \
    --deterministic reports/h0-0905-1446 \
    --judgments evaluations/shopping-final-v1/h0-0905-1446/judgments \
    --out evaluations/shopping-final-v1/h0-0905-1446/report.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DIMENSION_KEYS = [
    "clarification_strategy",
    "information_retention",
    "search_strategy",
    "candidate_utilization",
    "evidence_verification",
    "decision_quality",
    "termination_efficiency",
]
STATES = ("satisfied", "violated", "unknown", "not_applicable")

OUTCOME_CLASS_KEYS = [
    "success_gold",
    "success_valid_alternative",
    "success_partial_alternative",
    "wrong_purchase",
    "repeat_loop",
    "reward_unverifiable",
    "early_abstain",
    "graceful_stop",
    "max_steps",
]
ANOMALY_KEYS = [
    "repeated_action",
    "no_progress",
    "inferred_invalid_click",
    "inferred_invalid_buy",
    "inferred_invalid_option",
    "inferred_invalid_navigation",
    "superseded_hard_stop",
    "post_terminal_action",
]


def norm_tid(tid) -> str:
    return str(tid)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- #
# 输入加载
# --------------------------------------------------------------------------- #

def load_benchmark(bench_dir):
    bench_dir = Path(bench_dir)
    manifest = load_json(bench_dir / "manifest.json")
    tasks = {}
    for row in load_jsonl(bench_dir / "tasks.jsonl"):
        tasks[norm_tid(row["task_id"])] = row
    return manifest, tasks


def load_dir_jsons(dirpath, ids):
    """按 task_id 加载 <dir>/<tid>.json，返回 (found, missing)。"""
    found, missing = {}, []
    for tid in ids:
        p = Path(dirpath) / f"{tid}.json"
        if p.exists():
            found[tid] = load_json(p)
        else:
            missing.append(tid)
    return found, missing


def extra_ids_in_dir(dirpath, benchmark_ids):
    extras = []
    for f in Path(dirpath).glob("*.json"):
        if f.name == "manifest.json" or f.name.startswith("."):
            continue
        if f.stem.isdigit() and f.stem not in benchmark_ids:
            extras.append(f.stem)
    return sorted(extras, key=int)


def load_deterministic(det_dir):
    det_dir = Path(det_dir)
    rows = {}
    for row in load_jsonl(det_dir / "task_results.jsonl"):
        rows[norm_tid(row["task_id"])] = row
    summary = load_json(det_dir / "summary.json") if (det_dir / "summary.json").exists() else None
    breakdown = (load_json(det_dir / "failure_breakdown.json")
                 if (det_dir / "failure_breakdown.json").exists() else None)
    return rows, summary, breakdown


def count_ask_shopper(traces_dir, ids):
    """只读 model_trace，统计每个任务的 ask_shopper 调用次数。"""
    counts = {}
    traces_dir = Path(traces_dir)
    for tid in ids:
        p = traces_dir / f"{tid}.model_trace.json"
        if not p.exists():
            continue
        trace = load_json(p)
        counts[tid] = sum(1 for s in trace.get("steps") or []
                          if s.get("tool_name") == "ask_shopper")
    return counts


# --------------------------------------------------------------------------- #
# 严格校验
# --------------------------------------------------------------------------- #

def validate_inputs(args, bench_manifest, bench_tasks, bench_ids,
                    det_rows, rubrics, judgments, errors, warnings):
    if bench_manifest.get("task_count") != len(bench_manifest.get("task_ids", [])):
        errors.append("benchmark manifest.task_count 与 task_ids 长度不一致")
    if bench_manifest.get("task_count") != 200:
        warnings.append(f"benchmark task_count = {bench_manifest.get('task_count')} (期望 200)")
    if len(set(bench_ids)) != len(bench_ids):
        errors.append("benchmark task_ids 存在重复")

    def check_coverage(name, have_ids, rows_map):
        have = set(have_ids)
        missing = sorted(bench_ids - have, key=int)
        extra = sorted(have - bench_ids, key=int)
        for tid in missing:
            msg = f"缺少 {name}: task {tid}"
            (warnings if args.allow_missing else errors).append(msg)
        for tid in extra:
            (warnings if args.allow_missing else errors).append(
                f"{name} 出现 benchmark 之外的 task_id: {tid}")

    check_coverage("deterministic result", set(det_rows.keys()), det_rows)
    check_coverage("rubric", set(rubrics.keys()), rubrics)
    check_coverage("judgment", set(judgments.keys()), judgments)

    # judgment 内部完整性：verdict 覆盖、维度分合法、trace_source 隔离声明
    for tid, j in judgments.items():
        if tid not in bench_ids:
            continue
        rub = rubrics.get(tid)
        if rub is None:
            continue
        expected = {c.get("id") for c in rub.get("constraints") or []}
        verdicts = (j.get("judgment") or {}).get("rubric_verdicts") or []
        got = [v.get("rubric_id") for v in verdicts]
        if len(got) != len(set(got)):
            errors.append(f"task {tid}: judgment 存在重复 rubric_id")
        miss = expected - set(got)
        if miss:
            msg = f"task {tid}: judgment 缺少 verdict: {sorted(miss)}"
            (warnings if args.allow_missing else errors).append(msg)
        dims = (j.get("judgment") or {}).get("dimension_scores") or {}
        for k in DIMENSION_KEYS:
            v = dims.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v not in (0, 1, 2):
                errors.append(f"task {tid}: dimension {k} 非法: {v!r}")
        for v in verdicts:
            if v.get("status") not in STATES:
                errors.append(f"task {tid}: verdict {v.get('rubric_id')} status 非法")
            if not isinstance(v.get("step_reference"), list):
                errors.append(f"task {tid}: verdict {v.get('rubric_id')} step_reference 非 list")
        if (j.get("metadata") or {}).get("trace_source") != "model_trace_only":
            errors.append(f"task {tid}: judgment 未声明 trace_source=model_trace_only")

    # step_reference 有效性：需要 model_trace（只读）
    traces_dir = Path(args.run_dir) / "traces" if args.run_dir else None
    if traces_dir is not None and traces_dir.is_dir():
        for tid, j in judgments.items():
            if tid not in bench_ids:
                continue
            tp = traces_dir / f"{tid}.model_trace.json"
            if not tp.exists():
                (warnings if args.allow_missing else errors).append(
                    f"task {tid}: 缺 model_trace，无法校验 step_reference")
                continue
            valid = {s.get("step") for s in load_json(tp).get("steps") or []}
            for v in (j.get("judgment") or {}).get("rubric_verdicts") or []:
                bad = [r for r in v.get("step_reference") or [] if r not in valid]
                if bad:
                    errors.append(f"task {tid}: verdict {v.get('rubric_id')} "
                                  f"step_reference 不存在于 model_trace: {bad}")
    else:
        warnings.append("未找到 model_trace 目录，跳过 step_reference 有效性校验")


# --------------------------------------------------------------------------- #
# 面板构建
# --------------------------------------------------------------------------- #

def build_panel1_task(row):
    o = row.get("outcome") or {}
    return {
        "class": o.get("class"),
        "environment_done": bool(row.get("environment_done")),
        "task_success": bool(row.get("task_success")),
        "reward": o.get("reward"),
        "reward_type": o.get("reward_type"),
        "termination_reason": o.get("termination_reason"),
        "terminal_step_index": o.get("terminal_step_index"),
        # 新旧口径对照（deterministic-evaluator-v2 起可用）：不丢失第一次
        # 真实终局与旧 canonical 终局的差异。
        "first_terminal": row.get("first_terminal"),
        "legacy_canonical_terminal": row.get("legacy_canonical_terminal"),
    }


def build_panel2_task(rubric, judgment):
    verdict_by_id = {v.get("rubric_id"): v
                     for v in (judgment.get("judgment") or {}).get("rubric_verdicts") or []}
    constraints = []
    counts = Counter()
    for c in rubric.get("constraints") or []:
        v = verdict_by_id.get(c.get("id")) or {}
        status = v.get("status")
        constraints.append({
            "rubric_id": c.get("id"),
            "description": c.get("description"),
            "hardness": c.get("hardness"),
            "source": c.get("source"),
            "query_quote": c.get("query_quote"),
            "status": status,
            "step_reference": v.get("step_reference") or [],
            "reasoning": v.get("reasoning"),
        })
        counts[status if status in STATES else "missing"] += 1
    return {
        "constraints": constraints,
        "counts": {s: counts.get(s, 0) for s in STATES},
    }


def build_panel4_task(row):
    anomalies = row.get("anomalies") or []
    return {
        "step_count": row.get("step_count"),
        "agent_turn_completed": bool(row.get("agent_turn_completed")),
        "environment_done": bool(row.get("environment_done")),
        "post_terminal_action": "post_terminal_action" in anomalies,
        "failure_class": (row.get("failure") or {}).get("class"),
        "anomalies": anomalies,
    }


def rate(num, den):
    return round(num / den, 4) if den else None


def summarize_panel1(task_reports, bench_ids):
    classes = Counter()
    n_env_term = n_success = n_null = 0
    for tid in bench_ids:
        p1 = task_reports[tid]["panel1_environment"]
        cls = p1["class"] or "(null)"
        classes[cls] += 1
        n_null += p1["class"] is None
        n_env_term += p1["environment_done"]
        n_success += p1["task_success"]
    total = len(bench_ids)
    return {
        "total": total,
        "environment_terminal_count": n_env_term,
        "environment_terminal_rate": rate(n_env_term, total),
        "task_success_count": n_success,
        "task_success_rate": rate(n_success, total),
        **{k: classes.get(k, 0) for k in OUTCOME_CLASS_KEYS},
        "outcome_classes": dict(classes),
        "outcome_null_count": n_null,
    }


def summarize_panel2(task_reports, bench_ids):
    verdicts = []
    for tid in bench_ids:
        for c in task_reports[tid]["panel2_rubric"]["constraints"]:
            verdicts.append(c)
    total = len(verdicts)
    cnt = Counter(c["status"] for c in verdicts)
    denom = total - cnt.get("not_applicable", 0)

    def sub_rate(filt, sub=None):
        pool = [c for c in verdicts if filt(c)]
        if sub:
            pool = [c for c in pool if sub(c)]
        d = len(pool)
        return rate(sum(1 for c in pool if c["status"] == "satisfied"), d)

    by_hardness = {}
    for h in ("hard", "soft"):
        hc = Counter(c["status"] for c in verdicts if c["hardness"] == h)
        by_hardness[h] = {
            "total": sum(hc.values()),
            **{s: hc.get(s, 0) for s in STATES},
            "satisfaction_rate": rate(hc.get("satisfied", 0),
                                      sum(hc.get(s, 0) for s in ("satisfied", "violated", "unknown"))),
        }
    return {
        "total_constraints": total,
        "verdict_counts": {s: cnt.get(s, 0) for s in STATES},
        "overall_satisfaction_rate": rate(cnt.get("satisfied", 0), denom),
        "hard_satisfaction_rate": by_hardness["hard"]["satisfaction_rate"],
        "soft_satisfaction_rate": by_hardness["soft"]["satisfaction_rate"],
        "hard_violation_rate": rate(by_hardness["hard"].get("violated", 0),
                                    max(by_hardness["hard"]["total"], 1)),
        "unknown_rate": rate(cnt.get("unknown", 0), denom),
        "by_hardness": by_hardness,
        "source_distribution": dict(Counter(c["source"] for c in verdicts)),
        "hardness_distribution": dict(Counter(c["hardness"] for c in verdicts)),
    }


def summarize_panel3(task_reports, bench_ids, bench_tasks, ask_counts):
    scores = {k: [] for k in DIMENSION_KEYS}
    for tid in bench_ids:
        d = task_reports[tid]["panel3_trajectory"]["dimension_scores"]
        for k in DIMENSION_KEYS:
            scores[k].append(d[k])
    average = {k: round(sum(v) / len(v), 4) for k, v in scores.items()}
    distributions = {k: dict(Counter(scores[k])) for k in DIMENSION_KEYS}
    all_vals = [x for v in scores.values() for x in v]
    overall_average = round(sum(all_vals) / len(all_vals), 4) if all_vals else None

    def group_means(key_fn):
        groups = defaultdict(lambda: {k: [] for k in DIMENSION_KEYS})
        for tid in bench_ids:
            g = key_fn(tid)
            d = task_reports[tid]["panel3_trajectory"]["dimension_scores"]
            for k in DIMENSION_KEYS:
                groups[g][k].append(d[k])
        return {g: {k: round(sum(v) / len(v), 3) for k, v in dims.items()}
                for g, dims in sorted(groups.items(), key=lambda kv: str(kv[0]))}

    by_outcome = group_means(lambda tid: task_reports[tid]["panel1_environment"]["class"] or "(null)")
    by_complexity = group_means(
        lambda tid: ((bench_tasks.get(tid) or {}).get("metadata") or {}).get("complexity", "(unknown)"))
    by_domain = group_means(
        lambda tid: ((bench_tasks.get(tid) or {}).get("metadata") or {}).get("domain", "(unknown)"))

    panel = {
        "average_dimensions": average,
        "overall_average": overall_average,
        "dimension_distributions": distributions,
        "by_outcome_class": by_outcome,
        "by_complexity": by_complexity,
        "by_domain": by_domain,
    }

    if ask_counts is not None:
        used = [tid for tid in bench_ids if ask_counts.get(tid, 0) > 0]
        total_calls = sum(ask_counts.get(tid, 0) for tid in bench_ids)
        multi = {
            "ask_shopper_available": True,
            "tasks_with_ask_shopper": len(used),
            "ask_shopper_usage_rate": rate(len(used), len(bench_ids)),
            "total_ask_shopper_calls": total_calls,
            "avg_ask_shopper_calls_per_task": rate(total_calls, len(bench_ids)),
        }
        # 使用澄清与否 × 维度均分
        comp = {}
        for label, pool in (("with_ask_shopper", used),
                            ("without_ask_shopper", [t for t in bench_ids if t not in set(used)])):
            comp[label] = {
                "tasks": len(pool),
                "avg_dimensions": {
                    k: round(sum(task_reports[t]["panel3_trajectory"]["dimension_scores"][k]
                                 for t in pool) / len(pool), 3) if pool else None
                    for k in DIMENSION_KEYS},
            }
        multi["dimension_comparison"] = comp
        panel["multi_turn"] = multi
    else:
        panel["multi_turn"] = {"ask_shopper_available": False,
                               "note": "unavailable: 未提供 model_trace 目录"}
    return panel


def summarize_panel4(task_reports, bench_ids, failure_ids_by_class):
    steps = [task_reports[tid]["panel4_behavior"]["step_count"] or 0 for tid in bench_ids]
    anomaly_tasks = defaultdict(list)
    anomaly_counts = Counter()
    n_nonterm = n_atc = n_post = 0
    for tid in bench_ids:
        p4 = task_reports[tid]["panel4_behavior"]
        n_atc += p4["agent_turn_completed"]
        n_post += p4["post_terminal_action"]
        if not task_reports[tid]["panel1_environment"]["environment_done"]:
            n_nonterm += 1
        for a in p4["anomalies"]:
            anomaly_counts[a] += 1
            anomaly_tasks[a].append(int(tid))
    total = len(bench_ids)
    return {
        "average_steps": round(sum(steps) / total, 2) if total else None,
        "median_steps": statistics.median(steps) if steps else None,
        "min_steps": min(steps) if steps else None,
        "max_steps": max(steps) if steps else None,
        "non_terminal_count": n_nonterm,
        "agent_turn_completed_count": n_atc,
        "agent_turn_completed_rate": rate(n_atc, total),
        "post_terminal_action_count": n_post,
        "anomaly_counts": {k: anomaly_counts.get(k, 0) for k in ANOMALY_KEYS}
                          | {k: v for k, v in anomaly_counts.items() if k not in ANOMALY_KEYS},
        "anomaly_task_ids": dict(anomaly_tasks),
        "failure_classes": {cls: sorted(ids, key=int)
                            for cls, ids in sorted(failure_ids_by_class.items())},
    }


# --------------------------------------------------------------------------- #
# Markdown 渲染
# --------------------------------------------------------------------------- #

def pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def render_markdown(report):
    meta = report["metadata"]
    p1, p2, p3, p4 = report["panel1"], report["panel2"], report["panel3"], report["panel4"]
    L = []
    L.append("# Shopping Evaluation Report")
    L.append("")
    L.append("## Benchmark / Run Metadata")
    L.append("")
    L.append(md_table(["项", "值"], [
        ["benchmark_id", meta["benchmark_id"]],
        ["task_count", meta["task_count"]],
        ["evaluated_task_count", meta["evaluated_task_count"]],
        ["profile", meta.get("profile")],
        ["run_dir", meta.get("run_dir")],
        ["rubric_version", meta["rubric_version"]],
        ["rubric_strategy", meta.get("rubric_strategy")],
        ["judge_model", meta["judge_model"]],
        ["judge_trace_source", meta["judge_trace_source"]],
        ["environment_version", meta["environment_version"]],
        ["generated_at", meta["generated_at"]],
    ]))
    L.append("")

    L.append("## Panel 1: Environment Outcome")
    L.append("")
    L.append(md_table(["指标", "数值"], [
        ["total", p1["total"]],
        ["environment_terminal_count / rate", f"{p1['environment_terminal_count']} / {pct(p1['environment_terminal_rate'])}"],
        ["task_success_count / rate", f"{p1['task_success_count']} / {pct(p1['task_success_rate'])}"],
        ["outcome_null_count", p1["outcome_null_count"]],
    ]))
    L.append("")
    L.append("outcome class 分布（task_success 口径以 deterministic evaluator 为准，"
             "success_partial_alternative 不计入 task_success）：")
    L.append("")
    L.append(md_table(["outcome class", "数量"],
                      [(k, v) for k, v in sorted(p1["outcome_classes"].items(),
                                                 key=lambda kv: -kv[1])]))
    L.append("")

    L.append("## Panel 2: Rubric Satisfaction")
    L.append("")
    vc = p2["verdict_counts"]
    L.append(md_table(["状态", "数量", "比例"],
                      [(s, vc[s], pct(vc[s] / p2["total_constraints"])) for s in STATES]))
    L.append("")
    L.append(md_table(["指标", "数值"], [
        ["total_constraints", p2["total_constraints"]],
        ["overall_satisfaction_rate", pct(p2["overall_satisfaction_rate"])],
        ["hard_satisfaction_rate", pct(p2["hard_satisfaction_rate"])],
        ["soft_satisfaction_rate", pct(p2["soft_satisfaction_rate"])],
        ["hard_violation_rate", pct(p2["hard_violation_rate"])],
        ["unknown_rate", pct(p2["unknown_rate"])],
        ["hardness_distribution", json.dumps(p2["hardness_distribution"], ensure_ascii=False)],
        ["source_distribution", json.dumps(p2["source_distribution"], ensure_ascii=False)],
    ]))
    L.append("")

    L.append("## Panel 3: Trajectory Quality")
    L.append("")
    rows = []
    for k in DIMENSION_KEYS:
        d = p3["dimension_distributions"][k]
        rows.append([k, p3["average_dimensions"][k], d.get(0, 0), d.get(1, 0), d.get(2, 0)])
    L.append(md_table(["维度", "平均分", "0 分", "1 分", "2 分"], rows))
    L.append("")
    L.append(f"所有维度平均分：{p3['overall_average']}")
    mt = p3.get("multi_turn") or {}
    if mt.get("ask_shopper_available"):
        L.append("")
        L.append(f"ask_shopper：使用率 {pct(mt['ask_shopper_usage_rate'])}"
                 f"（{mt['tasks_with_ask_shopper']}/{p1['total']} 任务），"
                 f"总调用 {mt['total_ask_shopper_calls']} 次，"
                 f"平均 {mt['avg_ask_shopper_calls_per_task']} 次/任务")
        comp = mt.get("dimension_comparison") or {}
        if comp:
            L.append("")
            rows = [[k, comp["with_ask_shopper"]["avg_dimensions"][k],
                     comp["without_ask_shopper"]["avg_dimensions"][k]] for k in DIMENSION_KEYS]
            L.append(md_table(["维度", "有 ask_shopper 均分", "无 ask_shopper 均分"], rows))
    else:
        L.append("")
        L.append("ask_shopper 统计：unavailable（未提供 model_trace 目录）")
    L.append("")

    L.append("## Panel 4: Deterministic / Harness Behavior")
    L.append("")
    L.append(md_table(["指标", "数值"], [
        ["平均步数", p4["average_steps"]],
        ["中位步数", p4["median_steps"]],
        ["最小 / 最大步数", f"{p4['min_steps']} / {p4['max_steps']}"],
        ["non_terminal_count", p4["non_terminal_count"]],
        ["agent_turn_completed", f"{p4['agent_turn_completed_count']} / {pct(p4['agent_turn_completed_rate'])}"],
        ["post_terminal_action_count", p4["post_terminal_action_count"]],
    ]))
    L.append("")
    L.append("anomaly 分布：")
    L.append("")
    L.append(md_table(["anomaly", "数量"],
                      [(k, v) for k, v in sorted(p4["anomaly_counts"].items(),
                                                 key=lambda kv: -kv[1]) if v]))
    L.append("")
    L.append("failure classes：")
    L.append("")
    for cls, ids in p4["failure_classes"].items():
        shown = ", ".join(str(i) for i in ids[:20]) + ("…" if len(ids) > 20 else "")
        L.append(f"- **{cls}** ({len(ids)}): {shown}")
    L.append("")

    L.append("## Per-task Examples")
    L.append("")
    for ex in report.get("examples") or []:
        L.append(f"### {ex['label']} — task {ex['task_id']}")
        L.append("")
        L.append(f"> Query: {ex['query']}")
        L.append("")
        t = report["tasks"][str(ex["task_id"])]
        e1 = t["panel1_environment"]
        L.append(f"- Panel 1: class={e1['class']}, reward_type={e1['reward_type']}, "
                 f"task_success={e1['task_success']}")
        c2 = t["panel2_rubric"]["counts"]
        L.append(f"- Panel 2: satisfied={c2['satisfied']}, violated={c2['violated']}, "
                 f"unknown={c2['unknown']}")
        for c in t["panel2_rubric"]["constraints"]:
            L.append(f"  - {c['rubric_id']} [{c['hardness']}] {c['description']} → "
                     f"{c['status']} (steps {c['step_reference']})")
        d3 = t["panel3_trajectory"]["dimension_scores"]
        L.append("- Panel 3: " + ", ".join(f"{k}={v}" for k, v in d3.items()))
        b4 = t["panel4_behavior"]
        L.append(f"- Panel 4: steps={b4['step_count']}, anomalies={b4['anomalies']}")
        L.append("")

    L.append("---")
    L.append("")
    L.append("*本报告只合并已有评测产物：Panel 1/4 来自 deterministic evaluator，"
             "Panel 2/3 来自 model_trace-only LLM Judge + 冻结 Query-grounded Rubric。"
             "不包含任何 gold ASIN / hidden TaskFacts，各面板不合成单一总分。*")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def pick_examples(task_reports, bench_ids, ask_counts):
    """每类示例取第一个命中的 task。不引用任何 hidden 信息。"""
    examples = []
    by_outcome = defaultdict(list)
    by_failure = defaultdict(list)
    for tid in bench_ids:
        by_outcome[task_reports[tid]["panel1_environment"]["class"]].append(tid)
        fc = task_reports[tid]["panel4_behavior"]["failure_class"]
        if fc:
            by_failure[fc].append(tid)

    def add(label, tid):
        if tid is not None:
            examples.append({"label": label, "task_id": int(tid),
                             "query": task_reports[tid]["query"]})

    add("gold success", (by_outcome.get("success_gold") or [None])[0])
    add("invalid_buy_then_false_completion",
        (by_failure.get("invalid_buy_then_false_completion") or [None])[0])
    add("repeat_loop", (by_outcome.get("repeat_loop") or [None])[0])
    add("non_terminal_agent_stop", (by_failure.get("non_terminal_agent_stop") or [None])[0])
    if ask_counts:
        used = [tid for tid in bench_ids if ask_counts.get(tid, 0) > 0]
        add("with ask_shopper", used[0] if used else None)
    return examples


def main():
    ap = argparse.ArgumentParser(description="统一四面板评测报告生成器")
    ap.add_argument("--benchmark", default="benchmarks/shopping-final-v1")
    ap.add_argument("--run-dir", default=None, help="run 目录（提供 manifest 与 traces）")
    ap.add_argument("--deterministic", required=True,
                    help="deterministic evaluator 输出目录（含 task_results.jsonl）")
    ap.add_argument("--judgments", default=None, help="judgment 目录（v1）")
    ap.add_argument("--rubrics", default=None,
                    help="rubric 目录（默认 <benchmark>/rubrics）")
    ap.add_argument("--out", required=True, help="report.json 输出路径")
    ap.add_argument("--allow-missing", action="store_true",
                    help="调试用：缺数据降级为警告而非失败")
    # ── report v2 分支 ──
    ap.add_argument("--rubric-version", default="v1", choices=["v1", "v2"],
                    help="v2：读取 rubric v2 时间线与 judgment v2（来源/分母/版本可解释）")
    ap.add_argument("--rubrics-v2", default=None, help="rubric v2 目录")
    ap.add_argument("--judgments-v2", default=None, help="judgment v2 目录")
    ap.add_argument("--events", default=None, help="事件目录（可选，供澄清/同步统计）")
    args = ap.parse_args()

    if args.rubric_version == "v2":
        if not args.rubrics_v2 or not args.judgments_v2:
            print("--rubric-version v2 需要 --rubrics-v2 与 --judgments-v2", file=sys.stderr)
            return 2
        import eval.report_v2 as report_v2  # noqa: PLC0415
        return report_v2.run(args)

    if not args.judgments:
        print("v1 模式需要 --judgments", file=sys.stderr)
        return 2

    errors, warnings = [], []

    # 1. benchmark（唯一任务集合）
    bench_dir = Path(args.benchmark)
    if not (bench_dir / "manifest.json").exists():
        print(f"[失败] benchmark manifest 不存在: {bench_dir}", file=sys.stderr)
        sys.exit(1)
    bench_manifest, bench_tasks = load_benchmark(bench_dir)
    bench_ids = sorted({norm_tid(t) for t in bench_manifest["task_ids"]}, key=int)

    # 2. 三路数据
    rubrics_dir = Path(args.rubrics) if args.rubrics else bench_dir / "rubrics"
    rubrics, missing_rubrics = load_dir_jsons(rubrics_dir, bench_ids)
    judgments, missing_judgments = load_dir_jsons(args.judgments, bench_ids)
    det_rows, det_summary, failure_breakdown = load_deterministic(args.deterministic)

    errors.extend(f"rubric 目录多出未知 task: {t}" for t in extra_ids_in_dir(rubrics_dir, set(bench_ids)))
    errors.extend(f"judgment 目录多出未知 task: {t}" for t in extra_ids_in_dir(args.judgments, set(bench_ids)))
    det_ids = set(det_rows.keys())
    extra_det = sorted(det_ids - set(bench_ids), key=int)
    errors.extend(f"deterministic 多出未知 task: {t}" for t in extra_det)

    validate_inputs(args, bench_manifest, bench_tasks, set(bench_ids),
                    det_rows, rubrics, judgments, errors, warnings)

    # 统一报告保留并校验 deterministic 评测的协议信息：同一报告内必须使用
    # 同一终局口径；无声明（历史输出）视为 legacy，与显式口径混用报错。
    det_protocol_values = {
        (row.get("terminal_protocol") or "legacy-unlabeled")
        for row in det_rows.values()
    }
    det_evaluator_values = {
        (row.get("evaluator_version") or "legacy-unlabeled")
        for row in det_rows.values()
    }
    if len(det_protocol_values) > 1:
        errors.append(
            f"deterministic 结果混用终局协议（需先统一口径再报告）: "
            f"{sorted(det_protocol_values)}"
        )
    if len(det_evaluator_values) > 1:
        warnings.append(
            f"deterministic 结果混用评测器版本: {sorted(det_evaluator_values)}"
        )
    det_terminal_protocol = (
        next(iter(det_protocol_values)) if det_protocol_values else "legacy-unlabeled"
    )
    det_evaluator_version = (
        next(iter(det_evaluator_values)) if det_evaluator_values else "legacy-unlabeled"
    )

    if errors:
        print(f"[失败] 共 {len(errors)} 个校验错误：", file=sys.stderr)
        for e in errors[:30]:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    for w in warnings:
        print(f"[警告] {w}", file=sys.stderr)

    # 3. ask_shopper（只读 model_trace，可选）
    traces_dir = (Path(args.run_dir) / "traces") if args.run_dir else None
    ask_counts = count_ask_shopper(traces_dir, bench_ids) if traces_dir and traces_dir.is_dir() else None

    # 4. 逐任务合并
    evaluated = [tid for tid in bench_ids
                 if tid in det_rows and tid in rubrics and tid in judgments]
    task_reports = {}
    for tid in evaluated:
        row = det_rows[tid]
        task_row = bench_tasks.get(tid) or {}
        task_reports[tid] = {
            "task_id": int(tid),
            "query": task_row.get("query") or rubrics[tid].get("query") or "",
            "metadata": {
                "domain": (task_row.get("metadata") or {}).get("domain"),
                "complexity": (task_row.get("metadata") or {}).get("complexity"),
            },
            "panel1_environment": build_panel1_task(row),
            "panel2_rubric": build_panel2_task(rubrics[tid], judgments[tid]),
            "panel3_trajectory": {
                "dimension_scores": (judgments[tid].get("judgment") or {}).get("dimension_scores") or {},
            },
            "panel4_behavior": build_panel4_task(row),
        }

    # 严格模式下缺任务已报错；--allow-missing 时只报告存在的任务
    if not evaluated:
        print("[失败] 没有任何可合并的任务", file=sys.stderr)
        sys.exit(1)

    # failure class → task ids（以 task_results 的 failure 字段为准）
    failure_ids = defaultdict(list)
    for tid in evaluated:
        fc = task_reports[tid]["panel4_behavior"]["failure_class"]
        if fc:
            failure_ids[fc].append(int(tid))
    if failure_breakdown:
        for cls, ids in (failure_breakdown.get("failure_classes") or {}).items():
            failure_ids.setdefault(cls, [])  # 保留 0 计数类
            if sorted(ids) != sorted(failure_ids.get(cls, [])):
                warnings.append(f"failure_breakdown 与 task_results 不一致: {cls}")

    panel1 = summarize_panel1(task_reports, evaluated)
    panel2 = summarize_panel2(task_reports, evaluated)
    panel3 = summarize_panel3(task_reports, evaluated, bench_tasks, ask_counts)
    panel4 = summarize_panel4(task_reports, evaluated, failure_ids)

    # 与 deterministic summary.json 交叉核对（不篡改，只告警）
    if det_summary:
        for key in ("task_success_count", "task_success_rate", "non_terminal_count",
                    "post_terminal_action_count"):
            if key in det_summary and det_summary[key] != panel1.get(key, panel4.get(key)):
                warnings.append(f"panel 汇总与 deterministic summary.json 不一致: {key} "
                                f"({panel1.get(key, panel4.get(key))} vs {det_summary[key]})")

    jmanifest_path = Path(args.judgments) / "manifest.json"
    jmanifest = load_json(jmanifest_path) if jmanifest_path.exists() else {}
    rmanifest_path = rubrics_dir / "manifest.json"
    rmanifest = load_json(rmanifest_path) if rmanifest_path.exists() else {}
    run_manifest = (load_json(Path(args.run_dir) / "manifest.json")
                    if args.run_dir and (Path(args.run_dir) / "manifest.json").exists() else {})

    metadata = {
        "benchmark_id": bench_manifest.get("benchmark_id"),
        "task_count": len(bench_ids),
        "evaluated_task_count": len(evaluated),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": run_manifest.get("profile"),
        "run_dir": args.run_dir,
        "deterministic_source": str(Path(args.deterministic)),
        "judgment_source": str(Path(args.judgments)),
        "rubric_source": str(rubrics_dir),
        "judge_model": jmanifest.get("model"),
        "judge_trace_source": "model_trace_only",
        "model_trace_only_for_judge": True,
        "rubric_version": rmanifest.get("rubric_version", "v1"),
        "rubric_strategy": rmanifest.get("policy", {}).get("initial_query_only_first_version")
                           and "initial_query_only_v1" or "unknown",
        "environment_version": bench_manifest.get("environment_version"),
        "deterministic_terminal_protocol": det_terminal_protocol,
        "deterministic_evaluator_version": det_evaluator_version,
        "ask_shopper_source": ("model_trace" if ask_counts is not None else "unavailable"),
        "field_missing": [w for w in warnings if "缺" in w or "unavailable" in w.lower()] or None,
        "warnings": warnings or None,
    }

    report = {
        "metadata": metadata,
        "panel1": panel1,
        "panel2": panel2,
        "panel3": panel3,
        "panel4": panel4,
        "examples": pick_examples(task_reports, evaluated, ask_counts),
        "tasks": task_reports,
    }

    summary = {
        "benchmark_id": metadata["benchmark_id"],
        "task_count": metadata["task_count"],
        "evaluated_task_count": metadata["evaluated_task_count"],
        "panel1": {
            "task_success_rate": panel1["task_success_rate"],
            "task_success_count": panel1["task_success_count"],
            "environment_terminal_rate": panel1["environment_terminal_rate"],
            "outcome_classes": panel1["outcome_classes"],
        },
        "panel2": {
            "verdict_counts": panel2["verdict_counts"],
            "satisfaction_rate": panel2["overall_satisfaction_rate"],
            "hard_satisfaction_rate": panel2["hard_satisfaction_rate"],
            "soft_satisfaction_rate": panel2["soft_satisfaction_rate"],
            "unknown_rate": panel2["unknown_rate"],
        },
        "panel3": {
            "average_dimensions": panel3["average_dimensions"],
            "overall_average": panel3["overall_average"],
            "dimension_distributions": panel3["dimension_distributions"],
            "multi_turn": panel3.get("multi_turn"),
        },
        "panel4": {
            "average_steps": panel4["average_steps"],
            "median_steps": panel4["median_steps"],
            "anomaly_counts": panel4["anomaly_counts"],
            "failure_classes": {k: len(v) for k, v in panel4["failure_classes"].items()},
        },
        "metadata": metadata,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_path.parent / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_path.parent / "report.md").write_text(render_markdown(report), encoding="utf-8")

    print(f"合并 {len(evaluated)}/{len(bench_ids)} 个任务")
    print(f"Panel1 task_success: {panel1['task_success_count']}/{panel1['total']} "
          f"({pct(panel1['task_success_rate'])})")
    print(f"Panel2 satisfaction: {pct(panel2['overall_satisfaction_rate'])}，"
          f"unknown {pct(panel2['unknown_rate'])}")
    print(f"Panel3 维度均分: " +
          " ".join(f"{k}={v}" for k, v in panel3["average_dimensions"].items()))
    print(f"Panel4 平均步数 {panel4['average_steps']}，anomalies: {panel4['anomaly_counts']}")
    print(f"report  -> {out_path}")
    print(f"summary -> {out_path.parent / 'summary.json'}")
    print(f"md      -> {out_path.parent / 'report.md'}")


if __name__ == "__main__":
    main()
