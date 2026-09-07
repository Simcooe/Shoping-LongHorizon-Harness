#!/usr/bin/env python3
"""purchase_verifier（B4）单元测试：数量 / 单位 / 价格确定性核验。

覆盖验收场景：
- 263：100 根需求 + 50 支规格 → violated（绝不构造「买两包」）；
- 510：五只共 5 元 ≈ 单只 1 元 → satisfied（单位换算正确）；
- 51：多轴无可信组合价 → unknown（不猜价、不回填起价）；
- 无合法数量动作时 readiness 语义（blocked/unknown 兜底由调用方处理）。
"""
from __future__ import annotations

import unittest

from eval.purchase_verifier import (
    VERDICT_SATISFIED,
    VERDICT_UNKNOWN,
    VERDICT_VIOLATED,
    parse_price_expression,
    parse_quantities,
    units_per_pack_from_text,
    verify_price,
    verify_quantity,
)


class ParseQuantityTest(unittest.TestCase):
    def test_50_pack_parsed(self):
        self.assertEqual(
            units_per_pack_from_text("白色5mm*2米*50支  黑色备注")["value"], 50.0)

    def test_chinese_100_rods(self):
        self.assertEqual(units_per_pack_from_text("一百根")["value"], 100.0)

    def test_container_not_counted_as_units(self):
        # 「1盒100粒装」：优先取可数内层数量（100 粒），容器本身不计。
        self.assertEqual(units_per_pack_from_text("1盒100粒装")["value"], 100.0)
        self.assertEqual(units_per_pack_from_text("1盒100粒装")["unit"], "粒")

    def test_grain_inside_container_detected(self):
        # 100粒装：优先返回可数内层数量。
        self.assertEqual(units_per_pack_from_text("thankal中药版100粒装")["unit"], "粒")
        self.assertEqual(units_per_pack_from_text("thankal中药版100粒装")["value"], 100.0)


class ParsePriceTest(unittest.TestCase):
    def test_five_for_five(self):
        r = parse_price_expression("五只共5元")
        self.assertEqual(r["count"], 5.0)
        self.assertEqual(r["total"], 5.0)
        self.assertAlmostEqual(r["unit_price"], 1.0)


class VerifyQuantityTest(unittest.TestCase):
    def test_263_50_pack_violated_not_two_packs(self):
        r = verify_quantity(
            required_id="q263", requirement_version=1,
            required_text="一百根",
            selected_options={"颜色分类": "白色5mm*2米*50支  黑色备注"},
        )
        self.assertEqual(r["status"], VERDICT_VIOLATED)
        self.assertEqual(r["pack"]["value"], 50.0)
        self.assertIsNone(r["order_quantity"])

    def test_263_explicit_two_pack_is_satisfied(self):
        # 仅当存在合法数量动作（order_quantity=2）才核算 50×2=100。
        r = verify_quantity(
            required_id="q263", requirement_version=1,
            required_text="一百根",
            selected_options={"颜色分类": "白色5mm*2米*50支"},
            order_quantity=2,
        )
        self.assertEqual(r["status"], VERDICT_SATISFIED)
        self.assertEqual(r["total_units"], 100.0)

    def test_263_matching_100_pack_satisfied(self):
        r = verify_quantity(
            required_id="q263", requirement_version=1,
            required_text="一百根",
            selected_options={"颜色分类": "实心杆5毫米粗2米长100根"},
        )
        self.assertEqual(r["status"], VERDICT_SATISFIED)

    def test_unit_incompatible_unknown(self):
        r = verify_quantity(
            required_id="q1", requirement_version=1,
            required_text="5个",
            selected_options={"颜色分类": "白色5mm*2米*50支"},
        )
        self.assertEqual(r["status"], VERDICT_UNKNOWN)

    def test_no_pack_quantity_unknown(self):
        r = verify_quantity(
            required_id="q1", requirement_version=1,
            required_text="一百根",
            selected_options={"颜色分类": "红色"},
        )
        self.assertEqual(r["status"], VERDICT_UNKNOWN)

    def test_unparseable_requirement_unknown(self):
        r = verify_quantity(
            required_id="q1", requirement_version=1,
            required_text="来一些杆子",
            selected_options={"颜色分类": "白色5mm*2米*50支"},
        )
        self.assertEqual(r["status"], VERDICT_UNKNOWN)


class VerifyPriceTest(unittest.TestCase):
    def test_51_unverifiable_price_unknown(self):
        r = verify_price(
            required_id="p51", requirement_version=1,
            required_text="预算25元左右",
            price_resolution={"status": "unverifiable", "price": None,
                              "method": "multiple_effective_price_axes"},
        )
        self.assertEqual(r["status"], VERDICT_UNKNOWN)
        self.assertEqual(r["reason"], "price_not_verified")

    def test_51_no_price_resolution_unknown(self):
        r = verify_price(
            required_id="p51", requirement_version=1,
            required_text="预算25元左右",
            price_resolution=None,
        )
        self.assertEqual(r["status"], VERDICT_UNKNOWN)

    def test_510_unit_price_conversion_satisfied(self):
        # 五只共5元 → 单价 1 元；需求「单只约1元」。
        r = verify_price(
            required_id="p510", requirement_version=1,
            required_text="单只约1元",
            price_resolution={"status": "pass", "price": 5.0},
            selected_options={"数量": "五只"},
        )
        self.assertEqual(r["status"], VERDICT_SATISFIED)
        self.assertAlmostEqual(r["actual_unit_price"], 1.0)

    def test_unit_price_mismatch_violated(self):
        r = verify_price(
            required_id="p510", requirement_version=1,
            required_text="单只约1元",
            price_resolution={"status": "pass", "price": 12.0},
            selected_options={"数量": "五只"},
        )
        self.assertEqual(r["status"], VERDICT_VIOLATED)

    def test_total_price_match(self):
        r = verify_price(
            required_id="p1", requirement_version=1,
            required_text="五只共5元",
            price_resolution={"status": "pass", "price": 5.0},
        )
        self.assertEqual(r["status"], VERDICT_SATISFIED)


if __name__ == "__main__":
    unittest.main()
