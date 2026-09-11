#!/usr/bin/env python3
"""Fixture-only tests for report_v3; no LLM or simulator calls."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval import report_v3 as report


def make_verdict(kind="explicit", status="active", verdict="satisfied",
                 evidence="supported"):
    return {
        "requirement_id": "r0001" if kind == "explicit" else "t0001",
        "requirement_kind": kind,
        "effective_status": status,
        "user_requirement_verdict": verdict,
        "evidence_status": evidence,
        "evidence": [],
        "reasoning": "fixture",
    }


def make_task(task_id="1", outcome="success_gold", scores=None,
              verdicts=None, resolved=True, anomaly=None):
    if scores is None:
        scores = {key: 1 for key in report.DIMENSION_KEYS}
    if verdicts is None:
        verdicts = [make_verdict()]
    anomalies = [] if anomaly is None else [{"class": anomaly}]
    return {
        "task_id": task_id,
        "panel1_environment": {
            "class": outcome,
            "report_class": outcome,
            "environment_done": True,
            "environment_task_success": outcome == "success_gold",
            "gold_success": outcome == "success_gold",
            "purchase_occurred": outcome == "success_gold",
        },
        "panel2_user_requirements": {
            "decision": {
                "observed": True,
                "kind": "purchase",
                "requirements_resolved": resolved,
            },
            "rubric_verdicts": verdicts,
        },
        "panel3_process_quality": {"dimension_scores": scores},
        "panel4_deterministic_behavior": {
            "behavior": {"anomalies": anomalies},
        },
    }


def make_det_summary():
    return {
        "behavior_anomalies": {
            "no_progress": {
                "denominator": 2,
                "occurrence_count": 1,
                "task_count": 1,
                "task_rate": 0.5,
            }
        },
        "tools": {
            "full_trajectory": {
                "total_tool_calls": {"mean": 3.0, "median": 3.0, "p95": 4}
            },
            "through_terminal": {
                "total_tool_calls": {"mean": 2.5, "median": 2.5, "p95": 3}
            },
        },
        "inputs": {"manifest_unique_task_count": 2},
    }


class TestHelpers(unittest.TestCase):
    def test_no_terminal_gets_named_report_class(self):
        self.assertEqual(report.normalized_outcome_class({
            "class": None, "environment_done": False,
        }), "no_terminal_observed")

    def test_satisfaction_excludes_not_applicable(self):
        verdicts = [
            make_verdict(verdict="satisfied"),
            make_verdict(verdict="unknown", evidence="unknown"),
            make_verdict(verdict="not_applicable", evidence="unknown"),
        ]
        metric = report.satisfaction_metric(verdicts)
        self.assertEqual(metric["evaluated_denominator"], 2)
        self.assertEqual(metric["satisfaction_rate"], 0.5)

    def test_validate_task_sets_reports_missing_and_extra(self):
        errors = report.validate_task_sets(
            {"1", "2"}, {"judgments": {"1", "3"}})
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("缺少" in error for error in errors))
        self.assertTrue(any("多出" in error for error in errors))


class TestSummary(unittest.TestCase):
    def test_four_panels_and_no_aggregate_score(self):
        second_verdicts = [
            make_verdict(verdict="unknown", evidence="unknown"),
            make_verdict(
                kind="taskfact", status="modified", verdict="satisfied"),
            make_verdict(
                kind="taskfact", status="latent", verdict="not_applicable"),
        ]
        tasks = [
            make_task("1", anomaly="no_progress"),
            make_task("2", outcome="repeat_loop", verdicts=second_verdicts,
                      resolved=False),
        ]
        summary = report.build_summary(
            "h0", tasks, make_det_summary(), {"model": "judge-model"})
        self.assertEqual(summary["task_count"], 2)
        self.assertEqual(
            summary["panel1_environment"]["outcome_classes"],
            {"repeat_loop": 1, "success_gold": 1},
        )
        self.assertEqual(
            summary["panel2_user_requirements"]["taskfact_effective"]["count"], 1)
        self.assertEqual(
            summary["panel2_user_requirements"]["taskfact_latent"]["count"], 1)
        self.assertIsNone(
            summary["panel3_process_quality"]["aggregate_score"])
        self.assertIn("no_progress", summary["panel4_deterministic_behavior"]["anomalies"])

    def test_dimension_distribution(self):
        low = {key: 0 for key in report.DIMENSION_KEYS}
        high = {key: 2 for key in report.DIMENSION_KEYS}
        summary = report.build_summary(
            "h0", [make_task("1", scores=low), make_task("2", scores=high)],
            make_det_summary(), {"model": "judge-model"})
        dimension = summary["panel3_process_quality"]["dimensions"][
            "evidence_verification"]
        self.assertEqual(dimension["mean"], 1.0)
        self.assertEqual(dimension["distribution"], {"0": 1, "1": 0, "2": 1})


class TestOutputs(unittest.TestCase):
    def test_write_outputs_creates_four_files(self):
        summary = report.build_summary(
            "h0", [make_task()],
            {
                **make_det_summary(),
                "inputs": {"manifest_unique_task_count": 1},
            },
            {"model": "judge-model"},
        )
        tasks = [make_task()]
        breakdown = report.build_failure_breakdown(tasks)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            report.write_outputs(out, summary, tasks, breakdown)
            self.assertEqual(
                {p.name for p in out.iterdir()},
                {"summary.json", "task_results.jsonl",
                 "failure_breakdown.json", "report.md"},
            )
            self.assertEqual(
                json.loads((out / "summary.json").read_text())["task_count"], 1)
            self.assertIn(
                "Panel 4", (out / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
