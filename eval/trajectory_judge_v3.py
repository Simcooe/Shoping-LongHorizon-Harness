#!/usr/bin/env python3
"""离线 LLM Trajectory Judge v3（shopping-rubric-v2）。

读取冻结 Rubric、model_trace 的 step/tool_name/tool_args/observation，以及
raw_trace 中脱敏后的白名单 progress 诊断（consecutive_repeats /
no_progress_steps），评价 ShopSimulator 长程购物过程。

核心语义（v3.1 修复）：
- 「页面上存在 TaskFacts 事实」与「用户在本次轨迹中有效要求该事实」严格分开。
  * effective_status：该 requirement 在本次轨迹中是否被用户激活（active/latent/
    modified/rejected/revoked/unknown）；
  * user_requirement_verdict：作为用户需求是否满足（satisfied/violated/unknown/
    not_applicable）；
  * evidence_status：页面/轨迹事实是否支持/矛盾（supported/contradicted/unknown）。
- 初始 Query requirement 的证据来自冻结 Rubric（source=rubric），不来自 Agent
  的工具调用；model_trace 只能证明 Agent 后续看到了什么、做了什么。
- 只有真实 shopper 用户回复才能激活/修改/放宽/拒绝/撤销 TaskFacts requirement。

隔离边界（本模块绝不读取/不接收）：
- reward / reward_detail / reward_type
- termination_reason / purchase_success / 完整 purchase 回执
- gold ASIN / 完整 hidden TaskFacts / observation_state
- source_goals.private.jsonl / 旧 judgment / report / MEA runtime logs
- deterministic evaluator 结果

允许脱敏提供终局边界（只帮助区分「环境已终止但需求未满足」与「轨迹尚未完成」）：
- first_terminal_observed / terminal_step / post_terminal_steps_present

progress 只能作为过程诊断（循环/无进展/终止效率），不能作为商品满足、
购买成功或 Gold 命中的证据。

用法（真实 LLM）:
  python3 eval/trajectory_judge_v3.py \
    --input evaluations/h0 \
    --out /tmp/judge-smoke-v2 \
    --only 25,47,108,133,263,596,602 \
    --judge-mode llm \
    --concurrency 2

用法（mock，仅链路验证，不调 LLM）:
  python3 eval/trajectory_judge_v3.py \
    --input evaluations/h0 --out /tmp/judge-mock \
    --only 263 --judge-mode mock
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

JUDGE_VERSION = "trajectory-judge-v3"
RUBRIC_VERSION = "shopping-rubric-v2"
SCHEMA_NOTE = "shopping-judge-output-v3"
MANIFEST_SCHEMA = "shopping-judge-manifest-v3"
TERMINAL_PROTOCOL = "terminal-protocol-v2"

DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_EVIDENCE_BUDGET = 90000
DEFAULT_TIMEOUT = 300

EFFECTIVE_STATUSES = ("active", "latent", "modified", "rejected", "revoked", "unknown")
USER_VERDICTS = ("satisfied", "violated", "unknown", "not_applicable")
EVIDENCE_STATUSES = ("supported", "contradicted", "unknown")
DECISION_KINDS = ("purchase", "finish", "environment_terminal", "unresolved")
OBSERVED_KINDS = ("purchase", "finish", "environment_terminal")

DIMENSION_KEYS = [
    "clarification_strategy",
    "information_retention",
    "search_strategy",
    "candidate_utilization",
    "evidence_verification",
    "decision_quality",
    "termination_efficiency",
]

# 证据引用字段白名单（按 source 区分）。
EVIDENCE_FIELDS = {
    "rubric": ("query", "query_quote"),
    "model_trace": ("step", "tool_name", "tool_args", "observation"),
    "raw_progress": ("consecutive_repeats", "no_progress_steps"),
}
EVIDENCE_SOURCES = tuple(EVIDENCE_FIELDS.keys())

# shopper 回复事件标识（只有这些能激活/修改 TaskFacts requirement）。
SHOPPER_REPLY_EVENTS = ("shopper_reply", "ask_shopper_reply")

# 代理内容审核拒答特征。
REFUSAL_MARKERS = ("当前输入涉及敏感信息", "换个话题", "无法回答", "不能回答")

MOCK_MODEL_ID = "mock-judge-v3"

SHOPPER_REPLY_RE = re.compile(r"^用户回复[：:]\s*(.*)$", re.DOTALL)


JUDGE_SYSTEM_PROMPT = """# 任务：评判 ShopSimulator 长程购物 Agent 的一条执行轨迹

你是购物 Agent 评测的离线 LLM Judge。你只能使用冻结 Rubric、model_trace 和
白名单 progress diagnostics，以及脱敏后的终局边界（runtime_boundary）。

你不能访问或假设 reward、gold ASIN、hidden TaskFacts、termination_reason、
purchase_success、完整 raw state 或 deterministic evaluator 结果。

## 核心原则：把两件事严格分开
1. 「页面上存在某个 TaskFacts 事实」：这是商品/轨迹事实（evidence_status）。
2. 「用户在本次轨迹中有效要求该事实」：这是需求状态（effective_status）。
只有两者都成立时，才能判用户需求 satisfied。

## 输入说明
- query / explicit_requirements / taskfact_requirements：来自冻结 Rubric。
  * explicit_requirements：初始 Query 明确要求，从任务开始有效；
  * taskfact_requirements：完整 TaskFacts 候选，初始不一定已被用户告知。
- trajectory：model_trace 的每步（step / tool_name / tool_args / observation）。
  ask_shopper 的用户回复在 observation 中（"用户回复：…"）。
- progress：raw_trace 脱敏后的白名单诊断（consecutive_repeats /
  no_progress_steps），仅用于循环/无进展/终止效率，必须结合动作与 Observation。
- runtime_boundary：脱敏终局边界（first_terminal_observed / terminal_step /
  post_terminal_steps_present），只帮助区分「环境已终止」与「轨迹未完成」，
  不替代 model_trace 证据，也不能证明购买成功。

## 需求解释（requirement_interpretation）
对每条 requirement 给出 effective_status：
- active：初始 Query 明确要求，或已被真实 shopper 用户回复确认/激活；
- latent：TaskFacts 候选但本次轨迹中未被用户告知/确认；
- modified：用户通过 shopper 回复修改过（以最新回复为准）；
- rejected：用户明确拒绝；
- revoked：用户明确撤销；
- unknown：无法从轨迹判断。

