#!/usr/bin/env python3
"""历史轨迹事件重建（retrospective）语义测试。

覆盖：重复 step 编号、缺 raw、post_terminal 排除、无法映射显式报缺证据、
同事件重复/异内容重建、确定性位置映射。

运行：
  python3 eval/tests/test_reconstruct_events.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.reconstruct_events import (  # noqa: E402
    HeuristicExtractor,
    extract_ask_shopper_qa,
    reconstruct_task,
    terminal_boundary,
)
from eval.requirement_schema import EVENT_MODIFY  # noqa: E402


def model_trace(steps):
    return {"task": "测试任务", "steps": steps}


def qa_step(step, question, reply, pos_marker=None):
    # 真实 model_trace 格式：单步，问题在 tool_args、回复在 observation。
    return [
        {"step": step, "tool_name": "ask_shopper",
         "tool_args": {"question": question},
         "observation": reply},
    ]


class ReconstructionTest(unittest.TestCase):
    def test_qa_ids_stable_under_duplicate_step_numbers(self):
        steps = []
        steps += qa_step(5, "预算多少？", "预算两百块左右")
        steps += qa_step(5, "还有吗？", "没了")  # 同一 step 号
        mt = model_trace(steps)
        qa = extract_ask_shopper_qa(mt)
        self.assertEqual(len(qa), 2)
        self.assertEqual([q["qa_id"] for q in qa], ["qa-0001", "qa-0002"])

    def test_heuristic_budget_extraction(self):
        ext = HeuristicExtractor(base_budget_value={"upper": 100.0})
        # 启发式只提取可无歧义解析的阿拉伯数字预算（中文数字交给预算编译器）。
        steps = qa_step(3, "预算多少？", "预算改成200块")
        mt = model_trace(steps)
        doc = reconstruct_task(
            1, mt, {"steps": [{"raw": {"done": True}, "step": 4}]},
            "run-x", "bench-x", ext, {},
        )
        budget_events = [e for e in doc["events"]
                         if e["requirement_key"] == "budget"]
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0]["kind"], EVENT_MODIFY)
        self.assertEqual(budget_events[0]["status"], "applied")
        # 基数不同 → modify；提取值 200。
        self.assertEqual(budget_events[0]["new_value"]["upper"], 200.0)

    def test_post_terminal_events_excluded(self):
        ext = HeuristicExtractor(base_budget_value={"upper": 100.0})
        steps = []
        steps += qa_step(1, "预算多少？", "预算改成150块")   # 终局前
        # 第一次终局在 position 2（qa call + reply 之后的 done）。
        steps.append({"step": 2, "tool_name": "finish",
                      "tool_args": {"reason": "no_suitable_product"},
                      "observation": "Episode finished."})
        steps.append({"step": 3, "tool_name": "noop", "tool_args": {},
                      "observation": ""})
        steps += qa_step(4, "还要吗？", "预算改成200块")      # 终局后
        mt = model_trace(steps)
        raw = {"steps": [
            {"raw": {}, "step": 1},
            {"raw": {}, "step": 1},
            {"raw": {"done": True, "termination_reason": "max_steps"},
             "step": 2},
            {"raw": {}, "step": 3},
            {"raw": {}, "step": 4},
            {"raw": {}, "step": 4},
        ]}
        doc = reconstruct_task(1, mt, raw, "run-x", "bench-x", ext, {})
        boundary = doc["terminal_boundary"]["first_terminal_position"]
        self.assertEqual(boundary, 2)
        statuses = [e["status"] for e in doc["events"]]
        self.assertIn("applied", statuses)
        self.assertIn("post_terminal_excluded", statuses)
        # QA 记录保留原文（供事后解释），终局后置位标记。
        post = [q for q in doc["ask_shopper_qa"] if q["post_terminal"]]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["reply"], "预算改成200块")

    def test_unmapped_event_reported_not_guessed(self):
        ext = HeuristicExtractor(base_budget_value={"upper": 100.0})
        steps = qa_step(9, "预算？", "预算改成120块")
        mt = model_trace(steps)
        # 提供一个与 ask_shopper step 号对不上的 raw（无法唯一映射）。
        raw = {"steps": [{"raw": {"done": True}, "step": 99}]}
        doc = reconstruct_task(1, mt, raw, "run-x", "bench-x", ext, {})
        qa = doc["ask_shopper_qa"][0]
        # position 仍按 trace 出现顺序映射（确定性），不依赖 raw 的步号。
        self.assertTrue(qa["position_mapped"])
        ev0 = doc["events"][0]
        self.assertEqual(ev0["status"], "applied")
        self.assertIsNotNone(ev0["step_index"])

    def test_external_events_preserved_with_step_index(self):
        # mock/LLM 产物：外部事件直接给 step_index 时沿用。
        ext_meta = {
            "events": [
                {
                    "event_id": "x/e1", "kind": "modify",
                    "requirement_key": "budget",
                    "new_value": {"upper": 168.0},
                    "source_reply_id": "qa-0001",
                    "source_quote": "168可以",
                    "step_index": 2, "session": "run-x/916",
                }
            ],
        }

        class NoLocalExtractor:
            name = "external"
            version = "external-v1"

            def extract(self, *a, **k):
                return []

        steps = qa_step(2, "168可以吗？", "168可以")
        mt = model_trace(steps)
        raw = {"steps": [{"raw": {"done": True}, "step": 5}]}
        doc = reconstruct_task(
            916, mt, raw, "run-x", "bench-x", NoLocalExtractor(), ext_meta,
        )
        ev0 = doc["events"][0]
        self.assertEqual(ev0["step_index"], 2)
        self.assertEqual(ev0["status"], "applied")

    def test_terminal_boundary_none_without_raw(self):
        self.assertIsNone(terminal_boundary(None))
        self.assertIsNone(terminal_boundary({"steps": []}))


if __name__ == "__main__":
    unittest.main()
