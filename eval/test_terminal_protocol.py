#!/usr/bin/env python3
"""终局协议 v1/v2 的离线语义测试（合成 steps，不依赖真实轨迹）。

运行：
  python3 eval/test_terminal_protocol.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.trace_utils import (  # noqa: E402
    TERMINAL_PROTOCOL_V1,
    TERMINAL_PROTOCOL_V2,
    canonical_terminal_step,
    first_terminal_step,
    select_terminal,
)


def step(tool="click", done=False, reward_type=None, step_no=None):
    raw = {"done": done}
    if done:
        raw["reward_detail"] = {"reward_type": reward_type}
        raw["termination_reason"] = reward_type
        raw["reward"] = 1.0 if reward_type == "gold_purchase" else -0.65
    return {"step": step_no, "tool_name": tool, "tool_args": {}, "raw": raw}


class TerminalProtocolTest(unittest.TestCase):
    def test_v1_natural_terminal_overrides_hard_stop(self):
        # task 1151 形态：repeat_loop(idx 1) 后 gold_purchase(idx 2)。
        steps = [
            step("search"),
            step(done=True, reward_type="repeat_loop", step_no=11),
            step("click"),
            step(done=True, reward_type="gold_purchase", step_no=16),
        ]
        idx, s = canonical_terminal_step(steps)
        self.assertEqual(idx, 3)
        self.assertEqual(s["raw"]["reward_detail"]["reward_type"], "gold_purchase")

    def test_v2_first_terminal_locks(self):
        steps = [
            step("search"),
            step(done=True, reward_type="repeat_loop", step_no=11),
            step("click"),
            step(done=True, reward_type="gold_purchase", step_no=16),
        ]
        idx, s = first_terminal_step(steps)
        self.assertEqual(idx, 1)
        self.assertEqual(s["raw"]["reward_detail"]["reward_type"], "repeat_loop")

    def test_v2_abstain_after_hard_stop_keeps_hard_stop(self):
        # h0 15 例放弃案例形态：硬停止后出现 graceful_stop / early_abstain。
        steps = [
            step("search"),
            step(done=True, reward_type="max_steps", step_no=35),
            step(done=True, reward_type="max_steps", step_no=36),
            step("finish", done=True, reward_type="graceful_stop", step_no=39),
        ]
        idx, s = select_terminal(steps, TERMINAL_PROTOCOL_V2)
        self.assertEqual(idx, 1)
        self.assertEqual(s["raw"]["reward_detail"]["reward_type"], "max_steps")
        # v1 仍按历史口径取自然终局。
        idx1, s1 = select_terminal(steps, TERMINAL_PROTOCOL_V1)
        self.assertEqual(s1["raw"]["reward_detail"]["reward_type"], "graceful_stop")

    def test_no_terminal_in_either_protocol(self):
        steps = [step("search"), step("click")]
        self.assertEqual(first_terminal_step(steps), (None, None))
        self.assertEqual(canonical_terminal_step(steps), (None, None))

    def test_unknown_protocol_rejected(self):
        with self.assertRaises(ValueError):
            select_terminal([], "terminal-protocol-v9")


if __name__ == "__main__":
    unittest.main()