证据规则：
- 初始 Query requirement 的 active 证据必须来自 rubric（source=rubric,
  field=query 或 query_quote），不能拿 model_trace step 1 的 tool_args 当证据。
- TaskFacts requirement 的 active/modified/rejected/revoked 证据必须指向真实
  shopper 用户回复（source=model_trace, field=observation, event=shopper_reply
  或 ask_shopper_reply）。没有真实用户回复时，TaskFacts 必须保持 latent/unknown。
- 商品详情 Observation、Agent 自己的总结、ask_shopper 的提问本身，都不能证明
  用户确认了某个 TaskFacts requirement。

## 逐条 verdict（rubric_verdicts）
每条 requirement 恰好一个 verdict，含三个分离字段：
- effective_status：同上（与 requirement_interpretation 一致）。
- user_requirement_verdict：
  * satisfied：用户有效要求该事实，且 model-visible 页面证据明确满足；
  * violated：用户有效要求该事实，但明确违反（价格明确超预算、明确选错规格、
    用户明确拒绝后仍坚持该候选等）；
  * unknown：证据不足（未检查、观察缺失、未完成最终决定、证据被截断）；
  * not_applicable：该要求在本轨迹中不作为用户需求（如 latent 且未激活）。
- evidence_status：页面/轨迹事实状态：
  * supported：有 model-visible 证据支持该事实；
  * contradicted：有明确证据与该事实矛盾；
  * unknown：无证据可判断。

硬规则：
1. effective_status == latent 时，user_requirement_verdict 不能是 satisfied，
   也不能是 violated；推荐 not_applicable（不确定时用 unknown）。但若页面证据
   确实存在，evidence_status 仍可为 supported。
2. 搜索标题不能自动证明详情规格满足；Agent 自己的自然语言声明不是商品事实。
3. 缺证据是 unknown，不是 violated。
4. finish / "Episode finished." 不能被当作购买成功证据。
5. 需求修改、确认、放宽、拒绝以用户实际回复为准。
6. 只使用决策时刻之前（含决策时刻）的证据；终局后的动作不能证明之前的决策。
7. progress 不能证明商品满足或购买成功。
8. satisfied/violated 必须给出至少一个 source=model_trace 的证据引用；
   unknown/not_applicable 通常无引用或只有 rubric 引用。

## decision
- observed：是否观察到 purchase（buy now）/ finish（调用 finish）/
  environment_terminal（环境硬停止）决策动作；
- step：决策 step（轨迹中真实存在的编号；未观察到时为 null）；
- kind：purchase / finish / environment_terminal / unresolved；
- requirements_resolved：Judge 是否认为用户需求已经满足或明确处理（放弃/拒绝
  也算明确处理）。该字段独立于 observed，禁止用 observed 隐含任务成功。

requirements_resolved 硬规则：
1. kind=unresolved 或 environment_terminal 时必须为 false。
2. kind=purchase 时，只有全部 effective_status=active/modified 的需求均为
   satisfied，才能为 true；存在 unknown/violated/not_applicable 时必须为 false。
3. kind=finish 时，仅当 Agent 明确处理或明确放弃了全部有效需求时才可为 true。

## 七维过程质量（各 0/1/2）
0=明显问题或基本未完成；1=部分完成但有遗漏/低效；2=整体合理。
1. clarification_strategy：是否识别关键缺口并进行必要、具体、不重复的澄清。
2. information_retention：是否记住初始需求和最新用户回复，避免继续旧需求。
3. search_strategy：搜索词是否覆盖要求，是否根据结果收敛而非重复搜索。
4. candidate_utilization：是否打开、比较、筛选已发现候选并推进核验。
5. evidence_verification：购买/结束前是否核验类别、规格、数量、价格，而非只看标题。
6. decision_quality：最终选择或放弃是否符合有效需求和公开证据。
7. termination_efficiency：是否避免过早终止、循环、无效动作和终局后动作。

## 输出格式（只输出 JSON，无其他文本）
{
  "decision": {
    "observed": true,
    "step": 15,
    "kind": "purchase",
    "requirements_resolved": false
  },
  "requirement_interpretation": [
    {
      "requirement_id": "r0001",
      "requirement_kind": "explicit",
      "effective_status": "active",
      "basis": [
        {"source": "rubric", "field": "query_quote", "rubric_id": "r0001"}
      ],
      "reasoning": "一句话"
    },
    {
      "requirement_id": "t0001",
      "requirement_kind": "taskfact",
      "effective_status": "active",
      "basis": [
        {"source": "model_trace", "step": 1, "field": "observation",
         "event": "shopper_reply"}
      ],
      "reasoning": "用户在 step 1 明确确认实心圆棒"
    }
  ],
  "rubric_verdicts": [
    {
      "requirement_id": "r0001",
      "requirement_kind": "explicit",
      "effective_status": "active",
      "user_requirement_verdict": "satisfied",
      "evidence_status": "supported",
      "evidence": [
        {"source": "model_trace", "step": 15, "field": "observation"}
      ],
      "reasoning": "所选商品详情明确为玻璃纤维杆"
    }
  ],
  "dimension_scores": {
    "clarification_strategy": 0, "information_retention": 0,
    "search_strategy": 0, "candidate_utilization": 0,
    "evidence_verification": 0, "decision_quality": 0,
    "termination_efficiency": 0
  }
}

evidence.source 只能取 rubric / model_trace / raw_progress：
- rubric 的 field 取 query / query_quote，且带 rubric_id；
- model_trace 的 field 取 step / tool_name / tool_args / observation，可选
  event（shopper_reply / ask_shopper_reply）；
