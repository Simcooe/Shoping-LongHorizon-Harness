#!/usr/bin/env python3
"""LLM Trajectory Judge（shopping-final-v1）：只基于 model_trace + 冻结 Rubric 做过程评测。

迁移并升级自旧仓库 self-harness-dsh/eval/judge.py：

- Rubric schema 适配：新版 `constraints[].id / hardness / source / query_quote`
  （initial_query_only_v1），verdict 用 `rubric_id`；
- 七维度过程质量：clarification_strategy / information_retention / search_strategy /
  candidate_utilization / evidence_verification / decision_quality / termination_efficiency；
- 严格隔离：Judge 只读 `*.model_trace.json` 的 steps（tool_name / tool_args /
  model-visible observation），绝不读 terminal（含 reward / termination_reason）、
  raw_trace、source_goals.private.jsonl、gold ASIN、hidden TaskFacts；
- verdict 四态（satisfied / violated / unknown / not_applicable），必须覆盖全部
  Rubric 约束、引用真实存在的 model_trace step；证据不足优先 unknown，禁止猜测；
- 「Episode finished.」不携带结果信息；Agent 的最终自然语言声明不构成购买成功证据；
- JSON 解析 + 校验失败带反馈重试，重试后仍不完整则记 failed，不产出伪造 judgment。

用法:
  python3 eval/judge.py \
    --rubrics benchmarks/shopping-final-v1/rubrics \
    --traces runs/h0-0905-1446/traces \
    --out evaluations/shopping-final-v1/h0-0905-1446/judgments \
    --concurrency 4
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_ENV = REPO_ROOT / ".env"

DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_OBS_MAX_CHARS = 1800

RUBRIC_VERSION = "v1"

# 代理内容审核的拒答特征：出现这类文本说明触发了审核，应重试而不是当解析失败
REFUSAL_MARKERS = ("当前输入涉及敏感信息", "换个话题", "无法回答", "不能回答")

DIMENSION_KEYS = [
    "clarification_strategy",
    "information_retention",
    "search_strategy",
    "candidate_utilization",
    "evidence_verification",
    "decision_quality",
    "termination_efficiency",
]
FOUR_STATES = {"satisfied", "violated", "unknown", "not_applicable"}

JUDGE_SYSTEM_PROMPT = """# 任务：评判购物 Agent 的一条多轮执行轨迹（Rubric 四态 + 七维度过程质量）

你是购物 Agent 评测的 LLM Judge。场景是 Multi-Turn + Personalization 购物：用户的初始需求
不完整，Agent 可以调用 ask_shopper 向用户澄清，用户可能拒绝商品或修改需求。

你只能基于「Agent 实际可见的轨迹」判断：每一步的动作和 model-visible observation。
你拿不到、也不允许假设任何环境内部结果（是否真实购买成功、reward、终局原因）。

## 输入说明
- query：用户初始公开需求（rubric 的唯一约束来源）；
- visible_persona：Agent 初始可见的长期用户画像（如提供），仅供参考，不是当前硬约束；
- rubric：冻结约束清单，每条含 id / description / hardness / source / query_quote；
- trajectory：每步 `Step <n> [<tool_name>] <args>` + `Observation: <model-visible 文本>`。

## 角度一：逐条 rubric 判四态

对每一条约束输出：
- satisfied：model-visible observation 中有明确证据表明约束被满足（如详情页明确显示
  对应颜色/价格/规格，选中项明确匹配，或用户通过 ask_shopper 明确确认）；
- violated：model-visible observation 中有明确证据表明约束被违反（如价格明确超预算、
  明确选择了不符合要求的规格、用户明确拒绝后仍坚持该候选、最终决定明显不满足约束）；
- unknown：证据不足。Agent 没检查、observation 中没有该信息、轨迹停在未完成状态，
  都判 unknown。宁可 unknown，不要猜测；
- not_applicable：该约束确实不适用于当前轨迹（谨慎使用）。

关键规则：
1. 不能因为「没有证据」就判 violated；不能因为「看起来合理」或「标题像是符合」就判
   satisfied，必须有 observation 中的明确证据。
2. satisfied / violated 必须给出至少一个真实存在的 Step 序号作为证据（step_reference）。
   unknown / not_applicable 的 step_reference 通常为 []。
3. 「Episode finished.」「搜索功能是否可用: False」「可点击的按钮: []」只是环境终局
   提示，不代表购买成功或失败，不能作为任何约束的 satisfied / violated 证据。
4. Agent 的最终自然语言声明（如「已完成购买」「已找到合适商品」）不构成证据，
   必须看 observation 中是否有对应的页面/选项/用户确认证据。
