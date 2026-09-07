#!/usr/bin/env python3
"""rubric v2 端到端链路集成测试（真实 CLI 串联，mock judge，无 LLM）。

链路：事件（fixture）→ gen_rubric_v2（时间线编译）→ judge v2（mock）→
report v2（来源/分母/版本可解释）。

验收场景：
- 916 条件报价许可（不泛化、不 supersede 全局预算）；
- 1092 新增硬要求（首次透露前不追罚，不从 +60% 猜调光）；
- 6 候选匹配证据与最终满足分离（未购买 → unknown，候选证据保留）；
- 196 预算不丢失；
- 263 数量约束不被宽松规则洗掉；
- 343 修改/替换生命周期（初始被替换，不重复计入最终分母）。

运行：
  python3 eval/tests/test_rubric_v2_chain.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TASK_IDS = [916, 1092, 6, 196, 263, 343]

QUERIES = {
    916: "买一瓶中药胶囊，预算150元左右。",
    1092: "帮我找一个护眼台灯。",
    6: "想要红色手柄的水暖排气阀，价格在30元左右。",
    196: "想给小孩买蓝色打底衫，价格不超过20块钱。",
    263: "帮我找一百根玻璃纤维杆子，预算两百块左右。",
    343: "想要一件蓝色的连衣裙。",
}

BASE_RUBRICS = {
    916: [
        {"id": "c0001", "description": "价格不超过165元", "hardness": "hard",
         "source": "initial_query", "query_quote": "预算150元左右"},
        {"id": "c0002", "description": "商品品类为胶囊", "hardness": "hard",
         "source": "initial_query", "query_quote": "胶囊"},
    ],
    1092: [
        {"id": "c0001", "description": "商品品类为台灯", "hardness": "hard",
         "source": "initial_query", "query_quote": "台灯"},
    ],
    6: [
        {"id": "c0001", "description": "商品品类为水暖排气阀", "hardness": "hard",
         "source": "initial_query", "query_quote": "水暖排气阀"},
        {"id": "c0002", "description": "手柄颜色为红色", "hardness": "hard",
         "source": "initial_query", "query_quote": "红色手柄"},
        {"id": "c0003", "description": "价格预期约30元", "hardness": "soft",
         "source": "initial_query", "query_quote": "30元左右"},
    ],
    196: [
        {"id": "c0001", "description": "价格不超过20元", "hardness": "hard",
         "source": "initial_query", "query_quote": "价格不超过20块钱"},
        {"id": "c0002", "description": "商品品类为打底衫", "hardness": "hard",
         "source": "initial_query", "query_quote": "打底衫"},
    ],
    263: [
        {"id": "c0001", "description": "数量为100根", "hardness": "hard",
         "source": "initial_query", "query_quote": "一百根"},
        {"id": "c0002", "description": "预算约两百元", "hardness": "soft",
         "source": "initial_query", "query_quote": "两百块左右"},
    ],
    343: [
        {"id": "c0001", "description": "颜色为蓝色", "hardness": "hard",
         "source": "initial_query", "query_quote": "蓝色"},
        {"id": "c0002", "description": "风格为日系", "hardness": "soft",
         "source": "initial_query", "query_quote": "日系"},
    ],
}

# model_trace steps（每条一个工具调用，observation 为模型可见文本）。
MODEL_TRACES = {
    916: [
        {"step": 1, "tool_name": "search", "tool_args": {"keywords": "中药胶囊"},
         "observation": "搜索结果：中药胶囊 港版 多个候选"},
        {"step": 2, "tool_name": "ask_shopper",
         "tool_args": {"question": "港版中药版100粒，168元可以吗？"},
         "observation": "168可以，但要确认是香港进口中药版100粒。"},
        {"step": 3, "tool_name": "click", "tool_args": {"value": "A916"},
         "observation": "香港进口中药版100粒 胶囊 价格 168"},
        {"step": 4, "tool_name": "click", "tool_args": {"value": "Buy Now"},
         "observation": "Episode finished."},
    ],
    1092: [
        {"step": 1, "tool_name": "search", "tool_args": {"keywords": "台灯"},
         "observation": "搜索结果：台灯 多个候选"},
        {"step": 2, "tool_name": "ask_shopper",
         "tool_args": {"question": "需要调光吗？"},
         "observation": "必须能调光，这是硬要求。"},
        {"step": 3, "tool_name": "click", "tool_args": {"value": "P1092"},
         "observation": "台灯 亮度 +60%"},
        {"step": 4, "tool_name": "finish",
         "tool_args": {"reason": "no_suitable_product"},
         "observation": "Episode finished."},
    ],
    6: [
        {"step": 1, "tool_name": "search",
         "tool_args": {"keywords": "水暖排气阀 红色手柄"},
         "observation": "搜索结果：水暖排气阀 红色手柄 多个候选"},
        {"step": 2, "tool_name": "click", "tool_args": {"value": "743144367770"},
         "observation": "6分大流量（红色把手） 水暖排气阀 价格 33"},
        {"step": 3, "tool_name": "click",
         "tool_args": {"value": "6分大流量（红色把手）"},
         "observation": "Episode finished."},
    ],
    196: [
        {"step": 1, "tool_name": "search",
         "tool_args": {"keywords": "儿童打底衫 蓝色"},
         "observation": "儿童蓝色打底衫 价格不超过20块钱 多个候选"},
        {"step": 2, "tool_name": "click", "tool_args": {"value": "P196"},
         "observation": "蓝色打底衫 价格 18元"},
        {"step": 3, "tool_name": "click", "tool_args": {"value": "Buy Now"},
         "observation": "Episode finished."},
    ],
    263: [
        {"step": 1, "tool_name": "search",
         "tool_args": {"keywords": "玻璃纤维杆"},
         "observation": "搜索结果：玻璃纤维杆 多个候选"},
        {"step": 2, "tool_name": "click", "tool_args": {"value": "P263"},
         "observation": "玻璃纤维杆 50支装 价格 25"},
        {"step": 3, "tool_name": "click", "tool_args": {"value": "Buy Now"},
         "observation": "Episode finished."},
    ],
    343: [
        {"step": 1, "tool_name": "search", "tool_args": {"keywords": "蓝色连衣裙"},
         "observation": "搜索结果：蓝色连衣裙 多个候选"},
        {"step": 2, "tool_name": "ask_shopper",
         "tool_args": {"question": "蓝色可以吗？"},
         "observation": "改成雾蓝色，要挺括，日系通勤风。"},
        {"step": 3, "tool_name": "click", "tool_args": {"value": "A343"},
         "observation": "雾蓝色 连衣裙"},
        {"step": 4, "tool_name": "finish",
         "tool_args": {"reason": "no_suitable_product"},
         "observation": "Episode finished."},
    ],
}

# fixture 事件（mock 提取产物，直接给出，标注 retrospective）。
EVENTS = {
    916: [
        {"event_id": "916/e1", "kind": "conditional_accept",
         "requirement_key": "budget", "new_value": 168.0,
         "scope": {"asin": "A916", "price": 168.0, "option_spec": "中药版100粒"},
         "source_reply_id": "qa-0001",
         "source_quote": "168可以，但要确认是香港进口中药版100粒",
         "step_index": 1},
    ],
    1092: [
        {"event_id": "1092/e1", "kind": "add",
         "requirement_key": "function",
         "new_value": {"description": "必须能调光", "hardness": "hard"},
         "source_reply_id": "qa-0001", "source_quote": "必须能调光",
         "step_index": 1},
    ],
    343: [
        {"event_id": "343/e1", "kind": "modify", "requirement_key": "color",
         "new_value": "雾蓝色", "source_reply_id": "qa-0001",
         "source_quote": "改成雾蓝色", "step_index": 1},
        {"event_id": "343/e2", "kind": "add", "requirement_key": "silhouette",
         "new_value": {"description": "版型挺括", "hardness": "soft"},
         "source_reply_id": "qa-0001", "source_quote": "要挺括",
         "step_index": 1},
        {"event_id": "343/e3", "kind": "reject", "requirement_key": "candidate",
         "new_value": "不要这件", "scope": {"asin": "A343"},
         "source_reply_id": "qa-0001", "source_quote": "日系通勤风",
         "step_index": 2},
    ],
}

# 决策时刻（buy now / finish / hard_stop）与第一次终局位置。
TERMINAL_POS = {916: 3, 1092: 3, 6: 2, 196: 2, 263: 2, 343: 3}


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cli(args, cwd=ROOT):
    proc = subprocess.run(
        [sys.executable, *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=300,
    )
    return proc


class RubricV2ChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rubric_v2_chain_"))
        bench = cls.tmp / "benchmark"
        write(bench / "manifest.json", {
            "benchmark_id": "fixture-bench", "task_ids": TASK_IDS,
            "task_count": len(TASK_IDS),
            "environment_version": "shopsimulator-environment-v2.1",
        })
        with (bench / "tasks.jsonl").open("w", encoding="utf-8") as f:
            for tid in TASK_IDS:
                f.write(json.dumps({
                    "task_id": tid, "query": QUERIES[tid],
                    "metadata": {"domain": "测试域", "complexity": "medium"},
                }, ensure_ascii=False) + "\n")
        for tid in TASK_IDS:
            write(bench / "rubrics" / f"{tid}.json", {
                "task_id": str(tid), "rubric_version": "v1", "frozen": True,
                "query_mode": "initial_instruction_simple",
                "query": QUERIES[tid],
                "constraints": BASE_RUBRICS[tid],
            })
        for tid in TASK_IDS:
            write(cls.tmp / "traces" / f"{tid}.model_trace.json", {
                "task": QUERIES[tid], "steps": MODEL_TRACES[tid],
                "terminal_protocol": "terminal-protocol-v2",
            })
        for tid, evs in EVENTS.items():
            write(cls.tmp / "events" / f"{tid}.events.json", {
                "schema_version": "shopping-requirement-events-v1",
                "event_source": "retrospective",
                "benchmark_id": "fixture-bench", "run_id": "fixture-run",
                "task_id": str(tid), "trial_id": "t0",
                "terminal_boundary": {
                    "first_terminal_position": TERMINAL_POS[tid],
                    "protocol": "terminal-protocol-v2",
                },
                "ask_shopper_qa": [],
                "events": evs,
                "extractor": {"name": "mock", "version": "mock-v1"},
            })
        # 确定性评测结果（供 report v2 的环境/行为面板）。
        rows = []
        for tid in TASK_IDS:
            rows.append({
                "task_id": tid,
                "outcome": {"class": "repeat_loop" if tid == 6 else "success_gold",
                            "reward": 1.0 if tid != 6 else -0.65,
                            "reward_type": "repeat_loop" if tid == 6
                            else "gold_purchase"},
                "environment_done": True,
                "task_success": tid != 6,
                "step_count": len(MODEL_TRACES[tid]),
                "first_terminal": {"reward_valid": True},
                "legacy_canonical_terminal": {"reward_valid": True},
                "anomalies": [], "anomaly_details": [],
                "failure": None,
            })
        det = cls.tmp / "deterministic"
        det.mkdir(parents=True, exist_ok=True)
        with (det / "task_results.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        write(det / "summary.json", {"total": len(TASK_IDS),
                                     "terminal_protocol": "terminal-protocol-v2"})

        # 运行链路：gen_rubric_v2 → judge v2(mock) → report v2
        cls.rubrics_v2 = cls.tmp / "rubrics_v2"
        cls.judgments_v2 = cls.tmp / "judgments_v2"
        cls.report_out = cls.tmp / "report"

        p1 = run_cli([
            "eval/gen_rubric_v2.py", "--benchmark", str(bench),
            "--base-rubrics", str(bench / "rubrics"),
            "--events", str(cls.tmp / "events"),
            "--traces", str(cls.tmp / "traces"),
            "--run-id", "fixture-run", "--out", str(cls.rubrics_v2),
        ])
        assert p1.returncode == 0, f"gen_rubric_v2 failed:\n{p1.stderr}"

        p2 = run_cli([
            "eval/judge.py", "--rubric-version", "v2",
            "--rubrics-v2", str(cls.rubrics_v2),
            "--traces", str(cls.tmp / "traces"),
            "--judge-mode", "mock", "--out", str(cls.judgments_v2),
        ])
        assert p2.returncode == 0, f"judge v2 failed:\n{p2.stderr}"

        p3 = run_cli([
            "eval/report.py", "--rubric-version", "v2",
            "--benchmark", str(bench),
            "--deterministic", str(det),
            "--rubrics-v2", str(cls.rubrics_v2),
            "--judgments-v2", str(cls.judgments_v2),
            "--events", str(cls.tmp / "events"),
            "--out", str(cls.report_out),
        ])
        assert p3.returncode == 0, f"report v2 failed:\n{p3.stderr}"

    def _rubric(self, tid):
        return json.loads(
            (self.rubrics_v2 / f"{tid}.rubric_v2.json").read_text(encoding="utf-8"))

    def _judgment(self, tid):
        return json.loads(
            (self.judgments_v2 / f"{tid}.json").read_text(encoding="utf-8"))

    def test_916_conditional_accept_not_supersede_global(self):
        rub = self._rubric(916)
        # 全局预算约束仍是 active（单笔许可不把它标为 superseded）。
        budget = [c for c in rub["constraints"]
                  if c["requirement_key"] == "budget"]
        self.assertTrue(any(c["lifecycle_status"] == "active" for c in budget))
        self.assertIn("c0001", rub["final_constraint_ids"])
        # 条件许可单独记录，带精确 scope。
        perms = rub["conditional_permissions"]
        self.assertEqual(len(perms), 1)
        self.assertEqual(perms[0]["scope"]["asin"], "A916")
        self.assertEqual(perms[0]["scope"].get("price", perms[0]["scope"].get("amount")), 168.0)
        self.assertEqual(perms[0]["lifecycle_status"], "active")
        # Judge：条件许可单独展示，不产生新约束满足项。
        judg = self._judgment(916)
        verdict_ids = {v["constraint_id"]
                       for v in judg["judgment"]["final_requirement_verdicts"]}
        self.assertEqual(verdict_ids, set(rub["final_constraint_ids"]))

    def test_1092_new_hard_requirement_not_guessed_from_plus60(self):
        rub = self._rubric(1092)
        dim = [c for c in rub["constraints"]
               if c["requirement_key"] == "function"]
        self.assertEqual(len(dim), 1)
        self.assertEqual(dim[0]["hardness"], "hard")
        self.assertEqual(dim[0]["source"], "shopper_clarification")
        self.assertIn(dim[0]["id"], rub["final_constraint_ids"])
        # Judge：不能从「+60%」猜出调光 → 无候选证据、最终非 satisfied。
        judg = self._judgment(1092)
        verdicts = {v["constraint_id"]: v
                    for v in judg["judgment"]["final_requirement_verdicts"]}
        dim_verdict = verdicts[dim[0]["id"]]
        self.assertEqual(dim_verdict["status"], "unknown")
        cand = [c for c in judg["judgment"]["candidate_evidence"]
                if c["constraint_id"] == dim[0]["id"]]
        self.assertEqual(cand, [])

    def test_6_candidate_evidence_separated_from_final(self):
        judg = self._judgment(6)
        verdicts = {v["constraint_id"]: v
                    for v in judg["judgment"]["final_requirement_verdicts"]}
        # 未购买 → 最终满足状态 unknown，但候选证据保留。
        self.assertEqual(verdicts["c0002"]["status"], "unknown")
        cand_ids = {c["constraint_id"]
                    for c in judg["judgment"]["candidate_evidence"]}
        self.assertIn("c0002", cand_ids)  # 红色把手候选证据存在
        self.assertIn("c0001", cand_ids)  # 品类候选证据存在
        # 环境未成功与候选证据并存，不互相替代。
        report = json.loads(
            (self.report_out / "report_v2.json").read_text(encoding="utf-8"))
        task6 = report["tasks"]["6"]
        self.assertFalse(task6["panel1_environment"]["task_success"])
        self.assertTrue(task6["panel2_rubric"]["candidate_evidence"])

    def test_196_budget_not_lost(self):
        rub = self._rubric(196)
        budget = [c for c in rub["constraints"]
                  if c["requirement_key"] == "budget"]
        self.assertEqual(len(budget), 1)
        self.assertEqual(budget[0]["source_quote"], "价格不超过20块钱")
        self.assertIn("c0001", rub["final_constraint_ids"])

    def test_263_quantity_constraint_not_washed_away(self):
        rub = self._rubric(263)
        qty = [c for c in rub["constraints"]
               if c["requirement_key"] == "quantity"]
        self.assertEqual(len(qty), 1)
        self.assertEqual(qty[0]["hardness"], "hard")
        self.assertIn(qty[0]["id"], rub["final_constraint_ids"])
        # 实际 50 支 ≠ 100 根：不能被 mock 判为 satisfied。
        judg = self._judgment(263)
        verdicts = {v["constraint_id"]: v
                    for v in judg["judgment"]["final_requirement_verdicts"]}
        self.assertEqual(verdicts[qty[0]["id"]]["status"], "unknown")

    def test_343_supersede_lifecycle_and_denominator(self):
        rub = self._rubric(343)
        by_id = {c["id"]: c for c in rub["constraints"]}
        # 初始蓝色被雾蓝替换 → superseded，不进入最终集合。
        self.assertEqual(by_id["c0001"]["lifecycle_status"], "superseded")
        self.assertNotIn("c0001", rub["final_constraint_ids"])
        # 新增的雾蓝与挺括在最终集合。
        new_color = [c for c in rub["constraints"]
                     if c["requirement_key"] == "color"
                     and c["id"] != "c0001"]
        self.assertEqual(len(new_color), 1)
        self.assertIn(new_color[0]["id"], rub["final_constraint_ids"])
        self.assertIn(new_color[0]["supersedes"] in ("c0001", ["c0001"]),
                      (True,))
        # report 生命周期计数：至少一个 superseded。
        summary = json.loads(
            (self.report_out / "summary_v2.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(
            summary["panel2"]["lifecycle_counts"].get("superseded", 0), 1)

    def test_report_denominators_and_sources(self):
        summary = json.loads(
            (self.report_out / "summary_v2.json").read_text(encoding="utf-8"))
        p2 = summary["panel2"]
        # 分母明确、非负；满足率与分母一致或为 null。
        self.assertGreaterEqual(p2["final_satisfaction_denominator"], 0)
        self.assertEqual(p2["conditional_permissions"]["total"], 1)
        self.assertIn("initial_reference_satisfaction_rate", p2)
        # judge_mode 标注为 mock（不冒充真实模型成绩）。
        self.assertIn("mock", summary["metadata"]["judge_mode"])

    def test_resume_cache_by_input_fingerprint(self):
        # 再次运行 gen_rubric_v2：输入未变 → 命中缓存，不重新编译。
        p = run_cli([
            "eval/gen_rubric_v2.py", "--benchmark", str(self.tmp / "benchmark"),
            "--base-rubrics", str(self.tmp / "benchmark" / "rubrics"),
            "--events", str(self.tmp / "events"),
            "--traces", str(self.tmp / "traces"),
            "--run-id", "fixture-run", "--out", str(self.rubrics_v2),
        ])
        self.assertEqual(p.returncode, 0)
        manifest = json.loads(
            (self.rubrics_v2 / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["skipped_cache"]), len(TASK_IDS))


if __name__ == "__main__":
    unittest.main()
