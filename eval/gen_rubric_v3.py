#!/usr/bin/env python3
"""Frozen Rubric generator（Query + TaskFacts），按 docs/prompts/SHOP_RUBRIC_GENERATION_PLAN.md 实现。

数据流：
    benchmarks/shopping-final-v1/tasks.jsonl (公开 Query)
  + benchmarks/shopping-final-v1/source_goals.private.jsonl (TaskFacts)
    -> 代码提取候选字段（剔除 ASIN / reward / 内部元数据 / 完整 user_persona）
    -> LLM Rubric Curator（只负责选择、去重、归并、自然语言抽象、hardness 标注）
    -> 确定性校验（schema / query_quote / taskfacts_basis / 去重 / 泄漏）
    -> 冻结 Rubric

输出：
    evaluations/h0/rubrics/<task_id>.json
    evaluations/h0/rubrics/manifest.json
    evaluations/h0/rubric_audit/<task_id>.json   （不提供给 Judge）

本阶段禁止读取 model_trace / raw_trace / 旧 Judge 结果 / MEA runtime logs；
Rubric 与具体 Agent 轨迹无关，跨 h0/h1/mea-v1..v3 复用，生成后 frozen=true。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eval.gen_rubric import (  # noqa: E402  (复用既有工具，避免两套实现漂移)
    _leaf_strings,
    _parse_json_content,
    call_llm,
    load_benchmark,
    load_env,
    load_jsonl,
    resolve_model_config,
)

RUBRIC_VERSION = "shopping-rubric-v2"
GENERATOR_VERSION = "shopping-rubric-generator-v1"
BENCHMARK_ID = "shopping-final-v1"

HARDNESS_EXPLICIT = ("hard", "soft")
HARDNESS_TASKFACT = "reference"
STATUS_EXPLICIT = "explicit"
STATUS_TASKFACT = "taskfact"
SOURCE_INITIAL_QUERY = "initial_query"
SOURCE_QUERY_AND_TASKFACTS = "query_and_taskfacts"
SOURCE_TASKFACTS = "taskfacts"

# 合法 taskfacts_basis 字段路径（§4 字段映射）。
# 接受两种写法：Curator 输入候选名（taskfacts_candidates.<name>），
# 以及对应原始 TaskFacts 字段路径（goal.<name>）。
VALID_BASIS = {
    # Curator 候选字段名（传给 LLM 的精简候选）
    "category",
    "attributes",
    "core_functions",
    "options",
    "brand_candidates",
    "model_candidates",
    "price_upper",
    # 原始 TaskFacts 字段路径
    "goal.category",
    "goal.attributes",
    "goal.goal_options",
    "goal.expected_core_functions",
    "goal.required_options_by_key",
    "goal.expected_brand",
    "goal.expected_model",
    "goal.price_upper",
}

# 统一过程要求（§12：每个任务使用统一过程要求，不随 LLM 自由创造）
PROCESS_REQUIREMENTS = [
    {"id": "p0001", "description": "购买前应核验商品类别。"},
    {"id": "p0002", "description": "购买前应核验关键规格。"},
    {"id": "p0003", "description": "购买前应核验数量。"},
    {"id": "p0004", "description": "购买前应核验最终价格。"},
    {"id": "p0005", "description": "不能仅凭标题推断详情，应依据商品详情/属性证据。"},
    {"id": "p0006", "description": "信息不足时必要情况下应通过 shop_asker 澄清。"},
    {"id": "p0007", "description": "用户通过 shop_asker 明确修改要求后，后续行为应依据最新回复。"},
    {"id": "p0008", "description": "自然语言声明不能替代环境证据。"},
    {"id": "p0009", "description": "终局后不能继续行动。"},
]

# 环境/生成元数据标识，禁止以任何形式进入安全 Rubric
FORBIDDEN_LITERALS = (
    "source_product_index",
    "reason_key",
    "reward_feature_version",
    "option_axis_version",
    "feature_sources",
)

ID_RE = {
    "explicit": re.compile(r"^r\d{4}$"),
    "taskfact": re.compile(r"^t\d{4}$"),
    "process": re.compile(r"^p\d{4}$"),
}

SYSTEM_PROMPT = """你是 ShopSimulator 长程购物任务的 Rubric Curator。

