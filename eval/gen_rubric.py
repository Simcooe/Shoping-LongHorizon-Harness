#!/usr/bin/env python3
"""Rubric Builder（shopping-final-v1，v1）：基于冻结的 private source goals 生成并冻结 200 份
Query-grounded Rubric，供后续 LLM Trajectory Judge 使用。

迁移自旧仓库 self-harness-dsh/eval/gen_rubric.py，适配 Multi-Turn + Personalization 场景：

- 输入固定为 benchmarks/shopping-final-v1/source_goals.private.jsonl（不重新采样、不重新生成 goals）；
- task_id 沿用 benchmark（manifest.task_ids / tasks.jsonl），不新增不修改；
- v1 策略（initial-query-only）：constraints 只收录「用户初始公开 Query（instruction_simple）
  中逐字可引用的明确要求」。hidden goal 仅作为离线语义参考，绝不照抄；
- 不生成 shopper_clarification 约束（本阶段看不到真实 ask_shopper 对话），
  隐藏需求只在 metadata 里以计数形式记录（information_gap / clarification_required /
  hidden_candidate_constraint_count），后续 Judge 迁移时再扩展；
- Rubric 输出不含 gold ASIN / instruction_full 原文 / goal_options 原文 / reason_key /
  完整 user_persona / 未出现在 Query 中的 hidden TaskFacts；
- 生成并校验成功后置 frozen=true，H0/H1/H2 共用同一份，不随任何 Agent 轨迹修改。

用法（生成）:
  python3 eval/gen_rubric.py \
    --source-goals benchmarks/shopping-final-v1/source_goals.private.jsonl \
    --benchmark benchmarks/shopping-final-v1 \
    --out benchmarks/shopping-final-v1/rubrics \
    --concurrency 8

用法（离线校验，不调 LLM）:
  python3 eval/gen_rubric.py \
    --benchmark benchmarks/shopping-final-v1 \
    --out benchmarks/shopping-final-v1/rubrics \
    --verify
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# 项目根目录下的 .env 提供 RUBRIC_* / DEEPSEEK_* 配置
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = REPO_ROOT / ".env"

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"

RUBRIC_VERSION = "v1"
BENCHMARK_ID = "shopping-final-v1"

HARDNESS_VALUES = ("hard", "soft")
SOURCE_VALUES = ("initial_query", "visible_persona", "shopper_clarification")
# v1 策略：只生成 initial_query 约束
ALLOWED_GENERATED_SOURCES = ("initial_query",)

CONSTRAINT_ID_RE = re.compile(r"^c\d{4}$")

# 旧版字段名 → 新版字段名（兼容 LLM 输出的 candidate_id）
LLM_REQUIRED_FIELDS = ["description", "hardness", "query_quote", "selection_reason"]

SYSTEM_PROMPT = """# 任务：从「用户初始公开 Query」提炼多轮购物评测 rubric（v1，Query-grounded）

你是购物 Agent 评测的 rubric 生成器。场景是 Multi-Turn + Personalization 购物：
- Agent 一开始只能看到「初始公开 Query」（instruction_simple）和长期用户画像；
- 完整需求不完整，隐藏细节要靠 Agent 调用 ask_shopper 向用户澄清；
- 因此 rubric 只能评估「用户在初始 Query 里逐字明确表达的要求」。

输入给出两项：
1. query：用户初始公开 Query（唯一的约束来源）；
2. goal：目标商品的完整隐藏需求，仅作为语义背景帮你理解 Query 里的词（例如判断
   「圆框」是款式、「纯钛」是材质），绝对不可作为约束来源。

## 核心原则（必须遵守）

1. 只收录 Query 明确表达的要求；goal 里存在但 Query 没提的品牌、型号、规格、配件、
   属性、选项，一律不写（这是最重要的陷阱）。
2. 每条约束的 query_quote 必须是 Query 中的逐字原文片段（含标点），不得改写、扩写、
   合并不同句的词汇。