5. rubric 只包含用户初始 Query 明确表达的要求。用户没有说过、Agent 也没有通过
   ask_shopper 问到的信息，不属于任何 rubric 约束，不得据此判 violated。
6. 若用户通过 ask_shopper 修改了需求或拒绝了商品，后续行为按新信息评估；
   用户明确拒绝的候选若被购买，可判相关约束 violated。
7. 必须覆盖 rubric 中的每一条约束，不遗漏、不重复。
8. 若 Agent 最终没有购买或确定任何商品（例如放弃、no_suitable_product、轨迹终止），
   且轨迹中没有「Agent 明确选择/确认了不满足约束的商品」的证据，该约束应判 unknown，
   不要判 violated。violated 要求 Agent 明确选择、确认或购买了不满足约束的商品，
   或在用户明确拒绝某候选后仍坚持该候选。

## 角度二：七个维度过程质量打分（各 0/1/2）

0 = 存在明显问题或基本没有完成；1 = 部分完成但有遗漏/低效/不稳定；2 = 整体合理。

1. clarification_strategy（澄清策略）：是否识别需求不完整并在需要时调用 ask_shopper；
   问题是否具体、一次聚焦一个核心信息；是否避免询问已知信息；是否没有把用户画像直接
   当成当前硬约束；澄清后是否继续执行。注意：若初始 Query 已足够明确，没有调用
   ask_shopper 不扣分。
2. information_retention（信息保持）：是否记住 ask_shopper 的回复并在后续搜索/候选/
   规格选择中使用；是否前后矛盾；用户修改需求后是否改用新需求；是否重复问已答问题。
3. search_strategy（搜索策略）：搜索词是否覆盖关键初始要求；是否根据结果调整；
   是否避免重复/过宽搜索；是否逐步收敛。
4. candidate_utilization（候选利用）：是否打开搜索结果中的候选；是否利用已有候选而不
   是反复重搜；是否比较多个候选；发现合适候选后是否有效推进。
5. evidence_verification（证据核验）：购买前是否查看商品页证据（类别/关键属性/规格/
   价格）；是否区分标题推测和页面真实证据。
6. decision_quality（决策质量）：最终购买/放弃是否符合用户明确要求；是否避免明显错误
   购买；规格选择是否合理；证据不足时是否避免自信声称完成；是否妥善处理用户拒绝/
   需求变更。若轨迹可见页面未变化、无 Buy Now 可点，Agent 却声称购买成功，判低分。
7. termination_efficiency（终止效率）：是否在合理时机结束；是否过早停止；是否反复执行
   无效动作或陷入循环；是否在环境终局后继续调用工具；是否错误声称购买成功。

## 输出格式（只输出 JSON，不要解释）

{
  "dimension_scores": {
    "clarification_strategy": 0,
    "information_retention": 0,
    "search_strategy": 0,
    "candidate_utilization": 0,
    "evidence_verification": 0,
    "decision_quality": 0,
    "termination_efficiency": 0
  },
  "rubric_verdicts": [
    {
      "rubric_id": "c0001",
      "status": "satisfied",
      "step_reference": [2, 6],
      "reasoning": "一句话说明证据所在的 Step 和内容"
    }
  ]
}

