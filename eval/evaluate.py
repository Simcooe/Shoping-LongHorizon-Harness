#!/usr/bin/env python3
"""基于已有 trace/session 重新评测 run，修正分类语义（outcome / failure / anomalies 分离）。

不重跑任务、不调用模型、不调用 ShopSimulator、不修改原始文件。

用法:
  python3 eval/evaluate.py --traces runs/h0-0905-1446/traces --run-dir runs/h0-0905-1446
  python3 eval/evaluate.py --traces runs/h0-0905-1446/traces --run-dir runs/h0-0905-1446 --benchmark benchmarks/shopping-final-v1

输出:
  reports/<run-name>/{task_results.jsonl, summary.json, failure_breakdown.json, report.md}
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.trace_utils import (  # noqa: E402
    DEFAULT_TERMINAL_PROTOCOL,
    REWARD_TYPE_TO_CLASS,
    SUCCESS_TYPES,
    TERMINAL_PROTOCOL_V1,
    TERMINAL_PROTOCOL_V2,
    agent_turn_end,
    canonical_terminal_step,
    classify_non_terminal,
    first_terminal_step,
    last_action_info,
    read_json,
    read_session_events,
    scan_invalid_clicks,
    select_terminal,
    step_is_done,
    step_reward_type,
)

EVALUATOR_VERSION = "deterministic-evaluator-v2"


def _terminal_brief(steps: list[dict], idx: int | None, step: dict | None) -> dict | None:
    """把一个终局 step 摘要成可并列展示的短字段（新旧口径对照用）。"""
    if step is None:
        return None
    raw = step.get("raw") or {}
    rd = raw.get("reward_detail") or {}
    rt = rd.get("reward_type") or raw.get("termination_reason")
    return {
        "index": idx,
        "step": step.get("step"),
        "reward_type": rt,
        "class": REWARD_TYPE_TO_CLASS.get(rt, rt),
        "reward": raw.get("reward"),
        "termination_reason": raw.get("termination_reason"),
        "task_success": rt in SUCCESS_TYPES,
    }


def _merged_anomalies(anomaly_details: list[dict]) -> tuple[list[str], list[dict]]:
    """把 anomaly_details 汇总成 (classes 字符串列表, 结构化详情列表)。"""
    classes = [a["class"] for a in anomaly_details]
    return classes, anomaly_details


def scan_progress_anomalies(steps: list[dict], limit: int) -> list[dict]:
    """扫描 canonical terminal 之前（含 terminal）的 progress 信号。

    progress 是环境对每一步动作的确定性诊断，不能只读取终局 step；循环
    往往在终局之前已经形成。limit 用于排除 canonical terminal 之后的动作。
    """
    repeated_indices = []
    no_progress_indices = []
    bounded_steps = steps[:limit]

    for index, step in enumerate(bounded_steps):
        progress = (step.get("raw") or {}).get("progress") or {}
        if progress.get("consecutive_repeats", 0) >= 2:
            repeated_indices.append(index)
        if progress.get("no_progress_steps", 0) >= 4:
            no_progress_indices.append(index)

    anomalies = []
    if repeated_indices:
        anomalies.append({
            "class": "repeated_action",
            "step_indices": repeated_indices,
            "tool_names": [steps[i].get("tool_name") for i in repeated_indices],
            "count": len(repeated_indices),
        })
    if no_progress_indices:
        anomalies.append({
            "class": "no_progress",
            "step_indices": no_progress_indices,
            "tool_names": [steps[i].get("tool_name") for i in no_progress_indices],
            "count": len(no_progress_indices),
        })
    return anomalies


def evaluate_task(task_id: int, raw_steps: list[dict], events: list[dict], reset_state: dict | None,
                  terminal_protocol: str = DEFAULT_TERMINAL_PROTOCOL) -> dict:
    # 双口径并存：历史任务同时保留 first_terminal（v2 锁定口径）与旧
    # canonical_terminal（v1，自然终局覆盖硬停止），outcome 按选定协议。
    terminal_idx, terminal_step = select_terminal(raw_steps, terminal_protocol)
    first_idx, first_step = first_terminal_step(raw_steps)
    canon_idx, canon_step = canonical_terminal_step(raw_steps)

    outcome = {
        "class": None,
        "environment_done": terminal_step is not None,
        "task_success": False,
        "reward": None,
        "reward_type": None,
        "termination_reason": None,
        "terminal_step_index": terminal_idx,
    }

    failure = None
    anomaly_details: list[dict] = []

    if terminal_step is not None:
        raw = terminal_step.get("raw") or {}
        rd = raw.get("reward_detail") or {}
        rt = rd.get("reward_type") or raw.get("termination_reason")
        outcome["reward"] = raw.get("reward")
        outcome["reward_type"] = rt
        outcome["termination_reason"] = raw.get("termination_reason")
        outcome["class"] = REWARD_TYPE_TO_CLASS.get(rt, rt)
        outcome["task_success"] = rt in SUCCESS_TYPES
        # failure 必须为 null（环境正常产生终局）
        failure = None

        # 1) post_terminal_action：canonical terminal 之后的每个 raw step
        post = raw_steps[terminal_idx + 1:]
        if post:
            anomaly_details.append({
                "class": "post_terminal_action",
                "step_indices": [terminal_idx + 1 + i for i in range(len(post))],
                "tool_names": [s.get("tool_name") for s in post],
                "count": len(post),
            })

        # 2) superseded_hard_stop：canonical terminal 之前有硬停止被覆盖
        hard_before = [
            i for i in range(terminal_idx)
            if step_is_done(raw_steps[i]) and step_reward_type(raw_steps[i]) in ("repeat_loop", "max_steps")
        ]
        if hard_before:
            anomaly_details.append({
                "class": "superseded_hard_stop",
                "step_indices": hard_before,
                "tool_names": [raw_steps[i].get("tool_name") for i in hard_before],
                "count": len(hard_before),
            })

        # 3) 扫描 canonical terminal 之前（含 terminal）的 progress 异常。
        #    canonical terminal 之后的动作不参与主过程诊断。
        anomaly_details.extend(scan_progress_anomalies(raw_steps, terminal_idx + 1))

        # 4) 扫描 canonical terminal 之前的非法 click（根因信息）
        for a in scan_invalid_clicks(raw_steps, terminal_idx, reset_state):
            anomaly_details.append(a)
    else:
        # 无 canonical terminal：outcome.class 必须为 null，失败原因进 failure.class
        outcome["class"] = None
        outcome["task_success"] = False
        fclass = classify_non_terminal(raw_steps, reset_state)
        info = last_action_info(raw_steps, reset_state)
        failure = {
            "class": fclass,
            "reason": "非终局状态下执行了推断非法 Buy Now，随后 Agent turn completed"
            if fclass == "invalid_buy_then_false_completion"
            else "agent stopped without an environment terminal",
            "last_action": info,
            "last_page_type": info.get("page_type"),
            "last_available_actions": info.get("available_actions", []),
        }
        # 整条非终局轨迹的非法 click 都作为 anomaly。
        for a in scan_invalid_clicks(raw_steps, len(raw_steps), reset_state):
            anomaly_details.append(a)
        # 非终局轨迹同样扫描每一步 progress。
        anomaly_details.extend(scan_progress_anomalies(raw_steps, len(raw_steps)))

    # 合并同类型 anomaly（按 class 合并 step_indices）
    merged: dict[str, dict] = {}
    for a in anomaly_details:
        cls = a["class"]
        if cls not in merged:
            merged[cls] = {
                "class": cls,
                "step_indices": [],
                "tool_names": [],
                "count": 0,
            }
        merged[cls]["step_indices"].extend(a.get("step_indices", []))
        merged[cls]["tool_names"].extend(a.get("tool_names", []))
    # 去重 step_indices，保持顺序
    for cls in merged:
        seen = []
        for i in merged[cls]["step_indices"]:
            if i not in seen:
                seen.append(i)
        merged[cls]["step_indices"] = seen
        merged[cls]["count"] = len(seen)

    anomaly_details = [merged[cls] for cls in sorted(merged)]
    anomaly_classes = [a["class"] for a in anomaly_details]

    # agent 状态（从 session）
    agent = agent_turn_end(events)

    return {
        "task_id": task_id,
        "evaluator_version": EVALUATOR_VERSION,
        "terminal_protocol": terminal_protocol,
        "first_terminal": _terminal_brief(raw_steps, first_idx, first_step),
        "legacy_canonical_terminal": _terminal_brief(raw_steps, canon_idx, canon_step),
        "outcome": outcome,
        "failure": failure,
        "anomalies": anomaly_classes,
        "anomaly_details": anomaly_details,
        "agent": agent,
        # 兼容旧字段
        "agent_turn_completed": agent["turn_completed"],
        "agent_turn_end_kind": agent["turn_end_kind"],
        "environment_done": outcome["environment_done"],
        "task_success": outcome["task_success"],
        "step_count": len(raw_steps),
        "terminal_step_index": terminal_idx,
        "last_action": last_action_info(raw_steps, reset_state),
    }


def build_summary(results: list[dict], terminal_protocol: str = DEFAULT_TERMINAL_PROTOCOL) -> dict:
    total = len(results)
    env_terminal = sum(1 for r in results if r["outcome"]["environment_done"])
    success = sum(1 for r in results if r["outcome"]["task_success"])
    non_terminal = total - env_terminal
    agent_completed = sum(1 for r in results if r["agent"]["turn_completed"] is True)

    outcome_classes = Counter(r["outcome"]["class"] for r in results if r["outcome"]["class"])
    failure_classes = Counter(r["failure"]["class"] for r in results if r["failure"])
    anomaly_counts = Counter(a["class"] for r in results for a in r["anomaly_details"])
    post_terminal = sum(1 for r in results if any(a["class"] == "post_terminal_action" for a in r["anomaly_details"]))

    summary = {
        "evaluator_version": EVALUATOR_VERSION,
        "terminal_protocol": terminal_protocol,
        "total": total,
        "environment_terminal_count": env_terminal,
        "environment_terminal_rate": env_terminal / total if total else 0,
        "task_success_count": success,
        "task_success_rate": success / total if total else 0,
        "outcome_classes": dict(sorted(outcome_classes.items(), key=lambda x: -x[1])),
        "failure_classes": dict(sorted(failure_classes.items(), key=lambda x: -x[1])),
        "outcome_null_count": sum(1 for r in results if r["outcome"]["class"] is None),
        "failure_null_count": sum(1 for r in results if r["failure"] is None),
        "non_terminal_count": non_terminal,
        "agent_turn_completed_count": agent_completed,
        "agent_turn_completed_rate": agent_completed / total if total else 0,
        "post_terminal_action_count": post_terminal,
        "anomaly_counts": dict(sorted(anomaly_counts.items(), key=lambda x: -x[1])),
        # 兼容旧字段
        "success_gold": outcome_classes.get("success_gold", 0),
        "success_valid_alternative": outcome_classes.get("success_valid_alternative", 0),
        "success_partial_alternative": outcome_classes.get("success_partial_alternative", 0),
        "wrong_purchase": outcome_classes.get("wrong_purchase", 0),
        "repeat_loop": outcome_classes.get("repeat_loop", 0),
        "reward_unverifiable": outcome_classes.get("reward_unverifiable", 0),
        "early_abstain": outcome_classes.get("early_abstain", 0),
        "graceful_stop": outcome_classes.get("graceful_stop", 0),
        "max_steps": outcome_classes.get("max_steps", 0),
        "invalid_buy_then_false_completion": failure_classes.get("invalid_buy_then_false_completion", 0),
        "invalid_option_then_false_completion": failure_classes.get("invalid_option_then_false_completion", 0),
        "invalid_navigation_then_false_completion": failure_classes.get("invalid_navigation_then_false_completion", 0),
        "non_terminal_agent_stop": failure_classes.get("non_terminal_agent_stop", 0),
        "non_terminal_trace_incomplete": failure_classes.get("non_terminal_trace_incomplete", 0),
        "non_terminal_infrastructure_error": failure_classes.get("non_terminal_infrastructure_error", 0),
    }

    # v2 口径下另附旧口径（canonical terminal）的 outcome 分布，便于新旧对照；
    # 不覆盖、不改写任何旧口径字段。
    if terminal_protocol == TERMINAL_PROTOCOL_V2:
        legacy_classes = Counter(
            (r.get("legacy_canonical_terminal") or {}).get("class")
            for r in results
            if r.get("legacy_canonical_terminal")
        )
        summary["legacy_outcome_classes"] = dict(
            sorted(legacy_classes.items(), key=lambda x: -x[1])
        )
        summary["legacy_task_success_count"] = sum(
            1 for r in results
            if (r.get("legacy_canonical_terminal") or {}).get("task_success")
        )
    return summary


def main(argv):
    parser = argparse.ArgumentParser(description="重新评测已有 run（不重跑任务）")
    parser.add_argument("--traces", required=True, help="traces 目录")
    parser.add_argument("--run-dir", required=True, help="run 目录（含 sessions/ manifest.json）")
    parser.add_argument("--benchmark", default=None, help="benchmark 目录（可选）")
    parser.add_argument("--out", default=None, help="输出目录（默认 reports/<run 名>/）")
    parser.add_argument(
        "--terminal-protocol",
        default=DEFAULT_TERMINAL_PROTOCOL,
        choices=[TERMINAL_PROTOCOL_V1, TERMINAL_PROTOCOL_V2],
        help="终局口径：v1=历史 canonical（自然终局覆盖硬停止），"
             "v2=统一终止协议（第一条 done 终局锁定）。默认 v2。",
    )
    parser.add_argument(
        "--force-overwrite",
        action="store_true",
        help="允许覆盖已存在的输出目录（默认拒绝，保护历史报告）",
    )
    args = parser.parse_args(argv)

    traces_dir = Path(args.traces)
    run_dir = Path(args.run_dir)

    if not traces_dir.exists():
        raise SystemExit(f"traces 目录不存在: {traces_dir}")
    if not run_dir.exists():
        raise SystemExit(f"run 目录不存在: {run_dir}")

    raw_files = sorted(traces_dir.glob("*.raw_trace.json"))
    if not raw_files:
        raise SystemExit(f"traces 目录下没有 *.raw_trace.json: {traces_dir}")
    task_ids = [int(p.name.split(".")[0]) for p in raw_files]

    session_map = {}
    run_manifest_path = run_dir / "manifest.json"
    if run_manifest_path.exists():
        run_manifest = read_json(run_manifest_path)
        for g in run_manifest.get("goals", []):
            session_map[g.get("task_id")] = g.get("session")
    else:
        session_dirs = sorted((run_dir / "sessions").glob("session-*"))
        if len(session_dirs) == len(task_ids):
            session_map = {tid: s.name for tid, s in zip(sorted(task_ids), session_dirs)}

    zstd = shutil.which("zstd")
    if not zstd:
        raise SystemExit("需要 zstd 可执行文件")

    results = []
    for tid in sorted(task_ids):
        raw = read_json(traces_dir / f"{tid}.raw_trace.json")
        steps = raw.get("steps") or []
        reset_state = (raw.get("reset") or {}).get("observation_state") or None
        events = []
        if session_map.get(tid):
            sess_dir = run_dir / "sessions" / session_map[tid]
            events = read_session_events(sess_dir, zstd_bin=zstd)
        results.append(evaluate_task(tid, steps, events, reset_state,
                                     terminal_protocol=args.terminal_protocol))

    results.sort(key=lambda r: r["task_id"])
    summary = build_summary(results, terminal_protocol=args.terminal_protocol)
    # 数据指纹：同一 environment_version 下也能分辨输入数据与口径，
    # 便于审计「不同协议对同一批轨迹」的可复现性。
    import hashlib
    _fp = hashlib.sha256()
    for _f in sorted(raw_files, key=lambda p: p.name):
        _fp.update(f"{_f.name}:{_f.stat().st_size}\n".encode())
    summary.update({
        "run_dir": str(run_dir),
        "traces_dir": str(traces_dir),
        "input_fingerprint": _fp.hexdigest()[:16],
    })

    # failure_breakdown：outcome_classes 和 failure_classes 分开
    failure_breakdown = {
        "outcome_classes": {},
        "failure_classes": {},
    }
    for r in results:
        oc = r["outcome"]["class"]
        if oc:
            failure_breakdown["outcome_classes"].setdefault(oc, []).append(r["task_id"])
        fc = r["failure"]["class"] if r["failure"] else None
        if fc:
            failure_breakdown["failure_classes"].setdefault(fc, []).append(r["task_id"])

    out_dir = Path(args.out) if args.out else (ROOT / "reports" / run_dir.name)
    # 结果保护：旧 run 的确定性报告默认不可覆盖。重评新协议时必须使用
    # 独立输出目录，或显式 --force-overwrite。
    if (out_dir / "task_results.jsonl").exists() and not args.force_overwrite:
        raise SystemExit(
            f"拒绝覆盖已有评测结果: {out_dir / 'task_results.jsonl'}\n"
            f"  - 新协议重评请使用独立输出目录：--out reports/{run_dir.name}-<protocol>\n"
            f"  - 或显式覆盖：--force-overwrite"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "task_results.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results),
        encoding="utf-8",
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "failure_breakdown.json").write_text(
        json.dumps(failure_breakdown, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # report.md
    lines = [
        f"# Evaluate Report: {run_dir.name}",
        "",
        f"- 总任务数: {summary['total']}",
        f"- 环境终局 (environment_done): {summary['environment_terminal_count']} ({summary['environment_terminal_rate']:.1%})",
        f"- 任务成功 (task_success): {summary['task_success_count']} ({summary['task_success_rate']:.1%})",
        f"- Agent turn completed: {summary['agent_turn_completed_count']} ({summary['agent_turn_completed_rate']:.1%})",
        f"- post_terminal_action: {summary['post_terminal_action_count']}",
        f"- non_terminal: {summary['non_terminal_count']}",
        "",
        "## Environment Outcomes",
        "",
        "| outcome | count |",
        "|---|---|",
    ]
    for cls, cnt in summary["outcome_classes"].items():
        lines.append(f"| {cls} | {cnt} |")
    lines += [
        "",
        "## Non-terminal / Failure Causes",
        "",
        "| failure | count |",
        "|---|---|",
    ]
    for cls, cnt in summary["failure_classes"].items():
        lines.append(f"| {cls} | {cnt} |")
    lines += [
        "",
        "## Anomalies",
        "",
        "| anomaly | count |",
        "|---|---|",
    ]
    for cls, cnt in summary["anomaly_counts"].items():
        lines.append(f"| {cls} | {cnt} |")
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
