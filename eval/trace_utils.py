#!/usr/bin/env python3
"""Evaluate pipeline 共享工具：canonical terminal、非法点击推断、session 事件。

只读已有 trace/session，不做模型调用、不重跑任务、不修改原始文件。

关键语义：trace 里每个 step 的 observation_state 是「动作后」状态。因此推断
某个 click 是否非法必须用「动作前」状态（即上一个 step 的动作后状态，或
reset 的初始 observation_state）。本模块用 replay_states 重放状态链来保证
推断正确，字段统一用 inferred_*，不伪装成 pre-action ground truth。
"""
from __future__ import annotations

import html
import json
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# 任务结果类终局（真实任务结果：购买 / 放弃 / 买错 / 不可判）
NATURAL_TERMINALS = {
    "gold_purchase",
    "valid_alternative_purchase",
    "partial_alternative_purchase",
    "wrong_purchase",
    "reward_unverifiable",
    "graceful_stop",
    "early_abstain",
}

# 硬停止（环境检测到异常强制终止，agent 可能继续后被后续终局覆盖）
HARD_STOPS = {"repeat_loop", "max_steps"}

# --------------------------------------------------------------------------- #
# 终局协议版本
# --------------------------------------------------------------------------- #
# v1（历史口径）：第一条任务结果类自然终局为 canonical terminal，
#   后来的自然终局会覆盖之前的硬停止（如 repeat_loop 后 finish）。
# v2（统一终止协议）：第一条 done=true 的终局即锁定（与环境侧
#   terminal-lock-v1 对齐）；硬停止之后的动作不得追认为停止前获得的信息。
TERMINAL_PROTOCOL_V1 = "terminal-protocol-v1"
TERMINAL_PROTOCOL_V2 = "terminal-protocol-v2"
DEFAULT_TERMINAL_PROTOCOL = TERMINAL_PROTOCOL_V2

# task_success 判据：reward_type 属于真正买中
SUCCESS_TYPES = {"gold_purchase", "valid_alternative_purchase"}

# reward_type -> outcome.class
REWARD_TYPE_TO_CLASS = {
    "gold_purchase": "success_gold",
    "valid_alternative_purchase": "success_valid_alternative",
    "partial_alternative_purchase": "success_partial_alternative",
    "wrong_purchase": "wrong_purchase",
    "graceful_stop": "graceful_stop",
    "early_abstain": "early_abstain",
    "repeat_loop": "repeat_loop",
    "max_steps": "max_steps",
    "reward_unverifiable": "reward_unverifiable",
}

