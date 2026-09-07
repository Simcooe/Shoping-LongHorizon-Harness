#!/usr/bin/env python3
"""Interaction Router（B3）单元测试：区分 ask / continue / done / waiting / stop。

覆盖 case 47 / 204 式：追问写在最终文字里、未走 ask_shopper、环境未终局
→ 决策为 ask，且会路由回 ask_shopper 继续同一任务。
"""
from __future__ import annotations

import unittest

from eval.interaction_router import (
    DECISION_ASK,
    DECISION_CONTINUE,
    DECISION_DONE,
    DECISION_NON_TERMINAL_AGENT_STOP,
    DECISION_WAITING,
    classify_turn,
    looks_like_question,
)


def model_trace(steps):
    return {"task": "buy something", "steps": steps}


def raw_trace(done=False, purchase=None, steps=None):
    terminal = {"done": done, "purchase": purchase or {}}
    return {"terminal": terminal, "steps": steps or []}


class InteractionRouterTest(unittest.TestCase):
    def test_case47_question_in_final_text_requires_ask(self):
        mt = model_trace([
            {"step": 1, "tool_name": "search", "tool_args": {},
             "observation": "搜索结果"},
            {"step": 2, "tool_name": "click", "tool_args": {"value": "酒红色单球"},
             "observation": "已选规格"},
        ])
        rt = raw_trace(done=False)
        decision = classify_turn(
            mt, rt,
            "酒红色单球19元，请问您需要购买几个酒红色单球呢？",
        )
        self.assertEqual(decision["decision"], DECISION_ASK)
        self.assertEqual(decision["reason"], "question_left_in_final_text")

    def test_case204_confirm_question_requires_ask(self):
        mt = model_trace([
            {"step": 1, "tool_name": "click", "tool_args": {"value": "P204"},
             "observation": "商品详情"},
        ])
        rt = raw_trace(done=False)
        decision = classify_turn(mt, rt, "请问是否确认购买这款64元的商品？")
        self.assertEqual(decision["decision"], DECISION_ASK)

    def test_ask_shopper_reply_then_continue(self):
        mt = model_trace([
            {"step": 1, "tool_name": "ask_shopper",
             "tool_args": {"question": "要几个？"},
             "observation": "用户回复：两个"},
        ])
        rt = raw_trace(done=False)
        decision = classify_turn(mt, rt, "")
        self.assertEqual(decision["decision"], DECISION_CONTINUE)
        self.assertEqual(decision["reply"], "两个")

    def test_ask_shopper_no_reply_waiting(self):
        mt = model_trace([
            {"step": 1, "tool_name": "ask_shopper",
             "tool_args": {"question": "要几个？"},
             "observation": "用户回复："},
        ])
        rt = raw_trace(done=False)
        decision = classify_turn(mt, rt, "")
        self.assertEqual(decision["decision"], DECISION_WAITING)

    def test_terminal_done_decision_done(self):
        mt = model_trace([
            {"step": 1, "tool_name": "click", "tool_args": {"value": "buy now"},
             "observation": "Episode finished."},
        ])
        rt = raw_trace(done=True, purchase={"asin": "A"})
        decision = classify_turn(mt, rt, "购买完成")
        self.assertEqual(decision["decision"], DECISION_DONE)
        self.assertEqual(decision["purchase"]["asin"], "A")

    def test_stop_without_terminal_or_question(self):
        mt = model_trace([
            {"step": 1, "tool_name": "search", "tool_args": {},
             "observation": "搜索结果"},
        ])
        rt = raw_trace(done=False)
        decision = classify_turn(mt, rt, "我完成了")
        self.assertEqual(decision["decision"], DECISION_NON_TERMINAL_AGENT_STOP)


if __name__ == "__main__":
    unittest.main()