## 执行要求
- 只输出 JSON；rubric_verdicts 覆盖全部约束；status 只能四选一；
- dimension_scores 每项只能是 0 / 1 / 2；
- step_reference 中的序号必须真实存在于 trajectory 的 Step 编号；
- reasoning 非空且简短。"""


# --------------------------------------------------------------------------- #
# 通用工具（与 gen_rubric.py 保持一致）
# --------------------------------------------------------------------------- #

def load_env(path):
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


def resolve_model_config(env, prefix="JUDGE", default_model=DEFAULT_MODEL):
    api_key = env.get(f"{prefix}_API_KEY") or env.get("DEEPSEEK_API_KEY")
    base_url = (
        env.get(f"{prefix}_BASE_URL")
        or env.get("DEEPSEEK_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = env.get(f"{prefix}_MODEL") or default_model
    return api_key, base_url, model


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


def call_judge(api_key, base_url, model, messages, include_response_format):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0}
    if include_response_format:
        # 若代理不支持 response_format 会报错，重试时去掉
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
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read().decode("utf-8")
    obj = json.loads(body)
    return obj["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# 轨迹序列化（只用 model-visible steps；排除 terminal / reward）
# --------------------------------------------------------------------------- #

def _truncate_obs(obs, max_chars):
    """超长 observation 用 head + tail 截断：尾部保留按钮列表/价格等关键证据。"""
    if len(obs) <= max_chars:
        return obs
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return obs[:head] + "\n…[中段已截断]…\n" + obs[-tail:]


def serialize_trajectory(steps, public_query, obs_max_chars):
    parts = []
    for s in steps:
        tool_name = s.get("tool_name") or ""
        tool_args = s.get("tool_args") or {}
        if isinstance(tool_args, dict):
            args_str = " ".join(f'{k}="{v}"' for k, v in tool_args.items())
        else:
            args_str = str(tool_args)
        obs = s.get("observation") or ""
        # 每步 observation 都重复嵌入完整 Instruction，纯噪声；去掉。
        if public_query:
            obs = obs.replace(f"Instruction: [SEP] {public_query} [SEP] ", "")
        obs = _truncate_obs(obs, obs_max_chars)
        parts.append(f"Step {s.get('step')} [{tool_name}] {args_str}\nObservation: {obs}")
    return "\n\n".join(parts)


PERSONA_MARKER = "用户画像（长期偏好，仅供参考；用户当前明确表达优先于画像）：\n"


def split_agent_initial_input(trace):
    """把 trace['task'] 拆成 (公开 query, 可见画像 JSON 文本)。

    trace['task'] 是 Agent 初始可见输入（instruction_simple + 过滤后的长期画像），
    全部为 model-visible，允许给 Judge；但画像必须标注为非约束来源。
    """
    task = trace.get("task") or ""
    if PERSONA_MARKER in task:
        query_part, persona_part = task.split(PERSONA_MARKER, 1)
        return query_part.strip(), persona_part.strip()
    return task.strip(), ""


# --------------------------------------------------------------------------- #
# Judge 输出解析与严格校验
# --------------------------------------------------------------------------- #

def parse_and_validate(content, expected_ids, valid_steps):
    """解析 LLM 输出。返回 (judgment_or_None, errors[])。任何错误都触发重试。"""
    errors = []
    try:
        obj = _parse_json_content(content)
    except Exception as e:  # noqa: BLE001
        return None, [f"JSON 解析失败: {e}"]
    if not isinstance(obj, dict):
        return None, ["顶层不是 JSON 对象"]

    # 维度分
    dims = obj.get("dimension_scores")
    if not isinstance(dims, dict):
        errors.append("dimension_scores 不是 dict")
        dims = {}
    for k in DIMENSION_KEYS:
        v = dims.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v not in (0, 1, 2):
            errors.append(f"dimension_scores.{k} 缺失或不在 0/1/2: {v!r}")

    # verdicts
    verdicts = obj.get("rubric_verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        errors.append("rubric_verdicts 不是非空 list")
        return None, errors

    kept, seen = [], set()
    for i, v in enumerate(verdicts):
        where = f"rubric_verdicts[{i}]"
        if not isinstance(v, dict):
            errors.append(f"{where} 不是 object")
            continue
        rid = v.get("rubric_id")
        if rid not in expected_ids:
            errors.append(f"{where} rubric_id 不在 rubric 中: {rid!r}")
            continue
        if rid in seen:
            errors.append(f"{where} rubric_id 重复: {rid}")
            continue
        seen.add(rid)
        status = v.get("status")
        if status not in FOUR_STATES:
            errors.append(f"{where} status 非法: {status!r}")
            continue
        refs = v.get("step_reference")
        if not isinstance(refs, list):
            errors.append(f"{where} step_reference 不是 list")
            continue
        bad_refs = [r for r in refs if not isinstance(r, int) or r not in valid_steps]
        if bad_refs:
            errors.append(f"{where} step_reference 含不存在的 step: {bad_refs}")
            continue
        if status in ("satisfied", "violated") and not refs:
            errors.append(f"{where} {status} 必须给出至少一个证据 step")
            continue
        reasoning = (v.get("reasoning") or "").strip()
        if not reasoning:
            errors.append(f"{where} reasoning 为空")
            continue
        kept.append({
            "rubric_id": rid,
            "status": status,
            "step_reference": refs,
            "reasoning": reasoning,
        })

    missing = expected_ids - seen
    if missing:
        errors.append(f"缺少 verdict 的 rubric_id: {sorted(missing)}")

    if errors:
        return None, errors
    kept.sort(key=lambda v: v["rubric_id"])
    return {
        "rubric_verdicts": kept,
        "dimension_scores": {k: dims[k] for k in DIMENSION_KEYS},
    }, []


# --------------------------------------------------------------------------- #
# 单任务 Judge
# --------------------------------------------------------------------------- #

def build_user_input(rubric, trace, obs_max_chars):
    """组装给 Judge 的输入。只包含 model-visible 信息。"""
    steps = trace.get("steps") or []
    public_query = rubric.get("query") or ""
    task_query, persona = split_agent_initial_input(trace)
    if not public_query:
        public_query = task_query

    trajectory = serialize_trajectory(steps, public_query, obs_max_chars)

    payload = {
        "query": public_query,
        "rubric": {
            "rubric_version": RUBRIC_VERSION,
            "strategy": "initial_query_only_v1",
            "constraints": [
                {k: c.get(k) for k in ("id", "description", "hardness", "source", "query_quote")}
                for c in rubric.get("constraints") or []
            ],
        },
        "trajectory": trajectory,
        "existing_steps": [s.get("step") for s in steps],
        "notes": [
            "rubric 只包含用户初始 Query 明确表达的要求；hidden 需求未列入，不得据此判 violated。",
            "『Episode finished.』等终局提示不携带结果信息，不能作为证据。",
            "step_reference 只能引用 existing_steps 中列出的编号。",
        ],
    }
    if persona:
        payload["visible_persona"] = persona
        payload["notes"].append(
            "visible_persona 是长期画像，仅供参考，不是当前约束；不得作为判证据。")
    return payload


def judge_one(api_key, base_url, model, rubric, trace, obs_max_chars,
              max_attempts=3, refused_path=None):
    steps = trace.get("steps") or []
    expected_ids = {c.get("id") for c in rubric.get("constraints") or [] if c.get("id")}
    valid_steps = {s.get("step") for s in steps}
    user_content = json.dumps(build_user_input(rubric, trace, obs_max_chars),
                              ensure_ascii=False)

    feedback = None
    last_err = None
    for attempt in range(max_attempts):
        content_input = user_content
        if feedback:
            content_input += ("\n\n上一次输出未通过校验，问题如下，请修正后重新输出完整 JSON：\n"
                              + "\n".join(feedback if isinstance(feedback, list) else [feedback]))
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": content_input},
        ]
        try:
            # 第一次带 response_format，后续去掉（容错代理不支持）
            content = call_judge(api_key, base_url, model, messages,
                                 include_response_format=(attempt == 0))
            if any(m in content for m in REFUSAL_MARKERS):
                # 内容型拒答重试无效；把当次输入落盘方便定位触发词
                if refused_path:
                    Path(refused_path).write_text(user_content, encoding="utf-8")
                return None, f"内容审核拒答: {content[:80]}", attempt + 1
            judgment, errors = parse_and_validate(content, expected_ids, valid_steps)
            if judgment is not None:
                return judgment, None, attempt + 1
            feedback = errors
            last_err = "；".join(errors[:5])
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            feedback = None
            if attempt < max_attempts - 1:
                time.sleep(2 * (attempt + 1))
    return None, last_err, max_attempts


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def find_trace_ids(traces_dir):
    ids = []
    for f in Path(traces_dir).glob("*.model_trace.json"):
        stem = f.name.split(".model_trace.json")[0]
        if stem.isdigit():
            ids.append(stem)
    return sorted(ids, key=int)


def find_rubric_ids(rubrics_dir):
    ids = []
    for f in Path(rubrics_dir).glob("*.json"):
        if f.name == "manifest.json":
            continue
        if f.stem.isdigit():
            ids.append(f.stem)
    return sorted(ids, key=int)


def scan_frozen(out_dir):
    frozen = set()
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return frozen
    for f in out_dir.glob("*.json"):
        if f.name == "manifest.json":
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("frozen") is True and d.get("task_id") is not None:
            frozen.add(str(d["task_id"]))
    return frozen


def write_manifest(out_dir, total, succeeded, failed, skipped, missing_rubric,
                   missing_trace, model):
    manifest = {
        "benchmark_id": "shopping-final-v1",
        "rubric_version": RUBRIC_VERSION,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "missing_rubric": missing_rubric,
        "missing_trace": missing_trace,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "model_trace_only": True,
            "raw_reward_excluded": True,
            "termination_reason_excluded": True,
            "gold_asin_excluded": True,
            "hidden_taskfacts_excluded": True,
            "frozen_query_grounded_rubric": True,
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser(description="LLM Trajectory Judge（model_trace only）")
    ap.add_argument("--rubrics", default="benchmarks/shopping-final-v1/rubrics")
    ap.add_argument("--traces", required=True, help="trace 目录（*.model_trace.json）")
    ap.add_argument("--out", required=True, help="judgment 输出目录")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tasks", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="覆盖已冻结的 judgment")
    ap.add_argument("--obs-max-chars", type=int, default=DEFAULT_OBS_MAX_CHARS,
                    help="每步 observation 截断长度（head+tail 保留证据）")
    ap.add_argument("--env", default=str(DEFAULT_ENV), help=".env 路径")
    # ── rubric v2 分支（时间线化判定）──
    ap.add_argument("--rubric-version", default="v1", choices=["v1", "v2"],
                    help="v2：读取动态 rubric 时间线（--rubrics-v2），"
                         "按决策时刻评判；v1 为历史口径。")
    ap.add_argument("--rubrics-v2", default=None, help="rubric v2 目录（--rubric-version v2 必需）")
    ap.add_argument("--judge-mode", default="llm", choices=["llm", "mock"],
                    help="mock：确定性链路验证模式，输出显式标注，不得冒充真实模型")
    ap.add_argument("--evidence-budget", type=int, default=120000,
                    help="v2 证据预算（字符），超预算按步压缩并标记 truncated")
    args = ap.parse_args()

    if args.rubric_version == "v2":
        if not args.rubrics_v2:
            print("--rubric-version v2 需要 --rubrics-v2", file=sys.stderr)
            return 2
        import eval.judge_v2 as judge_v2  # noqa: PLC0415
        return judge_v2.run(args)

    env = load_env(args.env)
    api_key, base_url, model = resolve_model_config(env)
    if not api_key:
        print("缺少 JUDGE_API_KEY / DEEPSEEK_API_KEY（检查 .env）", file=sys.stderr)
        sys.exit(1)

    rubrics_dir = Path(args.rubrics)
    traces_dir = Path(args.traces)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rubric_ids = find_rubric_ids(rubrics_dir)
    trace_ids = find_trace_ids(traces_dir)
    # 以 rubric 集合为评测基准；缺 trace 的单独记录
    target_ids = rubric_ids[:]
    missing_trace = sorted(set(rubric_ids) - set(trace_ids), key=int)
    missing_rubric = sorted(set(trace_ids) - set(rubric_ids), key=int)
    if args.max_tasks is not None:
        target_ids = target_ids[: args.max_tasks]

    frozen_ids = scan_frozen(out_dir)
    skipped, to_judge = [], []
    for tid in target_ids:
        if tid in missing_trace:
            continue
        if not args.force and tid in frozen_ids:
            skipped.append(tid)
        else:
            to_judge.append(tid)

    print(f"rubric 总数 {len(rubric_ids)}，trace 总数 {len(trace_ids)}，"
          f"缺 trace {len(missing_trace)}，缺 rubric {len(missing_rubric)}，"
          f"跳过(已冻结) {len(skipped)}，待判 {len(to_judge)}，model={model}")

    succeeded, failed = [], []

    def run(tid):
        rubric = json.loads((rubrics_dir / f"{tid}.json").read_text(encoding="utf-8"))
        trace = json.loads((traces_dir / f"{tid}.model_trace.json").read_text(encoding="utf-8"))
        refused_path = out_dir / f".refused-{tid}.json"
        judgment, err, attempts = judge_one(
            api_key, base_url, model, rubric, trace,
            obs_max_chars=args.obs_max_chars, refused_path=str(refused_path))
        return tid, rubric, judgment, err, attempts

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(run, tid): tid for tid in to_judge}
        for fut in as_completed(futures):
            tid, rubric, judgment, err, attempts = fut.result()
            if err is not None:
                failed.append(tid)
                print(f"[失败] {tid}: {err}", file=sys.stderr)
                continue
            rec = {
                "task_id": tid,
                "rubric_version": RUBRIC_VERSION,
                "frozen": True,
                "query": rubric.get("query") or "",
                "judgment": judgment,
                "metadata": {
                    "model": model,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "trace_source": "model_trace_only",
                    "rubric_source": "frozen_query_grounded_v1",
                    "attempts": attempts,
                },
            }
            (out_dir / f"{tid}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            succeeded.append(tid)
            n_sat = sum(v["status"] == "satisfied" for v in judgment["rubric_verdicts"])
            print(f"[{tid}] 成功（尝试 {attempts} 次，约束 {len(judgment['rubric_verdicts'])} 条，"
                  f"satisfied {n_sat}）")

    succeeded_all = sorted(succeeded + skipped, key=int)
    write_manifest(out_dir, len(rubric_ids), succeeded_all, sorted(failed, key=int),
                   sorted(skipped, key=int), missing_rubric, missing_trace, model)
    print(f"\n完成：成功 {len(succeeded)}，失败 {len(failed)}，跳过 {len(skipped)}")
    print(f"manifest -> {out_dir / 'manifest.json'}")
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
