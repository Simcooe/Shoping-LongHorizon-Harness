#!/usr/bin/env python3
"""Judge v2：时间线化四态 + 七维过程质量（model_trace-only）。

与 v1 的区别：
- 读取 rubric v2 时间线（final 约束集合 + 条件许可 + 事件），按「决策时刻」
  评判；不因未来需求批评过去，也不按被撤销要求扣最终分；
- final_requirement_verdicts 与 candidate_evidence 分开：候选页有匹配证据
  不等于最终决定满足（未购买时最终状态为 unknown，证据单独保留）；
- 每步证据引用使用 trace 内出现顺序（position），并校验引用真实存在且
  不晚于决策时刻；
- 证据预算替代固定头尾截断：整体预算内保留完整观察，超预算时按步压缩并
  输出 evidence_truncated/coverage，缺关键证据判 unknown；
- 两种模式：
  * --judge-mode llm：真实 LLM 调用（代码路径完整，需 API 凭据）；
  * --judge-mode mock：确定性证据检索，仅用于链路验证，输出显式标注
    judge_mode=mock，不得写入正式 evaluations 冒充真实模型结果。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.judge import (  # noqa: E402
    DIMENSION_KEYS,
    _parse_json_content,
    call_judge,
    load_env,
    resolve_model_config,
)
from eval.requirement_schema import (  # noqa: E402
    LIFECYCLE_ACTIVE,
    SOURCE_INITIAL_QUERY,
    canonical_json,
)

JUDGE_OUTPUT_SCHEMA = "shopping-judge-output-v2"
FOUR_STATES = ("satisfied", "violated", "unknown", "not_applicable")
MOCK_MODEL_ID = "mock-judge-v1"

JUDGE_V2_SYSTEM_PROMPT = """# 任务：多轮购物轨迹的需求满足度评判（rubric v2，时间线感知）

你是购物 Agent 评测的 LLM Judge。输入包含：初始公开 Query、rubric v2 的
最终有效约束（决策时刻有效，含生命周期标注）、条件报价许可、决策信息、
以及 Agent 的 model-visible 轨迹（按出现顺序 position 编号）。

## 判定规则

对每条最终有效约束输出四态之一：
- satisfied：最终决定（购买/最终候选）有明确的 model-visible 证据满足该约束；
- violated：轨迹中有明确证据表明最终决定或已执行动作违反该约束；
- unknown：证据不足（未检查、观察缺失、未完成最终决定、证据被截断）；
- not_applicable：确实不适用（谨慎使用，不得替代生命周期排除）。

关键规则：
1. 候选页曾出现匹配证据 ≠ 最终决定满足。只有「最终购买/选定候选」的
   证据才能判 satisfied；没有有效最终决定时用 unknown，候选证据写入
   candidate_evidence。
2. 不因约束在决策时刻之后才出现的信息批评之前的动作；被
   superseded/revoked 的约束不在最终集合里，不要评判。
3. 条件报价许可只在其 scope（商品/规格/金额/数量）内放宽对应约束；
   其他金额/规格/商品不放宽。
4. 「Episode finished.」不是成交证据；Agent 最终文字声称「买好了」不能
   单独证明成交，必须有页面/选项/价格证据。
5. 不读取也不输出任何后台 reward、gold 匹配或内部推理。
6. step_reference 必须引用输入中真实存在的 position，且不得晚于决策时刻；
   引用要指明是哪次动作与哪段可见证据。

## 七维过程质量（各 0/1/2，附简短理由与证据 position）

clarification_strategy / information_retention / search_strategy /
candidate_utilization / evidence_verification / decision_quality /
termination_efficiency。

0=明显问题或基本未完成；1=部分完成但有遗漏/低效；2=整体合理。
没有 ask_shopper 不自动扣澄清分（初始需求可能已完整）；有 ask 也不自动
证明信息保持优秀，必须给出证据。终止后继续动作、虚假完成声明应反映在
decision_quality 与 termination_efficiency。

## 输出格式（只输出 JSON）

{
  "candidate_evidence": [
    {"constraint_id": "c0001", "position": 2, "quote": "页面原文片段"}
  ],
  "final_requirement_verdicts": [
    {
      "constraint_id": "c0001",
      "status": "satisfied",
      "requirement_version": 3,
      "position_reference": [2, 6],
      "event_reference": ["..."],
      "reasoning": "一句话：哪次动作、哪段可见证据"
    }
  ],
  "dimension_scores": {"clarification_strategy": 2, "information_retention": 2,
    "search_strategy": 1, "candidate_utilization": 1,
    "evidence_verification": 1, "decision_quality": 1,
    "termination_efficiency": 1},
  "dimension_reasons": {"clarification_strategy": "...", "information_retention": "...",
    "search_strategy": "...", "candidate_utilization": "...",
    "evidence_verification": "...", "decision_quality": "...",
    "termination_efficiency": "..."}
}