# 导航按钮（归一化后），用于区分 option vs navigation
NAV_BUTTONS = {
    "back to search",
    "< prev",
    "next >",
    "description",
    "features",
    "reviews",
    "attributes",
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalize_action(value) -> str:
    """动作值归一化：HTML 反转义 + 去空白 + 小写。"""
    return html.unescape(str(value or "")).strip().casefold()


def guard_norm(value) -> str:
    """对齐 src/buy-guard.js 的归一化（只反转义尖括号）。"""
    return str(value or "").replace("&lt;", "<").replace("&gt;", ">").strip().lower()


def is_buy_now(value) -> bool:
    return guard_norm(value) == "buy now"


def step_reward_type(step: dict) -> str | None:
    raw = step.get("raw") or {}
    rd = raw.get("reward_detail") or {}
    return rd.get("reward_type") or raw.get("termination_reason")


def step_is_done(step: dict) -> bool:
    return (step.get("raw") or {}).get("done") is True


def step_observation_state(step: dict) -> dict | None:
    raw = step.get("raw") or {}
    os_ = raw.get("observation_state")
    return os_ if isinstance(os_, dict) else None


def canonical_terminal_step(steps: list[dict]) -> tuple[int | None, dict | None]:
    """返回 (0-based index, step) 的 canonical terminal（terminal-protocol-v1，历史口径）。

    规则：第一条「任务结果类终局」（NATURAL_TERMINALS）作为 canonical terminal；
    若不存在任务结果终局，则退化为第一条硬停止（repeat_loop / max_steps）。
    这样 task 1151 的 repeat_loop（idx 11）会被后续 gold_purchase（idx 16）
    覆盖，符合旧 spec 验收。新协议请使用 first_terminal_step()。
    """
    first_hard_idx = None
    first_hard_step = None
    for index, step in enumerate(steps):
        if not step_is_done(step):
            continue
        rt = step_reward_type(step)
        if rt in NATURAL_TERMINALS:
            return index, step
        if rt in HARD_STOPS and first_hard_idx is None:
            first_hard_idx = index
            first_hard_step = step
    if first_hard_idx is not None:
        return first_hard_idx, first_hard_step
    return None, None


def first_terminal_step(steps: list[dict]) -> tuple[int | None, dict | None]:
    """terminal-protocol-v2：第一条 done=true 的终局即锁定。

    与环境侧 terminal-lock-v1 对齐：硬停止（如 max_steps / repeat_loop）
    之后的 finish / 购买不再改写终局；后续动作只能作为事后解释。
    """
    for index, step in enumerate(steps):
        if step_is_done(step):
            return index, step
    return None, None


def select_terminal(steps: list[dict], protocol: str) -> tuple[int | None, dict | None]:
    """按协议版本选择终局。"""
    if protocol == TERMINAL_PROTOCOL_V1:
        return canonical_terminal_step(steps)
    if protocol == TERMINAL_PROTOCOL_V2:
        return first_terminal_step(steps)
    raise ValueError(f"unknown terminal protocol: {protocol}")


def replay_states(steps: list[dict], reset_state: dict | None) -> list[tuple[dict | None, dict | None]]:
    """重放 steps，返回每个 step 的 (动作前状态, 动作后状态)。

    动作前状态 = 上一个环境工具的 observation_state（或 reset 初始状态）。
    """
    previous = reset_state or {}
    result = []
    for step in steps:
        post = step_observation_state(step)
        result.append((previous if isinstance(previous, dict) else None, post))
        if post is not None:
            previous = post
    return result


def infer_invalid_click(step: dict, previous_state: dict | None = None) -> bool:
    """推断该 click 是否非法（用「动作前」状态，近似而非 ground truth）。

    previous_state 为空时回退到动作后状态（退化，仅兼容）。
    """
    if step.get("tool_name") != "click":
        return False
    value = normalize_action((step.get("tool_args") or {}).get("value"))
    state = previous_state
    if state is None:
        state = step_observation_state(step)
    if not isinstance(state, dict):
        return False
    actions = {normalize_action(x) for x in state.get("actions", [])}
    return value not in actions


def classify_invalid_click(value: str, page_type: str | None) -> str:
    """把非法 click 归类为具体 anomaly 类型（spec 第七节）。

    - value == buy now → inferred_invalid_buy
    - information_subpage 上其他非法 click → inferred_invalid_navigation
    - product_detail 上非法 option（非导航按钮）→ inferred_invalid_option
    - 其余 → inferred_invalid_click
    """
    if is_buy_now(value):
        return "inferred_invalid_buy"
    if page_type == "information_subpage":
        return "inferred_invalid_navigation"
    if page_type == "product_detail":
        if normalize_action(value) in NAV_BUTTONS:
            return "inferred_invalid_navigation"
        return "inferred_invalid_option"
    if normalize_action(value) in NAV_BUTTONS:
        return "inferred_invalid_navigation"
    return "inferred_invalid_click"


def classify_non_terminal(steps: list[dict], reset_state: dict | None = None) -> str:
    """无 canonical terminal 时的 failure.class（spec 第六节决策树）。

    用「动作前」状态判断最后一步 click 是否非法。
    """
    if not steps:
        return "non_terminal_trace_incomplete"
    states = replay_states(steps, reset_state)
    last = steps[-1]
    prev_state = states[-1][0] if states else None
    tool_name = last.get("tool_name")
    value = normalize_action((last.get("tool_args") or {}).get("value"))
    prev_actions = (prev_state or {}).get("actions", []) if isinstance(prev_state, dict) else []
    actions = {normalize_action(a) for a in prev_actions}
    page_type = (prev_state or {}).get("page_type") if isinstance(prev_state, dict) else None
    invalid = tool_name == "click" and value not in actions

    if tool_name == "click" and is_buy_now(value) and invalid:
        return "invalid_buy_then_false_completion"
    if tool_name == "click" and value != "buy now" and invalid and page_type == "product_detail":
        return "invalid_option_then_false_completion"
    return "non_terminal_agent_stop"


def scan_invalid_clicks(steps: list[dict], limit: int, reset_state: dict | None) -> list[dict]:
    """扫描 [0, limit) 区间内所有推断非法 click，返回结构化 anomaly 列表。

    用「动作前」状态链判断，避免把合法的 ASIN / description / back-to-search
    点击误报为非法。
    """
    states = replay_states(steps, reset_state)
    found = []
    for idx in range(min(limit, len(steps))):
        step = steps[idx]
        if step.get("tool_name") != "click":
            continue
        prev_state = states[idx][0] if idx < len(states) else None
        if not infer_invalid_click(step, prev_state):
            continue
        value = normalize_action((step.get("tool_args") or {}).get("value"))
        page_type = prev_state.get("page_type") if isinstance(prev_state, dict) else None
        cls = classify_invalid_click(value, page_type)
        prev_actions = prev_state.get("actions", []) if isinstance(prev_state, dict) else []
        found.append({
            "class": cls,
            "step_indices": [idx],
            "tool_names": ["click"],
            "tool_args": step.get("tool_args") or {},
            "page_type": page_type,
            "available_actions": list(prev_actions),
        })
    return found


def last_action_info(steps: list[dict], reset_state: dict | None = None) -> dict:
    if not steps:
        return {"tool_name": None, "tool_args": {}, "page_type": None,
                "available_actions": [], "done": None}
    states = replay_states(steps, reset_state)
    last = steps[-1]
    prev_state = states[-1][0] if states else None
    return {
        "tool_name": last.get("tool_name"),
        "tool_args": last.get("tool_args") or {},
        "page_type": prev_state.get("page_type") if isinstance(prev_state, dict) else None,
        "available_actions": list(prev_state.get("actions", [])) if isinstance(prev_state, dict) else [],
        "done": (last.get("raw") or {}).get("done"),
    }


def read_session_events(session_dir: Path, zstd_bin: str = "zstd") -> list[dict]:
    """解压 session.jsonl.zstd 并返回事件列表。失败返回空列表。"""
    session_file = session_dir / "session.jsonl.zstd"
    if not session_file.exists():
        hits = list(session_dir.glob("**/session.jsonl.zstd"))
        if hits:
            session_file = hits[0]
        else:
            return []
    try:
        raw = subprocess.check_output(
            [zstd_bin, "-dc", str(session_file)],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except Exception:
        return []
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def agent_turn_end(events: list[dict]) -> dict:
    """从 session 事件中提取最后一个 turn/end 的 reason。"""
    turns = [e for e in events if e.get("type") == "turn/end"]
    if not turns:
        return {"turn_completed": None, "turn_end_kind": "unknown"}
    reason = turns[-1].get("data", {}).get("reason") or {}
    kind = reason.get("kind")
    return {
        "turn_completed": kind == "completed",
        "turn_end_kind": kind if kind else "unknown",
    }
