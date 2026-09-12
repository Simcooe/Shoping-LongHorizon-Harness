#!/usr/bin/env python3
"""Summarize MEA-v4 role budgets, outcomes, retries and paired traces."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from collections import Counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    manifest = json.loads((args.run / "manifest.json").read_text())
    rows = []
    for rec in manifest.get("goals", []):
        tid = str(rec["task_id"])
        attempts = sorted((args.run / "tasks" / tid / "attempts").glob("*/controller-report.json"))
        report = json.loads(attempts[-1].read_text()) if attempts else {}
        rows.append({
            "task_id": rec["task_id"], "status": rec.get("status"),
            "harness_outcome": report.get("harness_outcome"),
            "runtime_status": report.get("runtime_status"),
            "environment_done": report.get("environment_done"),
            "final_receipt_verified": report.get("final_receipt_verified"),
            "task_success": report.get("task_success", report.get("harness_outcome") == "audited_success"),
            "manager_calls": report.get("manager_calls"),
            "manager_decisions": report.get("manager_decisions"),
            "ask_count": report.get("ask_count"),
            "total_tool_calls": report.get("total_tool_calls"),
            "rounds": report.get("rounds"), "elapsed_ms": report.get("elapsed_ms"),
        })
    summary = {
        "schema": "longhorizon-mea-run-summary-v1", "run": str(args.run),
        "task_count": len(rows),
        "outcomes": dict(Counter(row["harness_outcome"] for row in rows)),
        "runtime_statuses": dict(Counter(row["runtime_status"] for row in rows)),
        "task_success": {
            "count": sum(row["task_success"] is True for row in rows),
            "denominator": len(rows),
        },
        "rows": rows,
        "usage_missing_is_null": True,
        "cost_missing_is_null": True,
    }
    out = args.out or args.run / "validation" / "role-budget-summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k:v for k,v in summary.items() if k != "rows"}, ensure_ascii=False, indent=2))
    print(out)

if __name__ == "__main__": main()