覆盖全部最终有效约束，不遗漏、不重复；status 四选一；分数只能 0/1/2。"""


# --------------------------------------------------------------------------- #
# 轨迹序列化（证据预算，替代固定截断）
# --------------------------------------------------------------------------- #

def serialize_trace_with_budget(model_trace: dict, decision_pos: int | None,
                                budget: int) -> tuple[str, dict]:
    """按出现顺序序列化步骤；超预算时按步压缩并报告覆盖状态。"""
    steps = model_trace.get("steps") or []
    end = (decision_pos + 2) if decision_pos is not None else len(steps)
    included = steps[:min(end, len(steps))]
    rendered = []
    for pos, s in enumerate(included):
        tool = s.get("tool_name") or ""
        args = s.get("tool_args") or {}
        args_str = json.dumps(args, ensure_ascii=False)
        obs = str(s.get("observation") or "")
        rendered.append({
            "position": pos,
            "step": s.get("step"),
            "text": f"[pos {pos}] Step {s.get('step')} [{tool}] {args_str}\n"
                    f"Observation: {obs}",
            "obs_len": len(obs),
        })
    total = sum(len(r["text"]) for r in rendered)
    truncated = False
    if total > budget and rendered:
        truncated = True
        per_step = max(400, budget // len(rendered))
        for r in rendered:
            if len(r["text"]) > per_step:
                head = int(per_step * 0.7)
                tail = per_step - head
                r["text"] = (r["text"][:head]
                             + "\n…[证据预算内截断]…\n"
                             + r["text"][-tail:])
    text = "\n\n".join(r["text"] for r in rendered)
    coverage = {
        "steps_total": len(steps),
        "steps_included": len(included),
        "chars": sum(len(r["text"]) for r in rendered),
        "evidence_budget": budget,
        "evidence_truncated": truncated,
    }
    return text, coverage


# --------------------------------------------------------------------------- #
# mock judge（确定性，仅链路验证；显式标注，不得冒充真实模型）
# --------------------------------------------------------------------------- #

def _constraint_evidence_texts(constraint: dict) -> list[str]:
    texts = []
    if constraint.get("source_quote"):
        texts.append(str(constraint["source_quote"]))
    value = constraint.get("value")
    if isinstance(value, dict):
        if value.get("quote"):
            texts.append(str(value["quote"]))
        if value.get("upper") is not None:
            texts.append(str(value["upper"]))
    elif value is not None:
        texts.append(str(value))
    return [t for t in texts if t]


def mock_judge(rubric_v2: dict, model_trace: dict) -> dict:
    """确定性证据检索：候选证据与最终满足分离，不猜语义。"""
    steps = model_trace.get("steps") or []
    decision = rubric_v2.get("decision") or {}
    decision_pos = decision.get("position")
    final_ids = set(rubric_v2.get("final_constraint_ids") or [])
    constraints = [
        c for c in rubric_v2.get("constraints") or [] if c["id"] in final_ids
    ]
    permissions = [
        p for p in rubric_v2.get("conditional_permissions") or []
        if p.get("lifecycle_status") == LIFECYCLE_ACTIVE
    ]

    def obs_window(lo, hi):
        out = []
        for pos in range(max(0, lo), min(len(steps), hi)):
            out.append(str(steps[pos].get("observation") or ""))
        return "\n".join(out)

    candidate_evidence = []
    verdicts = []
    for c in constraints:
        needles = _constraint_evidence_texts(c)
        found_positions = []
        for pos, s in enumerate(steps):
            if decision_pos is not None and pos > decision_pos:
                break
            # ask_shopper 的观测是用户的话（需求来源），不是商品/页面证据，
            # 不能作为约束满足的候选证据（避免循环自证）。
            if s.get("tool_name") == "ask_shopper":
                continue
            obs = str(s.get("observation") or "")
            for needle in needles:
                if needle and needle in obs:
                    found_positions.append(pos)
                    break
        if found_positions:
            candidate_evidence.append({
                "constraint_id": c["id"],
                "position": found_positions[-1],
                "quote": needles[0][:80],
            })

        status = "unknown"
        refs = []
        if decision.get("kind") == "purchase_attempt" and decision_pos is not None:
            # 最终页面（决策步及前一步）包含证据才视为最终满足。
            final_window = obs_window(decision_pos - 1, decision_pos + 1)
            if any(n and n in final_window for n in needles):
                status = "satisfied"
                refs = [p for p in found_positions if p >= decision_pos - 1]
                refs = refs or [decision_pos]
        verdicts.append({
            "constraint_id": c["id"],
            "status": status,
            "requirement_version": decision.get("requirement_version"),
            "position_reference": refs,
            "event_reference": [c.get("source_event_id")] if c.get("source_event_id") else [],
            "reasoning": (
                "mock：最终决策页面可见证据匹配" if status == "satisfied"
                else "mock：无最终决策证据（链路验证占位，非真实模型判定）"
            ),
        })

    # 条件许可：只报告 scope 状态，不产生新约束满足项。
    permission_notes = [
        {
            "permission_id": p["permission_id"],
            "scope": p.get("scope"),
            "lifecycle_status": p.get("lifecycle_status"),
        }
        for p in permissions
    ]

    ask_positions = [
        pos for pos, s in enumerate(steps) if s.get("tool_name") == "ask_shopper"
    ]
    search_count = sum(1 for s in steps if s.get("tool_name") == "search")
    product_clicks = sum(
        1 for s in steps
        if s.get("tool_name") == "click"
        and re.fullmatch(r"\d{9,}", str((s.get("tool_args") or {}).get("value") or ""))
    )
    subpage_clicks = sum(
        1 for s in steps
        if s.get("tool_name") == "click"
        and str((s.get("tool_args") or {}).get("value") or "").lower()
        in ("description", "features", "reviews")
    )
    post_terminal = bool(decision_pos is not None and len(steps) > decision_pos + 1)
    has_evidence_satisfied = any(v["status"] == "satisfied" for v in verdicts)

    scores, reasons = {}, {}
    scores["clarification_strategy"] = 2 if len(ask_positions) <= 3 else 1
    reasons["clarification_strategy"] = (
        f"mock：ask_shopper {len(ask_positions)} 次（机械统计）"
    )
    scores["information_retention"] = 1 if ask_positions else 2
    reasons["information_retention"] = (
        "mock：存在澄清但保持情况未验证" if ask_positions
        else "mock：无澄清可保持"
    )
    scores["search_strategy"] = 2 if search_count >= 2 else (1 if search_count == 1 else 0)
    reasons["search_strategy"] = f"mock：search {search_count} 次"
    scores["candidate_utilization"] = 2 if product_clicks >= 2 else (1 if product_clicks == 1 else 0)
    reasons["candidate_utilization"] = f"mock：打开候选 {product_clicks} 个"
    scores["evidence_verification"] = 2 if subpage_clicks >= 1 else 1
    reasons["evidence_verification"] = f"mock：子页查看 {subpage_clicks} 次"
    if decision.get("kind") == "purchase_attempt":
        scores["decision_quality"] = 2 if has_evidence_satisfied else 1
        reasons["decision_quality"] = "mock：购买尝试" + (
            "且最终页面有证据" if has_evidence_satisfied else "但最终页面证据不足"
        )
    elif decision.get("kind") == "agent_stop":
        scores["decision_quality"] = 0
        reasons["decision_quality"] = "mock：无终局动作即停止"
    else:
        scores["decision_quality"] = 1
        reasons["decision_quality"] = f"mock：决策类型 {decision.get('kind')}"
    if post_terminal:
        scores["termination_efficiency"] = 0
        reasons["termination_efficiency"] = "mock：终局后仍有动作"
    else:
        scores["termination_efficiency"] = 2 if decision.get("kind") in (
            "purchase_attempt", "finish") else 1
        reasons["termination_efficiency"] = f"mock：决策类型 {decision.get('kind')}"

    return {
        "candidate_evidence": candidate_evidence,
        "final_requirement_verdicts": verdicts,
        "conditional_permission_notes": permission_notes,
        "dimension_scores": scores,
        "dimension_reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# LLM 输出校验
# --------------------------------------------------------------------------- #

def validate_v2_output(obj: dict, rubric_v2: dict, valid_positions: set,
                       decision_pos: int | None) -> list[str]:
    errors = []
    final_ids = set(rubric_v2.get("final_constraint_ids") or [])
    verdicts = obj.get("final_requirement_verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        errors.append("final_requirement_verdicts 不是非空 list")
        return errors
    seen = set()
    for i, v in enumerate(verdicts):
        where = f"verdict[{i}]"
        cid = v.get("constraint_id")
        if cid not in final_ids:
            errors.append(f"{where}: constraint_id 不在最终集合: {cid}")
            continue
        if cid in seen:
            errors.append(f"{where}: constraint_id 重复: {cid}")
            continue
        seen.add(cid)
        if v.get("status") not in FOUR_STATES:
            errors.append(f"{where}: status 非法: {v.get('status')}")
        refs = v.get("position_reference")
        if not isinstance(refs, list):
            errors.append(f"{where}: position_reference 不是 list")
            continue
        for p in refs:
            if not isinstance(p, int) or p not in valid_positions:
                errors.append(f"{where}: position 不存在: {p}")
            elif decision_pos is not None and p > decision_pos:
                errors.append(f"{where}: position {p} 晚于决策时刻 {decision_pos}")
        if v.get("status") in ("satisfied", "violated") and not refs:
            errors.append(f"{where}: {v.get('status')} 必须有证据引用")
        if not str(v.get("reasoning") or "").strip():
            errors.append(f"{where}: reasoning 为空")
    missing = final_ids - seen
    if missing:
        errors.append(f"缺少 verdict 的约束: {sorted(missing)}")
    scores = obj.get("dimension_scores") or {}
    for k in DIMENSION_KEYS:
        val = scores.get(k)
        if not isinstance(val, int) or isinstance(val, bool) or val not in (0, 1, 2):
            errors.append(f"dimension_scores.{k} 非法: {val!r}")
    reasons = obj.get("dimension_reasons") or {}
    for k in DIMENSION_KEYS:
        if not str(reasons.get(k) or "").strip():
            errors.append(f"dimension_reasons.{k} 为空")
    ce = obj.get("candidate_evidence")
    if not isinstance(ce, list):
        errors.append("candidate_evidence 不是 list")
    else:
        for i, e in enumerate(ce):
            p = e.get("position")
            if not isinstance(p, int) or p not in valid_positions:
                errors.append(f"candidate_evidence[{i}]: position 不存在: {p}")
    return errors


def build_v2_user_input(rubric_v2: dict, trace_text: str, public_query: str) -> str:
    final_ids = set(rubric_v2.get("final_constraint_ids") or [])
    constraints = [
        c for c in rubric_v2.get("constraints") or [] if c["id"] in final_ids
    ]
    payload = {
        "query": public_query,
        "decision": rubric_v2.get("decision"),
        "final_constraints": constraints,
        "conditional_permissions": rubric_v2.get("conditional_permissions"),
        "requirement_version_at_decision": (
            rubric_v2.get("decision") or {}
        ).get("requirement_version"),
        "timeline": rubric_v2.get("requirement_versions"),
        "trajectory": trace_text,
        "notes": [
            "position 为轨迹内出现顺序编号；引用必须真实存在且不晚于决策时刻。",
            "superseded/revoked 约束已排除在最终集合外，不要评判。",
            "候选证据与最终满足分离：未购买时最终状态用 unknown。",
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def judge_task_v2_llm(api_key, base_url, model, rubric_v2, trace_text,
                      public_query, valid_positions, decision_pos,
                      max_attempts=3):
    feedback = None
    last_err = None
    for attempt in range(max_attempts):
        content_input = build_v2_user_input(rubric_v2, trace_text, public_query)
        if feedback:
            content_input += ("\n\n上一次输出未通过校验，请修正后重新输出完整 JSON：\n"
                              + "\n".join(feedback))
        messages = [
            {"role": "system", "content": JUDGE_V2_SYSTEM_PROMPT},
            {"role": "user", "content": content_input},
        ]
        try:
            content = call_judge(api_key, base_url, model, messages,
                                 include_response_format=(attempt == 0))
            obj = _parse_json_content(content)
            errors = validate_v2_output(obj, rubric_v2, valid_positions, decision_pos)
            if not errors:
                return obj, None, attempt + 1
            feedback = errors
            last_err = "；".join(errors[:5])
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            feedback = None
    return None, last_err, max_attempts


# --------------------------------------------------------------------------- #
# CLI 主流程（由 eval/judge.py --rubric-version v2 分发）
# --------------------------------------------------------------------------- #

def run(args) -> int:
    env = load_env(args.env)
    api_key, base_url, model = resolve_model_config(env)
    if args.judge_mode == "llm" and not api_key:
        print("缺少 JUDGE_API_KEY / DEEPSEEK_API_KEY（检查 .env）", file=sys.stderr)
        return 1

    rubrics_v2_dir = Path(args.rubrics_v2)
    traces_dir = Path(args.traces)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if "judgments" == out_dir.name and not args.judge_mode == "mock":
        pass  # v2 输出目录由调用方指定，这里不做额外限制

    task_ids = sorted(
        (p.name.split(".rubric_v2.json")[0]
         for p in rubrics_v2_dir.glob("*.rubric_v2.json")),
        key=lambda t: int(t) if t.isdigit() else 0,
    )
    if args.max_tasks is not None:
        task_ids = task_ids[: args.max_tasks]

    frozen = set()
    if not args.force:
        for f in out_dir.glob("*.json"):
            if f.name == "manifest.json":
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("frozen") is True and d.get("schema_version") == JUDGE_OUTPUT_SCHEMA:
                frozen.add(f.stem)

    succeeded, failed, skipped = [], [], []
    rows = []
    for tid in task_ids:
        if tid in frozen:
            skipped.append(tid)
            continue
        rubric_v2 = json.loads(
            (rubrics_v2_dir / f"{tid}.rubric_v2.json").read_text(encoding="utf-8")
        )
        trace_path = traces_dir / f"{tid}.model_trace.json"
        if not trace_path.exists():
            failed.append({"task_id": tid, "reason": "model_trace 缺失"})
            continue
        model_trace = json.loads(trace_path.read_text(encoding="utf-8"))

        decision = rubric_v2.get("decision") or {}
        decision_pos = decision.get("position")
        trace_text, coverage = serialize_trace_with_budget(
            model_trace, decision_pos, args.evidence_budget
        )
        valid_positions = set(range(min(
            len(model_trace.get("steps") or []),
            (decision_pos + 2) if decision_pos is not None
            else len(model_trace.get("steps") or []),
        )))

        public_query = rubric_v2.get("metadata", {}).get("query") or ""
        if not public_query:
            task_text = str(model_trace.get("task") or "")
            public_query = task_text.split("\n\n用户画像")[0].strip()

        if args.judge_mode == "mock":
            judgment = mock_judge(rubric_v2, model_trace)
            errors = validate_v2_output(judgment, rubric_v2, valid_positions,
                                        decision_pos)
            if errors:
                failed.append({"task_id": tid, "reason": "; ".join(errors[:5])})
                continue
            model_id = MOCK_MODEL_ID
            attempts = 0
        else:
            judgment, err, attempts = judge_task_v2_llm(
                api_key, base_url, model, rubric_v2, trace_text, public_query,
                valid_positions, decision_pos,
            )
            if err is not None:
                failed.append({"task_id": tid, "reason": err})
                continue
            model_id = model

        rec = {
            "task_id": tid,
            "schema_version": JUDGE_OUTPUT_SCHEMA,
            "rubric_version": "v2",
            "frozen": True,
            "judgment": judgment,
            "metadata": {
                "judge_mode": args.judge_mode,
                "model": model_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "trace_source": "model_trace_only",
                "event_source": rubric_v2.get("event_source"),
                "terminal_protocol": "terminal-protocol-v2",
                "decision": decision,
                "requirement_version_at_decision": decision.get("requirement_version"),
                "evidence_coverage": coverage,
                "attempts": attempts,
            },
        }
        (out_dir / f"{tid}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        succeeded.append(tid)
        rows.append({
            "task_id": tid,
            "judge_mode": args.judge_mode,
            "final_verdict_count": len(judgment["final_requirement_verdicts"]),
        })

    manifest = {
        "schema_version": JUDGE_OUTPUT_SCHEMA,
        "rubric_version": "v2",
        "judge_mode": args.judge_mode,
        "model": MOCK_MODEL_ID if args.judge_mode == "mock" else model,
        "rubrics_v2_dir": str(rubrics_v2_dir),
        "traces_dir": str(traces_dir),
        "task_count": len(task_ids),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "mock 判定仅用于链路验证，不是真实模型结果。"
            if args.judge_mode == "mock" else None
        ),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "judge_mode": args.judge_mode,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "skipped": len(skipped),
    }, ensure_ascii=False))
    print(f"judgments_v2 -> {out_dir}")
    return 2 if failed else 0