3. 不写入 asin、价格的具体成交价、goal_options 原文、instruction_full 原文、
   用户画像内容、任何「标准答案」式表述。
4. 预算表述按措辞判定：
   - 「不超过 / 以内 / 以下 / 预算上限」等明确上限 → hard；
   - 「左右 / 大约 / 大概」等模糊价位 → 写成「价格预期约 X 元」，判 soft；
   - 「价格在 A-B 之间」等明确区间 → hard。
5. 主观体验（「最好」「尽量」「希望」「更好看」「更软」「性价比高」）→ soft。
6. 明确的品类、品牌、型号、颜色、尺寸、规格、材质、产地、功能、适用人群、预算上限 → hard。
7. 同一要求不重复列出；不为凑数硬拆，也不遗漏 Query 明确要求。

## 输出格式（只输出 JSON，不要解释）

{
  "selected_constraints": [
    {
      "candidate_id": "c0001",
      "description": "自然语言描述的约束（一句话，不要出现 asin/标准答案措辞）",
      "hardness": "hard 或 soft",
      "query_quote": "Query 中的逐字原文片段",
      "selection_reason": "为什么保留这条、为什么判 hard/soft（简短）"
    }
  ]
}

## 示例

输入：
{"query": "想买一台300元以内的白色空气炸锅，最好能看到里面。", "goal": {"category": "…›空气炸锅", "attributes": ["5L", "1500W", "可视窗"], "expected_brand": ["某品牌"], "expected_model": [], "goal_options": ["5L 白色可视窗款"], "price_upper": 300}}

输出：
{
  "selected_constraints": [
    {"candidate_id": "c0001", "description": "商品品类为空气炸锅", "hardness": "hard", "query_quote": "空气炸锅", "selection_reason": "Query 明确提出了商品类型"},
    {"candidate_id": "c0002", "description": "颜色为白色", "hardness": "hard", "query_quote": "白色", "selection_reason": "Query 明确提出了颜色"},
    {"candidate_id": "c0003", "description": "价格不超过300元", "hardness": "hard", "query_quote": "300元以内", "selection_reason": "Query 明确给出预算上限"},
    {"candidate_id": "c0004", "description": "希望可以看到锅内食物状态（如可视窗设计）", "hardness": "soft", "query_quote": "最好能看到里面", "selection_reason": "「最好」开头的主观偏好，未指定具体实现方式，判 soft"}
  ]
}

