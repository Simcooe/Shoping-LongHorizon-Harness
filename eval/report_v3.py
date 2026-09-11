#!/usr/bin/env python3
"""Shopping Harness 统一四面板报告 v3。

只合并已有 deterministic-v3 与 trajectory-judge-v3 结果；不调用 LLM、
ShopSimulator 或 DSH，也不修改输入。

用法：
  python3 eval/report_v3.py --input evaluations/h0

默认输出到 <input>/report/：
  summary.json / task_results.jsonl / failure_breakdown.json / report.md
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import trajectory_judge_v3 as judge_v3

REPORT_SCHEMA = "shopping-harness-report-v3"
REPORT_VERSION = "report-v3"
EXPECTED_DETERMINISTIC_VERSION = "deterministic-evaluator-v3"
EXPECTED_JUDGE_VERSION = judge_v3.JUDGE_VERSION
EXPECTED_RUBRIC_VERSION = judge_v3.RUBRIC_VERSION
EXPECTED_TERMINAL_PROTOCOL = judge_v3.TERMINAL_PROTOCOL

DIMENSION_KEYS = tuple(judge_v3.DIMENSION_KEYS)
USER_VERDICTS = tuple(judge_v3.USER_VERDICTS)
EVIDENCE_STATUSES = tuple(judge_v3.EVIDENCE_STATUSES)
EFFECTIVE_REQUIREMENT_STATUSES = ("active", "modified")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: JSON 非法: {exc}") from exc
        if not isinstance(row, dict):
            raise TypeError(f"{path}:{line_no}: 顶层必须是 object")
        rows.append(row)
    return rows


def norm_task_id(value) -> str:
    if isinstance(value, bool):
        raise TypeError(f"task_id 不能是 bool: {value!r}")
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError(f"task_id 必须是数字: {value!r}")
    return text


def sorted_task_ids(values) -> list[str]:
    return sorted((norm_task_id(v) for v in values), key=int)


def rows_by_task_id(rows: list[dict], label: str) -> dict[str, dict]:
    out = {}
    for row in rows:
        tid = norm_task_id(row.get("task_id"))
        if tid in out:
            raise ValueError(f"{label} 存在重复 task_id: {tid}")
        out[tid] = row
    return out


def load_numbered_jsons(directory: Path, label: str) -> dict[str, dict]:
    out = {}
    if not directory.is_dir():
        raise ValueError(f"缺少目录: {directory}")
    for path in directory.glob("[0-9]*.json"):
        tid = norm_task_id(path.stem)
        if tid in out:
            raise ValueError(f"{label} 存在重复 task_id: {tid}")
        out[tid] = read_json(path)
    return out


def rate(count: int, denominator: int) -> float | None:
    return round(count / denominator, 4) if denominator else None


def count_distribution(values, keys) -> dict:
    counts = Counter(values)
    return {str(key): counts.get(key, 0) for key in keys}


def metric(count: int, denominator: int) -> dict:
    return {
        "count": count,
        "denominator": denominator,
        "rate": rate(count, denominator),
    }


def normalized_outcome_class(outcome: dict) -> str:
    """与 deterministic summary 对齐：无终局任务单列而非使用 null。"""
    if outcome.get("class"):
        return str(outcome["class"])
    if not outcome.get("environment_done"):
        return "no_terminal_observed"
    return "unclassified_terminal"


def requirement_counts(verdicts: list[dict]) -> dict:
    verdict_counter = Counter(v.get("user_requirement_verdict") for v in verdicts)
    evidence_counter = Counter(v.get("evidence_status") for v in verdicts)
    return {
        "count": len(verdicts),
        "verdicts": {k: verdict_counter.get(k, 0) for k in USER_VERDICTS},
        "evidence": {k: evidence_counter.get(k, 0) for k in EVIDENCE_STATUSES},
    }


def satisfaction_metric(verdicts: list[dict]) -> dict:
    counts = Counter(v.get("user_requirement_verdict") for v in verdicts)
    denominator = sum(counts.get(k, 0) for k in ("satisfied", "violated", "unknown"))
    return {
        "satisfied": counts.get("satisfied", 0),
        "violated": counts.get("violated", 0),
        "unknown": counts.get("unknown", 0),
        "not_applicable": counts.get("not_applicable", 0),
        "evaluated_denominator": denominator,
        "satisfaction_rate": rate(counts.get("satisfied", 0), denominator),
        "definition": "satisfied / (satisfied + violated + unknown)",
    }


def validate_task_sets(expected: set[str], sources: dict[str, set[str]]) -> list[str]:
    errors = []
    for label, actual in sources.items():
        missing = sorted(expected - actual, key=int)
        extra = sorted(actual - expected, key=int)
        if missing:
            errors.append(f"{label} 缺少 task: {missing}")
        if extra:
            errors.append(f"{label} 多出 task: {extra}")
    return errors


def validate_inputs(input_dir: Path, manifest: dict, det_rows: dict[str, dict],
                    det_summary: dict, rubrics: dict[str, dict],
                    judgments: dict[str, dict], judgment_manifest: dict) -> list[str]:
    errors = []
    goals = manifest.get("goals") or []
    try:
        goal_ids = sorted_task_ids(g.get("task_id") for g in goals)
    except (AttributeError, ValueError) as exc:
        return [f"manifest.goals 非法: {exc}"]
    expected = set(goal_ids)

    if len(goal_ids) != len(expected):
        errors.append("manifest.goals 存在重复 task_id")
    if manifest.get("task_count") != len(goal_ids):
        errors.append("manifest.task_count 与 goals 数量不一致")
    if manifest.get("failed"):
        errors.append(f"输入 manifest 存在 failed: {manifest.get('failed')}")
    if manifest.get("skipped"):
        errors.append(f"输入 manifest 存在 skipped: {manifest.get('skipped')}")

    errors.extend(validate_task_sets(expected, {
        "deterministic": set(det_rows),
        "rubrics": set(rubrics),
        "judgments": set(judgments),
    }))

    if det_summary.get("evaluator_version") != EXPECTED_DETERMINISTIC_VERSION:
        errors.append(
            "deterministic evaluator_version 不匹配: "
            f"{det_summary.get('evaluator_version')!r}")
    if det_summary.get("terminal_protocol") != EXPECTED_TERMINAL_PROTOCOL:
        errors.append(
            "deterministic terminal_protocol 不匹配: "
            f"{det_summary.get('terminal_protocol')!r}")
    det_input_count = (det_summary.get("inputs") or {}).get(
        "manifest_unique_task_count")
    if det_input_count != len(expected):
        errors.append(
            "deterministic summary.inputs.manifest_unique_task_count "
            "与输入任务数不一致")

    if judgment_manifest.get("judge_version") != EXPECTED_JUDGE_VERSION:
        errors.append("judgment manifest judge_version 不匹配")
    if judgment_manifest.get("rubric_version") != EXPECTED_RUBRIC_VERSION:
        errors.append("judgment manifest rubric_version 不匹配")
    if judgment_manifest.get("failed"):
        errors.append(f"judgment manifest 存在 failed: {judgment_manifest['failed']}")
    requested = {norm_task_id(t) for t in judgment_manifest.get("requested_task_ids") or []}
    if requested != expected:
        errors.append("judgment manifest requested_task_ids 与输入任务集合不一致")

    for tid in sorted(expected, key=int):
        rubric = rubrics.get(tid)
        judgment = judgments.get(tid)
        det = det_rows.get(tid)
        if rubric is None or judgment is None or det is None:
            continue
        if norm_task_id(rubric.get("task_id")) != tid:
            errors.append(f"task {tid}: rubric 内部 task_id 不一致")
        if rubric.get("rubric_version") != EXPECTED_RUBRIC_VERSION:
            errors.append(f"task {tid}: rubric_version 不匹配")
        if rubric.get("frozen") is not True:
            errors.append(f"task {tid}: rubric 未冻结")
        if norm_task_id(judgment.get("task_id")) != tid:
            errors.append(f"task {tid}: judgment 内部 task_id 不一致")
        if judgment.get("judge_version") != EXPECTED_JUDGE_VERSION:
            errors.append(f"task {tid}: judge_version 不匹配")
        if judgment.get("rubric_version") != EXPECTED_RUBRIC_VERSION:
            errors.append(f"task {tid}: judgment rubric_version 不匹配")
        if judgment.get("judge_failed") is True:
            errors.append(f"task {tid}: judgment 标记为 failed")
        if norm_task_id(det.get("task_id")) != tid:
            errors.append(f"task {tid}: deterministic 内部 task_id 不一致")
        if det.get("evaluator_version") != EXPECTED_DETERMINISTIC_VERSION:
            errors.append(f"task {tid}: deterministic evaluator_version 不匹配")
        if det.get("terminal_protocol") != EXPECTED_TERMINAL_PROTOCOL:
            errors.append(f"task {tid}: deterministic terminal_protocol 不匹配")

        model_path = input_dir / "traces" / f"{tid}.model_trace.json"
        raw_path = input_dir / "traces" / f"{tid}.raw_trace.json"
        if not model_path.is_file() or not raw_path.is_file():
            errors.append(f"task {tid}: trace 缺失")
            continue
        model_trace = read_json(model_path)
        raw_trace = read_json(raw_path)
        boundary = judge_v3.extract_runtime_boundary(raw_trace)
        visible_trace, visible_progress = judge_v3.project_to_runtime_boundary(
            model_trace, raw_trace)
        validation_errors = judge_v3.validate_output(
            judgment, rubric, visible_trace, visible_progress, boundary,
        )
        if validation_errors:
            errors.append(
                f"task {tid}: judgment 校验失败: "
                + "; ".join(validation_errors[:3]))
        if (boundary.get("post_terminal_steps_present") is True
                and (judgment.get("_metadata") or {}).get("terminal_protocol")
                != EXPECTED_TERMINAL_PROTOCOL):
            errors.append(f"task {tid}: post-terminal judgment 缺少协议声明")

        outcome = det.get("outcome") or {}
        if boundary.get("first_terminal_observed") != bool(
                outcome.get("environment_done")):
            errors.append(f"task {tid}: Judge 与 deterministic 终局存在冲突")
        if boundary.get("terminal_step") != outcome.get("terminal_step"):
            errors.append(f"task {tid}: Judge 与 deterministic terminal_step 不一致")
    return errors


def merge_task(tid: str, det: dict, rubric: dict, judgment: dict) -> dict:
    verdicts = judgment.get("rubric_verdicts") or []
    explicit = [v for v in verdicts if v.get("requirement_kind") == "explicit"]
    taskfact_effective = [
        v for v in verdicts
        if v.get("requirement_kind") == "taskfact"
        and v.get("effective_status") in EFFECTIVE_REQUIREMENT_STATUSES
    ]
    taskfact_latent = [
        v for v in verdicts
        if v.get("requirement_kind") == "taskfact"
        and v.get("effective_status") == "latent"
    ]
    taskfact_inactive = [
        v for v in verdicts
        if v.get("requirement_kind") == "taskfact"
        and v.get("effective_status") in ("rejected", "revoked")
    ]
    environment = dict(det.get("outcome") or {})
    environment["report_class"] = normalized_outcome_class(environment)
    return {
        "schema": "shopping-harness-task-report-v3",
        "task_id": tid,
        "query": rubric.get("query"),
        "panel1_environment": environment,
        "panel2_user_requirements": {
            "decision": judgment.get("decision") or {},
            "explicit": requirement_counts(explicit),
            "taskfact_effective": requirement_counts(taskfact_effective),
            "taskfact_latent": requirement_counts(taskfact_latent),
            "taskfact_inactive": requirement_counts(taskfact_inactive),
            "effective_status_distribution": dict(sorted(Counter(
                v.get("effective_status") for v in verdicts).items())),
            "rubric_verdicts": verdicts,
        },
        "panel3_process_quality": {
            "dimension_scores": judgment.get("dimension_scores") or {},
        },
        "panel4_deterministic_behavior": {
            "behavior": det.get("behavior") or {},
            "coverage": det.get("coverage") or {},
            "trace_integrity": det.get("trace_integrity") or {},
            "warnings": [
                e for e in (det.get("errors") or [])
                if e.get("severity") == "warning"
            ],
        },
        "provenance": {
            "deterministic_evaluator": det.get("evaluator_version"),
            "judge_version": judgment.get("judge_version"),
            "judge_model": (judgment.get("_metadata") or {}).get("model"),
            "rubric_version": rubric.get("rubric_version"),
            "terminal_protocol": EXPECTED_TERMINAL_PROTOCOL,
        },
    }


def build_summary(profile: str, tasks: list[dict], det_summary: dict,
                  judgment_manifest: dict) -> dict:
    total = len(tasks)
    outcomes = [t["panel1_environment"] for t in tasks]
    outcome_classes = Counter(normalized_outcome_class(o) for o in outcomes)

    all_verdicts = [
        v for t in tasks
        for v in t["panel2_user_requirements"]["rubric_verdicts"]
    ]
    explicit = [v for v in all_verdicts if v.get("requirement_kind") == "explicit"]
    taskfact_effective = [
        v for v in all_verdicts
        if v.get("requirement_kind") == "taskfact"
        and v.get("effective_status") in EFFECTIVE_REQUIREMENT_STATUSES
    ]
    taskfact_latent = [
        v for v in all_verdicts
        if v.get("requirement_kind") == "taskfact"
        and v.get("effective_status") == "latent"
    ]
    taskfact_inactive = [
        v for v in all_verdicts
        if v.get("requirement_kind") == "taskfact"
        and v.get("effective_status") in ("rejected", "revoked")
    ]
    decisions = [t["panel2_user_requirements"]["decision"] for t in tasks]

    dimensions = {}
    for key in DIMENSION_KEYS:
        values = [
            int(t["panel3_process_quality"]["dimension_scores"][key])
            for t in tasks
        ]
        dimensions[key] = {
            "mean": round(statistics.fmean(values), 4) if values else None,
            "distribution": count_distribution(values, (0, 1, 2)),
            "task_count": len(values),
            "scale": "0=明显问题或未完成, 1=部分完成, 2=整体合理",
        }

    return {
        "schema": REPORT_SCHEMA,
        "report_version": REPORT_VERSION,
        "profile": profile,
        "task_count": total,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "deterministic_evaluator": EXPECTED_DETERMINISTIC_VERSION,
            "judge_version": EXPECTED_JUDGE_VERSION,
            "judge_model": judgment_manifest.get("model"),
            "rubric_version": EXPECTED_RUBRIC_VERSION,
            "terminal_protocol": EXPECTED_TERMINAL_PROTOCOL,
        },
        "panel1_environment": {
            "environment_terminal": metric(sum(
                bool(o.get("environment_done")) for o in outcomes), total),
            "environment_task_success": metric(sum(
                bool(o.get("environment_task_success")) for o in outcomes), total),
            "gold_success": metric(sum(
                bool(o.get("gold_success")) for o in outcomes), total),
            "purchase_occurred": metric(sum(
                bool(o.get("purchase_occurred")) for o in outcomes), total),
            "outcome_classes": dict(sorted(outcome_classes.items())),
            "definition": (
                "环境原生结果；environment_task_success 接受 gold 与 valid "
                "alternative，不等于公开用户需求语义满足。"
            ),
        },
        "panel2_user_requirements": {
            "decision": {
                "observed": metric(sum(bool(d.get("observed")) for d in decisions), total),
                "requirements_resolved": metric(sum(
                    bool(d.get("requirements_resolved")) for d in decisions), total),
                "kinds": dict(sorted(Counter(
                    d.get("kind") or "unknown" for d in decisions).items())),
            },
            "explicit": {
                **requirement_counts(explicit),
                "satisfaction": satisfaction_metric(explicit),
            },
            "taskfact_effective": {
                **requirement_counts(taskfact_effective),
                "satisfaction": satisfaction_metric(taskfact_effective),
                "definition": "effective_status 为 active 或 modified",
            },
            "taskfact_latent": {
                **requirement_counts(taskfact_latent),
                "definition": "未被本次真实 shopper 回复激活，不进入用户满足率",
            },
            "taskfact_inactive": {
                **requirement_counts(taskfact_inactive),
                "definition": "effective_status 为 rejected 或 revoked",
            },
            "effective_status_distribution": dict(sorted(Counter(
                v.get("effective_status") for v in all_verdicts).items())),
        },
        "panel3_process_quality": {
            "dimensions": dimensions,
            "aggregate_score": None,
            "note": "七维独立报告，不加权为单一总分。",
        },
        "panel4_deterministic_behavior": {
            "anomalies": det_summary.get("behavior_anomalies") or {},
            "tool_usage": det_summary.get("tools") or {},
            "input_integrity": {
                key: value
                for key, value in (det_summary.get("inputs") or {}).items()
                if key != "input_sha256"
            },
        },
        "notes": [
            "四个面板相互独立，不生成加权总分。",
            "latent TaskFacts 只报告页面证据，不计为用户需求 satisfied/violated。",
            "缺失证据计 unknown，不计 violated。",
            "确定性 inferred_* 行为是基于公开状态重放的推断，不等于环境真值。",
        ],
    }


def build_failure_breakdown(tasks: list[dict]) -> dict:
    outcomes: dict[str, list[str]] = defaultdict(list)
    anomalies: dict[str, list[str]] = defaultdict(list)
    dimension_zero: dict[str, list[str]] = {key: [] for key in DIMENSION_KEYS}
    unresolved_decision = []
    unresolved_requirements = []
    explicit_violations = []
    taskfact_violations = []

    for task in tasks:
        tid = task["task_id"]
        outcome = normalized_outcome_class(task["panel1_environment"])
        outcomes[outcome].append(tid)
        panel2 = task["panel2_user_requirements"]
        decision = panel2["decision"]
        if not decision.get("observed"):
            unresolved_decision.append(tid)
        if not decision.get("requirements_resolved"):
            unresolved_requirements.append(tid)
        for verdict in panel2["rubric_verdicts"]:
            if verdict.get("user_requirement_verdict") != "violated":
                continue
            if verdict.get("requirement_kind") == "explicit":
                explicit_violations.append(tid)
            elif verdict.get("effective_status") in EFFECTIVE_REQUIREMENT_STATUSES:
                taskfact_violations.append(tid)
        for key, score in task["panel3_process_quality"]["dimension_scores"].items():
            if score == 0:
                dimension_zero[key].append(tid)
        for anomaly in task["panel4_deterministic_behavior"]["behavior"].get(
                "anomalies") or []:
            anomalies[anomaly.get("class") or "unknown"].append(tid)

    def group(values: dict[str, list[str]]) -> dict:
        return {
            key: {"task_count": len(set(ids)), "task_ids": sorted(set(ids), key=int)}
            for key, ids in sorted(values.items())
        }

    return {
        "schema": "shopping-harness-failure-breakdown-v3",
        "environment_outcomes": group(outcomes),
        "behavior_anomalies": group(anomalies),
        "judge": {
            "decision_unresolved": sorted(unresolved_decision, key=int),
            "requirements_unresolved": sorted(unresolved_requirements, key=int),
            "explicit_requirement_violations": sorted(set(explicit_violations), key=int),
            "effective_taskfact_violations": sorted(set(taskfact_violations), key=int),
            "dimension_score_zero": {
                key: sorted(ids, key=int) for key, ids in dimension_zero.items()
            },
        },
    }


def pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(summary: dict) -> str:
    p1 = summary["panel1_environment"]
    p2 = summary["panel2_user_requirements"]
    p3 = summary["panel3_process_quality"]
    p4 = summary["panel4_deterministic_behavior"]
    lines = [
        f"# {summary['profile']} Harness Evaluation Report",
        "",
        f"- Tasks: {summary['task_count']}",
        f"- Deterministic evaluator: `{summary['provenance']['deterministic_evaluator']}`",
        (f"- Judge: `{summary['provenance']['judge_version']}` "
         f"(`{summary['provenance']['judge_model']}`)"),
        f"- Rubric: `{summary['provenance']['rubric_version']}`",
        f"- Terminal protocol: `{summary['provenance']['terminal_protocol']}`",
        "",
        "The four panels are independent. No weighted aggregate score is produced.",
        "",
        "## Panel 1 — Environment outcome",
        "",
        "| Metric | Count | Denominator | Rate |",
        "|---|---:|---:|---:|",
    ]
    for label, key in (
        ("Environment terminal", "environment_terminal"),
        ("Environment task success", "environment_task_success"),
        ("Gold success", "gold_success"),
        ("Purchase receipt observed", "purchase_occurred"),
    ):
        item = p1[key]
        lines.append(
            f"| {label} | {item['count']} | {item['denominator']} | {pct(item['rate'])} |")
    lines.extend([
        "",
        "Outcome classes:",
        "",
        "| Class | Tasks |",
        "|---|---:|",
    ])
    for key, count in p1["outcome_classes"].items():
        lines.append(f"| `{key}` | {count} |")

    lines.extend([
        "",
        "## Panel 2 — User requirement satisfaction",
        "",
        "| Requirement group | Satisfied | Violated | Unknown | N/A | Evaluated | Rate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for label, key in (
        ("Explicit", "explicit"),
        ("TaskFact effective", "taskfact_effective"),
    ):
        item = p2[key]["satisfaction"]
        lines.append(
            f"| {label} | {item['satisfied']} | {item['violated']} | "
            f"{item['unknown']} | {item['not_applicable']} | "
            f"{item['evaluated_denominator']} | {pct(item['satisfaction_rate'])} |")
    latent = p2["taskfact_latent"]
    lines.extend([
        "",
        f"Latent TaskFacts: {latent['count']} (excluded from user satisfaction rates).",
        "",
        "Decision summary:",
        "",
        "| Metric | Count | Denominator | Rate |",
        "|---|---:|---:|---:|",
    ])
    for label, key in (
        ("Decision observed", "observed"),
        ("Requirements resolved", "requirements_resolved"),
    ):
        item = p2["decision"][key]
        lines.append(
            f"| {label} | {item['count']} | {item['denominator']} | {pct(item['rate'])} |")

    lines.extend([
        "",
        "## Panel 3 — Process quality (0–2)",
        "",
        "| Dimension | Mean | Score 0 | Score 1 | Score 2 |",
        "|---|---:|---:|---:|---:|",
    ])
    for key in DIMENSION_KEYS:
        item = p3["dimensions"][key]
        dist = item["distribution"]
        lines.append(
            f"| `{key}` | {item['mean']:.3f} | {dist['0']} | {dist['1']} | {dist['2']} |")

    lines.extend([
        "",
        "## Panel 4 — Deterministic behavior",
        "",
        "| Anomaly | Tasks | Occurrences | Task rate |",
        "|---|---:|---:|---:|",
    ])
    for key, item in p4["anomalies"].items():
        lines.append(
            f"| `{key}` | {item['task_count']} | {item['occurrence_count']} | "
            f"{pct(item['task_rate'])} |")
    tools = p4["tool_usage"]
    full_calls = (tools.get("full_trajectory") or {}).get("total_tool_calls") or {}
    terminal_calls = (tools.get("through_terminal") or {}).get("total_tool_calls") or {}
    lines.extend([
        "",
        "Tool-call counts:",
        "",
        "| Scope | Mean | Median | P95 |",
        "|---|---:|---:|---:|",
        (f"| Full trajectory | {full_calls.get('mean', 0):.3f} | "
         f"{full_calls.get('median')} | {full_calls.get('p95')} |"),
        (f"| Through first terminal | "
         f"{terminal_calls.get('mean', 0):.3f} | "
         f"{terminal_calls.get('median')} | {terminal_calls.get('p95')} |"),
        "",
        "## Interpretation notes",
        "",
    ])
    lines.extend(f"- {note}" for note in summary["notes"])
    lines.append("")
    return "\n".join(lines)


def write_outputs(out_dir: Path, summary: dict, tasks: list[dict],
                  failure_breakdown: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "task_results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in tasks),
        encoding="utf-8",
    )
    (out_dir / "failure_breakdown.json").write_text(
        json.dumps(failure_breakdown, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(
        render_markdown(summary), encoding="utf-8")


def run(input_dir: Path, out_dir: Path) -> tuple[dict, list[dict], dict]:
    manifest = read_json(input_dir / "manifest.json")
    det_dir = input_dir / "deterministic"
    judgment_dir = input_dir / "judgments"
    det_rows = rows_by_task_id(
        read_jsonl(det_dir / "task_results.jsonl"), "deterministic")
    det_summary = read_json(det_dir / "summary.json")
    rubrics = load_numbered_jsons(input_dir / "rubrics", "rubrics")
    judgments = load_numbered_jsons(judgment_dir, "judgments")
    judgment_manifest = read_json(judgment_dir / "manifest.json")

    errors = validate_inputs(
        input_dir, manifest, det_rows, det_summary,
        rubrics, judgments, judgment_manifest)
    if errors:
        joined = "\n".join(f"- {error}" for error in errors[:50])
        raise ValueError(f"报告输入校验失败（{len(errors)}项）：\n{joined}")

    task_ids = sorted_task_ids(g.get("task_id") for g in manifest.get("goals") or [])
    tasks = [
        merge_task(tid, det_rows[tid], rubrics[tid], judgments[tid])
        for tid in task_ids
    ]
    summary = build_summary(
        str(manifest.get("profile") or input_dir.name),
        tasks, det_summary, judgment_manifest)
    failure_breakdown = build_failure_breakdown(tasks)
    write_outputs(out_dir, summary, tasks, failure_breakdown)
    return summary, tasks, failure_breakdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="合并 deterministic-v3 与 trajectory-judge-v3 四面板报告")
    parser.add_argument(
        "--input", required=True,
        help="Harness 评测目录，如 evaluations/h0")
    parser.add_argument(
        "--out", default=None,
        help="输出目录；默认 <input>/report")
    args = parser.parse_args(argv)

    input_dir = Path(args.input)
    out_dir = Path(args.out) if args.out else input_dir / "report"
    try:
        summary, tasks, _breakdown = run(input_dir, out_dir)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "report_version": REPORT_VERSION,
        "task_count": len(tasks),
        "profile": summary["profile"],
        "out": str(out_dir),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
