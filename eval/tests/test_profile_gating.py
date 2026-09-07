#!/usr/bin/env python3
"""profile 门控（h0=E0 基线 / h1=E1）确定性单测。

覆盖：
- harness/h1/cordis.patch.yml 里每个 insert.name 都指向有 apply 的模块；
- resolve_runtime_controls 对 h0/h1/未知 profile/mode 的开关矩阵正确；
- run_purchase_verifier 对真实 263/510/51 回执的确定性判定（不调 LLM）；
- classify_turn 单次执行后的诊断分类（不再驱动重跑）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.interaction_router import (  # noqa: E402
    DECISION_ASK,
    classify_turn,
)

import scripts.run_benchmark as rb  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_inserts(yaml_text: str) -> list[str]:
    """从 cordis.patch.yml 提取 insert 块的 name（YAML 子集解析，无依赖）。"""
    names = []
    in_insert = False
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if stripped == "- insert:":
            in_insert = True
            continue
        if in_insert:
            if stripped.startswith("- id:"):
                continue
            if stripped.startswith("name:"):
                names.append(stripped.split(":", 1)[1].strip().strip("'\""))
                continue
            # 空行或回到顶层条目 → 结束当前 insert 块
            if not stripped.startswith("-") and not stripped.startswith("name:"):
                in_insert = False
    return names


class ProfileGatingTest(unittest.TestCase):
    def test_runtime_controls_matrix(self):
        self.assertEqual(rb.resolve_runtime_controls("h0", "auto"), {
            "purchase_verifier": False})
        self.assertEqual(rb.resolve_runtime_controls("h1", "auto"), {
            "purchase_verifier": True})
        self.assertEqual(rb.resolve_runtime_controls("unknown", "auto"), {
            "purchase_verifier": False})
        self.assertEqual(rb.resolve_runtime_controls("h0", "on"), {
            "purchase_verifier": True})
        self.assertEqual(rb.resolve_runtime_controls("h1", "off"), {
            "purchase_verifier": False})

    def test_runtime_controls_from_profile_json(self):
        # auto 模式应读取 harness/<profile>/runtime_controls.json，而非仅内置表。
        self.assertEqual(rb.load_profile_runtime_controls("h0"), {
            "purchase_verifier": False})
        self.assertEqual(rb.load_profile_runtime_controls("h1"), {
            "purchase_verifier": True})
        # 未知 profile 无 json → None（回退内置表，仍全 off）。
        self.assertIsNone(rb.load_profile_runtime_controls("does_not_exist"))

    def test_profile_json_matches_builtin_table(self):
        # 声明文件（json）与内置回退表保持一致，防止两处漂移。
        for prof in ("h0", "h1"):
            declared = rb.load_profile_runtime_controls(prof)
            self.assertIsNotNone(declared)
            self.assertEqual(declared, rb.PROFILE_RUNTIME_CONTROLS[prof])

    def test_h1_yaml_only_registers_real_plugins(self):
        yaml_text = (ROOT / "harness" / "h1" / "cordis.patch.yml").read_text(
            encoding="utf-8")
        names = extract_inserts(yaml_text)
        self.assertEqual(names, ["@shopping-longhorizon/shop-tools/buy-guard"])
        # buy-guard 是真插件（有 inject + apply）。
        src = (ROOT / "src" / "buy-guard.js").read_text(encoding="utf-8")
        self.assertIn("export const inject", src)
        self.assertIn("export function apply", src)

    def test_h0_yaml_has_no_insert(self):
        yaml_text = (ROOT / "harness" / "h0" / "cordis.patch.yml").read_text(
            encoding="utf-8")
        self.assertEqual(extract_inserts(yaml_text), [])

    def test_package_exports_no_completion_gate(self):
        pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertNotIn("./completion-gate", pkg["exports"])

    def _load_purchase(self, tid):
        d = json.loads(
            (ROOT / "runs" / "h0-0905-1446" / "traces"
             / f"{tid}.raw_trace.json").read_text(encoding="utf-8"))
        raw_steps = d.get("steps") or []
        term = rb._compute_terminal_v2(raw_steps)
        purchase = term.get("purchase") or {}
        price_resolution = None
        for s in raw_steps:
            raw = s.get("raw") or {}
            if raw.get("done"):
                rd = raw.get("reward_detail") or {}
                ev = rd.get("evidence") if isinstance(rd.get("evidence"), dict) else None
                price_resolution = (ev or {}).get("price_resolution")
                break
        return d.get("task", "").split("\n")[0], purchase, price_resolution

    def test_purchase_verifier_263_510_51(self):
        # 263：100 根 vs 50 支 → quantity violated
        t263, p263, pr263 = self._load_purchase(263)
        v263 = rb.run_purchase_verifier(263, t263, p263, pr263)
        self.assertEqual(v263["quantity"]["status"], "violated")
        # 510：单只约1元，五只共5元 → price satisfied（单位换算正确）
        t510, p510, pr510 = self._load_purchase(510)
        v510 = rb.run_purchase_verifier(510, t510, p510, pr510)
        self.assertEqual(v510["price"]["status"], "satisfied")
        # 51：多轴无组合价 → price unknown（不猜价）
        t51, p51, pr51 = self._load_purchase(51)
        v51 = rb.run_purchase_verifier(51, t51, p51, pr51)
        self.assertEqual(v51["price"]["status"], "unknown")

    def test_case47_router_gating(self):
        # case 47 式：环境未终局、无回执、模型最终文字是追问但未调 ask_shopper。
        # 单次执行后，classify_turn 只作诊断记录（ask），不再驱动重跑。
        mt = {"task": "买酒红色银莲花", "steps": [
            {"step": 1, "tool_name": "search", "tool_args": {},
             "observation": "搜索结果"},
        ]}
        rt = {"terminal": {"done": False, "purchase": {}}, "steps": []}
        final_text = "酒红色单球19元，请问您需要购买几个酒红色单球呢？"
        decision = classify_turn(mt, rt, final_text)
        self.assertEqual(decision["decision"], DECISION_ASK)


if __name__ == "__main__":
    unittest.main()