注意：示例中 goal 的 5L、1500W、品牌、可视窗规格原文都没有写进 rubric，因为 Query 没有
逐字提出（「能看到里面」只作为 soft 偏好，不升级为「必须可视窗」）。"""


# --------------------------------------------------------------------------- #
# 通用工具
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


def resolve_model_config(env, prefix="RUBRIC", default_model=DEFAULT_MODEL):
    """<PREFIX>_* 为空时回退 DEEPSEEK_*。"""
    api_key = env.get(f"{prefix}_API_KEY") or env.get("DEEPSEEK_API_KEY")
    base_url = (
        env.get(f"{prefix}_BASE_URL")
        or env.get("DEEPSEEK_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = env.get(f"{prefix}_MODEL") or default_model
    return api_key, base_url, model


def _parse_json_content(content):
    """先直接 json.loads，失败则剥离首尾非 JSON 文本。"""
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


def call_llm(api_key, base_url, model, messages, include_response_format):
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
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read().decode("utf-8")
    obj = json.loads(body)
    return obj["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# Benchmark 输入加载与对齐校验
# --------------------------------------------------------------------------- #

def load_jsonl(path):
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_benchmark(benchmark_dir):
    """加载 manifest / tasks.jsonl，返回 (manifest, {task_id(str): task_row})。"""
    benchmark_dir = Path(benchmark_dir)
    manifest = json.loads((benchmark_dir / "manifest.json").read_text(encoding="utf-8"))
    tasks = {}
    for row in load_jsonl(benchmark_dir / "tasks.jsonl"):
        tasks[str(row["task_id"])] = row
    return manifest, tasks


def _leaf_strings(obj, out):
    """递归收集 JSON 中的所有字符串叶子值（用于泄漏指纹）。"""
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _leaf_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _leaf_strings(v, out)
    return out


class TaskContext:
    """单条任务的公开信息与隐藏泄漏指纹。"""

    def __init__(self, goal_row, public_query, manifest, task_row):
        self.tid = str(goal_row["task_id"])
        self.goal_row = goal_row
        self.public_query = public_query
        self.query_mode = manifest.get("query_mode", "initial_instruction_simple")
        meta = (task_row or {}).get("metadata") or {}
        self.has_information_gap = bool(meta.get("has_information_gap", True))

        goal = goal_row.get("goal") or {}
        self.asin = str(goal_row.get("asin") or goal.get("asin") or "")
        self.instruction_full = goal.get("instruction_full") or goal_row.get("instruction_full") or ""

        # hidden 候选约束（仅计数用，不写入 rubric）
        hidden_texts = []
        for key in ("attributes", "expected_core_functions", "expected_brand",
                    "expected_model", "goal_options"):
            for v in goal.get(key) or []:
                if isinstance(v, str):
                    hidden_texts.append(v)
        for opt in (goal.get("required_options_by_key") or {}).values():
            if isinstance(opt, dict) and isinstance(opt.get("value"), str):
                hidden_texts.append(opt["value"])
        self.hidden_candidate_constraint_count = len({
            t for t in hidden_texts if t and t not in public_query
        })

        # 泄漏指纹：hidden goal / persona 中出现、但公开 Query 中没有的字符串
        banned = []
        if self.asin:
            banned.append(self.asin)
        if self.instruction_full:
            banned.append(self.instruction_full)
        for t in hidden_texts:
            banned.append(t)
        persona = goal_row.get("user_persona") or goal.get("user_persona") or {}
        _leaf_strings(persona, banned)
        self.banned_tokens = sorted({
            t.strip() for t in banned
            if isinstance(t, str) and len(t.strip()) >= 2 and t not in public_query
        })

    def scan_leaks(self, text):
        """返回 rubric 文本中命中的泄漏片段（去重，最多报 5 个）。"""
        hits = []
        for tok in self.banned_tokens:
            if tok in text:
                hits.append(tok)
                if len(hits) >= 5:
                    break
        return hits


def load_and_check_inputs(source_goals_path, benchmark_dir):
    """加载 private goals 并与 benchmark 对齐校验；返回 (manifest, tasks, contexts)。"""
    manifest, tasks = load_benchmark(benchmark_dir)
    goals = load_jsonl(source_goals_path)

    errors = []
    if manifest.get("task_count") != len(manifest.get("task_ids", [])):
        errors.append("manifest.task_count 与 task_ids 长度不一致")
    goal_ids = [str(g["task_id"]) for g in goals]
    if len(goal_ids) != len(set(goal_ids)):
        errors.append("source_goals.private.jsonl 存在重复 task_id")
    if set(goal_ids) != set(tasks.keys()):
        errors.append("source goals task_id 集合与 tasks.jsonl 不一致")
    if set(goal_ids) != {str(t) for t in manifest.get("task_ids", [])}:
        errors.append("source goals task_id 集合与 manifest.task_ids 不一致")

    contexts = {}
    for g in goals:
        tid = str(g["task_id"])
        row = tasks.get(tid)
        public_query = (row or {}).get("query") or ""
        if row is not None and public_query != g.get("instruction_simple"):
            errors.append(f"task {tid}: tasks.jsonl query 与 instruction_simple 不一致")
        if not public_query:
            errors.append(f"task {tid}: 缺少公开 query")
        contexts[tid] = TaskContext(g, public_query, manifest, row)

    if errors:
        for e in errors[:20]:
            print(f"[输入校验失败] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[输入校验] source goals {len(goals)} 条，task_id 唯一，"
          f"与 manifest/tasks.jsonl 对齐，query == instruction_simple")
    return manifest, tasks, contexts


# --------------------------------------------------------------------------- #
# Rubric 校验（生成时与 --verify 共用）
# --------------------------------------------------------------------------- #

def validate_constraints(constraints, ctx):
    """对最终 constraints 列表做完整校验，返回 (errors, warnings)。"""
    errors, warnings = [], []
    if not isinstance(constraints, list) or not constraints:
        return ["constraints 不是非空 list"], warnings

    seen_ids, seen_desc = set(), set()
    for i, c in enumerate(constraints):
        where = f"constraints[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{where} 不是 object")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not CONSTRAINT_ID_RE.match(cid):
            errors.append(f"{where} id 非法: {cid!r}")
        elif cid in seen_ids:
            errors.append(f"{where} id 重复: {cid}")
        else:
            seen_ids.add(cid)
        if cid != f"c{len(seen_ids):04d}":
            warnings.append(f"{where} id 未按 c0001 递增")
        desc = (c.get("description") or "").strip()
        if not desc:
            errors.append(f"{where} description 为空")
        if c.get("hardness") not in HARDNESS_VALUES:
            errors.append(f"{where} hardness 非法: {c.get('hardness')!r}")
        if c.get("source") not in SOURCE_VALUES:
            errors.append(f"{where} source 非法: {c.get('source')!r}")
        quote = c.get("query_quote")
        if c.get("source") == "initial_query":
            if not isinstance(quote, str) or not quote.strip():
                errors.append(f"{where} initial_query 约束缺少 query_quote")
            elif quote not in ctx.public_query:
                errors.append(f"{where} query_quote 不在公开 Query 中逐字出现: {quote!r}")
        norm = re.sub(r"\s+", "", desc.lower())
        if norm in seen_desc:
            errors.append(f"{where} description 重复: {desc!r}")
        seen_desc.add(norm)

    text = json.dumps(constraints, ensure_ascii=False)
    if ctx.asin and ctx.asin in text:
        errors.append("constraints 中出现 gold ASIN")
    if ctx.instruction_full and ctx.instruction_full in text:
        errors.append("constraints 中出现 instruction_full 原文")
    for hit in ctx.scan_leaks(text):
        errors.append(f"constraints 泄漏 hidden/persona 信息: {hit!r}")
    return errors, warnings


def validate_rubric_file(rec, ctx, benchmark_tids):
    """校验单个落盘后的 rubric 记录（--verify 用）。"""
    errors = []
    if not isinstance(rec, dict):
        return ["顶层不是 object"]
    if str(rec.get("task_id")) != ctx.tid:
        errors.append(f"task_id 不匹配: {rec.get('task_id')!r} != {ctx.tid}")
    if rec.get("task_id") not in benchmark_tids and str(rec.get("task_id")) not in benchmark_tids:
        errors.append("task_id 不在 benchmark manifest.task_ids 中")
    if rec.get("frozen") is not True:
        errors.append("frozen != true")
    if rec.get("rubric_version") != RUBRIC_VERSION:
        errors.append(f"rubric_version != {RUBRIC_VERSION}")
    if rec.get("query_mode") != ctx.query_mode:
        errors.append(f"query_mode != {ctx.query_mode}")
    cons = rec.get("constraints")
    errs, _ = validate_constraints(cons, ctx)
    errors.extend(errs)
    # 整文件泄漏扫描（包含 metadata / generation_metadata）
    full = json.dumps(rec, ensure_ascii=False)
    if ctx.asin and ctx.asin in full:
        errors.append("rubric 文件中出现 gold ASIN")
    if ctx.instruction_full and ctx.instruction_full in full:
        errors.append("rubric 文件中出现 instruction_full 原文")
    if str(ctx.goal_row.get("reason_key")) not in ("None", "null", "") and \
            str(ctx.goal_row.get("reason_key")) in full:
        errors.append("rubric 文件中出现 reason_key")
    return errors


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

def build_user_message(ctx, feedback=None):
    goal = ctx.goal_row.get("goal") or {}
    user = json.dumps({"query": ctx.public_query, "goal": goal}, ensure_ascii=False)
    if feedback:
        user += "\n\n上一次输出未通过校验，问题如下，请修正后重新输出完整 JSON：\n" + feedback
    return user


def postprocess_llm_output(content, ctx):
    """解析 LLM 输出 → v1 constraints。返回 (constraints, feedback_or_None)。"""
    obj = _parse_json_content(content)
    if not isinstance(obj, dict):
        return None, "顶层不是 JSON 对象"
    cons = obj.get("selected_constraints")
    if not isinstance(cons, list) or not cons:
        return None, "selected_constraints 不是非空 list"

    kept, problems = [], []
    seen_desc = set()
    for i, c in enumerate(cons):
        where = f"selected_constraints[{i}]"
        if not isinstance(c, dict) or not all(k in c for k in LLM_REQUIRED_FIELDS):
            problems.append(f"{where} 缺少必要字段")
            continue
        if c.get("hardness") not in HARDNESS_VALUES:
            problems.append(f"{where} hardness 非法: {c.get('hardness')!r}")
            continue
        quote = c.get("query_quote")
        if not isinstance(quote, str) or quote not in ctx.public_query:
            problems.append(f"{where} query_quote 不在 Query 中逐字出现: {quote!r}")
            continue
        desc = str(c.get("description") or "").strip()
        if not desc:
            problems.append(f"{where} description 为空")
            continue
        norm = re.sub(r"\s+", "", desc.lower())
        if norm in seen_desc:
            problems.append(f"{where} description 重复，已丢弃")
            continue
        seen_desc.add(norm)
        kept.append({
            "id": f"c{len(kept) + 1:04d}",
            "description": desc,
            "hardness": c["hardness"],
            "source": "initial_query",
            "query_quote": quote,
            "selection_reason": str(c.get("selection_reason") or "").strip(),
        })

    if not kept:
        return None, "没有任何通过校验的约束。\n" + "\n".join(problems)

    text = json.dumps(kept, ensure_ascii=False)
    leaks = ctx.scan_leaks(text)
    if ctx.asin and ctx.asin in text:
        leaks.append(ctx.asin)
    if ctx.instruction_full and ctx.instruction_full in text:
        leaks.append("instruction_full 原文")
    if leaks:
        return None, ("输出泄漏了 Query 未提及的隐藏/画像信息（疑似照抄 goal）："
                      + "、".join(repr(t) for t in leaks)
                      + "。只保留 Query 逐字明确表达的要求。")

    feedback = "\n".join(problems) if problems else None
    return kept, feedback


def generate_one(api_key, base_url, model, ctx, max_attempts=3):
    feedback = None
    last_err = None
    for attempt in range(max_attempts):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(ctx, feedback)},
        ]
        try:
            # 第一次带 response_format，后续去掉（容错代理不支持）
            content = call_llm(api_key, base_url, model, messages,
                               include_response_format=(attempt == 0))
            kept, feedback = postprocess_llm_output(content, ctx)
            if kept is not None:
                errors, _ = validate_constraints(kept, ctx)
                if not errors:
                    return kept, None
                feedback = "\n".join(errors)
            last_err = feedback
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            feedback = None
    return None, last_err


def build_rubric_record(ctx, constraints, model):
    return {
        "task_id": ctx.tid,
        "rubric_version": RUBRIC_VERSION,
        "frozen": True,
        "query_mode": ctx.query_mode,
        "query": ctx.public_query,
        "constraints": constraints,
        "metadata": {
            "strategy": "initial_query_only_v1",
            "information_gap": ctx.has_information_gap,
            "clarification_required": ctx.has_information_gap,
            "initial_constraint_count": len(constraints),
            "hidden_candidate_constraint_count": ctx.hidden_candidate_constraint_count,
            "note": "hidden 候选约束仅为内部计数；Agent 未通过 ask_shopper 澄清前不得视为其已知约束。",
        },
        "generation_metadata": {
            "model": model,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_task_id": ctx.tid,
        },
    }


def write_manifest(out_dir, manifest_base, succeeded, failed, skipped, model):
    manifest = {
        "benchmark_id": manifest_base.get("benchmark_id", BENCHMARK_ID),
        "rubric_version": RUBRIC_VERSION,
        "frozen": True,
        "source": "source_goals.private.jsonl",
        "task_count": len(manifest_base.get("task_ids", [])),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "query_grounded": True,
            "gold_asin_excluded": True,
            "hidden_taskfacts_excluded": True,
            "multi_turn_personalization": True,
            "initial_query_only_first_version": True,
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


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


def cmd_generate(args):
    env = load_env(args.env)
    api_key, base_url, model = resolve_model_config(env)
    if not api_key:
        print("缺少 RUBRIC_API_KEY / DEEPSEEK_API_KEY（检查 .env）", file=sys.stderr)
        sys.exit(1)

    manifest, _, contexts = load_and_check_inputs(args.source_goals, args.benchmark)

    tids = [str(t) for t in manifest["task_ids"]]
    if args.max_tasks is not None:
        tids = tids[: args.max_tasks]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    frozen_ids = scan_frozen(out_dir)
    skipped, to_generate = [], []
    for tid in tids:
        if not args.force and tid in frozen_ids:
            skipped.append(tid)
        else:
            to_generate.append(tid)

    print(f"总任务 {len(tids)}，跳过(已冻结) {len(skipped)}，待生成 {len(to_generate)}，"
          f"model={model}")

    succeeded, failed = [], []

    def run(tid):
        constraints, err = generate_one(api_key, base_url, model, contexts[tid])
        return tid, constraints, err

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(run, tid): tid for tid in to_generate}
        for fut in as_completed(futures):
            tid, constraints, err = fut.result()
            if err is not None:
                failed.append(tid)
                print(f"[失败] {tid}: {err}", file=sys.stderr)
                continue
            rec = build_rubric_record(contexts[tid], constraints, model)
            errors = validate_rubric_file(rec, contexts[tid], set(tids))
            if errors:
                failed.append(tid)
                for e in errors:
                    print(f"[失败] {tid}: {e}", file=sys.stderr)
                continue
            (out_dir / f"{tid}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            succeeded.append(tid)
            print(f"[{tid}] 成功，约束 {len(constraints)} 条")

    succeeded_all = sorted(succeeded + skipped, key=lambda t: int(t))
    write_manifest(out_dir, manifest, succeeded_all, sorted(failed, key=lambda t: int(t)),
                   sorted(skipped, key=lambda t: int(t)), model)
    print(f"\n完成：成功 {len(succeeded)}，失败 {len(failed)}，跳过 {len(skipped)}（已冻结）")
    print(f"manifest -> {out_dir / 'manifest.json'}")
    if failed:
        sys.exit(2)


# --------------------------------------------------------------------------- #
# --verify：离线全量校验
# --------------------------------------------------------------------------- #

def cmd_verify(args):
    source_goals = args.source_goals or (Path(args.benchmark) / "source_goals.private.jsonl")
    manifest, _, contexts = load_and_check_inputs(source_goals, args.benchmark)
    benchmark_tids = {str(t) for t in manifest["task_ids"]}
    out_dir = Path(args.out)

    errors, warnings = [], []

    if len(contexts) != 200:
        errors.append(f"source goals 数量 {len(contexts)} != 200")

    # rubric 文件集合
    files = {f.stem: f for f in out_dir.glob("*.json") if f.name != "manifest.json"}
    if set(files.keys()) != benchmark_tids:
        missing = sorted(benchmark_tids - set(files.keys()), key=int)
        extra = sorted(set(files.keys()) - benchmark_tids, key=lambda t: int(t) if t.isdigit() else 0)
        errors.append(f"rubric 文件 task_id 集合与 benchmark 不一致，"
                      f"缺失 {len(missing)}，多余 {len(extra)}")

    hard_n = soft_n = cons_total = 0
    source_counter = {}
    not_frozen = []
    for tid in sorted(files, key=lambda t: int(t)):
        try:
            rec = json.loads(files[tid].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"{tid}: 文件不可读: {e}")
            continue
        errs = validate_rubric_file(rec, contexts[tid], benchmark_tids)
        errors.extend(f"{tid}: {e}" for e in errs)
        if rec.get("frozen") is not True:
            not_frozen.append(tid)
        for c in rec.get("constraints") or []:
            cons_total += 1
            hard_n += c.get("hardness") == "hard"
            soft_n += c.get("hardness") == "soft"
            source_counter[c.get("source")] = source_counter.get(c.get("source"), 0) + 1

    # manifest 校验
    try:
        rmanifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        rmanifest = None
        errors.append(f"rubrics/manifest.json 不可读: {e}")
    if rmanifest is not None:
        if len(rmanifest.get("succeeded", [])) != 200:
            errors.append(f"manifest.succeeded 数量 {len(rmanifest.get('succeeded', []))} != 200")
        if rmanifest.get("failed"):
            errors.append(f"manifest.failed 非空: {rmanifest['failed'][:5]}...")
        if {str(t) for t in rmanifest.get("succeeded", [])} != benchmark_tids:
            errors.append("manifest.succeeded 与 benchmark task_id 集合不一致")
        if rmanifest.get("frozen") is not True:
            errors.append("manifest.frozen != true")
        if rmanifest.get("rubric_version") != RUBRIC_VERSION:
            errors.append("manifest.rubric_version != v1")

    print(f"rubric 文件数: {len(files)} / 200")
    print(f"约束总数: {cons_total}（平均 {cons_total / max(len(files), 1):.2f} 条/任务）")
    print(f"hard / soft: {hard_n} / {soft_n}")
    print(f"source 分布: {source_counter}")
    if not_frozen:
        errors.append(f"frozen != true 的文件: {not_frozen[:5]}...")

    if errors:
        print(f"\n[FAIL] 共 {len(errors)} 个问题：", file=sys.stderr)
        for e in errors[:30]:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    for w in warnings:
        print(f"警告: {w}")
    print("\n[PASS] 200 份 Rubric 结构、对齐、引用、泄漏检查全部通过。")


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="生成/校验 shopping-final-v1 冻结 Rubric")
    ap.add_argument("--source-goals", help="source_goals.private.jsonl 路径（正式入口）")
    ap.add_argument("--goals", help="--source-goals 的旧接口别名")
    ap.add_argument("--benchmark", default="benchmarks/shopping-final-v1",
                    help="benchmark 目录（含 manifest.json / tasks.jsonl）")
    ap.add_argument("--out", default="benchmarks/shopping-final-v1/rubrics",
                    help="rubric 输出目录")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-tasks", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="覆盖已冻结的 rubric")
    ap.add_argument("--env", default=str(DEFAULT_ENV), help=".env 路径")
    ap.add_argument("--verify", action="store_true",
                    help="不调 LLM，对已生成 rubric 做离线全量校验")
    args = ap.parse_args()

    args.source_goals = args.source_goals or args.goals
    if args.verify:
        cmd_verify(args)
        return
    if not args.source_goals:
        print("生成模式需要 --source-goals（或旧接口 --goals）", file=sys.stderr)
        sys.exit(1)
    cmd_generate(args)


if __name__ == "__main__":
    main()
