#!/usr/bin/env python3
"""Interaction End Classifier（原 Interaction Router）：单次执行后的结束诊断。

纯函数，可独立测试，不依赖 LLM / 环境。只负责「分类」模型本轮如何结束，
不再驱动任何重跑（runner 已改为单次执行）。

分类结果（decision，仅作诊断记录）：
- done：环境已终局（无论有无购买回执，购买与否由 Completion Gate 再判）；
- ask：模型在最终文字里写了追问，但没有调用 ask_shopper 工具
  （case 47/204 式）——如实记录为「该调工具却没调」；
- continue：最后一动是 ask_shopper 且已拿到真实回复，但本轮就此结束；
- waiting：最后一动是 ask_shopper 但用户暂未回答（空回复）；
- non_terminal_agent_stop：环境未终局、无追问、也无回执地停止。
"""

from __future__ import annotations

import re

DECISION_DONE = "done"
DECISION_ASK = "ask"
DECISION_CONTINUE = "continue"
DECISION_WAITING = "waiting"
DECISION_NON_TERMINAL_AGENT_STOP = "non_terminal_agent_stop"

# 「写在最终回复里的追问」启发式：问号，或常见确认/询问句式。
_QUESTION_HINT = re.compile(r"[?？]|请问|是否|需要.*(吗|么)|确认一下|您要|您想")


def ask_shopper_reply(observation: str) -> str:
    """从 ask_shopper 的模型可见 observation（「用户回复：<reply>」）提取回复。"""
    if not isinstance(observation, str):
        return ""
    m = re.search(r"用户回复[：:]\s*(.*)", observation)
    return m.group(1).strip() if m else ""


def has_ask_shopper(steps: list) -> bool:
    return any(s.get("tool_name") == "ask_shopper" for s in steps or [])


def looks_like_question(text: str) -> bool:
    return bool(text) and bool(_QUESTION_HINT.search(str(text)))


def terminal_done(raw_trace: dict) -> bool:
    terminal = (raw_trace or {}).get("terminal") or {}
    if terminal.get("done") is True:
        return True
    # 兜底：任一 raw step done=true（terminal 缺失时）。
    for s in (raw_trace or {}).get("steps") or []:
        if (s.get("raw") or {}).get("done") is True:
            return True
    return False


def terminal_purchase(raw_trace: dict) -> dict:
    terminal = (raw_trace or {}).get("terminal") or {}
    purchase = terminal.get("purchase") or {}
    if purchase.get("asin"):
        return purchase
    for s in reversed((raw_trace or {}).get("steps") or []):
        p = (s.get("raw") or {}).get("purchase") or {}
        if p.get("asin"):
            return p
    return {}


def classify_turn(model_trace: dict, raw_trace: dict, final_text: str) -> dict:
    """给定一轮的模型视角 + 环境视角 + 最终文字，返回控制决策。

    final_text：本轮模型最终自然语言回复（headless stdout / session 终末文字）。
    """
    steps = (model_trace or {}).get("steps") or []
    last = steps[-1] if steps else None
    last_tool = last.get("tool_name") if last else None

    if terminal_done(raw_trace):
        return {"decision": DECISION_DONE,
                "purchase": terminal_purchase(raw_trace),
                "reason": "environment_terminal"}

    if last_tool == "ask_shopper":
        reply = ask_shopper_reply((last or {}).get("observation") or "")
        if not reply:
            return {"decision": DECISION_WAITING,
                    "reason": "user_has_not_answered"}
        return {"decision": DECISION_CONTINUE,
                "reply": reply,
                "reason": "reply_received_but_turn_ended"}

    # 环境未终局、最后一动不是 ask_shopper：若模型在最终文字里写了追问
    # 却没走工具，则路由回 ask_shopper。
    if looks_like_question(final_text) and not has_ask_shopper(steps):
        return {"decision": DECISION_ASK,
                "final_text": final_text,
                "reason": "question_left_in_final_text"}

    return {"decision": DECISION_NON_TERMINAL_AGENT_STOP,
            "reason": "stopped_without_terminal_or_question"}