输入包括：
1. 用户初始公开需求 query（用户明确表达要求的最高来源）；
2. 用户完整需求 instruction_full（完整需求参考，帮助理解 query 中词语的语义）；
3. 代码从 TaskFacts 提取的候选字段 taskfacts_candidates（完整任务目标，属于隐藏需求，
   Agent 开始时未必知道，但属于本任务的完整目标）。

请生成一份与具体 Agent 轨迹无关、生成后冻结、并由所有 Harness/Profile 复用的任务 Rubric。
你只输出两类需求（process_requirements 由生成器统一附加，无需你输出）：

1. explicit_requirements：用户在初始公开 Query 中「明确表达」的要求（商品类别、品牌、
   型号、颜色、材质、尺寸、数量、价格、功能、使用场景、适用人群等）。
   - 每条必须有 query_quote，且必须是 Query 中逐字出现的片段（含标点），不得改写、扩写。
   - source 取 "initial_query"；若该要求同时也在 TaskFacts 中有明确依据，取
     "query_and_taskfacts" 并在 taskfacts_basis 中列出对应字段路径。
2. taskfact_requirements：TaskFacts 中属于完整任务目标、但 Query 未必完整表达的要求
   （完整规格、包装规格、隐藏数量要求、目标功能、目标材质、目标使用场景、目标品牌或型号等）。
   - source 固定 "taskfacts"，status 固定 "taskfact"，hardness 固定 "reference"，
     query_quote 固定 null，taskfacts_basis 必须列出依据的字段路径。
   - 这些要求不代表 Agent 在任务开始时已经知道；不要把它们写成 Agent 已知约束。

必须遵守：
- Query 是用户显式表达要求的最高来源。
- TaskFacts 可以补充完整任务目标，但不能改变 Query 的含义。
- Query 和 TaskFacts 表达同一要求时只保留一条（统一放入 explicit_requirements，
  source 标为 "query_and_taskfacts"）。
- asin、reward、reason_key、source_product_index、feature_sources、option_axis_version
  等内部字段不能成为 requirement。
- user_persona 偏好不能自动变成 hard requirement；本输入不会传 user_persona，不得臆造画像。
- 不根据任何 Agent trajectory 生成 Rubric。
- 不为 requirements 预先决定是否可以被 shopper 修改；shopper 修改由后续 Judge 根据真实
  轨迹判断。
- 硬度判定：「不超过、以内、以下、上限、价格在 A-B 之间」通常是 hard；
  「左右、大约、大概、最好、尽量、希望」通常是 soft。
  - 价格上限 price_upper 是隐藏目标：若 Query 用「左右/大约」等模糊措辞，Query 价格应判 soft，
  不要把 price_upper 与 Query 模糊价格合并成 hard；price_upper 可单独作为
  taskfact_requirement（hardness=reference）记录。
- 同一语义要求不重复拆分；TaskFacts 中的重复信息应归并为少量语义清晰的 requirement，
  而不是机械罗列（例如 attributes 与 expected_core_functions 表达同一含义时只保留一条）。
- 不要自由创造 TaskFacts 中没有依据的具体值；description 用自然语言概括，不要原样照抄
  隐藏选项字符串，也不要出现「必须购买 ASIN xxx」这类表述。
- 只输出 JSON，不要解释。

## 输出格式（严格 JSON，无其他文本）

