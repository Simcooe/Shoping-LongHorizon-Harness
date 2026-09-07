#!/usr/bin/env python3
"""统一报告 v2：四面板保留，来源/分母/版本可解释。

只合并统计，不做自然语言语义提取。与 v1 的区别：

- 环境面板：选定协议结果 + first/legacy 对照 + 静态/动态结果可用性 +
  决策时需求版本 + reward 有效性/价格缺口；历史动态结果不可核实时标
  unavailable/unknown，不填 0、不伪造成功；
- Rubric 面板：初始公开需求与对话新增/澄清分开；最终指标只统计决策时刻
  有效约束（排除 superseded/revoked 但保留时间线）；条件许可单独展示，
  不重复计入满足项；显示来源 quote/事件/版本/四态/理由；
- 七维面板：均值/分布/理由样本；按是否澄清/复杂度/领域分组并给样本数；
  初始与动态指标分开，不混算单一总满足率；
- 行为面板：真实决策/购买状态、首次停止与事后动作、非法点击、漏选、
  同步失败、无进展等已证实行为，标明任务数/动作数。

分子/分母定义：
- final_satisfaction_rate = satisfied / (satisfied + violated + unknown)，
  分母为决策时刻有效约束数（不含 not_applicable 与生命周期排除项）；
  零分母返回 null 且样本数为 0。

输出（--out 目录下）：report_v2.json / summary_v2.json / report_v2.md，
不覆盖任何 v1 文件。

用法:
  python3 eval/report.py --rubric-version v2 \
    --benchmark benchmarks/shopping-final-v1 \
    --deterministic reports/<run>[-terminal-v2] \
    --rubrics-v2 evaluations/<bench>/<run>/rubrics_v2 \
    --judgments-v2 evaluations/<bench>/<run>/judgments_v2 \
    [--events evaluations/<bench>/<run>/events] \
    --out evaluations/<bench>/<run>
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.requirement_schema import (  # noqa: E402
    LIFECYCLE_ACTIVE,
    LIFECYCLE_REVOKED,
    LIFECYCLE_SUPERSEDED,
    SCHEMA_VERSION,
    SOURCE_INITIAL_QUERY,
    SOURCE_SHOPPER_CLARIFICATION,
)
from eval.report import load_deterministic, load_benchmark  # noqa: E402

REPORT_SCHEMA = "shopping-report-v2"
STATES = ("satisfied", "violated", "unknown", "not_applicable")
DIMENSION_KEYS = [
    "clarification_strategy", "information_retention", "search_strategy",
    "candidate_utilization", "evidence_verification", "decision_quality",
    "termination_efficiency",
]


def rate(num, den):
    return round(num / den, 4) if den else None


def load_jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip()]


def run(args) -> int:
    errors, warnings = [], []

    bench_dir = Path(args.benchmark)
    bench_manifest, bench_tasks = load_benchmark(bench_dir)
    bench_ids = sorted({str(t) for t in bench_manifest["task_ids"]}, key=int)

    det_rows, det_summary, _ = load_deterministic(args.deterministic)

    rubrics_v2_dir = Path(args.rubrics_v2)
    judgments_v2_dir = Path(args.judgments_v2)
    events_dir = Path(args.events) if getattr(args, "events", None) else None

    # ── 覆盖与对齐校验 ──────────────────────────────────────────────
    def check_coverage(name, have: set):
        missing = sorted(set(bench_ids) - have, key=int)
        extra = sorted(have - set(bench_ids), key=int)
        for tid in missing:
            (warnings if args.allow_missing else errors).append(
                f"缺少 {name}: task {tid}")
        for tid in extra:
            errors.append(f"{name} 出现 benchmark 之外的 task: {tid}")

    rubric_ids = {p.stem.replace(".rubric_v2", "")
                  for p in rubrics_v2_dir.glob("*.rubric_v2.json")}
    judgment_ids = {p.stem for p in judgments_v2_dir.glob("*.json")
                    if p.name != "manifest.json"}
    check_coverage("rubric_v2", rubric_ids)
    check_coverage("judgment_v2", judgment_ids)
    check_coverage("deterministic", set(det_rows.keys()))

    # 版本一致性：混版报错
    schema_versions = set()
    judge_modes = set()
    for tid in judgment_ids & set(bench_ids):
        doc = json.loads((judgments_v2_dir / f"{tid}.json").read_text(encoding="utf-8"))
        schema_versions.add(doc.get("schema_version"))
        judge_modes.add((doc.get("metadata") or {}).get("judge_mode"))
    if len(schema_versions) > 1:
        errors.append(f"judgment_v2 schema_version 混用: {sorted(schema_versions)}")
    if schema_versions and schema_versions != {"shopping-judge-output-v2"}:
        errors.append(f"judgment_v2 schema_version 非法: {sorted(schema_versions)}")

    det_protocols = {r.get("terminal_protocol") or "legacy-unlabeled"
                     for r in det_rows.values()}
    if len(det_protocols) > 1:
        errors.append(f"deterministic 终局协议混用: {sorted(det_protocols)}")

    if errors:
        print(f"[失败] 共 {len(errors)} 个校验错误：", file=sys.stderr)
        for e in errors[:30]:
            print(f"  - {e}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"[警告] {w}", file=sys.stderr)

    # ── 逐任务合并 ─────────────────────────────────────────────────
    tasks = {}
    evaluated = []
    for tid in bench_ids:
        if tid not in rubric_ids or tid not in judgment_ids or tid not in det_rows:
            continue
        evaluated.append(tid)
        rub = json.loads((rubrics_v2_dir / f"{tid}.rubric_v2.json").read_text(encoding="utf-8"))
        judg = json.loads((judgments_v2_dir / f"{tid}.json").read_text(encoding="utf-8"))
        det = det_rows[tid]

        if rub.get("schema_version") != SCHEMA_VERSION:
            print(f"[失败] task {tid}: rubric_v2 schema_version 不匹配", file=sys.stderr)
            return 1

        constraints_by_id = {c["id"]: c for c in rub.get("constraints") or []}
        final_ids = set(rub.get("final_constraint_ids") or [])
        verdicts_by_id = {
            v["constraint_id"]: v
            for v in (judg.get("judgment") or {}).get("final_requirement_verdicts") or []
        }

        # 约束 ID 精确覆盖校验（Judge 侧已校验，这里防混版/篡改）
        if set(verdicts_by_id) != final_ids:
            print(f"[失败] task {tid}: verdict 未精确覆盖最终约束集合", file=sys.stderr)
            return 1

        final_constraints = []
        for cid in sorted(final_ids):
            c = constraints_by_id[cid]
            v = verdicts_by_id[cid]
            final_constraints.append({
                "id": cid,
                "requirement_key": c.get("requirement_key"),
                "description": c.get("description"),
                "hardness": c.get("hardness"),
                "source": c.get("source"),
                "source_quote": c.get("source_quote"),
                "source_event_id": c.get("source_event_id"),
                "lifecycle_status": c.get("lifecycle_status"),
                "status": v.get("status"),
                "position_reference": v.get("position_reference"),
                "reasoning": v.get("reasoning"),
            })

        outcome = det.get("outcome") or {}
        dynamic_result = det.get("active_requirement_result")
        tasks[tid] = {
            "task_id": int(tid),
            "query": (bench_tasks.get(tid) or {}).get("query")
                     or rub.get("metadata", {}).get("query"),
            "metadata": {
                "domain": ((bench_tasks.get(tid) or {}).get("metadata") or {}).get("domain"),
                "complexity": ((bench_tasks.get(tid) or {}).get("metadata") or {}).get("complexity"),
            },
            "panel1_environment": {
                "class": outcome.get("class"),
                "environment_done": bool(det.get("environment_done")),
                "task_success": bool(det.get("task_success")),
                "reward": outcome.get("reward"),
                "reward_type": outcome.get("reward_type"),
                "reward_valid": (det.get("first_terminal") or {}).get("reward_valid")
                                if det.get("first_terminal") else None,
                "first_terminal": det.get("first_terminal"),
                "legacy_canonical_terminal": det.get("legacy_canonical_terminal"),
                "requirement_version_at_decision": (
                    (judg.get("metadata") or {}).get("requirement_version_at_decision")
                ),
                "initial_static_result": "available",
                "active_requirement_result": (
                    dynamic_result if dynamic_result is not None else "unavailable"
                ),
                "price_data_gap": outcome.get("class") == "reward_unverifiable",
            },
            "panel2_rubric": {
                "event_source": rub.get("event_source"),
                "decision": rub.get("decision"),
                "final_constraints": final_constraints,
                "timeline_constraints": [
                    {
                        "id": c["id"],
                        "requirement_key": c.get("requirement_key"),
                        "source": c.get("source"),
                        "lifecycle_status": c.get("lifecycle_status"),
                        "effective_from_event": c.get("effective_from_event"),
                        "effective_until_event": c.get("effective_until_event"),
                        "supersedes": c.get("supersedes"),
                    }
                    for c in rub.get("constraints") or []
                    if c["id"] not in final_ids
                ],
                "conditional_permissions": rub.get("conditional_permissions") or [],
                "skipped_events": rub.get("skipped_events") or [],
                "candidate_evidence": (judg.get("judgment") or {}).get("candidate_evidence") or [],
            },
            "panel3_trajectory": {
                "dimension_scores": (judg.get("judgment") or {}).get("dimension_scores") or {},
                "dimension_reasons": (judg.get("judgment") or {}).get("dimension_reasons") or {},
            },
            "panel4_behavior": {
                "step_count": det.get("step_count"),
                "anomalies": det.get("anomalies") or [],
                "failure_class": (det.get("failure") or {}).get("class"),
                "post_terminal_action": any(
                    a.get("class") == "post_terminal_action"
                    for a in det.get("anomaly_details") or []
                ),
                "decision_kind": (rub.get("decision") or {}).get("kind"),
            },
        }

    # ── Panel 2 指标（精确定义分子分母）──────────────────────────────
    final_status = Counter()
    final_by_source = defaultdict(Counter)
    final_by_hardness = defaultdict(Counter)
    initial_ref_status = Counter()
    lifecycle_counts = Counter()
    clarified_timeline = 0
    permission_counter = Counter()
    permission_lifecycle = Counter()
    per_task_counts = {}

    for tid in evaluated:
        t = tasks[tid]
        rub = json.loads((rubrics_v2_dir / f"{tid}.rubric_v2.json").read_text(encoding="utf-8"))
        for c in rub.get("constraints") or []:
            lifecycle_counts[c.get("lifecycle_status")] += 1
            if c.get("source") == SOURCE_SHOPPER_CLARIFICATION:
                clarified_timeline += 1
        for p in t["panel2_rubric"]["conditional_permissions"]:
            permission_counter["total"] += 1
            permission_lifecycle[p.get("lifecycle_status", LIFECYCLE_ACTIVE)] += 1
        fc = t["panel2_rubric"]["final_constraints"]
        per_task_counts[tid] = len(fc)
        for c in fc:
            st = c["status"]
            final_status[st] += 1
            final_by_source[c["source"]][st] += 1
            final_by_hardness[c["hardness"]][st] += 1
            if c["source"] == SOURCE_INITIAL_QUERY:
                initial_ref_status[st] += 1

    def sat_rate(counter: Counter):
        den = counter["satisfied"] + counter["violated"] + counter["unknown"]
        return rate(counter["satisfied"], den), den

    final_rate, final_den = sat_rate(final_status)
    initial_rate, initial_den = sat_rate(initial_ref_status)
    hard_rate, hard_den = sat_rate(final_by_hardness.get("hard", Counter()))
    soft_rate, soft_den = sat_rate(final_by_hardness.get("soft", Counter()))

    # ── Panel 3 指标 ─────────────────────────────────────────────────
    dim_values = {k: [] for k in DIMENSION_KEYS}
    dim_reason_samples = {k: [] for k in DIMENSION_KEYS}
    for tid in evaluated:
        scores = tasks[tid]["panel3_trajectory"]["dimension_scores"]
        reasons = tasks[tid]["panel3_trajectory"]["dimension_reasons"]
        for k in DIMENSION_KEYS:
            if isinstance(scores.get(k), int):
                dim_values[k].append(scores[k])
                if len(dim_reason_samples[k]) < 3 and reasons.get(k):
                    dim_reason_samples[k].append(
                        {"task_id": int(tid), "reason": reasons[k]})

    dim_stats = {
        k: {
            "mean": rate(sum(v), len(v)) if v else None,
            "distribution": dict(Counter(v)),
            "sample_count": len(v),
        }
        for k, v in dim_values.items()
    }

    # 分组（给样本数）
    def group_stats(predicate):
        groups = defaultdict(lambda: {k: [] for k in DIMENSION_KEYS})
        for tid in evaluated:
            g = predicate(tid)
            for k in DIMENSION_KEYS:
                v = tasks[tid]["panel3_trajectory"]["dimension_scores"].get(k)
                if isinstance(v, int):
                    groups[g][k].append(v)
        return {
            str(g): {
                "sample_count": len(next(iter(dims.values()), [])),
                **{k: rate(sum(v), len(v)) for k, v in dims.items()},
            }
            for g, dims in sorted(groups.items(), key=lambda kv: str(kv[0]))
        }

    qa_counts = {}
    if events_dir is not None and events_dir.is_dir():
        for tid in evaluated:
            ep = events_dir / f"{tid}.events.json"
            if ep.exists():
                try:
                    qa_counts[tid] = len(
                        json.loads(ep.read_text(encoding="utf-8")).get("ask_shopper_qa") or []
                    )
                except (OSError, json.JSONDecodeError):
                    qa_counts[tid] = 0
    if qa_counts:
        by_clarified = group_stats(
            lambda tid: "with_clarification" if qa_counts.get(tid, 0) > 0
            else "no_clarification")
    else:
        by_clarified = None
    by_complexity = group_stats(
        lambda tid: tasks[tid]["metadata"]["complexity"] or "(unknown)")
    by_domain = group_stats(
        lambda tid: tasks[tid]["metadata"]["domain"] or "(unknown)")

    # ── Panel 1 / 4 汇总 ─────────────────────────────────────────────
    outcome_classes = Counter(
        tasks[tid]["panel1_environment"]["class"] for tid in evaluated
        if tasks[tid]["panel1_environment"]["class"])
    success = sum(1 for tid in evaluated
                  if tasks[tid]["panel1_environment"]["task_success"])
    price_gap = sum(1 for tid in evaluated
                    if tasks[tid]["panel1_environment"]["price_data_gap"])
    decision_kinds = Counter(tasks[tid]["panel4_behavior"]["decision_kind"]
                             for tid in evaluated)
    anomaly_task_counter = Counter()
    anomaly_action_counter = Counter()
    for tid in evaluated:
        det = det_rows[tid]
        seen_classes = set()
        for a in det.get("anomaly_details") or []:
            anomaly_action_counter[a["class"]] += a.get("count", 0)
            if a["class"] not in seen_classes:
                anomaly_task_counter[a["class"]] += 1
                seen_classes.add(a["class"])
    sync_issue_counter = Counter()
    post_terminal_event_count = 0
    if events_dir is not None and events_dir.is_dir():
        for tid in evaluated:
            ep = events_dir / f"{tid}.events.json"
            if not ep.exists():
                continue
            try:
                doc = json.loads(ep.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for ev in doc.get("events") or []:
                if ev.get("status") in ("conflict", "version_mismatch",
                                        "evidence_unmapped", "rejected_invalid"):
                    sync_issue_counter[ev["status"]] += 1
                if ev.get("status") == "post_terminal_excluded":
                    post_terminal_event_count += 1

    judge_modes_final = sorted(judge_modes) if judge_modes else []
    metadata = {
        "report_schema": REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": bench_manifest.get("benchmark_id"),
        "task_count": len(bench_ids),
        "evaluated_task_count": len(evaluated),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deterministic_source": str(Path(args.deterministic)),
        "rubrics_v2_source": str(rubrics_v2_dir),
        "judgments_v2_source": str(judgments_v2_dir),
        "events_source": str(events_dir) if events_dir else None,
        "deterministic_terminal_protocol": next(iter(det_protocols)),
        "judge_mode": judge_modes_final,
        "judge_mode_note": (
            "mock 判定仅用于链路验证，不是真实模型成绩。"
            if "mock" in judge_modes_final else None
        ),
        "environment_version": bench_manifest.get("environment_version"),
        "warnings": warnings or None,
    }

    summary = {
        "report_schema": REPORT_SCHEMA,
        "benchmark_id": metadata["benchmark_id"],
        "task_count": metadata["task_count"],
        "evaluated_task_count": metadata["evaluated_task_count"],
        "panel1": {
            "task_success_count": success,
            "task_success_rate": rate(success, len(evaluated)),
            "outcome_classes": dict(outcome_classes),
            "price_data_gap_count": price_gap,
            "active_requirement_result": "unavailable（历史 run 无环境动态裁判）",
        },
        "panel2": {
            "final_constraint_sample_count": final_den
            + final_status.get("not_applicable", 0),
            "final_verdict_counts": {s: final_status.get(s, 0) for s in STATES},
            "final_satisfaction_rate": final_rate,
            "final_satisfaction_denominator": final_den,
            "initial_reference_satisfaction_rate": initial_rate,
            "initial_reference_denominator": initial_den,
            "hard_satisfaction_rate": hard_rate,
            "hard_denominator": hard_den,
            "soft_satisfaction_rate": soft_rate,
            "soft_denominator": soft_den,
            "lifecycle_counts": dict(lifecycle_counts),
            "clarified_constraints_timeline": clarified_timeline,
            "clarified_constraints_final_active": dict(
                final_by_source.get(SOURCE_SHOPPER_CLARIFICATION, Counter())),
            "conditional_permissions": {
                "total": permission_counter.get("total", 0),
                "lifecycle": dict(permission_lifecycle),
                "note": "条件许可单独展示，不计入满足项分母。",
            },
        },
        "panel3": {
            "dimensions": dim_stats,
            "reason_samples": dim_reason_samples,
            "by_clarification": by_clarified,
            "by_complexity": by_complexity,
            "by_domain": by_domain,
            "note": "初始与动态指标分开；不同约束数的任务不混算单一总满足率。",
        },
        "panel4": {
            "decision_kinds": dict(decision_kinds),
            "anomaly_task_counts": dict(anomaly_task_counter),
            "anomaly_action_counts": dict(anomaly_action_counter),
            "sync_issue_counts": dict(sync_issue_counter),
            "post_terminal_event_count": post_terminal_event_count,
        },
        "metadata": metadata,
    }

    report = {
        "metadata": metadata,
        "panel1_summary": summary["panel1"],
        "panel2_summary": summary["panel2"],
        "panel3_summary": summary["panel3"],
        "panel4_summary": summary["panel4"],
        "tasks": tasks,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report_v2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary_v2.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report_v2.md").write_text(
        render_markdown(summary, tasks, evaluated), encoding="utf-8")

    print(json.dumps({
        "evaluated": len(evaluated),
        "final_satisfaction_rate": final_rate,
        "final_denominator": final_den,
        "judge_mode": judge_modes_final,
    }, ensure_ascii=False))
    print(f"report_v2 -> {out_dir}")
    return 0


def render_markdown(summary: dict, tasks: dict, evaluated: list) -> str:
    md = summary["metadata"]
    p1, p2, p3, p4 = (summary["panel1"], summary["panel2"],
                      summary["panel3"], summary["panel4"])
    L = ["# Shopping Evaluation Report v2", ""]
    L += [
        "## Metadata", "",
        f"- benchmark: {md['benchmark_id']}",
        f"- 任务: {md['evaluated_task_count']}/{md['task_count']}",
        f"- deterministic 协议: {md['deterministic_terminal_protocol']}",
        f"- judge_mode: {md['judge_mode']}（{md.get('judge_mode_note') or '真实模型'}）",
        f"- schema: {md['report_schema']} / {md['schema_version']}",
        "",
    ]
    L += ["## Panel 1: Environment Outcome", "",
          "| 指标 | 数值 |", "|---|---|",
          f"| task_success | {p1['task_success_count']} ({p1['task_success_rate']}) |",
          f"| 价格数据缺口（reward_unverifiable） | {p1['price_data_gap_count']} |",
          f"| 动态环境结果 | {p1['active_requirement_result']} |", ""]
    L += ["outcome 分布：", ""]
    for cls, cnt in sorted(p1["outcome_classes"].items(), key=lambda kv: -kv[1]):
        L.append(f"- {cls}: {cnt}")
    L += ["", "## Panel 2: Rubric Satisfaction（决策时刻有效约束）", "",
          "| 状态 | 数量 |", "|---|---|"]
    for s in STATES:
        L.append(f"| {s} | {p2['final_verdict_counts'][s]} |")
    L += [
        "",
        f"- final_satisfaction_rate: {p2['final_satisfaction_rate']}"
        f"（分母 {p2['final_satisfaction_denominator']}，不含 N/A 与生命周期排除项）",
        f"- 初始公开约束对照满足率: {p2['initial_reference_satisfaction_rate']}"
        f"（分母 {p2['initial_reference_denominator']}）",
        f"- hard: {p2['hard_satisfaction_rate']}（{p2['hard_denominator']}）"
        f" / soft: {p2['soft_satisfaction_rate']}（{p2['soft_denominator']}）",
        f"- 生命周期: {p2['lifecycle_counts']}",
        f"- 澄清约束（时间线）: {p2['clarified_constraints_timeline']}；"
        f"最终有效: {p2['clarified_constraints_final_active']}",
        f"- 条件许可: {p2['conditional_permissions']['total']}"
        f"（{p2['conditional_permissions']['lifecycle']}），单独展示",
        "",
    ]
    L += ["## Panel 3: Trajectory Quality", "",
          "| 维度 | 均值 | 0 | 1 | 2 | 样本 |", "|---|---|---|---|---|---|"]
    for k in DIMENSION_KEYS:
        d = p3["dimensions"][k]
        dist = d["distribution"]
        L.append(f"| {k} | {d['mean']} | {dist.get(0, 0)} | {dist.get(1, 0)} "
                 f"| {dist.get(2, 0)} | {d['sample_count']} |")
    if p3.get("by_clarification"):
        L += ["", "按是否澄清分组（样本数/均值）："]
        for g, stats in p3["by_clarification"].items():
            L.append(f"- {g}: n={stats['sample_count']}, "
                     f"decision_quality={stats.get('decision_quality')}")
    L += ["", "## Panel 4: Behavior", "",
          f"- 决策类型: {p4['decision_kinds']}",
          f"- 异常（任务数）: {p4['anomaly_task_counts']}",
          f"- 异常（动作数）: {p4['anomaly_action_counts']}",
          f"- 事件同步问题: {p4['sync_issue_counts']}",
          f"- 终局后事件（排除）: {p4['post_terminal_event_count']}",
          "", "---", "",
          "*v2 报告只合并统计；mock judge_mode 的结果仅用于链路验证，"
          "不是真实模型成绩。*", ""]
    return "\n".join(L)
