#!/usr/bin/env python3
"""Stage-6 mechanism validation and paired-run manifest tooling.

This command is intentionally non-destructive: it validates existing artifacts,
creates a frozen experiment plan, and can summarize already completed runs.
It never launches a new holdout or silently retries a task.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_matrix(path: Path) -> dict:
    matrix = load(path)
    assert matrix["schema"] == "longhorizon-mea-experiment-matrix-v1"
    ids = [item["id"] for item in matrix["configurations"]]
    assert len(ids) == len(set(ids)), "duplicate experiment configuration"
    for item in matrix["configurations"]:
        assert item["max_rounds"] > 0
        assert isinstance(item["purchase_gate"], bool)
    return matrix


def validate_run(run: Path) -> dict:
    errors = []
    manifest_path = run / "manifest.json"
    if not manifest_path.exists():
        return {"run": str(run), "valid": False, "errors": ["manifest.json missing"]}
    manifest = load(manifest_path)
    goals = manifest.get("goals", [])
    for rec in goals:
        tid = str(rec.get("task_id"))
        attempt_dirs = list((run / "tasks" / tid / "attempts").glob("*/"))
        if not attempt_dirs:
            errors.append(f"task {tid}: attempt missing")
            continue
        latest = sorted(attempt_dirs)[-1]
        journal = latest / "journal.jsonl"
        report = latest / "controller-report.json"
        if not journal.exists(): errors.append(f"task {tid}: journal missing")
        if not report.exists(): errors.append(f"task {tid}: controller report missing")
        trace_model = run / "traces" / f"{tid}.model_trace.json"
        trace_raw = run / "traces" / f"{tid}.raw_trace.json"
        if not trace_model.exists() or not trace_raw.exists():
            errors.append(f"task {tid}: paired traces missing")
        if report.exists():
            data = load(report)
            if data.get("runtime_status") == "completed" and data.get("harness_outcome") == "audited_success":
                if not data.get("final_receipt_verified"):
                    errors.append(f"task {tid}: audited_success without final receipt")
            if data.get("environment_done") and len(data.get("audits", [])) == 0:
                errors.append(f"task {tid}: environment_done without audit")
            if data.get("harness_outcome") == "environment_terminated_unresolved":
                if data.get("runtime_status") != "completed":
                    errors.append(f"task {tid}: normal unresolved terminal must complete runtime")
                if data.get("task_success") is not False:
                    errors.append(f"task {tid}: unresolved terminal must be task failure")
    return {"run": str(run), "valid": not errors, "errors": errors,
            "task_count": len(goals), "manifest_sha256": digest(manifest_path)}


def freeze_plan(matrix_path: Path, output: Path) -> None:
    matrix = validate_matrix(matrix_path)
    plan = {
        "schema": "longhorizon-mea-frozen-plan-v1",
        "matrix": matrix,
        "matrix_sha256": digest(matrix_path),
        "created_by": "scripts/validate_mea_v4.py",
        "holdout_started": False,
        "policy": "No new large-scale holdout is launched without explicit request.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=ROOT / "configs/mea-v4-experiment-matrix.json")
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--run", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    validate_matrix(args.matrix)
    if args.freeze:
        freeze_plan(args.matrix, args.freeze)
    results = [validate_run(run.resolve()) for run in args.run]
    if results:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(item["valid"] for item in results) else 1
    if not args.freeze:
        print(json.dumps({"matrix": "valid", "matrix_sha256": digest(args.matrix)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