{
  "explicit_requirements": [
    {
      "id": "r0001",
      "description": "自然语言描述的用户明确要求",
      "source": "initial_query 或 query_and_taskfacts",
      "hardness": "hard 或 soft",
      "query_quote": "Query 中的逐字原文片段",
      "taskfacts_basis": ["goal.category"]  // 可选；source=query_and_taskfacts 时必填
    }
  ],
  "taskfact_requirements": [
    {
      "id": "t0001",
      "description": "自然语言概括的完整任务目标要求",
      "source": "taskfacts",
      "hardness": "reference",
      "query_quote": null,
      "taskfacts_basis": ["goal.goal_options", "goal.required_options_by_key"]
    }
  ]
}"""


# --------------------------------------------------------------------------- #
# 输入加载与候选字段提取
# --------------------------------------------------------------------------- #

class TaskContext:
    """单条任务的公开信息与隐藏泄漏指纹。"""

    def __init__(self, goal_row, public_query):
        self.tid = str(goal_row["task_id"])
        self.goal_row = goal_row
        self.public_query = public_query
        self.instruction_full = (
            goal_row.get("instruction_full")
            or (goal_row.get("goal") or {}).get("instruction_full")
            or ""
        )
        goal = goal_row.get("goal") or {}
        self.asin = str(goal_row.get("asin") or goal.get("asin") or "")
        self.category = goal.get("category") or ""
        self.attributes = [str(x) for x in (goal.get("attributes") or []) if x]
        self.core_functions = [
            str(x) for x in (goal.get("expected_core_functions") or []) if x
        ]
        self.goal_options = [str(x) for x in (goal.get("goal_options") or []) if x]
        self.brands = [str(x) for x in (goal.get("expected_brand") or []) if x]
        self.models = [str(x) for x in (goal.get("expected_model") or []) if x]
        self.price_upper = goal.get("price_upper")
        self.required_options = {}
        for k, v in (goal.get("required_options_by_key") or {}).items():
            if isinstance(v, dict):
                self.required_options[str(k)] = {
                    "value": str(v.get("value") or ""),
                    "axis": str(v.get("source_axis") or v.get("axis") or ""),
                }
            else:
                self.required_options[str(k)] = {"value": str(v), "axis": ""}
        self.source_product_index = goal_row.get("source_product_index")
        self.reason_key = goal_row.get("reason_key") or goal.get("reason_key")

    def candidates(self) -> dict:
        """§14：只把精简候选传给 Curator，剔除 ASIN / persona / 内部元数据。"""
        options = []
        for v in self.goal_options:
            options.append({"key": "option", "value": v, "axis": ""})
        for k, obj in self.required_options.items():
            options.append({"key": k, "value": obj["value"], "axis": obj["axis"]})
        return {
            "task_id": self.tid,
            "query": self.public_query,
            "instruction_full": self.instruction_full,
            "taskfacts_candidates": {
                "category": self.category,
                "attributes": self.attributes,
                "core_functions": self.core_functions,
                "options": options,
                "brand_candidates": self.brands,
                "model_candidates": self.models,
                "price_upper": self.price_upper,
            },
        }

    def leak_tokens(self) -> list[str]:
        """返回「禁止逐字出现在安全 Rubric」中的 token 列表。

        硬禁止：gold ASIN、完整 instruction_full 原文、内部元数据标识、以及只出现在
        user_persona 中（且不在 Query / TaskFacts 候选里）的画像字段。
        允许：Query 已出现的词，以及 TaskFacts 候选字段里的具体值（attributes /
        options / core_functions / brand / model / category）——这些是 Curator 可以
        用来做自然语言归并的合法依据，不属于泄漏。"""
        banned = []
        if self.asin and len(self.asin) >= 4:
            banned.append(self.asin)
        if self.instruction_full and len(self.instruction_full) >= 4:
            banned.append(self.instruction_full)

        allowed = {self.category}
        for src in (self.attributes, self.core_functions, self.goal_options,
                    self.brands, self.models):
            allowed.update(src)
        for obj in self.required_options.values():
            allowed.add(obj["value"])
        for v in list(self.required_options.values()):
            allowed.add(v["value"])

        persona = self.goal_row.get("user_persona") or (
            self.goal_row.get("goal") or {}
        ).get("user_persona") or {}
        persona_leaves = []
        _leaf_strings(persona, persona_leaves)
        for t in persona_leaves:
            t = str(t).strip()
            if len(t) < 4:
                continue
            if t in self.public_query or t in allowed:
                continue
            banned.append(t)
        return sorted(set(banned))

    def scan_leaks(self, text: str) -> list[str]:
        hits = []
        low = text.lower()
        for lit in FORBIDDEN_LITERALS:
            if lit in low:
                hits.append(f"内部元数据标识 {lit!r}")
        for tok in self.leak_tokens():
            if tok in text:
                hits.append(tok)
                if len(hits) >= 8:
                    break
        return hits


def load_and_check_inputs(source_goals_path, benchmark_dir):
    """加载并做 Query/TaskFacts 对齐校验；返回 (manifest, tasks, contexts)。"""
    manifest, tasks = load_benchmark(benchmark_dir)
    goals = load_jsonl(source_goals_path)

    errors = []
    goal_ids = [str(g["task_id"]) for g in goals]
    if len(goal_ids) != len(set(goal_ids)):
        errors.append("source_goals.private.jsonl 存在重复 task_id")
    if set(goal_ids) != {str(t) for t in manifest.get("task_ids", [])}:
        errors.append("source goals task_id 集合与 manifest.task_ids 不一致")

    contexts = {}
    for g in goals:
        tid = str(g["task_id"])
        row = tasks.get(tid)
        public_query = (row or {}).get("query") or ""
        if row is not None and public_query != g.get("instruction_simple"):
            errors.append(f"task {tid}: tasks.jsonl.query 与 instruction_simple 不一致")
        if not public_query:
            errors.append(f"task {tid}: 缺少公开 query")
        contexts[tid] = TaskContext(g, public_query)

    if errors:
        for e in errors[:20]:
            print(f"[输入校验失败] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[输入校验] source goals {len(goals)} 条，task_id 唯一，"
          f"与 manifest/tasks.jsonl 对齐，query == instruction_simple")
    return manifest, tasks, contexts


# --------------------------------------------------------------------------- #
# 确定性校验
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def validate_requirements(doc: dict, ctx: TaskContext):
    """对完整 Rubric 结构做确定性校验；返回 (errors, warnings)。"""
    errors, warnings = [], []

    if not isinstance(doc, dict):
        return ["顶层不是 object"], []
    if str(doc.get("task_id")) != ctx.tid:
        errors.append(f"task_id 不匹配: {doc.get('task_id')!r} != {ctx.tid}")
    if doc.get("rubric_version") != RUBRIC_VERSION:
        errors.append(f"rubric_version != {RUBRIC_VERSION}")
    if doc.get("frozen") is not True:
        errors.append("frozen != true")
    if (doc.get("query") or "") != ctx.public_query:
        errors.append("rubric.query 与公开 query 不一致")

    seen_ids = set()
    seen_desc = set()

    def check_common(item, where, kind):
        if not isinstance(item, dict):
            errors.append(f"{where} 不是 object")
            return
        iid = item.get("id")
        if not isinstance(iid, str) or not ID_RE[kind].match(iid):
            errors.append(f"{where} id 非法: {iid!r}")
        elif iid in seen_ids:
            errors.append(f"{where} id 重复: {iid}")
        else:
            seen_ids.add(iid)
        desc = (item.get("description") or "").strip()
        if not desc:
            errors.append(f"{where} description 为空")
        if _norm(desc) in seen_desc:
            errors.append(f"{where} description 重复: {desc!r}")
            warnings.append(f"{where} description 重复（已计入错误）: {desc!r}")
        seen_desc.add(_norm(desc))

    # explicit_requirements
    explicit = doc.get("explicit_requirements")
    if not isinstance(explicit, list) or not explicit:
        errors.append("explicit_requirements 不是非空 list")
    else:
        for i, item in enumerate(explicit):
            where = f"explicit_requirements[{i}]"
            check_common(item, where, "explicit")
            if item.get("status") != STATUS_EXPLICIT:
                errors.append(f"{where} status != explicit")
            if item.get("hardness") not in HARDNESS_EXPLICIT:
                errors.append(f"{where} hardness 非法: {item.get('hardness')!r}")
            src = item.get("source")
            if src not in (SOURCE_INITIAL_QUERY, SOURCE_QUERY_AND_TASKFACTS):
                errors.append(f"{where} source 非法: {src!r}")
            quote = item.get("query_quote")
            if not isinstance(quote, str) or not quote.strip():
                errors.append(f"{where} query_quote 为空")
            elif quote not in ctx.public_query:
                errors.append(f"{where} query_quote 不在公开 Query 中逐字出现: {quote!r}")
            # 价格硬度与合并规则（§8/§9）：模糊措辞不得标 hard；
            # 也不得把 Query 模糊价格与 TaskFacts price_upper 上限合并成 hard。
            desc_text = str(item.get("description") or "")
            fuzzy = any(m in (quote or "") for m in ("左右", "大约", "大概", "差不多", "上下"))
            hard_cap = any(m in desc_text for m in ("不超过", "以内", "以下", "上限", "不得高于"))
            if fuzzy and item.get("hardness") == "hard":
                errors.append(
                    f"{where} 含模糊措辞（左右/大约/大概等）却标 hard，应判 soft")
            if fuzzy and hard_cap:
                errors.append(
                    f"{where} 将 Query 模糊价格与 TaskFacts 上限合并成 hard；"
                    f"应分开：Query 价格标 soft，price_upper 作为 taskfact_requirement(reference)")
            basis = item.get("taskfacts_basis")
            if basis is not None and not isinstance(basis, list):
                errors.append(f"{where} taskfacts_basis 不是 list")
            elif src == SOURCE_QUERY_AND_TASKFACTS:
                if not basis:
                    errors.append(f"{where} query_and_taskfacts 缺少 taskfacts_basis")
                else:
                    for p in basis:
                        if p not in VALID_BASIS:
                            errors.append(f"{where} taskfacts_basis 字段路径非法: {p!r}")

    # taskfact_requirements
    taskfacts = doc.get("taskfact_requirements")
    if not isinstance(taskfacts, list):
        errors.append("taskfact_requirements 不是 list")
    else:
        for i, item in enumerate(taskfacts):
            where = f"taskfact_requirements[{i}]"
            check_common(item, where, "taskfact")
            if item.get("status") != STATUS_TASKFACT:
                errors.append(f"{where} status != taskfact")
            if item.get("hardness") != HARDNESS_TASKFACT:
                errors.append(f"{where} hardness != reference")
            if item.get("source") != SOURCE_TASKFACTS:
                errors.append(f"{where} source != taskfacts")
            if item.get("query_quote") is not None:
                errors.append(f"{where} query_quote 应为 null")
            basis = item.get("taskfacts_basis")
            if not isinstance(basis, list) or not basis:
                errors.append(f"{where} 缺少 taskfacts_basis")
            else:
                for p in basis:
                    if p not in VALID_BASIS:
                        errors.append(f"{where} taskfacts_basis 字段路径非法: {p!r}")

    # process_requirements
    process = doc.get("process_requirements")
    if not isinstance(process, list) or not process:
        errors.append("process_requirements 不是非空 list")
    else:
        for i, item in enumerate(process):
            where = f"process_requirements[{i}]"
            check_common(item, where, "process")
            if not (item.get("description") or "").strip():
                errors.append(f"{where} description 为空")

    # lifecycle_policy
    lp = doc.get("lifecycle_policy")
    if not isinstance(lp, dict) \
            or lp.get("shopper_changes_are_judge_time_interpretation") is not True \
            or lp.get("rubric_is_not_modified_by_trajectory") is not True:
        errors.append("lifecycle_policy 缺失或语义不正确")

    # 泄漏扫描（整文件）
    full = json.dumps(doc, ensure_ascii=False)
    for hit in ctx.scan_leaks(full):
        errors.append(f"泄漏 hidden/内部信息: {hit!r}")

    # 显式需求对 query 信号（价格/数量）的覆盖，仅警告不硬失败
    uncovered = uncovered_query_signals(ctx.public_query, explicit or [])
    for s in uncovered:
        warnings.append(f"query 信号未被 explicit_requirements 覆盖: {s}")

    return errors, warnings


def uncovered_query_signals(query: str, explicit: list) -> list[str]:
    """粗略检测 Query 中的价格/数量信号是否被 explicit_requirements 覆盖（只做提醒）。"""
    signals = []
    for m in re.finditer(r"\d+(?:\.\d+)?\s*(?:元|块钱|块)(?:\s*(?:左右|以内|上下|之间|以下))?", query):
        signals.append(m.group(0))
    if re.search(r"元之间", query):
        signals.append("元之间")
    for m in re.finditer(
        r"(?:[一二两三四五六七八九十百千万]+|\d+)\s*(?:根|支|个|只|件|盒|套|条|双|片|袋|瓶|本|台|颗|对|箱|张|把|米|升|克|斤|毫升|千克)",
        query,
    ):
        raw = m.group(0)
        # 单件量词（一+量词）通常由品类/规格隐含，不做硬性覆盖提醒。
        if re.match(r"^一[根支个只件盒套条双片袋瓶本台颗对箱张把]$", raw):
            continue
        signals.append(raw)

    uncovered = []
    for s in signals:
        clean = _norm(s)
        covered = any(
            clean in _norm(r.get("query_quote") or "") or clean in _norm(r.get("description") or "")
            for r in explicit
        )
        if not covered:
            uncovered.append(s)
    return uncovered


# --------------------------------------------------------------------------- #
# LLM 归并
# --------------------------------------------------------------------------- #

def build_user_message(ctx: TaskContext, feedback=None) -> str:
    user = json.dumps(ctx.candidates(), ensure_ascii=False)
    if feedback:
        user += "\n\n上一次输出未通过校验，问题如下，请修正后重新输出完整 JSON：\n" + feedback
    return user


def _normalize_requirements(explicit_raw, taskfact_raw):
    """把 LLM 输出归一化：status/source/hardness/query_quote 等确定性语义由代码填写，
    避免 LLM 漏写导致整条失败；LLM 只负责 description / hardness / query_quote /
    taskfacts_basis 这些语义判断。"""
    explicit, taskfacts = [], []
    for item in explicit_raw or []:
        if not isinstance(item, dict):
            continue
        basis = item.get("taskfacts_basis")
        if not isinstance(basis, list):
            basis = []
        out = {
            "id": item.get("id"),
            "description": str(item.get("description") or "").strip(),
            "source": SOURCE_QUERY_AND_TASKFACTS if basis else SOURCE_INITIAL_QUERY,
            "status": STATUS_EXPLICIT,
            "hardness": item.get("hardness"),
            "query_quote": item.get("query_quote"),
        }
        if basis:
            out["taskfacts_basis"] = basis
        explicit.append(out)
    for item in taskfact_raw or []:
        if not isinstance(item, dict):
            continue
        basis = item.get("taskfacts_basis")
        if not isinstance(basis, list):
            basis = []
        out = {
            "id": item.get("id"),
            "description": str(item.get("description") or "").strip(),
            "source": SOURCE_TASKFACTS,
            "status": STATUS_TASKFACT,
            "hardness": HARDNESS_TASKFACT,
            "query_quote": None,
        }
        if basis:
            out["taskfacts_basis"] = basis
        taskfacts.append(out)
    return explicit, taskfacts


def postprocess_llm_output(content, ctx: TaskContext):
    """解析 LLM 输出 → (explicit_requirements, taskfact_requirements, feedback)。"""
    obj = _parse_json_content(content)
    if not isinstance(obj, dict):
        return None, None, "顶层不是 JSON 对象"
    explicit, taskfacts = _normalize_requirements(
        obj.get("explicit_requirements"), obj.get("taskfact_requirements")
    )
    return explicit, taskfacts, None


def generate_one(api_key, base_url, model, ctx: TaskContext, max_attempts=3):
    feedback = None
    last_err = None
    for attempt in range(max_attempts):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(ctx, feedback)},
        ]
        try:
            content = call_llm(api_key, base_url, model, messages,
                               include_response_format=(attempt == 0))
            explicit, taskfacts, _ = postprocess_llm_output(content, ctx)
            if explicit is None:
                last_err = "LLM 输出无法解析为 JSON 或缺少 explicit_requirements"
                feedback = last_err
                continue
            doc = build_rubric_record(ctx, explicit, taskfacts, model)
            errors, _warnings = validate_requirements(doc, ctx)
            if not errors:
                return doc, None
            feedback = "\n".join(errors[:10])
            last_err = feedback
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            feedback = None
    return None, last_err


def build_rubric_record(ctx: TaskContext, explicit, taskfacts, model) -> dict:
    return {
        "task_id": ctx.tid,
        "rubric_version": RUBRIC_VERSION,
        "frozen": True,
        "query": ctx.public_query,
        "explicit_requirements": explicit,
        "taskfact_requirements": taskfacts,
        "process_requirements": PROCESS_REQUIREMENTS,
        "lifecycle_policy": {
            "shopper_changes_are_judge_time_interpretation": True,
            "rubric_is_not_modified_by_trajectory": True,
        },
        "generation_metadata": {
            "source": "query_plus_taskfacts",
            "trajectory_independent": True,
            "shared_across_harness_profiles": True,
            "model": model,
            "generator_version": GENERATOR_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def build_audit(ctx: TaskContext, doc: dict, errors: list, warnings: list) -> dict:
    full = json.dumps(doc, ensure_ascii=False)
    return {
        "task_id": ctx.tid,
        "query_requirement_count": len(doc.get("explicit_requirements") or []),
        "taskfact_requirement_count": len(doc.get("taskfact_requirements") or []),
        "process_requirement_count": len(doc.get("process_requirements") or []),
        "gold_asin_present_in_safe_rubric": bool(ctx.asin and ctx.asin in full),
        "hidden_leak_hits": [h for h in ctx.scan_leaks(full) if h],
        "duplicate_requirements": [
            e for e in errors if "重复" in e or "id 重复" in e
        ],
        "query_quote_errors": [e for e in errors if "query_quote" in e],
        "taskfacts_basis_errors": [e for e in errors if "taskfacts_basis" in e],
        "validation_errors": errors,
        "warnings": warnings,
    }


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 生成 / 校验主流程
# --------------------------------------------------------------------------- #

def cmd_generate(args):
    env = load_env(args.env)
    api_key, base_url, model = resolve_model_config(env, prefix="RUBRIC")
    if not api_key:
        print("缺少 RUBRIC_API_KEY / DEEPSEEK_API_KEY（检查 .env）", file=sys.stderr)
        sys.exit(1)

    manifest, tasks, contexts = load_and_check_inputs(args.source_goals, args.benchmark)

    all_ids = [str(t) for t in manifest["task_ids"]]
    if args.only:
        only = [t.strip() for t in args.only.split(",") if t.strip()]
        unknown = [t for t in only if t not in contexts]
        if unknown:
            print(f"[错误] --only 含未知 task_id: {unknown}", file=sys.stderr)
            sys.exit(1)
        tids = only
    else:
        tids = all_ids

    if args.max_tasks is not None:
        tids = tids[: args.max_tasks]

    out_dir = Path(args.out)
    audit_dir = Path(args.audit_out)
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)

    # 跳过已冻结（除非 --force）
    to_generate, skipped = [], []
    for tid in tids:
        fp = out_dir / f"{tid}.json"
        if not args.force and fp.exists():
            try:
                if json.loads(fp.read_text(encoding="utf-8")).get("frozen") is True:
                    skipped.append(tid)
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        to_generate.append(tid)

    print(f"总任务 {len(tids)}，跳过(已冻结) {len(skipped)}，待生成 {len(to_generate)}，"
          f"model={model}")

    succeeded, failed = [], []

    def run(tid):
        doc, err = generate_one(api_key, base_url, model, contexts[tid])
        return tid, doc, err

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(run, tid): tid for tid in to_generate}
        for fut in as_completed(futures):
            tid, doc, err = fut.result()
            if err is not None:
                failed.append({"task_id": tid, "reason": str(err)[:400]})
                print(f"[失败] {tid}: {err}", file=sys.stderr)
                continue
            errors, warnings = validate_requirements(doc, contexts[tid])
            if errors:
                failed.append({"task_id": tid, "reason": "; ".join(errors[:5])})
                for e in errors[:5]:
                    print(f"[失败] {tid}: {e}", file=sys.stderr)
                continue
            (out_dir / f"{tid}.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            audit = build_audit(contexts[tid], doc, errors, warnings)
            (audit_dir / f"{tid}.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            succeeded.append(tid)
            print(f"[{tid}] 成功 explicit={len(doc['explicit_requirements'])} "
                  f"taskfact={len(doc['taskfact_requirements'])} "
                  f"warn={len(warnings)}")

    succeeded_all = sorted(succeeded + skipped, key=lambda t: int(t))
    failed_sorted = sorted(failed, key=lambda f: int(f["task_id"]))
    write_manifest(out_dir, manifest, succeeded_all, failed_sorted, skipped,
                   args.source_goals, args.benchmark, model)

    print(f"\n完成：成功 {len(succeeded)}，失败 {len(failed)}，跳过(已冻结) {len(skipped)}")
    print(f"rubrics -> {out_dir}")
    print(f"rubric_audit -> {audit_dir}")
    if failed:
        print(f"失败任务 {len(failed)} 个，manifest 不置 frozen=true")
        sys.exit(2)
    return 0


def write_manifest(out_dir, manifest_base, succeeded_all, failed, skipped,
                   source_goals_path, benchmark_dir, model):
    m = {
        "rubric_version": RUBRIC_VERSION,
        "frozen": len(failed) == 0,
        "source_policy": "query_plus_taskfacts",
        "trajectory_independent": True,
        "shared_across_harness_profiles": True,
        "task_count": len(manifest_base.get("task_ids", [])),
        "generated_count": len(succeeded_all),
        "failed_count": len(failed),
        "failed_tasks": failed,
        "generator_version": GENERATOR_VERSION,
        "model": model,
        "tasks_sha256": sha256_file(Path(benchmark_dir) / "tasks.jsonl"),
        "taskfacts_sha256": sha256_file(source_goals_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return m


def cmd_verify(args):
    manifest, tasks, contexts = load_and_check_inputs(args.source_goals, args.benchmark)
    out_dir = Path(args.out)
    audit_dir = Path(args.audit_out)

    files = {f.stem: f for f in out_dir.glob("*.json") if f.name != "manifest.json"}
    benchmark_tids = {str(t) for t in manifest["task_ids"]}

    errors, warnings = [], []
    if set(files.keys()) != benchmark_tids:
        missing = sorted(benchmark_tids - set(files.keys()), key=int)
        extra = sorted(set(files.keys()) - benchmark_tids)
        errors.append(f"rubric 文件集合与 benchmark 不一致：缺失 {len(missing)}，多余 {len(extra)}")

    stats = {"hard": 0, "soft": 0, "reference": 0, "explicit": 0, "taskfact": 0,
             "process": 0}
    not_frozen = []
    for tid in sorted(files, key=lambda t: int(t)):
        try:
            doc = json.loads(files[tid].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            errors.append(f"{tid}: 文件不可读: {e}")
            continue
        errs, warns = validate_requirements(doc, contexts[tid])
        errors.extend(f"{tid}: {e}" for e in errs)
        warnings.extend(f"{tid}: {w}" for w in warns)
        if doc.get("frozen") is not True:
            not_frozen.append(tid)
        for r in doc.get("explicit_requirements") or []:
            stats["explicit"] += 1
            stats[r.get("hardness")] = stats.get(r.get("hardness"), 0) + 1
        for r in doc.get("taskfact_requirements") or []:
            stats["taskfact"] += 1
            stats["reference"] += 1
        stats["process"] += len(doc.get("process_requirements") or [])

    # 重新写 audit（离线校验也能刷新审计）
    for tid, fp in files.items():
        try:
            doc = json.loads(fp.read_text(encoding="utf-8"))
            errs, warns = validate_requirements(doc, contexts[tid])
            audit = build_audit(contexts[tid], doc, errs, warns)
            (audit_dir / f"{tid}.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            continue

    print(f"rubric 文件数: {len(files)} / 200")
    print(f"requirements: explicit={stats['explicit']} taskfact={stats['taskfact']} "
          f"process={stats['process']}")
    print(f"hardness: hard={stats['hard']} soft={stats['soft']} reference={stats['reference']}")
    if not_frozen:
        errors.append(f"frozen != true 的文件: {not_frozen[:5]}")

    # manifest 校验
    try:
        rmanifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        if rmanifest.get("failed_count"):
            errors.append(f"manifest.failed_count 非 0: {rmanifest.get('failed_count')}")
        elif rmanifest.get("frozen") is not True:
            errors.append("manifest.frozen != true（无失败时）")
        if rmanifest.get("generated_count") != 200:
            errors.append(f"manifest.generated_count {rmanifest.get('generated_count')} != 200")
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f"rubrics/manifest.json 不可读: {e}")

    if errors:
        print(f"\n[FAIL] 共 {len(errors)} 个错误（前 30 条）：", file=sys.stderr)
        for e in errors[:30]:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print(f"\n[PASS] 全部校验通过（warnings {len(warnings)} 条，多为 query 信号覆盖提醒）。")
    if warnings:
        for w in warnings[:20]:
            print(f"  警告: {w}")


def main():
    ap = argparse.ArgumentParser(
        description="生成/校验 shopping-final-v1 冻结 Rubric（Query + TaskFacts，v2 schema）")
    ap.add_argument("--source-goals",
                    default="benchmarks/shopping-final-v1/source_goals.private.jsonl")
    ap.add_argument("--benchmark", default="benchmarks/shopping-final-v1")
    ap.add_argument("--out", default="evaluations/h0/rubrics")
    ap.add_argument("--audit-out", default="evaluations/h0/rubric_audit")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-tasks", type=int, default=None)
    ap.add_argument("--only", default=None, help="逗号分隔 task_id（人工抽查用）")
    ap.add_argument("--force", action="store_true", help="覆盖已冻结的 rubric")
    ap.add_argument("--env", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--verify", action="store_true", help="离线全量校验，不调 LLM")
    args = ap.parse_args()

    if args.verify:
        cmd_verify(args)
        return
    raise SystemExit(cmd_generate(args))


if __name__ == "__main__":
    main()
