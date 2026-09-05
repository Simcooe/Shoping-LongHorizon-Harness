#!/usr/bin/env python3
"""冻结 shopping-final-v1：200 条 Multi-Turn + Personalization 购物任务。

本脚本完成两件事：
  1. 从 ShopSimulator 全量商品数据中，按与运行时完全一致的顺序重建
     all_products 与 all_goals（global goal index 语义），筛选候选池，
     用固定 seed 确定性分层采样恰好 200 条，写入 benchmarks/shopping-final-v1/。
  2. --verify 模式下不重新采样，只校验已冻结结果与全量目标/候选条件一致。

生成（在仓库根目录）：
  python3 scripts/freeze_benchmark.py \
      --data environments/ShopSimulator/shop_env/data/items_eval_train.json \
      --out benchmarks/shopping-final-v1 \
      --count 200 --seed 20260904 --persona-mode true

校验：
  python3 scripts/freeze_benchmark.py --benchmark benchmarks/shopping-final-v1 --verify

task_id 语义：全局 goal index（= load_products 后 all_products 的下标 =
               get_goals 返回 goals 的下标）。不允许先过滤 eval 再建 goal，
              也不允许把 200 条重新编号为 0..199。

本脚本优先加载真实环境代码（web_agent_site.engine.engine/goal，需要 flask/
rich/tqdm 等，位于 ShopSimulator venv）；不可用时回退到内置的纯标准库镜像
（与 v2.1 环境逐行对齐），并用 asin 对齐 + 数量断言双重保证。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import random
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

BENCHMARK_ID = "shopping-final-v1"
BENCHMARK_VERSION = "v1"
SOURCE_TAG = "eval"
TASK_ID_SEMANTICS = "global_goal_index"
ENVIRONMENT_VERSION = "shopsimulator-environment-v2.1"
INTERACTION_MODE = "multi_turn_personalization"
QUERY_MODE = "initial_instruction_simple"

REWARD_FEATURE_VERSION = "shopping-reward-features-v1"
OPTION_AXIS_VERSION = "option-axis-v1"
BRAND_ALIAS_VERSION = "brand-aliases-v1"

# 公开 tasks.jsonl 中禁止出现的字段名（递归检查 key）
SENSITIVE_PUBLIC_KEYS = {
    "asin", "gold_asin", "goldasin", "goal", "taskfacts", "instruction_full",
    "instructionfull", "goal_options", "goaloptions", "expected_brand",
    "expectedbrand", "expected_model", "expectedmodel", "user_persona",
    "userpersona", "reason_key", "reasonkey", "hidden", "source_goal",
    "sourcegoal", "rubric",
}

# run_batch.sh persona 模式下注入画像时使用的前缀（仅用于文档，不改变逻辑）
PERSONA_PREFIX = "用户画像（长期偏好，仅供参考；用户当前明确表达优先于画像）："


# --------------------------------------------------------------------------- #
# 纯标准库镜像：与 ShopSimulator v2.1 环境逐行对齐
# --------------------------------------------------------------------------- #

def _clean_product_keys(products):
    for product in products:
        for key in (
            "product_information", "brand", "brand_url", "list_price",
            "availability_quantity", "availability_status", "total_reviews",
            "total_answered_questions", "seller_id", "seller_name",
            "fulfilled_by_amazon", "fast_track_message", "aplus_present",
            "small_description_old",
        ):
            product.pop(key, None)
    return products


def _generate_product_prices(all_products):
    product_prices = {}
    for product in all_products:
        asin = product["asin"]
        pricing = product["pricing"]
        if not pricing:
            price = 100.0
        elif len(pricing) >= 1:
            price = pricing[0]
        product_prices[asin] = price
    return product_prices


def _load_products_stdlib(filepath):
    with open(filepath, encoding="utf-8") as f:
        products = json.load(f)

    products = _clean_product_keys(products)

    all_reviews = {}
    all_ratings = {}

    asins = set()
    all_products = []
    attribute_to_asins = defaultdict(set)

    for i, p in enumerate(products):
        asin = p["asin"]
        if asin == "nan" or len(asin) > 20:
            continue
        if asin in asins:
            continue
        asins.add(asin)

        products[i]["shop_name"] = p["shop_name"]
        products[i]["category"] = p["category"]
        products[i]["query"] = p.get("query", "")
        products[i]["product_category"] = p.get("product_category", "")
        products[i]["Title"] = p["title"]
        products[i]["Description"] = p.get("full_description", "")
        products[i]["Reviews"] = all_reviews.get(asin, [])
        products[i]["Rating"] = all_ratings.get(asin, "N.A.")

        for r in products[i]["Reviews"]:
            if "score" not in r:
                r["score"] = r.pop("stars")
            if "review" not in r:
                r["body"] = ""
            else:
                r["body"] = r.pop("review")
        products[i]["BulletPoints"] = (
            p.get("small_description", "")
            if isinstance(p.get("small_description", ""), list)
            else [p.get("small_description", "")]
        )

        pricing = p.get("pricing")
        if pricing is None or not pricing:
            pricing = [100.0]
            price_tag = "100.0"
        else:
            for j in range(len(pricing)):
                pricing[j] = pricing[j]
            if len(pricing) == 1:
                price_tag = f"{pricing[0]}"
            else:
                price_tag = f"{pricing[0]} to {pricing[1]}"
                pricing = pricing[:2]
        products[i]["pricing"] = pricing
        products[i]["Price"] = price_tag

        options = {}
        option_to_image = {}
        option_to_price = {}
        customization_options = p.get("customization_options", "")
        if customization_options:
            for option_name, option_contents in customization_options.items():
                if option_contents is None:
                    continue
                option_name = option_name.lower()
                option_values = []
                for option_content in option_contents:
                    option_value = (
                        option_content["value"].strip().replace("/", " | ").lower()
                    )
                    option_image = option_content.get("image", None)
                    option_values.append(option_value)
                    option_to_image[option_value] = option_image
                    option_to_price[option_value] = option_content.get("price", None)
                options[option_name] = option_values
        products[i]["options"] = options
        products[i]["option_to_image"] = option_to_image
        products[i]["option_to_price"] = option_to_price

        products[i]["Attributes"] = products[i]["attribute"]
        products[i]["instruction_text"] = p["instructions"][0]["instruction"]
        products[i]["instruction_attributes"] = p["instructions"][0]["attributes"]

        products[i]["MainImage"] = p["images"][0]
        products[i]["query"] = p["query"].lower().strip()
        products[i]["user_persona"] = p.get("user_persona", None)
        products[i]["reason_key"] = p.get("reason_key", None)

        all_products.append(products[i])

    product_item_dict = {p["asin"]: p for p in all_products}
    product_prices = _generate_product_prices(all_products)
    return all_products, product_item_dict, product_prices, attribute_to_asins


# ---- constraints.py 镜像 -------------------------------------------------- #

def _explicit_budget_from_instruction(instruction):
    text = str(instruction or "").replace(",", "")

    def scaled(number, unit):
        value = float(number)
        normalized_unit = str(unit or "").casefold()
        if normalized_unit == "万":
            value *= 10000
        elif normalized_unit in {"千", "k"}:
            value *= 1000
        return value

    shorthand = re.search(
        r"预算(?:控制)?在?\s*(\d+)\s*万\s*(\d+)\s*(?:千)?\s*(以内|以下|内|左右)?",
        text,
    )
    if shorthand:
        value = float(shorthand.group(1)) * 10000 + float(shorthand.group(2)) * 1000
        if shorthand.group(3) == "左右":
            value *= 1.1
        return value

    price_range = re.search(
        r"(?:预算|价格)(?:控制)?在?\s*"
        r"(\d+(?:\.\d+)?)\s*(万|千|[kK])?\s*元?\s*"
        r"(?:-|~|～|至|到)\s*"
        r"(\d+(?:\.\d+)?)\s*(万|千|[kK])?\s*元?"
        r"(?:之间|以内|以下|左右)?",
        text,
    )
    if price_range:
        low = scaled(price_range.group(1), price_range.group(2))
        high = scaled(price_range.group(3), price_range.group(4))
        if low > 0 and high >= low:
            return high

    if re.search(r"(?:预算|价格)(?:控制)?在?\s*\d+(?:\.\d+)?\s*[kK]\s*\+", text):
        return None

    patterns = (
        r"预算(?:控制)?在?\s*(\d+(?:\.\d+)?)\s*(万|千|[kK])?\s*元?(以内|以下|内|左右)?(?!\s*[-~～至到+kK])",
        r"价格(?:控制)?在?\s*(\d+(?:\.\d+)?)\s*(万|千|[kK])?\s*元?(以内|以下|内|左右)?(?!\s*[-~～至到+kK])",
        r"(?:不超过|不高于|最高)\s*(\d+(?:\.\d+)?)\s*(万|千)?\s*元",
        r"(\d+(?:\.\d+)?)\s*(万|千)?\s*元(以内|以下)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = scaled(match.group(1), match.group(2))
            qualifier = (
                match.group(3) if match.lastindex and match.lastindex >= 3 else None
            )
            if qualifier == "左右":
                value *= 1.1
            if value > 0:
                return value
    return None


# ---- reward_features.py + comparators.py 镜像 ------------------------------ #

_AXIS_ALIASES = {
    "color": {"颜色", "颜色分类"},
    "size": {"尺码", "鞋码"},
    "dimensions": {"尺寸", "大小"},
    "net_content": {"净含量", "总净含量"},
    "flavor": {"口味", "食品口味"},
    "specification": {"规格", "规格描述", "规格类型"},
    "bundle": {"套餐", "套餐类型", "组合套餐"},
    "capacity": {"容量", "规格容量"},
}
_MODEL_TOKEN = re.compile(
    r"(?<![a-z0-9])(?=[a-z0-9._+-]{2,24}(?![a-z0-9]))"
    r"(?=[a-z0-9._+-]*\d)[a-z0-9._+-]+",
    flags=re.IGNORECASE,
)
_SHOP_SUFFIXES = ("官方旗舰店", "旗舰店", "专卖店", "专营店", "企业店", "店")


def _normalize_text(value, remove_space=True):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    if remove_space:
        return re.sub(r"\s+", "", text)
    return re.sub(r"\s+", " ", text)


def _normalize_option_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("/", "|")
    return re.sub(r"\s+", "", text)


def _canonicalize_option_axis(value):
    normalized = _normalize_option_text(value)
    for canonical, aliases in _AXIS_ALIASES.items():
        if normalized in {_normalize_option_text(alias) for alias in aliases}:
            return canonical
    return normalized


def _clean_list(value):
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _target_option_axes(target_product):
    axes = {}
    for raw_axis, entries in (target_product.get("customization_options") or {}).items():
        values = []
        for entry in entries or []:
            if isinstance(entry, dict) and _normalize_option_text(entry.get("value")):
                values.append(str(entry["value"]))
        axes[str(raw_axis)] = values
    return axes


def _resolve_required_options(option_values, target_product):
    axes = _target_option_axes(target_product)
    resolved = {}
    unresolved = []
    for required_value in option_values:
        normalized_required = _normalize_option_text(required_value)
        matches = [
            raw_axis
            for raw_axis, values in axes.items()
            if normalized_required in {_normalize_option_text(value) for value in values}
        ]
        if len(matches) != 1:
            unresolved.append(
                {
                    "value": required_value,
                    "reason": "axis_not_found" if not matches else "axis_ambiguous",
                    "axes": matches,
                }
            )
            continue
        raw_axis = matches[0]
        canonical_axis = _canonicalize_option_axis(raw_axis)
        if canonical_axis in resolved:
            unresolved.append(
                {
                    "value": required_value,
                    "reason": "canonical_axis_collision",
                    "axes": [raw_axis],
                }
            )
            continue
        resolved[canonical_axis] = {
            "value": required_value,
            "source_axis": raw_axis,
            "source": "instruction.instruction_options",
        }
    return resolved, unresolved


def _load_brand_aliases(config_path):
    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if payload.get("version") != BRAND_ALIAS_VERSION:
        raise ValueError("brand alias table has the wrong version")
    result = {}
    for canonical, aliases in (payload.get("aliases") or {}).items():
        canonical_normalized = _normalize_text(canonical)
        result[canonical_normalized] = canonical_normalized
        for alias in aliases or []:
            result[_normalize_text(alias)] = canonical_normalized
    return result


def _explicit_brand(instruction, target_product, aliases):
    instruction_text = _normalize_text(instruction)
    target_text = _normalize_text(
        " ".join(
            str(value)
            for value in (target_product.get("title"), target_product.get("shop_name"))
            if value
        )
    )
    matches = {
        canonical
        for alias, canonical in aliases.items()
        if len(alias) >= 2 and alias in instruction_text and alias in target_text
    }
    shop_name = _normalize_text(target_product.get("shop_name"))
    for suffix in _SHOP_SUFFIXES:
        normalized_suffix = _normalize_text(suffix)
        if shop_name.endswith(normalized_suffix):
            shop_name = shop_name[: -len(normalized_suffix)]
            break
    title = _normalize_text(target_product.get("title"))
    for length in range(min(len(shop_name), 12), 1, -1):
        prefix = shop_name[:length]
        if prefix in instruction_text and prefix in title:
            matches.add(prefix)
            break
    return sorted(matches)


def _explicit_models(instruction, target_product):
    instruction_tokens = {
        token.casefold() for token in _MODEL_TOKEN.findall(instruction)
    }
    target_text = " ".join(
        str(value)
        for value in (target_product.get("title"), target_product.get("full_description"))
        if value
    )
    target_tokens = {
        token.casefold() for token in _MODEL_TOKEN.findall(target_text)
    }
    return sorted(instruction_tokens.intersection(target_tokens))


def _compile_reward_features(instruction_record, target_product, aliases):
    instruction = instruction_record if isinstance(instruction_record, dict) else {}
    product = target_product if isinstance(target_product, dict) else {}
    instruction_text = str(instruction.get("instruction") or "")
    option_values = _clean_list(instruction.get("instruction_options"))
    required_options, unresolved_options = _resolve_required_options(
        option_values, product
    )
    return {
        "reward_feature_version": REWARD_FEATURE_VERSION,
        "category": product.get("category"),
        "expected_brand": _explicit_brand(instruction_text, product, aliases),
        "expected_model": _explicit_models(instruction_text, product),
        "expected_core_functions": _clean_list(instruction.get("attributes")),
        "required_options_by_key": required_options,
        "unresolved_option_requirements": unresolved_options,
        "option_axis_version": OPTION_AXIS_VERSION,
        "feature_sources": {
            "category": "task.target_product.category",
            "brand": "instruction_explicit_alias",
            "model": "instruction_target_token_intersection",
            "core_functions": "instruction.attributes",
            "options": "instruction.instruction_options",
        },
    }


def _get_goals_stdlib(all_products, product_prices, if_persona, aliases):
    goals = []
    for item in all_products:
        if "instructions" not in item:
            continue
        asin = item["asin"]
        for product in item["instructions"]:
            attributes = product.get("attributes", [])
            if len(attributes) == 0:
                continue

            if product_prices is not None:
                price_upper = _explicit_budget_from_instruction(product["instruction"])
            else:
                price_upper = 10000000

            if not isinstance(item.get("user_persona"), dict):
                item["user_persona"] = {}
            user_persona = item["user_persona"].copy()
            reason_key = item.get("reason_key")
            if user_persona and "__reasoning__" in user_persona:
                reasoning_value = user_persona.pop("__reasoning__")
                ordered_persona = {"__reasoning__": reasoning_value}
                ordered_persona.update(user_persona)
                user_persona = ordered_persona

            if if_persona:
                instruction_text = (
                    product.get("instruction_sample")
                    or product.get("instruction_simple")
                    or product["instruction"]
                )
            else:
                instruction_text = product["instruction"]

            goal = {
                "asin": asin,
                "category": item["category"],
                "query": item["query"],
                "name": item["title"],
                "instruction_text": instruction_text,
                "instruction_full": product["instruction"],
                "instruction_simple": product.get("instruction_simple"),
                "attributes": attributes,
                "price_upper": price_upper,
                "goal_options": product.get("instruction_options"),
                "user_persona": user_persona,
                "reason_key": reason_key,
            }
            goal.update(_compile_reward_features(product, item, aliases))
            goals.append(goal)
    for goal in goals:
        goal["weight"] = 1
    return goals


# --------------------------------------------------------------------------- #
# 引擎加载：优先真实环境代码，回退纯标准库镜像
# --------------------------------------------------------------------------- #

def _config_path_for(data_file: Path) -> Path:
    # data_file = <shop_env>/data/items_eval_train.json → configs 在 <shop_env>/configs
    return data_file.parent.parent / "configs" / "brand_aliases.json"


def _load_engine(data_file: Path):
    """返回 (load_products, get_goals, loader_name)。"""
    shop_env_dir = str(data_file.parent.parent)
    try:
        if shop_env_dir not in sys.path:
            sys.path.insert(0, shop_env_dir)
        # 真实引擎需要 flask / rich / tqdm（get_goals 不需要 spacy/thefuzz）
        from web_agent_site.engine.engine import load_products as real_load
        from web_agent_site.engine.goal import get_goals as real_get

        def load(filepath):
            return real_load(filepath=filepath, num_products=None, human_goals=True)

        def goals(all_products, product_prices, if_persona):
            return real_get(all_products, product_prices, if_persona=if_persona)

        return load, goals, "environment_engine"
    except Exception:  # noqa: BLE001 — 任何导入失败都回退镜像
        aliases = _load_brand_aliases(_config_path_for(data_file))

        def load(filepath):
            return _load_products_stdlib(filepath)

        def goals(all_products, product_prices, if_persona):
            return _get_goals_stdlib(all_products, product_prices, if_persona, aliases)

        return load, goals, "stdlib_mirror"


# --------------------------------------------------------------------------- #
# metadata / 候选筛选
# --------------------------------------------------------------------------- #

def _complexity(full_constraint_count):
    if full_constraint_count <= 3:
        return "low"
    if full_constraint_count <= 6:
        return "medium"
    return "high"


def _hidden_attribute_count(attributes, instruction_options, instruction_simple):
    """确定性统计：完整需求里的 gold 属性/规格值中，未在模糊需求里出现的个数。"""
    simple_norm = _normalize_text(instruction_simple)
    hidden = 0
    for attr in attributes or []:
        if _normalize_text(attr) not in simple_norm:
            hidden += 1
    for opt in instruction_options or []:
        if _normalize_text(opt) not in simple_norm:
            hidden += 1
    return hidden


def _persona_field_count(user_persona):
    if not isinstance(user_persona, dict):
        return 0
    return sum(1 for k in user_persona if k != "__reasoning__")


def _build_metadata(goal, product, instruction_record):
    instruction_full = goal["instruction_full"]
    instruction_simple = goal["instruction_simple"]
    attributes = instruction_record.get("attributes") or []
    instruction_options = instruction_record.get("instruction_options") or []
    expected_brand = goal.get("expected_brand") or []
    expected_model = goal.get("expected_model") or []
    user_persona = goal.get("user_persona") or {}

    has_budget = _explicit_budget_from_instruction(instruction_full) is not None
    has_brand = len(expected_brand) > 0
    has_model = len(expected_model) > 0
    has_options = len(instruction_options) > 0

    full_constraint_count = (
        len(attributes)
        + len(instruction_options)
        + int(has_budget)
        + int(has_brand)
        + int(has_model)
    )

    return {
        "persona_mode": True,
        "interaction_mode": INTERACTION_MODE,
        "query_mode": QUERY_MODE,
        "domain": product.get("domain_zh", ""),
        "has_information_gap": True,
        "has_persona": True,
        "has_budget": has_budget,
        "has_brand": has_brand,
        "has_model": has_model,
        "has_options": has_options,
        "persona_field_count": _persona_field_count(user_persona),
        "simple_query_length": len(instruction_simple or ""),
        "full_query_length": len(instruction_full or ""),
        "hidden_attribute_count": _hidden_attribute_count(
            attributes, instruction_options, instruction_simple
        ),
        "full_constraint_count": full_constraint_count,
        "complexity": _complexity(full_constraint_count),
    }


def _is_candidate(product, instruction_record):
    if product.get("tag") != SOURCE_TAG:
        return False
    if not product.get("user_persona"):
        return False
    simple = instruction_record.get("instruction_simple")
    full = instruction_record.get("instruction")
    if not simple:
        return False
    if simple == full:
        return False
    return True


def _rebuild(data_file, if_persona):
    """按运行时顺序重建 all_products/all_goals，并做数量 + asin 对齐断言。"""
    load_products, get_goals, loader = _load_engine(data_file)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        all_products, product_item_dict, product_prices, _ = load_products(str(data_file))
        all_goals = get_goals(all_products, product_prices, if_persona=if_persona)

    if len(all_products) != len(all_goals):
        raise SystemExit(
            f"FATAL: all_products({len(all_products)}) != all_goals({len(all_goals)})"
        )
    for idx, (product, goal) in enumerate(zip(all_products, all_goals)):
        if str(product["asin"]) != str(goal["asin"]):
            raise SystemExit(
                f"FATAL: asin 无法对齐 @ global_idx={idx}: "
                f"product={product['asin']} goal={goal['asin']}"
            )
    return all_products, all_goals, loader


# --------------------------------------------------------------------------- #
# 确定性分层采样
# --------------------------------------------------------------------------- #

def _select_task_ids(candidates, count, seed):
    """确定性分层采样：域配额（Hamilton 法）+ 域内特征均匀系统抽样。

    - candidates: list[dict]，每个含 keys: global_idx, domain, sort_key
    - 保证每个 domain 至少 1 条（域数 < count 时），总量恰为 count。
    - 域内按平衡特征键排序后，用 seed 旋转 + 系统抽样，跨特征空间均匀铺开。
    """
    rng = random.Random(seed)

    domains = {}
    for c in candidates:
        domains.setdefault(c["domain"], []).append(c)
    domain_names = sorted(domains)

    # Hamilton (largest remainder) 配额分配，每个域至少 1
    n_dom = len(domain_names)
    if n_dom > count:
        raise SystemExit(f"FATAL: {n_dom} 个 domain 超过采样数 {count}")
    base = count // n_dom
    remainder = count % n_dom
    quota = {d: base for d in domain_names}
    for d in domain_names[:remainder]:
        quota[d] += 1
    # 容量裁剪：某域候选不足配额时，把多余配额转移给还有余量的域
    overflow = 0
    for d in domain_names:
        avail = len(domains[d])
        if quota[d] > avail:
            overflow += quota[d] - avail
            quota[d] = avail
    i = 0
    while overflow > 0:
        d = domain_names[i % n_dom]
        if len(domains[d]) > quota[d]:
            quota[d] += 1
            overflow -= 1
        i += 1
        if i > 100000:
            raise SystemExit("FATAL: 配额再分配死循环")

    selected_indices = []
    for d in domain_names:
        members = sorted(domains[d], key=lambda c: (c["sort_key"], c["global_idx"]))
        q = quota[d]
        n = len(members)
        if q == 0:
            continue
        if q >= n:
            selected_indices.extend(m["global_idx"] for m in members)
            continue
        # seed 旋转 + 系统抽样（步长按整除，保证恰 q 个且互异）
        rotate = rng.randrange(n) if n > 0 else 0
        for j in range(q):
            pos = (rotate + (j * n) // q) % n
            selected_indices.append(members[pos]["global_idx"])

    # 兜底：若仍不足/超量，用 seed 从剩余候选中补齐或从已选中剔除
    selected_set = set(selected_indices)
    if len(selected_set) != count:
        raise SystemExit(
            f"FATAL: 采样数 {len(selected_set)} != {count}"
        )

    return sorted(selected_set)


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

def _write_json(path, obj):
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _statistics(candidate_pool, selected_records):
    stats = {
        "candidate_count": len(candidate_pool),
        "selected_count": len(selected_records),
        "domains": {},
        "has_budget": {"true": 0, "false": 0},
        "has_brand": {"true": 0, "false": 0},
        "has_model": {"true": 0, "false": 0},
        "has_options": {"true": 0, "false": 0},
        "has_persona": {"true": 0, "false": 0},
        "has_information_gap": {"true": 0, "false": 0},
        "complexity": {"low": 0, "medium": 0, "high": 0},
        "persona_field_count": {},
        "hidden_attribute_count": {},
        "full_constraint_count": {},
    }
    for rec in selected_records:
        m = rec["metadata"]
        d = m["domain"]
        stats["domains"][d] = stats["domains"].get(d, 0) + 1
        for key in ("has_budget", "has_brand", "has_model", "has_options",
                    "has_persona", "has_information_gap"):
            stats[key][str(m[key]).lower()] = stats[key].get(str(m[key]).lower(), 0) + 1
        stats["complexity"][m["complexity"]] = stats["complexity"].get(m["complexity"], 0) + 1
        for key in ("persona_field_count", "hidden_attribute_count", "full_constraint_count"):
            v = str(m[key])
            stats[key][v] = stats[key].get(v, 0) + 1
    # 只保留实际出现的桶，且按 key 排序
    for key in ("domains", "persona_field_count", "hidden_attribute_count", "full_constraint_count"):
        stats[key] = dict(sorted(stats[key].items()))
    for key in ("has_budget", "has_brand", "has_model", "has_options",
                "has_persona", "has_information_gap", "complexity"):
        stats[key] = {k: v for k, v in stats[key].items() if v > 0}
    return stats


def freeze(data_file: Path, out_dir: Path, count: int, seed: int, persona_mode: bool):
    if count != 200:
        raise SystemExit(f"FATAL: 本版本仅支持冻结 200 条，收到 count={count}")

    all_products, all_goals, loader = _rebuild(data_file, if_persona=persona_mode)

    candidates = []
    for global_idx, (product, goal) in enumerate(zip(all_products, all_goals)):
        instruction_record = (
            product.get("instructions") or [{}]
        )[0]
        if not _is_candidate(product, instruction_record):
            continue
        metadata = _build_metadata(goal, product, instruction_record)
        sort_key = (
            metadata["complexity"],
            int(metadata["has_budget"]),
            int(metadata["has_brand"]),
            int(metadata["has_model"]),
            int(metadata["has_options"]),
            metadata["persona_field_count"],
            metadata["hidden_attribute_count"],
        )
        candidates.append(
            {
                "global_idx": global_idx,
                "domain": metadata["domain"],
                "sort_key": sort_key,
            }
        )

    if len(candidates) < count:
        raise SystemExit(
            f"FATAL: 候选池 {len(candidates)} 不足 {count}"
        )

    task_ids = _select_task_ids(candidates, count, seed)

    out_dir.mkdir(parents=True, exist_ok=True)

    tasks_lines = []
    private_lines = []
    for global_idx in task_ids:
        product = all_products[global_idx]
        goal = all_goals[global_idx]
        instruction_record = (product.get("instructions") or [{}])[0]
        metadata = _build_metadata(goal, product, instruction_record)

        public = {
            "task_id": global_idx,
            "query": goal["instruction_simple"],
            "domain": metadata["domain"],
            "metadata": metadata,
        }
        tasks_lines.append(json.dumps(public, ensure_ascii=False))

        private = {
            "task_id": global_idx,
            "source_product_index": global_idx,
            "asin": str(product["asin"]),
            "instruction_simple": goal["instruction_simple"],
            "instruction_full": goal["instruction_full"],
            "query": goal["instruction_simple"],
            "goal": goal,
            "user_persona": product.get("user_persona"),
            "reason_key": goal.get("reason_key"),
        }
        private_lines.append(json.dumps(private, ensure_ascii=False))

    # 公开/私有文件按 task_id 升序写入
    tasks_path = out_dir / "tasks.jsonl"
    private_path = out_dir / "source_goals.private.jsonl"
    tasks_path.write_text("\n".join(tasks_lines) + "\n", encoding="utf-8")
    private_path.write_text("\n".join(private_lines) + "\n", encoding="utf-8")

    selected_records = [
        {
            "metadata": _build_metadata(
                all_goals[i],
                all_products[i],
                (all_products[i].get("instructions") or [{}])[0],
            )
        }
        for i in task_ids
    ]

    manifest = {
        "benchmark_id": BENCHMARK_ID,
        "version": BENCHMARK_VERSION,
        "frozen": True,
        "task_count": count,
        "task_ids": task_ids,
        "source_tag": SOURCE_TAG,
        "task_id_semantics": TASK_ID_SEMANTICS,
        "source_data": str(data_file),
        "environment_version": ENVIRONMENT_VERSION,
        "persona_mode": bool(persona_mode),
        "interaction_mode": INTERACTION_MODE,
        "shopper_simulator_required": True,
        "query_mode": QUERY_MODE,
        "sampling": {
            "method": "stratified_deterministic",
            "seed": seed,
            "description": (
                "domain quota (Hamilton largest-remainder, min 1 per domain) + "
                "within-domain feature-key systematic sampling with seeded rotation"
            ),
        },
        "selection_criteria": {
            "source_tag": SOURCE_TAG,
            "user_persona_nonempty": True,
            "instruction_simple_nonempty": True,
            "instruction_simple_neq_instruction_full": True,
            "multi_turn": True,
            "personalization": True,
        },
        "rubric_version": "v1",
        "statistics": _statistics(candidates, selected_records),
        "files": {
            "tasks": "tasks.jsonl",
            "private_source_goals": "source_goals.private.jsonl",
        },
        "agent_query_form": {
            "persona_mode": True,
            "public_query": "instruction_simple",
            "runtime_query_composition": (
                "instruction_simple + '\\n\\n" + PERSONA_PREFIX + "\\n' "
                "+ json.dumps(user_persona minus '__reasoning__')"
            ),
            "note": (
                "公开 tasks.jsonl 只存 instruction_simple，避免泄漏 user_persona 中"
                "镜像 gold 属性的隐藏需求（如最近搜索关键词）。可见画像由 run_batch.sh "
                "在 SHOPSIM_IF_PERSONA=1 时于运行时注入，不来自公开 benchmark 文件。"
            ),
        },
        "generated_with": {
            "loader": loader,
            "freeze_script": "scripts/freeze_benchmark.py",
            "freeze_script_version": "1.0",
        },
    }
    _write_json(out_dir / "manifest.json", manifest)

    print(f"==> 冻结完成：{count} 条任务写入 {out_dir}")
    print(f"    候选池：{len(candidates)}（loader={loader}）")
    print(f"    task_ids 数量：{len(task_ids)}，升序：{task_ids == sorted(task_ids)}")
    return 0


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def _read_jsonl(path: Path):
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _recursive_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k))
            keys |= _recursive_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _recursive_keys(v)
    return keys


def verify(benchmark_dir: Path):
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)

    manifest_path = benchmark_dir / "manifest.json"
    tasks_path = benchmark_dir / "tasks.jsonl"
    private_path = benchmark_dir / "source_goals.private.jsonl"

    check(manifest_path.exists(), "manifest.json 不存在")
    check(tasks_path.exists(), "tasks.jsonl 不存在")
    check(private_path.exists(), "source_goals.private.jsonl 不存在")
    if errors:
        _fail(errors)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tasks = _read_jsonl(tasks_path)
    private = _read_jsonl(private_path)

    check(manifest.get("frozen") is True, "frozen != true")
    check(manifest.get("task_count") == 200, "task_count != 200")

    task_ids = manifest.get("task_ids") or []
    check(len(task_ids) == 200, f"task_ids 数量 {len(task_ids)} != 200")
    check(len(task_ids) == len(set(task_ids)), "task_ids 存在重复")
    check(task_ids == sorted(task_ids), "task_ids 未按升序")

    check(len(tasks) == 200, f"tasks.jsonl 行数 {len(tasks)} != 200")
    check(len(private) == 200, f"private 行数 {len(private)} != 200")

    tasks_ids = {t.get("task_id") for t in tasks}
    private_ids = {p.get("task_id") for p in private}
    check(tasks_ids == set(task_ids), "tasks.jsonl 与 manifest task_id 集合不一致")
    check(private_ids == set(task_ids), "private 与 manifest task_id 集合不一致")

    # source_product_index == task_id
    for p in private:
        check(
            p.get("source_product_index") == p.get("task_id"),
            f"task {p.get('task_id')}: source_product_index != task_id",
        )

    # 公开文件无敏感字段
    for t in tasks:
        keys = _recursive_keys(t)
        leaked = keys & SENSITIVE_PUBLIC_KEYS
        check(not leaked, f"task {t.get('task_id')}: 公开文件含敏感字段 {sorted(leaked)}")

    # 用全量目标重建做深度校验
    data_file = manifest.get("source_data")
    if data_file and Path(data_file).exists():
        all_products, all_goals, _ = _rebuild(Path(data_file), if_persona=manifest.get("persona_mode", True))
        full_ids = set(range(len(all_goals)))
        check(set(task_ids) <= full_ids, "存在 task_id 不在全量 goals 范围内")

        # 对齐 + 候选条件逐条复核
        priv_by_idx = {p["task_id"]: p for p in private}
        task_by_idx = {t["task_id"]: t for t in tasks}
        task_id_set = set(task_ids)

        candidates = []
        selected_records = []
        for i, (product, goal) in enumerate(zip(all_products, all_goals)):
            rec = (product.get("instructions") or [{}])[0]
            if _is_candidate(product, rec):
                metadata = _build_metadata(goal, product, rec)
                sort_key = (
                    metadata["complexity"],
                    int(metadata["has_budget"]),
                    int(metadata["has_brand"]),
                    int(metadata["has_model"]),
                    int(metadata["has_options"]),
                    metadata["persona_field_count"],
                    metadata["hidden_attribute_count"],
                )
                candidates.append(
                    {"global_idx": i, "domain": metadata["domain"], "sort_key": sort_key}
                )
                if i in task_id_set:
                    selected_records.append({"metadata": metadata})

        recomputed_stats = _statistics(candidates, selected_records)

        for i in task_ids:
            product = all_products[i]
            goal = all_goals[i]
            rec = (product.get("instructions") or [{}])[0]
            check(product.get("tag") == SOURCE_TAG, f"task {i}: tag != eval")
            check(bool(product.get("user_persona")), f"task {i}: user_persona 为空")
            check(bool(goal.get("instruction_simple")), f"task {i}: instruction_simple 为空")
            check(
                goal.get("instruction_simple") != goal.get("instruction_full"),
                f"task {i}: instruction_simple == instruction_full",
            )

            priv = priv_by_idx[i]
            pub = task_by_idx[i]
            check(
                priv.get("instruction_simple") == goal.get("instruction_simple"),
                f"task {i}: private instruction_simple 与 goal 不一致",
            )
            check(
                priv.get("instruction_full") == goal.get("instruction_full"),
                f"task {i}: private instruction_full 与 goal 不一致",
            )
            check(
                pub.get("query") == goal.get("instruction_simple"),
                f"task {i}: 公开 query 与 query_mode(instruction_simple) 不一致",
            )

        # statistics 一致性
        for key in ("candidate_count", "selected_count", "domains",
                    "has_budget", "has_brand", "has_model", "has_options",
                    "has_persona", "has_information_gap", "complexity"):
            check(
                manifest.get("statistics", {}).get(key) == recomputed_stats.get(key),
                f"statistics.{key} 与实际不一致",
            )
    else:
        errors.append(f"无法读取 source_data: {data_file}")

    # 复现性：同 seed 同候选池应产出同一批 task_ids
    if data_file and Path(data_file).exists():
        all_products, all_goals, _ = _rebuild(Path(data_file), if_persona=manifest.get("persona_mode", True))
        seed = manifest.get("sampling", {}).get("seed")
        candidates = []
        for i, (product, goal) in enumerate(zip(all_products, all_goals)):
            rec = (product.get("instructions") or [{}])[0]
            if _is_candidate(product, rec):
                metadata = _build_metadata(goal, product, rec)
                sort_key = (
                    metadata["complexity"],
                    int(metadata["has_budget"]),
                    int(metadata["has_brand"]),
                    int(metadata["has_model"]),
                    int(metadata["has_options"]),
                    metadata["persona_field_count"],
                    metadata["hidden_attribute_count"],
                )
                candidates.append(
                    {"global_idx": i, "domain": metadata["domain"], "sort_key": sort_key}
                )
        if seed is not None:
            repro = _select_task_ids(candidates, manifest.get("task_count", 200), seed)
            check(repro == task_ids, "同 seed 复现失败")

    if errors:
        _fail(errors)

    print("==> verify 通过")
    print(f"    task_count={len(task_ids)}")
    print(f"    domains={len(manifest.get('statistics', {}).get('domains', {}))}")
    print(f"    candidate_count={manifest.get('statistics', {}).get('candidate_count')}")
    return 0


def _fail(errors):
    print(f"==> verify 失败：{len(errors)} 个错误", file=sys.stderr)
    for e in errors[:50]:
        print(f"  - {e}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv):
    parser = argparse.ArgumentParser(description="冻结/校验 shopping-final-v1 benchmark")
    parser.add_argument("--data", help="items_eval_train.json 路径")
    parser.add_argument("--out", help="输出目录 benchmarks/shopping-final-v1")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260904)
    parser.add_argument("--persona-mode", default="true")
    parser.add_argument("--benchmark", help="已冻结目录（配合 --verify）")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)

    if args.verify:
        if not args.benchmark:
            parser.error("--verify 需要 --benchmark")
        return verify(Path(args.benchmark))

    if not args.data or not args.out:
        parser.error("生成模式需要 --data 与 --out")
    persona_mode = str(args.persona_mode).strip().lower() in ("1", "true", "yes")
    return freeze(
        Path(args.data),
        Path(args.out),
        args.count,
        args.seed,
        persona_mode,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