- raw_progress 的 field 取 consecutive_repeats / no_progress_steps。
覆盖全部 requirement，不遗漏、不重复。只输出 JSON。"""


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

def load_env(path: str) -> dict:
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def resolve_model_config(env: dict, prefix: str = "JUDGE") -> tuple[str, str, str]:
    api_key = env.get(f"{prefix}_API_KEY") or env.get("DEEPSEEK_API_KEY")
    base_url = (
        env.get(f"{prefix}_BASE_URL")
        or env.get("DEEPSEEK_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = env.get(f"{prefix}_MODEL") or DEFAULT_MODEL
    return api_key, base_url, model


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_json_content(content):
    if isinstance(content, (dict, list)):
        return content
    text = content.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("content 里找不到 JSON 对象")
        return json.loads(text[start : end + 1])


def call_llm(api_key, base_url, model, messages, include_response_format,
             timeout=DEFAULT_TIMEOUT):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0}
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    obj = json.loads(body)
    return obj["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# 需求 / shopper 回复 / progress / 终局边界 提取
# --------------------------------------------------------------------------- #

def flatten_requirements(rubric: dict) -> tuple[list[dict], list[dict]]:
    """返回 (explicit, taskfact)，每条含 id/kind/description 等白名单字段。"""
    explicit = []
    for r in rubric.get("explicit_requirements") or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        explicit.append({
            "id": r["id"],
            "kind": "explicit",
            "description": str(r.get("description") or ""),
            "hardness": r.get("hardness"),
            "source": r.get("source"),
            "query_quote": r.get("query_quote"),
            "taskfacts_basis": r.get("taskfacts_basis"),
        })
    taskfact = []
    for r in rubric.get("taskfact_requirements") or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        taskfact.append({
            "id": r["id"],
            "kind": "taskfact",
            "description": str(r.get("description") or ""),
            "hardness": r.get("hardness"),
            "source": r.get("source"),
            "query_quote": r.get("query_quote"),
            "taskfacts_basis": r.get("taskfacts_basis"),
        })
    return explicit, taskfact


def all_requirement_ids(explicit: list[dict], taskfact: list[dict]) -> set[str]:
    return {r["id"] for r in explicit} | {r["id"] for r in taskfact}


def extract_shopper_replies(model_trace: dict) -> list[dict]:
    """从 model_trace 提取真实的 shopper 用户回复。

    返回 [{"step", "question", "reply", "evidence_ref"}]。只有识别出的真实
    用户回复才能激活/修改/放宽/拒绝/撤销 TaskFacts requirement。
    """
    out = []
    for s in model_trace.get("steps") or []:
        if not isinstance(s, dict) or s.get("tool_name") != "ask_shopper":
            continue
        args = s.get("tool_args") or {}
        question = args.get("question") if isinstance(args, dict) else None
        obs = str(s.get("observation") or "")
        m = SHOPPER_REPLY_RE.match(obs)
        if not m or not m.group(1).strip():
            continue
        step = s.get("step")
        out.append({
            "step": step,
            "question": question,
            "reply": m.group(1).strip(),
            "evidence_ref": {
                "source": "model_trace",
                "step": step,
                "field": "observation",
                "event": "shopper_reply",
            },
        })
    return out


def extract_progress(raw_trace: dict) -> list[dict]:
    """只提取 raw_trace 每步的 progress 白名单字段。

    返回 [{"step": n, "consecutive_repeats": x, "no_progress_steps": y}]，
    仅包含 progress 存在且含目标字段的 step。绝不复刻 reward/termination/
    purchase/observation_state 等其他 raw 字段。
    """
    out = []
    for s in raw_trace.get("steps") or []:
        if not isinstance(s, dict):
            continue
        raw = s.get("raw")
        if not isinstance(raw, dict):
            continue
        progress = raw.get("progress")
        if not isinstance(progress, dict):
            continue
        entry = {"step": s.get("step")}
        for field in EVIDENCE_FIELDS["raw_progress"]:
            val = progress.get(field)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                entry[field] = val
        if any(f in entry for f in EVIDENCE_FIELDS["raw_progress"]):
            out.append(entry)
    return out


def extract_runtime_boundary(raw_trace: dict) -> dict:
    """脱敏终局边界投影：只允许 first_terminal_observed / terminal_step /
    post_terminal_steps_present。绝不读取 reward/termination_reason/purchase。
    """
    steps = raw_trace.get("steps") or []
    terminal_step = None
    for s in steps:
        if not isinstance(s, dict):
            continue
        raw = s.get("raw")
        if isinstance(raw, dict) and raw.get("done") is True:
            terminal_step = s.get("step")
            break
    boundary = {
        "first_terminal_observed": terminal_step is not None,
        "terminal_step": terminal_step,
        "post_terminal_steps_present": False,
    }
    if terminal_step is not None:
        seen_terminal = False
        for s in steps:
            if not isinstance(s, dict):
                continue
            raw = s.get("raw")
            done = isinstance(raw, dict) and raw.get("done") is True
            if not seen_terminal and done:
                seen_terminal = True
                continue
            if seen_terminal:
                boundary["post_terminal_steps_present"] = True
                break
    return boundary


def project_to_runtime_boundary(model_trace: dict,
                                raw_trace: dict) -> tuple[dict, list[dict]]:
    """生成仅到第一终局（含终局步）的 Judge 可见投影。

    原始 trace 不做任何修改。terminal_step 缺失时保留完整轨迹。
    """
    raw_steps = raw_trace.get("steps") or []
    terminal_position = None
    for position, step in enumerate(raw_steps):
        raw = step.get("raw") if isinstance(step, dict) else None
        if isinstance(raw, dict) and raw.get("done") is True:
            terminal_position = position
            break

    if terminal_position is None:
        return model_trace, extract_progress(raw_trace)

    # model/raw trace 在导出时按位置对齐；不能按 step 数值比较，因为历史数据中
    # step 编号可能重复。
    visible_steps = [
        s for s in (model_trace.get("steps") or [])[:terminal_position + 1]
        if isinstance(s, dict)
    ]
    visible_trace = dict(model_trace)
    visible_trace["steps"] = visible_steps
    visible_trace["step_count"] = len(visible_steps)
    visible_raw_trace = dict(raw_trace)
    visible_raw_trace["steps"] = raw_steps[:terminal_position + 1]
    visible_progress = extract_progress(visible_raw_trace)
    return visible_trace, visible_progress


# --------------------------------------------------------------------------- #
# 轨迹序列化（证据预算）
# --------------------------------------------------------------------------- #

def _truncate_obs(obs: str, max_chars: int) -> str:
    if len(obs) <= max_chars:
        return obs
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return obs[:head] + "\n…[中段已截断]…\n" + obs[-tail:]


def serialize_model_steps(model_trace: dict, query: str, budget: int,
                          obs_max_chars: int = 3000) -> tuple[str, dict]:
    """序列化 model_trace 步骤（白名单字段），超预算时按步压缩。"""
    steps = model_trace.get("steps") or []
    rendered = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        tool_name = s.get("tool_name") or ""
        tool_args = s.get("tool_args") or {}
        if isinstance(tool_args, dict):
            args_str = json.dumps(tool_args, ensure_ascii=False)
        else:
            args_str = str(tool_args)
        obs = str(s.get("observation") or "")
        if query:
            obs = obs.replace(f"Instruction: [SEP] {query} [SEP] ", "")
        obs = _truncate_obs(obs, obs_max_chars)
        rendered.append({
            "step": s.get("step"),
            "text": (f"Step {s.get('step')} [{tool_name}] {args_str}\n"
                     f"Observation: {obs}"),
        })
    total = sum(len(r["text"]) for r in rendered)
    truncated = False
    if total > budget and rendered:
        truncated = True
        per_step = max(500, budget // len(rendered))
        for r in rendered:
            if len(r["text"]) > per_step:
                head = int(per_step * 0.7)
                tail = per_step - head
                r["text"] = (r["text"][:head] + "\n…[证据预算内截断]…\n"
                             + r["text"][-tail:])
    text = "\n\n".join(r["text"] for r in rendered)
    coverage = {
        "steps_total": len(steps),
        "steps_included": len(rendered),
        "chars": sum(len(r["text"]) for r in rendered),
        "evidence_budget": budget,
        "evidence_truncated": truncated,
    }
    return text, coverage


def serialize_progress(progress: list[dict]) -> str:
    if not progress:
        return "(无可用 progress 诊断)"
    lines = []
    for p in progress:
        fields = " ".join(
            f"{f}={p[f]}" for f in EVIDENCE_FIELDS["raw_progress"] if f in p
        )
        lines.append(f"Step {p['step']} progress: {fields}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 输入组装（只含白名单信息）
# --------------------------------------------------------------------------- #

def build_input(rubric: dict, model_trace: dict, raw_trace: dict,
                budget: int) -> dict:
    explicit, taskfact = flatten_requirements(rubric)
    query = str(rubric.get("query") or "").strip()
    boundary = extract_runtime_boundary(raw_trace)
    visible_trace, visible_progress = project_to_runtime_boundary(
        model_trace, raw_trace)
    trace_text, coverage = serialize_model_steps(visible_trace, query, budget)
    progress_text = serialize_progress(visible_progress)
    shopper_replies = extract_shopper_replies(visible_trace)
    step_numbers = [
        s.get("step") for s in (visible_trace.get("steps") or [])
        if isinstance(s, dict)
    ]
    coverage["post_terminal_steps_excluded"] = max(
        0,
        len(model_trace.get("steps") or []) - len(visible_trace.get("steps") or []),
    )
    return {
        "query": query,
        "explicit_requirements": explicit,
        "taskfact_requirements": taskfact,
        "trajectory": trace_text,
        "shopper_replies": shopper_replies,
        "progress": progress_text,
        "runtime_boundary": boundary,
        "existing_steps": step_numbers,
        "notes": [
            "evidence.step 必须引用 existing_steps 中的真实编号。",
            "初始 Query requirement 的 active 证据来自 rubric（source=rubric）。",
            "taskfact_requirements 只有被真实 shopper 回复确认/修改/拒绝才能非 latent；否则不得判 user_requirement_verdict 为 satisfied/violated。",
            "progress 仅作过程诊断，不能证明商品满足/购买成功/Gold 命中。",
            "runtime_boundary 只区分环境是否终止，不证明购买成功或需求满足。",
            "『Episode finished.』不携带成交结果信息。",
            "只使用决策时刻之前（含决策时刻）的证据。",
            "trajectory/existing_steps/progress 已在第一终局截断；不得引用终局后的步骤。",
        ],
        "coverage": coverage,
    }


# --------------------------------------------------------------------------- #
# 严格校验
# --------------------------------------------------------------------------- #

def _validate_evidence_refs(refs, valid_steps: set, decision_step,
                            progress_steps: set,
                            terminal_step=None,
                            allowed_fields: dict = EVIDENCE_FIELDS) -> list[str]:
    """校验证据引用。返回错误列表。"""
    errors = []
    if not isinstance(refs, list):
        return ["evidence/basis 不是 list"]
    for i, ref in enumerate(refs):
        where = f"[{i}]"
        if not isinstance(ref, dict):
            errors.append(f"{where} 不是结构化对象（禁止裸 step 数字）")
            continue
        source = ref.get("source")
        if source not in allowed_fields:
            errors.append(f"{where}: source 非法 {source!r}")
            continue
        field = ref.get("field")
        if field not in allowed_fields[source]:
            errors.append(f"{where}: field 非法 {field!r}（source={source}）")
            continue
        if source == "rubric":
            if not ref.get("rubric_id"):
                errors.append(f"{where}: rubric 证据必须带 rubric_id")
            step = ref.get("step")
            if step is not None:
                errors.append(f"{where}: rubric 证据不应带 step")
            continue
        step = ref.get("step")
        if not isinstance(step, int) or isinstance(step, bool):
            errors.append(f"{where}: step 不是整数 {step!r}")
            continue
        if (isinstance(terminal_step, int) and not isinstance(terminal_step, bool)
                and step > terminal_step):
            errors.append(f"{where}: step {step} 晚于第一终局 {terminal_step}")
            continue
        if step not in valid_steps:
            errors.append(f"{where}: step {step} 不存在于轨迹")
            continue
        if source == "raw_progress" and step not in progress_steps:
            errors.append(f"{where}: raw_progress step {step} 无 progress 诊断")
            continue
        if decision_step is not None and step > decision_step:
            errors.append(f"{where}: step {step} 晚于决策时刻 {decision_step}")
    return errors


def _has_shopper_reply_basis(basis: list) -> bool:
    return any(
        isinstance(b, dict)
        and b.get("source") == "model_trace"
        and b.get("field") == "observation"
        and b.get("event") in SHOPPER_REPLY_EVENTS
        for b in basis
    )


def _has_model_trace_evidence(evidence: list) -> bool:
    return any(
        isinstance(e, dict) and e.get("source") == "model_trace"
        for e in evidence
    )


def validate_output(obj, rubric: dict, model_trace: dict,
                    progress: list[dict], runtime_boundary: dict | None = None) -> list[str]:
    errors = []
    if not isinstance(obj, dict):
        return ["顶层不是 JSON 对象"]

    explicit, taskfact = flatten_requirements(rubric)
    expected = all_requirement_ids(explicit, taskfact)
    kind_by_id = {r["id"]: r["kind"] for r in explicit + taskfact}
    steps = [s for s in (model_trace.get("steps") or []) if isinstance(s, dict)]
    valid_steps = {s.get("step") for s in steps if isinstance(s.get("step"), int)}
    progress_steps = {p["step"] for p in progress}
    terminal_step = ((runtime_boundary or {}).get("terminal_step")
                     if isinstance(runtime_boundary, dict) else None)

    # decision
    decision = obj.get("decision")
    if not isinstance(decision, dict):
        errors.append("decision 不是 object")
        decision_step = None
    else:
        observed = decision.get("observed")
        if not isinstance(observed, bool):
            errors.append("decision.observed 不是 bool")
        if not isinstance(decision.get("requirements_resolved"), bool):
            errors.append("decision.requirements_resolved 不是 bool")
        kind = decision.get("kind")
        if kind not in DECISION_KINDS:
            errors.append(f"decision.kind 非法: {kind!r}")
        step = decision.get("step")
        if observed:
            decision_step = step
            if kind not in OBSERVED_KINDS:
                errors.append(f"observed=true 时 kind 不能是 {kind!r}")
            if not isinstance(step, int) or isinstance(step, bool):
                errors.append("observed=true 时 decision.step 必须是整数")
                decision_step = None
            elif step not in valid_steps:
                errors.append(f"decision.step {step} 不存在于轨迹")
                decision_step = None
            elif (isinstance(terminal_step, int)
                  and not isinstance(terminal_step, bool)
                  and step > terminal_step):
                errors.append(
                    f"decision.step {step} 晚于第一终局 {terminal_step}")
                decision_step = None
        else:
            decision_step = None
            if kind != "unresolved":
                errors.append(f"observed=false 时 kind 必须是 unresolved，得到 {kind!r}")
            if step is not None:
                errors.append(f"observed=false 时 step 应为 null，得到 {step!r}")

    # requirement_interpretation：每条 requirement 恰好出现一次
    interp = obj.get("requirement_interpretation")
    if not isinstance(interp, list):
        errors.append("requirement_interpretation 不是 list")
        interp = []
    seen_interp = set()
    for i, it in enumerate(interp):
        where = f"requirement_interpretation[{i}]"
        if not isinstance(it, dict):
            errors.append(f"{where} 不是 object")
            continue
        rid = it.get("requirement_id")
        if rid not in expected:
            errors.append(f"{where}: requirement_id 不在 rubric 中 {rid!r}")
            continue
        if rid in seen_interp:
            errors.append(f"{where}: requirement_id 重复 {rid}")
            continue
        seen_interp.add(rid)
        kind = it.get("requirement_kind")
        if kind != kind_by_id.get(rid):
            errors.append(f"{where}: requirement_kind 与 rubric 不一致")
        status = it.get("effective_status")
        if status not in EFFECTIVE_STATUSES:
            errors.append(f"{where}: effective_status 非法 {status!r}")
            continue
        basis = it.get("basis") or []
        errors.extend(_validate_evidence_refs(
            basis, valid_steps, decision_step, progress_steps, terminal_step))
        # 激活/修改/拒绝/撤销的证据来源规则。
        if kind == "taskfact" and status in ("active", "modified", "rejected", "revoked") \
                and not _has_shopper_reply_basis(basis):
            errors.append(
                f"{where}: taskfact {status} 必须引用真实 shopper 回复"
                f"（event=shopper_reply/ask_shopper_reply）")
        if kind == "explicit" and status in ("modified", "rejected", "revoked") \
                and not _has_shopper_reply_basis(basis):
            errors.append(f"{where}: explicit {status} 必须引用真实 shopper 回复")
        if not str(it.get("reasoning") or "").strip():
            errors.append(f"{where}: reasoning 为空")
    missing = expected - seen_interp
    if missing:
        errors.append(f"requirement_interpretation 缺少: {sorted(missing)}")

    # rubric_verdicts：每条 requirement 恰好出现一次
    verdicts = obj.get("rubric_verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        errors.append("rubric_verdicts 不是非空 list")
        verdicts = []
    seen_verdict = set()
    for i, v in enumerate(verdicts):
        where = f"rubric_verdicts[{i}]"
        if not isinstance(v, dict):
            errors.append(f"{where} 不是 object")
            continue
        rid = v.get("requirement_id")
        if rid not in expected:
            errors.append(f"{where}: requirement_id 不在 rubric 中 {rid!r}")
            continue
        if rid in seen_verdict:
            errors.append(f"{where}: requirement_id 重复 {rid}")
            continue
        seen_verdict.add(rid)
        kind = v.get("requirement_kind")
        if kind != kind_by_id.get(rid):
            errors.append(f"{where}: requirement_kind 与 rubric 不一致")
        status = v.get("effective_status")
        if status not in EFFECTIVE_STATUSES:
            errors.append(f"{where}: effective_status 非法 {status!r}")
            continue
        uv = v.get("user_requirement_verdict")
        if uv not in USER_VERDICTS:
            errors.append(f"{where}: user_requirement_verdict 非法 {uv!r}")
            continue
        es = v.get("evidence_status")
        if es not in EVIDENCE_STATUSES:
            errors.append(f"{where}: evidence_status 非法 {es!r}")
            continue
        evidence = v.get("evidence") or []
        errors.extend(_validate_evidence_refs(
            evidence, valid_steps, decision_step, progress_steps, terminal_step))
        # 硬规则：latent 不能判 satisfied/violated。
        if status == "latent" and uv in ("satisfied", "violated"):
            errors.append(f"{where}: latent 时 user_requirement_verdict 不能是 {uv}")
        # satisfied/violated 必须有 model_trace 页面证据。
        if uv in ("satisfied", "violated") and not _has_model_trace_evidence(evidence):
            errors.append(f"{where}: {uv} 必须给出至少一个 model_trace 证据")
        if not str(v.get("reasoning") or "").strip():
            errors.append(f"{where}: reasoning 为空")
    missing = expected - seen_verdict
    if missing:
        errors.append(f"rubric_verdicts 缺少: {sorted(missing)}")

    # 一致性：两个视图的 effective_status 必须一致。
    interp_status = {it.get("requirement_id"): it.get("effective_status")
                     for it in interp if isinstance(it, dict)}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        rid = v.get("requirement_id")
        if rid in interp_status and v.get("effective_status") != interp_status[rid]:
            errors.append(
                f"rubric_verdicts[{rid}].effective_status 与 "
                f"requirement_interpretation 不一致")

    # requirements_resolved 必须与决策类型和有效需求 verdict 一致。
    if isinstance(decision, dict) and isinstance(
            decision.get("requirements_resolved"), bool):
        kind = decision.get("kind")
        resolved = decision["requirements_resolved"]
        if resolved and kind in ("unresolved", "environment_terminal"):
            errors.append(
                f"decision.kind={kind} 时 requirements_resolved 必须为 false")
        if resolved and kind == "purchase":
            unresolved_effective = [
                v.get("requirement_id") for v in verdicts
                if isinstance(v, dict)
                and v.get("effective_status") in ("active", "modified")
                and v.get("user_requirement_verdict") != "satisfied"
            ]
            if unresolved_effective:
                errors.append(
                    "purchase 存在未 satisfied 的有效需求，"
                    "requirements_resolved 必须为 false: "
                    + ", ".join(str(rid) for rid in unresolved_effective))

    # dimension_scores
    dims = obj.get("dimension_scores")
    if not isinstance(dims, dict):
        errors.append("dimension_scores 不是 dict")
    else:
        for k in DIMENSION_KEYS:
            val = dims.get(k)
            if not isinstance(val, int) or isinstance(val, bool) or val not in (0, 1, 2):
                errors.append(f"dimension_scores.{k} 缺失或不在 0/1/2: {val!r}")

    return errors


# --------------------------------------------------------------------------- #
# mock judge（确定性，仅链路验证；显式标注，不得冒充真实模型）
# --------------------------------------------------------------------------- #

def _infer_decision(model_trace: dict, boundary: dict) -> dict:
    steps = [s for s in (model_trace.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        return {"observed": False, "step": None, "kind": "unresolved",
                "requirements_resolved": False}
    last = steps[-1]
    tool = last.get("tool_name")
    value = str((last.get("tool_args") or {}).get("value") or "")
    if tool == "finish":
        return {"observed": True, "step": last.get("step"), "kind": "finish",
                "requirements_resolved": False}
    if tool == "click" and value.strip().casefold() == "buy now":
        return {"observed": True, "step": last.get("step"), "kind": "purchase",
                "requirements_resolved": False}
    if (boundary.get("first_terminal_observed")
            and last.get("step") == boundary.get("terminal_step")):
        return {"observed": True, "step": last.get("step"),
                "kind": "environment_terminal", "requirements_resolved": False}
    return {"observed": False, "step": None, "kind": "unresolved",
            "requirements_resolved": False}


def mock_judge(rubric: dict, model_trace: dict, raw_trace: dict) -> dict:
    """确定性占位 Judge：输出合法 JSON，用于链路验证，不产生语义结论。"""
    explicit, taskfact = flatten_requirements(rubric)
    boundary = extract_runtime_boundary(raw_trace)
    visible_trace, _visible_progress = project_to_runtime_boundary(
        model_trace, raw_trace)
    shopper_replies = extract_shopper_replies(visible_trace)

    interpretation = []
    for r in explicit:
        interpretation.append({
            "requirement_id": r["id"],
            "requirement_kind": "explicit",
            "effective_status": "active",
            "basis": [{"source": "rubric", "field": "query_quote",
                       "rubric_id": r["id"]}],
            "reasoning": "mock：初始 Query 明确要求，证据来自冻结 Rubric（链路验证占位）",
        })
    for r in taskfact:
        # TaskFacts 候选：mock 不做语义映射，一律 latent，绝不默认 active。
        interpretation.append({
            "requirement_id": r["id"],
            "requirement_kind": "taskfact",
            "effective_status": "latent",
            "basis": [],
            "reasoning": "mock：TaskFacts 候选未做语义确认（链路验证占位）",
        })

    verdicts = []
    for r in explicit:
        verdicts.append({
            "requirement_id": r["id"],
            "requirement_kind": "explicit",
            "effective_status": "active",
            "user_requirement_verdict": "unknown",
            "evidence_status": "unknown",
            "evidence": [],
            "reasoning": "mock：缺证据判 unknown（链路验证占位，非真实模型判定）",
        })
    for r in taskfact:
        verdicts.append({
            "requirement_id": r["id"],
            "requirement_kind": "taskfact",
            "effective_status": "latent",
            "user_requirement_verdict": "not_applicable",
            "evidence_status": "unknown",
            "evidence": [],
            "reasoning": "mock：latent 候选不作为本次用户需求（链路验证占位）",
        })

    steps = [s for s in (visible_trace.get("steps") or []) if isinstance(s, dict)]
    search_count = sum(1 for s in steps if s.get("tool_name") == "search")
    click_count = sum(1 for s in steps if s.get("tool_name") == "click")
    subpage = sum(
        1 for s in steps
        if s.get("tool_name") == "click"
        and str((s.get("tool_args") or {}).get("value") or "").strip().casefold()
        in ("description", "features", "reviews", "attributes")
    )
    ask_count = len(shopper_replies)
    decision = _infer_decision(visible_trace, boundary)
    post_terminal = boundary.get("post_terminal_steps_present", False)

    scores = {
        "clarification_strategy": 2 if 0 < ask_count <= 3 else (1 if ask_count == 0 else 0),
        "information_retention": 1 if ask_count else 2,
        "search_strategy": 2 if search_count >= 2 else (1 if search_count == 1 else 0),
        "candidate_utilization": 2 if click_count >= 2 else (1 if click_count == 1 else 0),
        "evidence_verification": 2 if subpage >= 1 else 1,
        "decision_quality": 1,
        "termination_efficiency": 0 if post_terminal else (2 if decision["observed"] else 1),
    }

    return {
        "decision": decision,
        "requirement_interpretation": interpretation,
        "rubric_verdicts": verdicts,
        "dimension_scores": scores,
    }


# --------------------------------------------------------------------------- #
# 单任务 Judge（LLM）
# --------------------------------------------------------------------------- #

def judge_task_llm(api_key, base_url, model, rubric, model_trace, raw_trace,
                   budget, timeout, max_attempts=2) -> tuple[dict | None, str | None, int]:
    """调用 LLM 并校验。非法输出最多重试一次（max_attempts 默认 2）。"""
    input_payload = build_input(rubric, model_trace, raw_trace, budget)
    boundary = extract_runtime_boundary(raw_trace)
    visible_trace, visible_progress = project_to_runtime_boundary(
        model_trace, raw_trace)
    user_content = json.dumps(input_payload, ensure_ascii=False)

    feedback = None
    last_err = None
    for attempt in range(max_attempts):
        content_input = user_content
        if feedback:
            content_input += ("\n\n上一次输出未通过校验，请修正后重新输出完整 JSON：\n"
                              + "\n".join(feedback))
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": content_input},
        ]
        try:
            content = call_llm(api_key, base_url, model, messages,
                               include_response_format=(attempt == 0),
                               timeout=timeout)
            if any(m in content for m in REFUSAL_MARKERS):
                return None, f"内容审核拒答: {content[:80]}", attempt + 1
            obj = _parse_json_content(content)
            errors = validate_output(
                obj, rubric, visible_trace, visible_progress, boundary)
            if not errors:
                return obj, None, attempt + 1
            feedback = errors
            last_err = "；".join(errors[:6])
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            feedback = None
            if attempt < max_attempts - 1:
                time.sleep(2 * (attempt + 1))
    return None, last_err, max_attempts


# --------------------------------------------------------------------------- #
# 结果状态判定 / 汇总
# --------------------------------------------------------------------------- #

def result_state(path: Path, rubric: dict, model_trace: dict, progress: list[dict],
                 runtime_boundary: dict | None = None,
                 raw_trace: dict | None = None) -> str:
    """返回 'done' / 'failed' / 'corrupt' / 'missing'。"""
    if not path.is_file():
        return "missing"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "corrupt"
    if not isinstance(obj, dict):
        return "corrupt"
    if obj.get("judge_failed") is True:
        return "failed"
    validation_trace = model_trace
    validation_progress = progress
    if isinstance(raw_trace, dict):
        validation_trace, validation_progress = project_to_runtime_boundary(
            model_trace, raw_trace)
    if (isinstance(runtime_boundary, dict)
            and runtime_boundary.get("post_terminal_steps_present") is True
            and (obj.get("_metadata") or {}).get("terminal_protocol")
            != TERMINAL_PROTOCOL):
        return "corrupt"
    if not validate_output(
            obj, rubric, validation_trace, validation_progress,
            runtime_boundary):
        return "done"
    return "corrupt"


def build_summary(results: list[tuple[str, dict]]) -> dict:
    """results 为 [(task_id, judgment)]。统计只针对本次传入的集合。"""
    total = len(results)
    verdict = {
        "explicit": Counter({s: 0 for s in USER_VERDICTS}),
        "taskfact_active": Counter({s: 0 for s in USER_VERDICTS}),
    }
    latent_count = 0
    latent_evidence = Counter({"supported": 0, "contradicted": 0, "unknown": 0})
    taskfact_inactive = Counter({"rejected": 0, "revoked": 0})
    taskfact_unknown_count = 0
    effective_counter = Counter()
    kind_counter = Counter()
    dim_sums = {k: 0 for k in DIMENSION_KEYS}
    observed_count = 0
    requirements_resolved_count = 0
    for _tid, r in results:
        d = r.get("decision") or {}
        if d.get("observed"):
            observed_count += 1
            kind_counter[d.get("kind")] += 1
        else:
            kind_counter["unresolved"] += 1
        if d.get("requirements_resolved"):
            requirements_resolved_count += 1
        for it in r.get("requirement_interpretation") or []:
            effective_counter[it.get("effective_status")] += 1
        for v in r.get("rubric_verdicts") or []:
            status = v.get("effective_status")
            kind = v.get("requirement_kind")
            uv = v.get("user_requirement_verdict")
            es = v.get("evidence_status")
            if kind == "explicit":
                verdict["explicit"][uv] += 1
            elif status in ("active", "modified"):
                verdict["taskfact_active"][uv] += 1
            elif status == "latent":
                latent_count += 1
                latent_evidence[es] += 1
            elif status in ("rejected", "revoked"):
                taskfact_inactive[status] += 1
            else:
                taskfact_unknown_count += 1
        for k in DIMENSION_KEYS:
            dim_sums[k] += int((r.get("dimension_scores") or {}).get(k) or 0)
    return {
        "schema": SCHEMA_NOTE,
        "judge_version": JUDGE_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "task_count": total,
        "decision": {
            "observed": observed_count,
            "unresolved": total - observed_count,
            "requirements_resolved": requirements_resolved_count,
            "kinds": dict(sorted(kind_counter.items())),
        },
        "verdict_distribution": {
            "explicit": dict(sorted(verdict["explicit"].items())),
            "taskfact_active": dict(sorted(verdict["taskfact_active"].items())),
            "taskfact_latent": {
                "count": latent_count,
                "evidence_supported": latent_evidence["supported"],
                "evidence_contradicted": latent_evidence["contradicted"],
                "evidence_unknown": latent_evidence["unknown"],
            },
            "taskfact_inactive": {
                "count": sum(taskfact_inactive.values()),
                "rejected": taskfact_inactive["rejected"],
                "revoked": taskfact_inactive["revoked"],
            },
            "taskfact_unknown": {"count": taskfact_unknown_count},
        },
        "effective_status_distribution": dict(sorted(effective_counter.items())),
        "dimension_score_means": {
            k: (dim_sums[k] / total if total else None) for k in DIMENSION_KEYS
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# CLI 主流程
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线 LLM Trajectory Judge v3")
    ap.add_argument("--input", default="evaluations/h0",
                    help="输入 run 目录（含 rubrics/ 与 traces/）")
    ap.add_argument("--out", required=True, help="judgment 输出目录")
    ap.add_argument("--only", default=None, help="逗号分隔 task_id")
    ap.add_argument("--max-tasks", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--evidence-budget", type=int, default=DEFAULT_EVIDENCE_BUDGET)
    ap.add_argument("--env", default=str(REPO_ROOT / ".env"), help=".env 路径")
    ap.add_argument("--judge-mode", default="llm", choices=["llm", "mock"],
                    help="mock：确定性链路验证模式，不调 LLM，输出显式标注")
    ap.add_argument("--resume", action="store_true",
                    help="跳过已存在且校验通过的结果（默认行为）")
    ap.add_argument("--retry-failed", action="store_true",
                    help="重新请求 judge_failed 的任务")
    ap.add_argument("--force", action="store_true",
                    help="强制重跑已存在的成功/失败结果；建议与 --only 配合")
    args = ap.parse_args(argv)

    input_dir = Path(args.input)
    rubrics_dir = input_dir / "rubrics"
    traces_dir = input_dir / "traces"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = load_env(args.env)
    api_key, base_url, model = resolve_model_config(env)
    if args.judge_mode == "llm" and not api_key:
        print("缺少 JUDGE_API_KEY / DEEPSEEK_API_KEY（检查 .env）", file=sys.stderr)
        return 1

    # 收集任务集合：rubric 为准，缺 trace 单独记录。
    rubric_ids = sorted(
        (f.stem for f in rubrics_dir.glob("*.json") if f.name != "manifest.json"),
        key=lambda t: int(t) if t.isdigit() else 0,
    )
    if args.only:
        only = [t.strip() for t in args.only.split(",") if t.strip()]
        unknown = [t for t in only if t not in rubric_ids]
        if unknown:
            print(f"[警告] --only 含不在 rubrics/ 的 task_id: {unknown}", file=sys.stderr)
        task_ids = [t for t in only if t in rubric_ids]
    else:
        task_ids = rubric_ids
    if args.max_tasks is not None:
        task_ids = task_ids[: args.max_tasks]

    requested = [int(t) for t in task_ids]
    print(f"任务集合 {len(task_ids)} 个，mode={args.judge_mode}，model={model}")

    succeeded, failed, skipped = [], [], []

    def run(tid: str):
        rubric_path = rubrics_dir / f"{tid}.json"
        trace_path = traces_dir / f"{tid}.model_trace.json"
        raw_path = traces_dir / f"{tid}.raw_trace.json"
        if not rubric_path.is_file():
            return tid, None, "rubric 缺失", 0, None
        if not trace_path.is_file():
            return tid, None, "model_trace 缺失", 0, None
        if not raw_path.is_file():
            return tid, None, "raw_trace 缺失", 0, None
        try:
            rubric = read_json(rubric_path)
            model_trace = read_json(trace_path)
            raw_trace = read_json(raw_path)
        except (OSError, json.JSONDecodeError) as exc:
            return tid, None, f"输入不可读: {exc}", 0, None

        progress = extract_progress(raw_trace)
        boundary = extract_runtime_boundary(raw_trace)
        visible_trace, visible_progress = project_to_runtime_boundary(
            model_trace, raw_trace)
        out_path = out_dir / f"{tid}.json"

        state = result_state(
            out_path, rubric, model_trace, progress, boundary, raw_trace)
        if state == "done" and not args.force:
            return tid, read_json(out_path), None, 0, "skipped"
        if state == "failed" and not (args.retry_failed or args.force):
            return tid, None, None, 0, "skipped"

        if args.judge_mode == "mock":
            judgment = mock_judge(rubric, model_trace, raw_trace)
            errors = validate_output(
                judgment, rubric, visible_trace, visible_progress, boundary)
            if errors:
                return tid, None, "; ".join(errors[:6]), 0, None
            model_id = MOCK_MODEL_ID
            attempts = 0
        else:
            judgment, err, attempts = judge_task_llm(
                api_key, base_url, model, rubric, model_trace, raw_trace,
                args.evidence_budget, args.timeout,
            )
            if err is not None:
                return tid, None, err, attempts, None
            model_id = model

        record = {
            "task_id": tid,
            "judge_version": JUDGE_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "runtime_boundary": boundary,
            "decision": judgment["decision"],
            "requirement_interpretation": judgment["requirement_interpretation"],
            "rubric_verdicts": judgment["rubric_verdicts"],
            "dimension_scores": judgment["dimension_scores"],
            "_metadata": {
                "judge_mode": args.judge_mode,
                "model": model_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempts,
                "trace_source": "model_trace + raw_trace(progress whitelist + boundary)",
                "terminal_protocol": TERMINAL_PROTOCOL,
            },
        }
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return tid, judgment, None, attempts, None

    all_results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(run, tid): tid for tid in task_ids}
        for fut in as_completed(futures):
            tid, judgment, err, attempts, skipped_flag = fut.result()
            if skipped_flag:
                skipped.append(tid)
                if judgment is not None:
                    all_results.append((tid, judgment))
                continue
            if err is not None:
                failed.append(tid)
                (out_dir / f"{tid}.json").write_text(
                    json.dumps({
                        "task_id": tid,
                        "judge_version": JUDGE_VERSION,
                        "judge_failed": True,
                        "reason": err,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"[失败] {tid}: {err}", file=sys.stderr)
                continue
            succeeded.append((tid, judgment))
            all_results.append((tid, judgment))
            n_verdict = len(judgment["rubric_verdicts"])
            print(f"[{tid}] 成功（尝试 {attempts} 次，verdicts {n_verdict}）")

    # 汇总：只描述本次请求的 task 集合与本次执行结果。
    summary = build_summary(all_results)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "judge_version": JUDGE_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "judge_mode": args.judge_mode,
        "model": MOCK_MODEL_ID if args.judge_mode == "mock" else model,
        "input_dir": str(input_dir),
        "requested_task_ids": requested,
        "task_count": len(requested),
        "succeeded": sorted(int(t) for t, _j in succeeded),
        "failed": sorted(int(t) for t in failed),
        "skipped": sorted(int(t) for t in skipped),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "mock 判定仅用于链路验证，不是真实模型结果。"
            if args.judge_mode == "mock" else None
        ),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "judge_mode": args.judge_mode,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "skipped": len(skipped),
    }, ensure_ascii=False))
    print(f"judgments -> {out_dir}")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
