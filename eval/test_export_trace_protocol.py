#!/usr/bin/env python3
"""export_trace 终局协议测试：第一次真实 done（v2）vs 最后 done（v1）。

运行：
  python3 eval/test_export_trace_protocol.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from export_trace import build_traces  # noqa: E402


def tool_events():
    """构造合成 session 事件：购买终局 → 锁定响应（重复同一终局）。"""
    events = []

    def call(step, name, args):
        events.append({
            "type": "tool/call",
            "data": {"step": step, "name": name, "arguments": args},
        })

    def result(raw, text="obs"):
        events.append({
            "type": "tool/result",
            "data": {
                "message": {"content": [{"type": "text", "text": text}]},
                "meta": {"raw": raw},
            },
        })

    events.append({
        "type": "user/message",
        "data": {"source": {"kind": "user"},
                 "content": [{"type": "text", "text": "测试任务"}]},
    })
    call(1, "click", {"value": "buy now"})
    result({
        "done": True,
        "reward": 1.0,
        "reward_valid": True,
        "termination_reason": "gold_purchase",
        "reward_detail": {"reward_type": "gold_purchase", "purchase_success": True},
        "purchase": {"asin": "111111111111", "price": 10},
    })
    # 终局后的锁定响应：同一终局快照，不得覆盖第一次终局。
    call(2, "finish", {"reason": "done"})
    result({
        "done": True,
        "locked": True,
        "terminal_lock_version": "terminal-lock-v1",
        "reward": 1.0,
        "reward_valid": True,
        "termination_reason": "gold_purchase",
        "reward_detail": {"reward_type": "gold_purchase", "purchase_success": True},
        "purchase": {"asin": "111111111111", "price": 10},
    })
    return events


def divergent_events():
    """旧数据形态：第一次硬停止终局，之后出现不同的自然终局。"""
    events = []

    def pair(step, raw):
        events.append({
            "type": "tool/call",
            "data": {"step": step, "name": "click", "arguments": {}},
        })
        events.append({
            "type": "tool/result",
            "data": {"message": {"content": [{"type": "text", "text": "o"}]},
                     "meta": {"raw": raw}},
        })

    events.append({
        "type": "user/message",
        "data": {"source": {"kind": "user"},
                 "content": [{"type": "text", "text": "测试任务"}]},
    })
    pair(1, {"done": True, "reward": -0.5, "reward_valid": True,
             "termination_reason": "max_steps",
             "reward_detail": {"reward_type": "max_steps"}, "purchase": {}})
    pair(2, {"done": True, "reward": -0.1, "reward_valid": True,
             "termination_reason": "graceful_stop",
             "reward_detail": {"reward_type": "graceful_stop"}, "purchase": {}})
    return events


class ExportTraceProtocolTest(unittest.TestCase):
    def test_v2_takes_first_done_and_keeps_purchase(self):
        model, raw = build_traces(tool_events(), terminal_protocol="terminal-protocol-v2")
        self.assertEqual(model["terminal_protocol"], "terminal-protocol-v2")
        self.assertEqual(raw["terminal_protocol"], "terminal-protocol-v2")
        self.assertEqual(model["terminal"]["termination_reason"], "gold_purchase")
        self.assertEqual(model["terminal"]["purchase"]["asin"], "111111111111")
        self.assertEqual(model["terminal"]["purchase"]["price"], 10)

    def test_v1_legacy_last_done(self):
        model, _ = build_traces(divergent_events(), terminal_protocol="terminal-protocol-v1")
        self.assertEqual(model["terminal"]["termination_reason"], "graceful_stop")

    def test_v2_first_terminal_locks_hard_stop(self):
        model, _ = build_traces(divergent_events(), terminal_protocol="terminal-protocol-v2")
        self.assertEqual(model["terminal"]["termination_reason"], "max_steps")
        self.assertEqual(model["terminal"]["reward"], -0.5)

    def test_unknown_protocol_rejected(self):
        with self.assertRaises(ValueError):
            build_traces([], terminal_protocol="terminal-protocol-v9")


if __name__ == "__main__":
    unittest.main()
