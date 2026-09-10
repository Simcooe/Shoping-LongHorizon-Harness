#!/usr/bin/env python3
"""Rubric v2 动态清单生成：base rubric + 该轨迹事件 → 时间线化约束清单。

- benchmark 级 base rubric（benchmarks/.../rubrics）保持轨迹无关，不改动；
- run/task/trial 级动态 rubric 编译到独立输出目录（不得写回共享的
  benchmarks/.../rubrics）；
- 编译为确定性重放（无 LLM）：语义提取在上游（重建/在线事件）完成；
- final 约束集合取「实际决策时刻」（购买尝试 / finish / 第一次终局），
  决策后才收到的事件不进入最终有效集合，只保留在时间线里；
- superseded/revoked 是生命周期，不偷换成 not_applicable；单笔报价许可
  不把全局预算约束标为 superseded；
- 缓存按「输入 + 影响语义的所有版本」指纹复用，支持 resume；输出带
  失败清单与输入 hash。

用法:
  python3 eval/gen_rubric_v2.py \
    --benchmark benchmarks/shopping-final-v1 \
    --base-rubrics benchmarks/shopping-final-v1/rubrics \
    --events evaluations/shopping-final-v1/<run>/events \
    --traces runs/<run>/traces \
    --run-id <run> \
    --out evaluations/shopping-final-v1/<run>/rubrics_v2 \
    [--only 916,1092] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.requirement_schema import (  # noqa: E402
    SCHEMA_VERSION,
    SOURCE_INITIAL_QUERY,
    TimelineCompiler,
    base_constraints_to_v2,
    file_hash,
    fingerprint,
    scope_matches_purchase,
    validate_rubric_v2,
)
from eval.requirement_schema import RequirementEventV2  # noqa: E402

COMPILER_VERSION = "rubric-v2-compiler-v1"


def is_buy_now_value(value) -> bool:
    return str(value or "").replace("&lt;", "<").replace("&gt;", ">") \
        .strip().lower() == "buy now"


def locate_decision(model_trace: dict, boundary: int | None) -> dict:
    """决策时刻定位：购买尝试优先，其次最后动作/第一次终局。"""
    steps = model_trace.get("steps") or []
    buy_positions = [
        p for p, s in enumerate(steps)
        if s.get("tool_name") == "click"
        and is_buy_now_value((s.get("tool_args") or {}).get("value"))
    ]
    if buy_positions:
        if boundary is not None:
            before = [p for p in buy_positions if p <= boundary]
            pos = before[0] if before else buy_positions[0]
        else:
            pos = buy_positions[0]
        return {"kind": "purchase_attempt", "position": pos}
    if steps:
        last = steps[-1]
        if last.get("tool_name") == "finish":
            return {"kind": "finish", "position": len(steps) - 1}
    if boundary is not None:
        return {"kind": "hard_stop", "position": boundary}
    return {"kind": "agent_stop", "position": len(steps) - 1 if steps else None}


def compile_task(task_id, benchmark_id, run_id, base_rubric: dict,
                 events_doc: dict | None, model_trace: dict,
                 base_rubric_hash: str, trace_hash: str) -> dict:
    session = f"{run_id}/{task_id}"
    trial_id = (events_doc or {}).get("trial_id", "t0")
    event_source = (events_doc or {}).get("event_source", "none")

    initial = base_constraints_to_v2(base_rubric, task_id, benchmark_id)
    compiler = TimelineCompiler(session, initial)

    events = []
    for d in (events_doc or {}).get("events") or []:
        ev = RequirementEventV2.from_dict(d)
        if not ev.session:
            ev.session = session
        events.append(ev)
    events.sort(key=lambda e: (e.seq if e.seq is not None else 10 ** 9))

    boundary_info = (events_doc or {}).get("terminal_boundary") or {}
    boundary = boundary_info.get("first_terminal_position")
    decision = locate_decision(model_trace, boundary)
    decision_pos = decision["position"]

    skipped = []
    pre_decision, post_decision = [], []
    for ev in events:
        if ev.status != "applied":
            skipped.append({
                "event_id": ev.event_id,
                "status": ev.status,
                "kind": ev.kind,
            })
            continue
        if decision_pos is not None and ev.step_index is not None \
                and ev.step_index <= decision_pos:
            pre_decision.append(ev)
        elif ev.step_index is None:
            skipped.append({
                "event_id": ev.event_id,
                "status": "evidence_unmapped",
                "kind": ev.kind,
            })
        else:
            post_decision.append(ev)

    apply_log = []
    for ev in pre_decision:
        res = compiler.apply(ev)
        apply_log.append({
            "event_id": ev.event_id, "applied": res["applied"],
            "reason": res["reason"], "phase": "pre_decision",
        })
    final_constraint_ids = [c.id for c in compiler.effective_set()]
    final_permission_ids = [
        p["permission_id"] for p in compiler.permissions
        if p.get("lifecycle_status") == "active"
    ]
    requirement_version_at_decision = compiler.version
    for ev in post_decision:
        res = compiler.apply(ev)
        apply_log.append({
            "event_id": ev.event_id, "applied": res["applied"],
            "reason": res["reason"], "phase": "post_decision",
        })

    doc = compiler.result(
        benchmark_id=benchmark_id,
        run_id=run_id,
        task_id=str(task_id),
        trial_id=trial_id,
        event_source=event_source,
        base_rubric_hash=base_rubric_hash,
        trace_hash=trace_hash,
        events_hash=fingerprint((events_doc or {}).get("events") or []),
        schema_version=SCHEMA_VERSION,
        decision={
            **decision,
            "requirement_version": requirement_version_at_decision,
        },
        final_constraint_ids=final_constraint_ids,
        final_permission_ids=final_permission_ids,
        skipped_events=skipped,
        requirement_events=[ev.to_dict() for ev in events],
        apply_log=apply_log,
        compiler_version=COMPILER_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        metadata={
            "extractor": (events_doc or {}).get("extractor"),
            "note": "final 集合取决策时刻；其后事件仅保留时间线，不影响最终判定。",
        },
    )
    doc["input_fingerprint"] = fingerprint({
        "base_rubric_hash": base_rubric_hash,
        "trace_hash": trace_hash,
        "events_hash": doc["events_hash"],
        "compiler_version": COMPILER_VERSION,
        "schema_version": SCHEMA_VERSION,
    })
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description="rubric v2 动态清单生成")
    ap.add_argument("--benchmark", default="benchmarks/shopping-final-v1")
    ap.add_argument("--base-rubrics", default=None,
                    help="base rubric 目录（默认 <benchmark>/rubrics）")
    ap.add_argument("--events", default=None, help="事件目录（可选）")
    ap.add_argument("--traces", default=None,
                    help="model_trace 目录（用于决策定位；缺省用事件文件边界）")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    bench = Path(args.benchmark)
    base_dir = Path(args.base_rubrics) if args.base_rubrics else bench / "rubrics"
    out_dir = Path(args.out)
    if out_dir.resolve() == base_dir.resolve():
        raise SystemExit("拒绝输出到共享的 base rubric 目录（benchmarks/.../rubrics）")
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((bench / "manifest.json").read_text(encoding="utf-8"))
    benchmark_id = manifest.get("benchmark_id")
    all_ids = [str(t) for t in manifest["task_ids"]]
    ids = ([t.strip() for t in args.only.split(",") if t.strip()]
           if args.only else all_ids)

    old_manifest_path = out_dir / "manifest.json"
    old_manifest = {}
    if old_manifest_path.exists():
        try:
            old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old_manifest = {}
    old_rows = {r["task_id"]: r for r in old_manifest.get("tasks") or []}

    succeeded, failed, skipped_cache = [], [], []
    rows = []
    for tid in ids:
        base_path = base_dir / f"{tid}.json"
        if not base_path.exists():
            failed.append({"task_id": tid, "reason": "base rubric 缺失"})
            continue
        base_rubric = json.loads(base_path.read_text(encoding="utf-8"))
        base_hash = file_hash(base_path)

        events_doc = None
        if args.events:
            ep = Path(args.events) / f"{tid}.events.json"
            if ep.exists():
                events_doc = json.loads(ep.read_text(encoding="utf-8"))

        model_trace = {"steps": []}
        trace_hash = "no-trace"
        if args.traces:
            tp = Path(args.traces) / f"{tid}.model_trace.json"
            if tp.exists():
                model_trace = json.loads(tp.read_text(encoding="utf-8"))
                trace_hash = file_hash(tp)

        input_fp = fingerprint({
            "base_rubric_hash": base_hash,
            "trace_hash": trace_hash,
            "events_hash": fingerprint((events_doc or {}).get("events") or []),
            "compiler_version": COMPILER_VERSION,
            "schema_version": SCHEMA_VERSION,
        })
        prev = old_rows.get(tid)
        out_path = out_dir / f"{tid}.rubric_v2.json"
        if not args.force and prev and out_path.exists() \
                and prev.get("input_fingerprint") == input_fp:
            skipped_cache.append(tid)
            rows.append(prev)
            continue

        try:
            doc = compile_task(
                tid, benchmark_id, args.run_id, base_rubric, events_doc,
                model_trace, base_hash, trace_hash,
            )
        except Exception as exc:  # noqa: BLE001
            failed.append({"task_id": tid, "reason": f"编译异常: {exc}"})
            continue
        errors = validate_rubric_v2(doc, expected_task_id=tid)
        if errors:
            failed.append({"task_id": tid, "reason": "; ".join(errors[:5])})
            continue
        out_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        succeeded.append(tid)
        rows.append({
            "task_id": tid,
            "input_fingerprint": input_fp,
            "event_source": doc["event_source"],
            "final_constraint_count": len(doc["final_constraint_ids"]),
            "permission_count": len(doc["final_permission_ids"]),
        })

    manifest_out = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "run_id": args.run_id,
        "rubric_version": "v2",
        "base_rubric_dir": str(base_dir),
        "compiler_version": COMPILER_VERSION,
        "event_source_expected": "retrospective/runtime/none",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task_count": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "skipped_cache": skipped_cache,
        "tasks": rows,
    }
    old_manifest_path.write_text(
        json.dumps(manifest_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "tasks": len(rows),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "skipped_cache": len(skipped_cache),
    }, ensure_ascii=False))
    print(f"rubrics_v2 -> {out_dir}")
    if failed:
        print("失败清单:", json.dumps(failed[:10], ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
