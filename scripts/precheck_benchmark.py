#!/usr/bin/env python3
"""Benchmark 预检：只读核验冻结任务的自洽性，不修改任何 benchmark 文件。

针对 benchmarks/<benchmark> 的 200 条任务，用商品源数据逐项检查：

1. 目标 SKU 是否存在于商品源；
2. 商品类目是否与目标一致；
3. 预算表达是否可编译（区分 declared / undeclared / parse_failed，
   并与冻结 goal 的 price_upper 对照，报告历史编译口径差异）；
4. 目标规格（required_options_by_key）是否在商品选项轴上可选；
5. 价格可核验性：是否存在可信完整组合价（缺组合价的多轴商品列出，
   作为「不可核验」数据问题清单，不得静默剔除）。

输出：--out 指定 JSON（默认 reports/benchmark-precheck/<benchmark>.json），
不写回 benchmark 目录，不重跑任何东西。

用法:
  python3 scripts/precheck_benchmark.py \
    --benchmark benchmarks/shopping-final-v1 \
    --items environments/ShopSimulator/shop_env/data/items_eval_train.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOP_ENV = ROOT / "environments" / "ShopSimulator" / "shop_env"
sys.path.insert(0, str(SHOP_ENV))

from web_agent_site.engine.constraints import compile_budget  # noqa: E402
from web_agent_site.engine.reward_features import (  # noqa: E402
    canonicalize_option_axis,
    normalize_option_text,
)


def load_products_by_asin(items_path: Path) -> dict:
    data = json.loads(items_path.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items") or []
    by_asin = {}
    for item in items:
        asin = str(item.get("asin") or "")
        if asin:
            by_asin[asin] = item
    return by_asin


def option_availability(product: dict, required_options_by_key: dict) -> dict:
    """检查目标规格在商品选项轴上的可选性（归一化对齐，不猜测）。"""
    raw = product.get("customization_options") or {}
    axes = {}
    collisions = []
    for raw_axis, entries in raw.items():
        canonical = canonicalize_option_axis(raw_axis)
        values = {
            normalize_option_text(e.get("value"))
            for e in (entries or [])
            if isinstance(e, dict) and normalize_option_text(e.get("value"))
        }
        if canonical in axes:
            collisions.append(canonical)
        else:
            axes[canonical] = {"source_axis": raw_axis, "values": values}
    for canonical in set(collisions):
        axes.pop(canonical, None)

    missing_axis, unavailable_value, available = [], [], []
    for canonical_axis, requirement in (required_options_by_key or {}).items():
        required_value = (
            requirement.get("value") if isinstance(requirement, dict) else requirement
        )
        axis = axes.get(canonical_axis)
        if axis is None:
            missing_axis.append(canonical_axis)
            continue
        if normalize_option_text(required_value) in axis["values"]:
            available.append(canonical_axis)
        else:
            unavailable_value.append(
                {"axis": canonical_axis, "value": required_value}
            )
    return {
        "available_axes": available,
        "missing_axes": missing_axis,
        "unavailable_values": unavailable_value,
        "axis_collisions": sorted(set(collisions)),
    }


def effective_price_axes(product: dict) -> list[str]:
    """统计价格随取值变化的选项轴数量（用于不可核验价格清单）。"""
    axes = []
    for raw_axis, entries in (product.get("customization_options") or {}).items():
        prices = set()
        for e in entries or []:
            if isinstance(e, dict) and e.get("price") is not None:
                prices.add(float(e["price"]))
        if len(prices) > 1:
            axes.append(raw_axis)
    return axes


def main():
    ap = argparse.ArgumentParser(description="benchmark 预检（只读）")
    ap.add_argument("--benchmark", default="benchmarks/shopping-final-v1")
    ap.add_argument(
        "--items",
        default="environments/ShopSimulator/shop_env/data/items_eval_train.json",
    )
    ap.add_argument(
        "--out", default=None,
        help="输出 JSON（默认 reports/benchmark-precheck/<benchmark>.json）",
    )
    args = ap.parse_args()

    bench_dir = Path(args.benchmark)
    manifest = json.loads((bench_dir / "manifest.json").read_text(encoding="utf-8"))
    goals = [
        json.loads(line)
        for line in (bench_dir / "source_goals.private.jsonl")
        .read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    bench_ids = {str(t) for t in manifest["task_ids"]}
    if {str(g["task_id"]) for g in goals} != bench_ids:
        print("[失败] source goals 与 benchmark task_ids 不一致", file=sys.stderr)
        sys.exit(1)

    print(f"加载商品源 {args.items} ...", file=sys.stderr)
    products = load_products_by_asin(Path(args.items))
    print(f"商品源 {len(products)} 个 asin", file=sys.stderr)

    rows = []
    budget_status = Counter()
    problems = Counter()
    unverifiable_price = []
    budget_recompile_diff = []

    for g in goals:
        tid = str(g["task_id"])
        asin = str(g.get("asin") or "")
        goal = g.get("goal") or {}
        product = products.get(asin)
        row = {
            "task_id": int(tid),
            "asin": asin,
            "sku_found": product is not None,
            "category_match": None,
            "budget": None,
            "options": None,
            "price_verifiable": None,
            "issues": [],
        }
        if product is None:
            row["issues"].append("target_sku_missing")
            problems["target_sku_missing"] += 1
            rows.append(row)
            continue

        row["category_match"] = product.get("category") == goal.get("category")
        if not row["category_match"]:
            row["issues"].append("category_mismatch")
            problems["category_mismatch"] += 1

        instruction = goal.get("instruction_full") or g.get("instruction_full") or ""
        budget = compile_budget(instruction)
        row["budget"] = {
            "status": budget["status"],
            "upper": budget["upper"],
            "lower": budget["lower"],
            "quote": budget["quote"],
            "reason": budget.get("reason"),
            "frozen_price_upper": goal.get("price_upper"),
        }
        budget_status[budget["status"]] += 1
        if budget["status"] == "parse_failed":
            row["issues"].append("budget_parse_failed")
            problems["budget_parse_failed"] += 1
        frozen = goal.get("price_upper")
        expected = budget["upper"] if budget["status"] == "declared" else None
        if frozen != expected and not (frozen is None and expected is None):
            row["issues"].append("frozen_price_upper_differs_from_recompile")
            budget_recompile_diff.append(
                {
                    "task_id": int(tid),
                    "frozen_price_upper": frozen,
                    "recompiled_upper": expected,
                    "recompile_status": budget["status"],
                    "quote": budget["quote"],
                }
            )
            problems["frozen_price_upper_differs_from_recompile"] += 1

        options = option_availability(product, goal.get("required_options_by_key") or {})
        row["options"] = options
        if options["missing_axes"]:
            row["issues"].append("required_option_axis_missing_on_product")
            problems["required_option_axis_missing_on_product"] += 1
        if options["unavailable_values"]:
            row["issues"].append("required_option_value_unavailable")
            problems["required_option_value_unavailable"] += 1

        axes = effective_price_axes(product)
        has_combinations = isinstance(product.get("variant_combinations"), list)
        verifiable = has_combinations or len(axes) <= 1
        row["price_verifiable"] = bool(verifiable)
        if not verifiable:
            row["issues"].append("multi_axis_without_variant_combinations")
            problems["multi_axis_without_variant_combinations"] += 1
            unverifiable_price.append(
                {
                    "task_id": int(tid),
                    "asin": asin,
                    "effective_price_axes": axes,
                }
            )
        rows.append(row)

    report = {
        "benchmark_id": manifest.get("benchmark_id"),
        "task_count": len(goals),
        "checked_against_items": str(Path(args.items)),
        "budget_compile_status": dict(budget_status),
        "problem_counts": dict(problems),
        "budget_recompile_diff": budget_recompile_diff,
        "unverifiable_price_products": unverifiable_price,
        "rows": rows,
        "notes": [
            "本预检只读，不修改 benchmark；数据问题需从可信源补数据或在下一次预检阶段隔离说明。",
            "budget_parse_failed 表示用户声明了预算但无法可靠编译，重跑时预算门槛判不可核验，不得冒充未声明。",
            "frozen_price_upper_differs_from_recompile 表示冻结 goal 的预算口径与 budget-compile-v2 不同，属于历史口径差异，只在新协议重跑时生效。",
        ],
    }

    out = Path(args.out) if args.out else (
        ROOT / "reports" / "benchmark-precheck" / f"{bench_dir.name}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(
        {
            "task_count": len(goals),
            "budget_compile_status": dict(budget_status),
            "problem_counts": dict(problems),
            "budget_recompile_diff_count": len(budget_recompile_diff),
            "unverifiable_price_count": len(unverifiable_price),
        },
        ensure_ascii=False, indent=2,
    ))
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
