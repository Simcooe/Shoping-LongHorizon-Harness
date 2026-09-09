#!/usr/bin/env python3
"""Fixture-only tests for the offline deterministic v3 evaluator."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.deterministic_v3 import (
    TERMINAL_PROTOCOL,
    build_failure_breakdown,
    build_summary,
    evaluate_run,
    evaluate_task,
    nearest_rank,
    replay_pre_states,
)


def observation(page_type="search_home", actions=None):
    value = {"page_type": page_type}
    if actions is not None:
        value["actions"] = actions
    return value


def raw_payload(
    *,
    state=None,
    done=False,
    reward_type=None,
    purchase_marker="default",
    progress_marker="default",
):
    payload = {"done": done}
    if state is not None:
        payload["observation_state"] = state
    if progress_marker == "default":
        payload["progress"] = {"consecutive_repeats": 0, "no_progress_steps": 0}
    elif progress_marker is not None:
        payload["progress"] = progress_marker
    if done:
        payload.update(
            {
                "reward": 1.0 if reward_type == "gold_purchase" else -0.5,
                "reward_valid": True,
                "termination_reason": reward_type,
                "reward_detail": {
                    "reward_type": reward_type,
                    "purchase_success": reward_type in {"gold_purchase", "valid_alternative_purchase"},
                },
            }
        )
        if purchase_marker == "default":
            payload["purchase"] = {}
        elif purchase_marker != "absent":
            payload["purchase"] = purchase_marker
    return payload


def step(index, name, args, raw=None, step_number=None):
    return {
        "step": index + 1 if step_number is None else step_number,
        "tool_name": name,
        "tool_args": args,
        "raw": raw if raw is not None else {"raw_missing": True},
    }


def model_from(raw_steps, *, protocol=TERMINAL_PROTOCOL, terminal=None):
    return {
        "step_count": len(raw_steps),
        "terminal_protocol": protocol,
        "steps": [
            {
                "step": item["step"],
                "tool_name": item["tool_name"],
                "tool_args": item["tool_args"],
                "observation": "fixture",
            }
            for item in raw_steps
        ],
        "terminal": terminal if terminal is not None else {"done": False},
    }


def raw_trace(raw_steps, *, reset_marker="default", protocol=TERMINAL_PROTOCOL):
    reset = (
        {"observation_state": observation(actions=["search"])}
        if reset_marker == "default"
        else reset_marker
    )
    return {
        "step_count": len(raw_steps),
        "terminal_protocol": protocol,
        "reset": reset,
        "steps": raw_steps,
    }


class Fixture:
    def __init__(self, case: unittest.TestCase):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def task(self, raw, model=None, task_id="1"):
        model = model if model is not None else model_from(raw["steps"])
        model_path = self.root / f"{task_id}.model_trace.json"
        raw_path = self.root / f"{task_id}.raw_trace.json"
        model_path.write_text(json.dumps(model), encoding="utf-8")
        raw_path.write_text(json.dumps(raw), encoding="utf-8")
        return evaluate_task(task_id, model_path, raw_path)

    def run(self, ids=("1",), files=None):
        run_dir = self.root / "run"
        traces = run_dir / "traces"
        traces.mkdir(parents=True)
        manifest = {"task_count": len(ids), "goals": [{"task_id": task_id} for task_id in ids]}
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if files is None:
            files = {}
            for task_id in ids:
                raw_steps = [step(0, "search", {"keywords": "x"}, raw_payload(state=observation("search_results", ["A"])))]
                files[f"{task_id}.raw_trace.json"] = raw_trace(raw_steps)
                files[f"{task_id}.model_trace.json"] = model_from(raw_steps)
        for name, payload in files.items():
            path = traces / name
            if isinstance(payload, str):
                path.write_text(payload, encoding="utf-8")
            else:
                path.write_text(json.dumps(payload), encoding="utf-8")
        out = self.root / "out"
        summary, code = evaluate_run(run_dir, out)
        results = [json.loads(line) for line in (out / "task_results.jsonl").read_text().splitlines()]
        return summary, code, results, out


class DeterministicV3Test(unittest.TestCase):
    def test_normal_gold_purchase_and_receipt(self):
        receipt = {"asin": "A", "name": "item", "price": 10, "options": {}, "category": "c"}
        steps = [
            step(0, "search", {"keywords": "x"}, raw_payload(state=observation("search_results", ["A"]))),
            step(1, "click", {"value": "A"}, raw_payload(state=observation("product_detail", ["Buy Now"]))),
            step(2, "click", {"value": "Buy Now"}, raw_payload(state=observation("terminal", []), done=True, reward_type="gold_purchase", purchase_marker=receipt)),
        ]
        model = model_from(
            steps,
            terminal={"done": True, "reward": 1.0, "reward_type": "gold_purchase", "purchase": receipt},
        )
        result = Fixture(self).task(raw_trace(steps), model)
        self.assertTrue(result["trace_integrity"]["passed"])
        self.assertEqual(result["outcome"]["class"], "success_gold")
        self.assertTrue(result["outcome"]["gold_success"])
        self.assertTrue(result["outcome"]["purchase_occurred"])
        self.assertNotIn("instruction_text", result["outcome"]["purchase_receipt"])

    def test_missing_file_writes_result_and_exits_nonzero(self):
        fixture = Fixture(self)
        raw_steps = []
        summary, code, results, _ = fixture.run(
            files={"1.raw_trace.json": raw_trace(raw_steps)}
        )
        self.assertEqual(code, 2)
        self.assertEqual(summary["inputs"]["missing_model_trace_ids"], ["1"])
        self.assertEqual(len(results), 1)
        self.assertIn("model_file_missing", {error["code"] for error in results[0]["errors"]})

    def test_bad_json_writes_result_and_exits_nonzero(self):
        fixture = Fixture(self)
        raw_steps = []
        summary, code, results, _ = fixture.run(
            files={
                "1.raw_trace.json": "{broken",
                "1.model_trace.json": model_from(raw_steps),
            }
        )
        self.assertEqual(code, 2)
        self.assertEqual(summary["inputs"]["actual_readable_task_count"], 0)
        self.assertIn("raw_json_unreadable", {error["code"] for error in results[0]["errors"]})

    def test_extra_trace_exits_nonzero(self):
        fixture = Fixture(self)
        raw_steps = []
        files = {
            "1.raw_trace.json": raw_trace(raw_steps),
            "1.model_trace.json": model_from(raw_steps),
            "2.raw_trace.json": raw_trace(raw_steps),
        }
        summary, code, _, _ = fixture.run(files=files)
        self.assertEqual(code, 2)
        self.assertEqual(summary["inputs"]["extra_raw_trace_ids"], ["2"])

    def test_length_tool_name_and_args_mismatches(self):
        fixture = Fixture(self)
        raw_steps = [step(0, "search", {"keywords": "x"}, raw_payload())]
        model = model_from(raw_steps)
        model["step_count"] = 2
        model["steps"].append({"step": 2, "tool_name": "click", "tool_args": {"value": "A"}})
        model["steps"][0]["tool_name"] = "click"
        model["steps"][0]["tool_args"] = {"value": "x"}
        result = fixture.task(raw_trace(raw_steps), model)
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("model_raw_length_mismatch", codes)
        self.assertIn("model_raw_step_mismatch", codes)
        self.assertEqual(result["behavior"]["total_tool_calls"], 0)

    def test_declared_step_count_mismatch(self):
        fixture = Fixture(self)
        raw_steps = [step(0, "search", {"keywords": "x"}, raw_payload())]
        raw = raw_trace(raw_steps)
        raw["step_count"] = 99
        result = fixture.task(raw)
        self.assertIn("raw_step_count_mismatch", {error["code"] for error in result["errors"]})
        self.assertFalse(result["trace_integrity"]["core_passed"])

    def test_protocol_missing_and_conflict_are_distinct(self):
        fixture = Fixture(self)
        missing_raw = raw_trace([])
        missing_model = model_from([])
        missing_raw.pop("terminal_protocol")
        missing_model.pop("terminal_protocol")
        missing = fixture.task(missing_raw, missing_model)
        self.assertEqual(missing["trace_integrity"]["protocol"]["status"], "missing")
        self.assertTrue(missing["trace_integrity"]["core_passed"])
        self.assertTrue(missing["trace_integrity"]["passed"])

        conflict_raw = raw_trace([], protocol="terminal-protocol-v1")
        conflict = fixture.task(conflict_raw, model_from([]), task_id="2")
        self.assertEqual(conflict["trace_integrity"]["protocol"]["status"], "conflict")
        self.assertIn("terminal_protocol_conflict", {error["code"] for error in conflict["errors"]})

    def test_structural_args_ignore_object_key_order(self):
        fixture = Fixture(self)
        raw_steps = [step(0, "other", {"a": 1, "b": 2}, {"ok": True})]
        model = model_from(raw_steps)
        model["steps"][0]["tool_args"] = {"b": 2, "a": 1}
        result = fixture.task(raw_trace(raw_steps), model)
        self.assertTrue(result["trace_integrity"]["alignment"]["ok"])

    def test_duplicate_dsh_step_is_not_an_alignment_error(self):
        fixture = Fixture(self)
        raw_steps = [
            step(0, "search", {"keywords": "x"}, raw_payload(), step_number=7),
            step(1, "click", {"value": "A"}, raw_payload(), step_number=7),
        ]
        result = fixture.task(raw_trace(raw_steps))
        self.assertTrue(result["trace_integrity"]["alignment"]["ok"])
        self.assertEqual(result["behavior"]["total_tool_calls"], 2)

    def test_control_raw_missing_is_expected_and_not_environment_gap(self):
        fixture = Fixture(self)
        raw_steps = [step(0, "mea_round_report", {"summary": "x"}, {"raw_missing": True})]
        result = fixture.task(raw_trace(raw_steps))
        self.assertTrue(result["trace_integrity"]["environment_evidence_complete"])
        self.assertEqual(result["behavior"]["control_tool_counts"], {"mea_round_report": 1})
        self.assertFalse(result["outcome"]["environment_done"])

    def test_first_done_is_locked(self):
        fixture = Fixture(self)
        steps = [
            step(0, "click", {"value": "x"}, raw_payload(done=True, reward_type="repeat_loop")),
            step(1, "click", {"value": "Buy Now"}, raw_payload(done=True, reward_type="gold_purchase", purchase_marker={"asin": "A", "name": "x", "price": 1, "options": {}})),
        ]
        result = fixture.task(raw_trace(steps, reset_marker={"observation_state": observation("product_detail", ["x"])}))
        self.assertEqual(result["outcome"]["terminal_index"], 0)
        self.assertEqual(result["outcome"]["class"], "repeat_loop")
        self.assertFalse(result["outcome"]["gold_success"])
        classes = [item["class"] for item in result["behavior"]["anomalies"]]
        self.assertIn("post_terminal_shopping_action", classes)
        self.assertIn("post_terminal_terminal_response", classes)

    def test_no_terminal_complete_is_false_not_intent_classification(self):
        result = Fixture(self).task(raw_trace([]))
        self.assertFalse(result["outcome"]["environment_done"])
        self.assertIsNone(result["outcome"]["class"])
        self.assertIn("no_terminal_observed", {error["code"] for error in result["errors"]})
        self.assertFalse(any("agent_stop" in error["code"] for error in result["errors"]))

    def test_no_terminal_with_environment_gap_is_unknown(self):
        steps = [step(0, "search", {"keywords": "x"}, {"raw_missing": True})]
        result = Fixture(self).task(raw_trace(steps))
        self.assertIsNone(result["outcome"]["environment_done"])
        self.assertFalse(result["trace_integrity"]["environment_evidence_complete"])

    def test_unknown_terminal_type_is_preserved(self):
        steps = [step(0, "finish", {"reason": "x"}, raw_payload(done=True, reward_type="future_type"))]
        result = Fixture(self).task(raw_trace(steps))
        self.assertEqual(result["outcome"]["reward_type"], "future_type")
        self.assertEqual(result["outcome"]["class"], "unknown_terminal_type")

    def test_purchase_receipt_empty_absent_and_malformed(self):
        fixture = Fixture(self)
        cases = [
            ("default", False),
            ("absent", None),
            ({"asin": "A"}, None),
            ([], None),
        ]
        for index, (receipt, expected) in enumerate(cases):
            steps = [step(0, "finish", {"reason": "x"}, raw_payload(done=True, reward_type="wrong_purchase", purchase_marker=receipt))]
            result = fixture.task(raw_trace(steps), task_id=str(index))
            self.assertIs(result["outcome"]["purchase_occurred"], expected)

    def test_wrong_purchase_can_still_have_purchase_occurred(self):
        receipt = {"asin": "A", "name": "wrong", "price": 5, "options": {}}
        steps = [step(0, "click", {"value": "Buy Now"}, raw_payload(done=True, reward_type="wrong_purchase", purchase_marker=receipt))]
        result = Fixture(self).task(
            raw_trace(steps, reset_marker={"observation_state": observation("product_detail", ["Buy Now"])})
        )
        self.assertTrue(result["outcome"]["purchase_occurred"])
        self.assertFalse(result["outcome"]["environment_task_success"])

    def test_reset_state_makes_click_checkable(self):
        steps = [step(0, "click", {"value": "A"}, raw_payload(state=observation("product_detail", ["Buy Now"])))]
        result = Fixture(self).task(
            raw_trace(steps, reset_marker={"observation_state": observation("search_results", ["A"])})
        )
        self.assertEqual(result["coverage"]["click_legality"]["evaluated_count"], 1)
        self.assertFalse(any(item["class"] in {"inferred_invalid_click", "invalid_click_unverifiable"} for item in result["behavior"]["anomalies"]))

    def test_missing_actions_unknown_but_empty_actions_invalid(self):
        fixture = Fixture(self)
        one = [step(0, "click", {"value": "A"}, raw_payload())]
        unknown = fixture.task(raw_trace(one, reset_marker={"observation_state": observation("search_results")}))
        invalid = fixture.task(raw_trace(one, reset_marker={"observation_state": observation("search_results", [])}), task_id="2")
        self.assertEqual(unknown["coverage"]["click_legality"]["unknown_count"], 1)
        self.assertIn("invalid_click_unverifiable", {item["class"] for item in unknown["behavior"]["anomalies"]})
        self.assertIn("inferred_invalid_click", {item["class"] for item in invalid["behavior"]["anomalies"]})

    def test_environment_gap_invalidates_state_until_new_observation(self):
        steps = [
            step(0, "search", {"keywords": "x"}, {"raw_missing": True}),
            step(1, "click", {"value": "A"}, raw_payload(state=observation("product_detail", ["Buy Now"]))),
            step(2, "click", {"value": "Buy Now"}, raw_payload()),
        ]
        result = Fixture(self).task(raw_trace(steps))
        click_items = {item["index"]: item["class"] for item in result["behavior"]["anomalies"] if item["tool_name"] == "click"}
        self.assertEqual(click_items[1], "invalid_click_unverifiable")
        self.assertNotIn(2, click_items)

    def test_ask_shopper_and_control_retain_page(self):
        steps = [
            step(0, "search", {"keywords": "x"}, raw_payload(state=observation("search_results", ["A"]))),
            step(1, "ask_shopper", {"question": "q"}, {"question": "q", "reply": "r"}),
            step(2, "mea_round_report", {"summary": "s"}, {"raw_missing": True}),
            step(3, "click", {"value": "A"}, raw_payload()),
        ]
        result = Fixture(self).task(raw_trace(steps))
        self.assertEqual(result["coverage"]["click_legality"]["evaluated_count"], 1)
        self.assertFalse(any(item["class"].startswith("inferred_invalid") for item in result["behavior"]["anomalies"]))

    def test_invalid_click_subclasses(self):
        fixture = Fixture(self)
        cases = [
            ("product_detail", "Buy Now", "inferred_invalid_buy"),
            ("product_detail", "Red", "inferred_invalid_option"),
            ("product_detail", "Description", "inferred_invalid_navigation"),
            ("information_subpage", "Features", "inferred_invalid_navigation"),
        ]
        for index, (page, value, expected) in enumerate(cases):
            steps = [step(0, "click", {"value": value}, raw_payload())]
            result = fixture.task(raw_trace(steps, reset_marker={"observation_state": observation(page, [])}), task_id=str(index))
            self.assertIn(expected, {item["class"] for item in result["behavior"]["anomalies"]})

    def test_progress_thresholds_and_post_terminal_exclusion(self):
        steps = [
            step(0, "search", {"keywords": "x"}, raw_payload(progress_marker={"consecutive_repeats": 1, "no_progress_steps": 3})),
            step(1, "click", {"value": "A"}, raw_payload(done=True, reward_type="repeat_loop", progress_marker={"consecutive_repeats": 2, "no_progress_steps": 4})),
            step(2, "click", {"value": "A"}, raw_payload(done=True, reward_type="repeat_loop", progress_marker={"consecutive_repeats": 9, "no_progress_steps": 9})),
        ]
        result = Fixture(self).task(raw_trace(steps, reset_marker={"observation_state": observation("search_results", ["A"])}))
        repeated = [item["index"] for item in result["behavior"]["anomalies"] if item["class"] == "repeated_action"]
        no_progress = [item["index"] for item in result["behavior"]["anomalies"] if item["class"] == "no_progress"]
        self.assertEqual(repeated, [1])
        self.assertEqual(no_progress, [1])

    def test_progress_missing_is_coverage_unknown(self):
        steps = [step(0, "search", {"keywords": "x"}, raw_payload(progress_marker=None))]
        result = Fixture(self).task(raw_trace(steps))
        self.assertEqual(result["coverage"]["progress"]["unknown_count"], 1)
        self.assertFalse(any(item["class"] in {"repeated_action", "no_progress"} for item in result["behavior"]["anomalies"]))

    def test_post_terminal_shopping_and_control_are_separate(self):
        steps = [
            step(0, "finish", {"reason": "x"}, raw_payload(done=True, reward_type="graceful_stop")),
            step(1, "ask_shopper", {"question": "q"}, {"question": "q", "reply": "r"}),
            step(2, "mea_round_report", {"summary": "s"}, {"raw_missing": True}),
        ]
        result = Fixture(self).task(raw_trace(steps))
        by_class = {item["class"]: item["index"] for item in result["behavior"]["anomalies"]}
        self.assertEqual(by_class["post_terminal_shopping_action"], 1)
        self.assertEqual(by_class["post_terminal_control_call"], 2)

    def test_replay_uses_nested_reset_state_only(self):
        steps = [step(0, "click", {"value": "A"}, raw_payload())]
        replay = replay_pre_states({"reset": observation("search_results", ["A"])}, steps)
        self.assertIsNone(replay[0]["state"])

    def test_nearest_rank_and_empty_distribution(self):
        self.assertIsNone(nearest_rank([]))
        self.assertEqual(nearest_rank(range(1, 21)), 19)
        self.assertEqual(nearest_rank([1]), 1)

    def test_summary_fixed_denominator_and_unknown(self):
        fixture = Fixture(self)
        complete = fixture.task(raw_trace([]), task_id="1")
        gap_steps = [step(0, "search", {"keywords": "x"}, {"raw_missing": True})]
        unknown = fixture.task(raw_trace(gap_steps), task_id="2")
        summary = build_summary(
            [complete, unknown],
            {"manifest_unique_task_count": 2},
            {"manifest.json": "hash"},
        )
        metric = summary["metrics"]["environment_terminal"]
        self.assertEqual(metric["denominator"], 2)
        self.assertEqual(metric["count"], 0)
        self.assertEqual(metric["rate"], 0)
        self.assertEqual(metric["unknown_count"], 1)

    def test_failure_breakdown_counts_tasks_and_occurrences(self):
        fixture = Fixture(self)
        steps = [
            step(0, "click", {"value": "x"}, raw_payload(state=observation("search_results", []))),
            step(1, "click", {"value": "y"}, raw_payload(state=observation("search_results", []))),
        ]
        result = fixture.task(raw_trace(steps, reset_marker={"observation_state": observation("search_results", [])}))
        breakdown = build_failure_breakdown([result])
        invalid = breakdown["behavior_anomalies"]["inferred_invalid_click_all"]
        self.assertEqual(invalid["task_count"], 1)
        self.assertEqual(invalid["occurrence_count"], 2)

    def test_run_is_byte_deterministic(self):
        fixture = Fixture(self)
        _, first_code, _, first_out = fixture.run()
        snapshots = {path.name: path.read_bytes() for path in first_out.iterdir()}
        run_dir = fixture.root / "run"
        second_out = fixture.root / "second"
        _, second_code = evaluate_run(run_dir, second_out)
        self.assertEqual(first_code, second_code)
        self.assertEqual(snapshots, {path.name: path.read_bytes() for path in second_out.iterdir()})


if __name__ == "__main__":
    unittest.main()
