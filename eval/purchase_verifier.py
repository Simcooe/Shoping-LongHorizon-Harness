#!/usr/bin/env python3
"""确定性购买字段校验器（purchase_verifier，B4）。

把数量 / 单位 / 价格等明确规则交给程序，不交给语义审计。纯函数、
可独立测试，无 LLM / 环境依赖。

输出四态（与任务要求一致）：
  satisfied / violated / unknown / not_applicable
并绑定 requirement_id 与证据引用。

核心原则（不得违反）：
- 数量：需求数量 vs 购买规格数量（50 支 ≠ 100 根），单位换算
  （只/根/盒/支/包）；无合法数量动作时绝不构造「买两包」假设。
- 价格：单位价 vs 总价（五只共 5 元 ≈ 单只 1 元）；不得用最低价 /
  起价（display_base_price）冒充成交价；无可信组合价 → unknown。
"""

from __future__ import annotations

import re

VERDICT_SATISFIED = "satisfied"
VERDICT_VIOLATED = "violated"
VERDICT_UNKNOWN = "unknown"
VERDICT_NOT_APPLICABLE = "not_applicable"
VERDICTS = {VERDICT_SATISFIED, VERDICT_VIOLATED, VERDICT_UNKNOWN, VERDICT_NOT_APPLICABLE}

# 可数单位按语义归组，组内可换算/比较；容器单位不与可数单位直接比较。
_UNIT_GROUPS = {
    "根": "rod", "支": "rod", "条": "rod",
    "个": "item", "只": "item", "件": "item", "枚": "item",
    "盒": "container", "瓶": "container", "包": "container",
    "袋": "container", "罐": "container", "箱": "container", "套": "container",
    "粒": "grain", "片": "tablet", "颗": "grain", "卷": "roll",
    "双": "pair", "对": "pair", "张": "sheet",
}

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_PLACE = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def unit_group(unit: str | None) -> str | None:
    if not unit:
        return None
    return _UNIT_GROUPS.get(unit.strip(), unit.strip())


def units_compatible(unit_a: str | None, unit_b: str | None) -> bool:
    ga = unit_group(unit_a)
    gb = unit_group(unit_b)
    if ga is None or gb is None:
        return False
    if ga == "container" or gb == "container":
        return ga == gb
    return ga == gb


def chinese_numeral_to_int(text: str) -> int | None:
    s = str(text or "").strip()
    if not s:
        return None
    total = 0
    section = 0
    number = 0
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch in _CN_PLACE:
            place = _CN_PLACE[ch]
            if place >= 10000:
                section = (section + number) * place
                total += section
                section = 0
                number = 0
            else:
                if number == 0:
                    number = 1
                section += number * place
                number = 0
        else:
            return None
    total += section + number
    return total if s else None


def _arabic_number(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _first_number(text: str) -> float | None:
    value = _arabic_number(text)
    if value is not None:
        return value
    m = re.search(r"[零一二两三四五六七八九十百千万]+", text)
    if not m:
        return None
    return chinese_numeral_to_int(m.group(0))


_UNIT_RE = "(根|支|条|个|只|件|枚|盒|瓶|包|袋|罐|箱|套|粒|片|颗|卷|双|对|张)"


def parse_quantities(text: str) -> list[dict]:
    """从规格文本确定性解析「<数量><单位>」序列。返回 [{value, unit, raw}]。"""
    if not isinstance(text, str):
        return []
    s = text.strip()
    matches: list[dict] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*" + _UNIT_RE + r"\s*(装)?", s):
        matches.append({"value": float(m.group(1)), "unit": m.group(2),
                        "inner": bool(m.group(3)), "raw": m.group(0)})
    for m in re.finditer(r"([零一二两三四五六七八九十百千万]+)\s*" + _UNIT_RE + r"\s*(装)?", s):
        value = chinese_numeral_to_int(m.group(1))
        if value is None:
            continue
        matches.append({"value": float(value), "unit": m.group(2),
                        "inner": bool(m.group(3)), "raw": m.group(0)})
    return matches


def units_per_pack_from_text(text: str) -> dict | None:
    """取规格文本里第一个「可数单位」数量作为每包/每规格数量。

    - 优先取非容器单位（根/支/只/个/粒/片…）；
    - 容器单位（盒/包/袋…）不当作可数数量；
    - 「100粒装」的「装」标记为容器内数量，也可采用。
    """
    matches = parse_quantities(text)
    for m in matches:
        if unit_group(m["unit"]) != "container":
            return m
    for m in matches:
        if m.get("inner"):
            return m
    return None


def parse_price_expression(text: str) -> dict | None:
    """解析「五只共5元」「5只装5元」「1元/只」等价格语义。

    返回 {count, unit, total, unit_price, raw}（能解析多少给多少）。
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    out: dict = {"count": None, "unit": None, "total": None, "unit_price": None, "raw": s}

    m = re.search(
        r"([零一二两三四五六七八九十百千万]+|\d+(?:\.\d+)?)\s*" + _UNIT_RE +
        r"(?:装)?\s*[共总]?(?:共价|总价)?\s*"
        r"([零一二两三四五六七八九十百千万]+|\d+(?:\.\d+)?)\s*元",
        s,
    )
    if m:
        out["count"] = _first_number(m.group(1))
        out["unit"] = m.group(2)
        total = _first_number(m.group(3))
        out["total"] = float(total) if total is not None else None
        if out["count"] and out["total"] is not None and out["count"] > 0:
            out["unit_price"] = out["total"] / out["count"]
        return out

    m = re.search(r"(\d+(?:\.\d+)?)\s*元\s*/\s*" + _UNIT_RE, s)
    if m:
        out["unit_price"] = float(m.group(1))
        out["unit"] = m.group(2)
        return out
    return None


def _unit_price_from_text(text: str) -> float | None:
    """提取「单只价格在一元左右 / 单价1元 / 每只约1元」的单位价。

    取紧邻单位价关键词后面的金额（支持中文数字），避免取到句中其它数字。
    """
    if not isinstance(text, str):
        return None
    m = re.search(
        r"(?:单价|单只|单个|每个|每只|每支|每根|每盒|一只|一支|一根|一盒|一件)"
        r"[^，。；;]*?([零一二两三四五六七八九十百千万]+|\d+(?:\.\d+)?)\s*元",
        text,
    )
    if not m:
        return None
    return _first_number(m.group(1))


def _evidence(required_id, requirement_key, requirement_version, **extra):
    return {
        "required_id": required_id,
        "requirement_key": requirement_key,
        "requirement_version": requirement_version,
        **extra,
    }


def verify_quantity(required_id, requirement_version, required_text,
                    selected_options, order_quantity=None):
    """确定性数量核算。

    required_text：需求数量文本（如「一百根」「100根」「5个」）。
    selected_options：购买规格（dict）或空。
    order_quantity：显式下单数量（默认 None，表示未提供数量工具/动作）。

    - 需求数量不可解析 → unknown；
    - 无合法数量动作且规格数量 < 需求数量 → violated（绝不构造多包假设）；
    - 规格可数数量 >= 需求数量且单位兼容 → satisfied；
    - 有显式 order_quantity → 按 规格数量×下单数量 核算。
    """
    req = units_per_pack_from_text(required_text)
    if req is None:
        return {"status": VERDICT_UNKNOWN, "reason": "required_quantity_unparseable",
                **_evidence(required_id, "quantity", requirement_version,
                            required_text=required_text)}

    opts = selected_options if isinstance(selected_options, dict) else {}
    option_text = " ".join(str(v) for v in opts.values())
    pack = units_per_pack_from_text(option_text)

    if pack is None:
        # 没有可数数量规格：既无法证明满足，也不能凭空判违反。
        return {"status": VERDICT_UNKNOWN, "reason": "pack_quantity_unavailable",
                "required": req, "selected_options": opts,
                **_evidence(required_id, "quantity", requirement_version,
                            required_text=required_text)}

    required_value = float(req["value"])
    pack_value = float(pack["value"])
    if not units_compatible(req["unit"], pack["unit"]):
        return {"status": VERDICT_UNKNOWN, "reason": "unit_incompatible",
                "required": req, "pack": pack, "selected_options": opts,
                **_evidence(required_id, "quantity", requirement_version,
                            required_text=required_text)}

    if order_quantity is not None:
        try:
            total_value = pack_value * float(order_quantity)
        except (TypeError, ValueError):
            return {"status": VERDICT_UNKNOWN, "reason": "order_quantity_invalid",
                    "required": req, "pack": pack, "order_quantity": order_quantity,
                    **_evidence(required_id, "quantity", requirement_version,
                                required_text=required_text)}
        status = VERDICT_SATISFIED if total_value >= required_value else VERDICT_VIOLATED
        return {"status": status, "required": req, "pack": pack,
                "order_quantity": order_quantity, "total_units": total_value,
                "reason": "total_units_vs_required",
                **_evidence(required_id, "quantity", requirement_version,
                            required_text=required_text)}

    # 无合法数量动作：单个规格数量就是本次购买的最大可交付数量。
    status = VERDICT_SATISFIED if pack_value >= required_value else VERDICT_VIOLATED
    return {"status": status, "required": req, "pack": pack,
            "order_quantity": None, "total_units": pack_value,
            "reason": "no_legal_multi_pack_action_assumed",
            **_evidence(required_id, "quantity", requirement_version,
                        required_text=required_text)}


def verify_price(required_id, requirement_version, required_text,
                 price_resolution, selected_options=None, display_base_price=None):
    """确定性价格核验。

    - price_resolution 不是 PASS 或无 price → unknown（不回填起价/最低价）；
    - required_text 含「总价」语义（如 五只共5元）→ 按单位价核验；
    - 含「单只/每只/一只」语义 → 直接比较单价；
    - 否则按绝对价格比较（unit_price=None 时退化为总价口径，需
      明确标注 evidence，由调用方决定是否足够）。
    """
    if not isinstance(price_resolution, dict):
        return {"status": VERDICT_UNKNOWN, "reason": "no_price_resolution",
                **_evidence(required_id, "price", requirement_version)}

    if price_resolution.get("status") != "pass" or price_resolution.get("price") is None:
        return {"status": VERDICT_UNKNOWN, "reason": "price_not_verified",
                "price_resolution": price_resolution,
                **_evidence(required_id, "price", requirement_version)}

    paid = float(price_resolution["price"])
    req = parse_price_expression(required_text)

    # 单位价语义：需求明确表达「单/每」价格（如 单只约1元）。
    unit_price_required = _unit_price_from_text(required_text)

    if unit_price_required is not None:
        # 需要用规格/总价信息推算出实际单价；无数量信息则 unknown。
        if not isinstance(selected_options, dict) or not selected_options:
            return {"status": VERDICT_UNKNOWN,
                    "reason": "unit_price_requires_quantity_context",
                    "required_unit_price": unit_price_required,
                    "paid": paid, "price_resolution": price_resolution,
                    **_evidence(required_id, "price", requirement_version)}
        option_text = " ".join(str(v) for v in selected_options.values())
        pack = units_per_pack_from_text(option_text)
        # 「五只共5元」：规格文本携带总价语义时的单价已由 parse_price_expression 处理。
        spec_price = parse_price_expression(option_text)
        if spec_price and spec_price.get("unit_price") is not None:
            actual_unit = float(spec_price["unit_price"])
            status = (VERDICT_SATISFIED
                      if _near(actual_unit, unit_price_required)
                      else VERDICT_VIOLATED)
            return {"status": status, "reason": "spec_unit_price_vs_required",
                    "required_unit_price": unit_price_required,
                    "actual_unit_price": actual_unit,
                    **_evidence(required_id, "price", requirement_version)}
        if pack is None:
            return {"status": VERDICT_UNKNOWN, "reason": "pack_quantity_unavailable",
                    "required_unit_price": unit_price_required, "paid": paid,
                    **_evidence(required_id, "price", requirement_version)}
        actual_unit = paid / pack["value"] if pack["value"] else None
        if actual_unit is None:
            return {"status": VERDICT_UNKNOWN, "reason": "zero_pack_size",
                    **_evidence(required_id, "price", requirement_version)}
        status = (VERDICT_SATISFIED if _near(actual_unit, unit_price_required)
                  else VERDICT_VIOLATED)
        return {"status": status, "reason": "paid_over_pack_vs_unit_price",
                "required_unit_price": unit_price_required,
                "actual_unit_price": actual_unit, "paid": paid,
                "pack": pack,
                **_evidence(required_id, "price", requirement_version)}

    # 总价/绝对价格语义（「五只共5元」按总价比较，不进入单位价分支）。
    total_required = None
    if req and req.get("total") is not None:
        total_required = float(req["total"])
    else:
        raw = _arabic_number(required_text)
        total_required = float(raw) if raw is not None else None

    if total_required is None:
        return {"status": VERDICT_UNKNOWN, "reason": "required_price_unparseable",
                "paid": paid, "price_resolution": price_resolution,
                **_evidence(required_id, "price", requirement_version)}

    status = VERDICT_SATISFIED if _near(paid, total_required) else VERDICT_VIOLATED
    return {"status": status, "reason": "paid_vs_required_total",
            "required_total": total_required, "paid": paid,
            "display_base_price": display_base_price,
            **_evidence(required_id, "price", requirement_version)}


def _near(a, b, rtol=0.05, atol=0.02):
    try:
        return abs(float(a) - float(b)) <= atol + rtol * abs(float(b))
    except (TypeError, ValueError):
        return False


def verify_budget_against_quote(required_id, requirement_version,
                                budget_upper, price_resolution):
    """确定性预算门槛（不含动态许可；动态许可由 scope_matches_purchase 处理）。"""
    if budget_upper is None:
        return {"status": VERDICT_NOT_APPLICABLE, "reason": "no_budget_upper",
                **_evidence(required_id, "budget", requirement_version)}
    if not isinstance(price_resolution, dict) or price_resolution.get("status") != "pass" \
            or price_resolution.get("price") is None:
        return {"status": VERDICT_UNKNOWN, "reason": "price_not_verified",
                **_evidence(required_id, "budget", requirement_version)}
    paid = float(price_resolution["price"])
    upper = float(budget_upper)
    status = VERDICT_SATISFIED if paid <= upper else VERDICT_VIOLATED
    return {"status": status, "reason": "quote_vs_budget_upper",
            "budget_upper": upper, "paid": paid,
            **_evidence(required_id, "budget", requirement_version)}
