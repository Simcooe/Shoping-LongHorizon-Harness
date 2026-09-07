#!/usr/bin/env python3
"""rubric v2 schema 与时间线编译器单元测试。

运行：
  python3 eval/tests/test_requirement_schema.py
或：
  python3 -m unittest discover -s eval/tests -t eval/tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.requirement_schema import (  # noqa: E402
    EVENT_ADD,
    EVENT_CONDITIONAL_ACCEPT,
    EVENT_MODIFY,
    EVENT_REJECT,
    EVENT_REVOKE,
    EVENT_REVEAL,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_REVOKED,
    LIFECYCLE_SUPERSEDED,
    SOURCE_INITIAL_QUERY,
    SOURCE_SHOPPER_CLARIFICATION,
    ConstraintV2,
    RequirementEventV2,
    TimelineCompiler,
    validate_event,
    validate_rubric_v2,
    scope_matches_purchase,
)


def initial_constraints():
    return [
        ConstraintV2(
            id="c0001", requirement_key="budget",
            description="价格不超过165元", hardness="hard",
            source=SOURCE_INITIAL_QUERY, source_quote="预算150元左右",
            value={"upper": 165.0},
        ),
        ConstraintV2(
            id="c0002", requirement_key="category",
            description="商品品类为胶囊", hardness="hard",
            source=SOURCE_INITIAL_QUERY, source_quote="胶囊",
            value="胶囊",
        ),
    ]


def ev(event_id, kind, key, new_value, **kwargs):
    kwargs.setdefault("session", "run-x/916")
    if "source_reply" in kwargs:
        kwargs["source_reply_id"] = kwargs.pop("source_reply")
    return RequirementEventV2(
        event_id=event_id, kind=kind, requirement_key=key,
        new_value=new_value, **kwargs,
    )


class TimelineCompilerTest(unittest.TestCase):
    def test_initial_set_active(self):
        c = TimelineCompiler("s", initial_constraints())
        self.assertEqual([x.id for x in c.effective_set()], ["c0001", "c0002"])
        self.assertEqual(c.version, 0)

    def test_reveal_dedup_does_not_inflate_denominator(self):
        c = TimelineCompiler("s", initial_constraints())
        c.apply(ev("e1", EVENT_REVEAL, "color", "偏黄草绿",
                   source_quote="要偏黄草绿"))
        n1 = len(c.constraints)
        # 同语义键+同值的重复澄清：合并，不新增约束（分母不膨胀）。
        c.apply(ev("e2", EVENT_REVEAL, "color", "偏黄草绿",
                   source_quote="再次确认偏黄草绿"))
        self.assertEqual(len(c.constraints), n1)
        merged = c.constraints[-1]
        self.assertEqual(merged.source_events, ["e1", "e2"])
        # 不同含义不得误合并。
        c.apply(ev("e3", EVENT_REVEAL, "color", "正绿",
                   source_quote="其实是正绿"))
        self.assertEqual(len(c.constraints), n1 + 1)

    def test_modify_supersedes_and_keeps_timeline(self):
        c = TimelineCompiler("s", initial_constraints())
        c.apply(ev("e1", EVENT_MODIFY, "budget", {"upper": 200.0},
                   source_quote="预算改成200"))
        old = c.constraints[0]
        self.assertEqual(old.lifecycle_status, LIFECYCLE_SUPERSEDED)
        self.assertEqual(old.effective_until_event, "e1")
        new = [x for x in c.constraints if x.requirement_key == "budget"
               and x.lifecycle_status == LIFECYCLE_ACTIVE]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].supersedes, "c0001")
        self.assertEqual(new[0].source, SOURCE_SHOPPER_CLARIFICATION)

    def test_conditional_accept_keeps_global_constraint(self):
        # 单笔报价许可不得把全局预算约束标为 superseded。
        c = TimelineCompiler("s", initial_constraints())
        res = c.apply(ev("e1", EVENT_CONDITIONAL_ACCEPT, "budget", 168.0,
                         scope={"asin": "A1", "price": 168.0},
                         source_reply="r1"))
        self.assertTrue(res["applied"])
        budget = [x for x in c.constraints if x.requirement_key == "budget"]
        self.assertEqual(budget[0].lifecycle_status, LIFECYCLE_ACTIVE)
        self.assertEqual(len(c.permissions), 1)
        self.assertEqual(c.permissions[0]["scope"]["asin"], "A1")

    def test_conditional_accept_requires_full_scope(self):
        c = TimelineCompiler("s", initial_constraints())
        no_price = c.apply(ev("e1", EVENT_CONDITIONAL_ACCEPT, "budget", 168.0,
                              scope={"asin": "A1"}, source_reply="r1"))
        self.assertFalse(no_price["applied"])
        no_source = c.apply(ev("e2", EVENT_CONDITIONAL_ACCEPT, "budget", 168.0,
                               scope={"asin": "A1", "price": 168.0}))
        self.assertFalse(no_source["applied"])
        self.assertEqual(c.permissions, [])

    def test_reject_invalidates_matching_permission(self):
        c = TimelineCompiler("s", initial_constraints())
        c.apply(ev("e1", EVENT_CONDITIONAL_ACCEPT, "budget", 168.0,
                   scope={"asin": "A1", "price": 168.0}, source_reply="r1"))
        c.apply(ev("e2", EVENT_REJECT, "budget", None,
                   scope={"asin": "A1"}, source_quote="这个不要了"))
        self.assertEqual(c.permissions[0]["lifecycle_status"], "rejected")
        self.assertEqual(c.rejections[0]["event_id"], "e2")

    def test_revoke_replay_semantics(self):
        # modify 100→120→150：撤销第一次 modify 不得覆盖较新有效修改。
        base = [ConstraintV2(id="c0001", requirement_key="budget",
                             description="预算100", hardness="hard",
                             source=SOURCE_INITIAL_QUERY,
                             source_quote="100元", value={"upper": 100.0})]
        c = TimelineCompiler("s", base)
        c.apply(ev("e1", EVENT_MODIFY, "budget", {"upper": 120.0}))
        c.apply(ev("e2", EVENT_MODIFY, "budget", {"upper": 150.0}))
        self.assertEqual(c.effective_set()[0].value, {"upper": 150.0})
        c.apply(ev("e3", EVENT_REVOKE, "budget", None, references_event="e1"))
        self.assertEqual(c.effective_set()[0].value, {"upper": 150.0})
        c.apply(ev("e4", EVENT_REVOKE, "budget", None, references_event="e2"))
        # 两次都撤销 → 回到初始 100。
        self.assertEqual(c.effective_set()[0].value, {"upper": 100.0})
        self.assertEqual(c.constraints[0].lifecycle_status, LIFECYCLE_ACTIVE)

    def test_duplicate_idempotent_and_conflict(self):
        c = TimelineCompiler("s", initial_constraints())
        first = c.apply(ev("e1", EVENT_MODIFY, "budget", {"upper": 200.0}))
        self.assertTrue(first["applied"])
        dup = c.apply(ev("e1", EVENT_MODIFY, "budget", {"upper": 200.0}))
        self.assertFalse(dup["applied"])
        self.assertEqual(dup["reason"], "duplicate_event")
        conflict = c.apply(ev("e1", EVENT_MODIFY, "budget", {"upper": 999.0}))
        self.assertFalse(conflict["applied"])
        self.assertEqual(conflict["reason"], "event_id_conflict")
        active = [x for x in c.constraints if x.requirement_key == "budget"
                  and x.lifecycle_status == LIFECYCLE_ACTIVE]
        self.assertEqual(active[0].value, {"upper": 200.0})

    def test_expected_version_check(self):
        c = TimelineCompiler("s", initial_constraints())
        stale = c.apply(ev("e1", EVENT_MODIFY, "budget", {"upper": 200.0},
                           expected_version=5))
        self.assertFalse(stale["applied"])
        self.assertEqual(stale["reason"], "version_mismatch")
        fresh = c.apply(ev("e2", EVENT_MODIFY, "budget", {"upper": 200.0},
                           expected_version=0))
        self.assertTrue(fresh["applied"])

    def test_post_terminal_and_unmapped_events_not_applied(self):
        c = TimelineCompiler("s", initial_constraints())
        post = ev("e1", EVENT_REVEAL, "color", "红色")
        post.status = "post_terminal_excluded"
        res = c.apply(post)
        self.assertFalse(res["applied"])
        self.assertEqual(res["reason"], "post_terminal_excluded")
        unmapped = ev("e2", EVENT_REVEAL, "size", "L")
        unmapped.status = "evidence_unmapped"
        res2 = c.apply(unmapped)
        self.assertFalse(res2["applied"])
        self.assertEqual(res2["reason"], "evidence_unmapped")
        self.assertEqual(len(c.constraints), 2)

    def test_event_validation(self):
        bad = ev("", EVENT_REVEAL, "color", "红")
        self.assertTrue(any("event_id" in e for e in validate_event(bad)))
        bad_kind = ev("e1", "unknown", "color", "红")
        self.assertTrue(any("kind" in e for e in validate_event(bad_kind)))
        ca = ev("e1", EVENT_CONDITIONAL_ACCEPT, "budget", 168.0,
                scope={"asin": "A1"})
        self.assertTrue(any("金额" in e for e in validate_event(ca)))
        revoke = RequirementEventV2(event_id="e1", session="s",
                                    kind=EVENT_REVOKE, requirement_key="budget")
        self.assertTrue(any("references_event" in e
                            for e in validate_event(revoke)))

    def test_result_schema_validation(self):
        c = TimelineCompiler("s", initial_constraints())
        doc = c.result(benchmark_id="b", run_id="r", task_id="1",
                       trial_id="t0")
        # validate_rubric_v2 需要 schema_version 等字段。
        doc["schema_version"] = "shopping-rubric-schema-v2"
        errors = validate_rubric_v2(doc, expected_task_id="1")
        self.assertEqual(errors, [])
        doc2 = dict(doc)
        doc2["constraints"] = [dict(doc["constraints"][0], id="dup"),
                               dict(doc["constraints"][1], id="dup")]
        self.assertTrue(any("重复" in e for e in validate_rubric_v2(doc2)))

    def test_scope_matches_purchase_precision(self):
        scope = {"asin": "A1", "price": 168.0, "option_spec": "中药版100粒"}
        ok = {"asin": "A1", "price": 168.0,
              "options": {"规格": "港版中药版100粒"}}
        self.assertTrue(scope_matches_purchase(scope, ok))
        self.assertFalse(scope_matches_purchase(
            scope, {"asin": "A2", "price": 168.0,
                    "options": {"规格": "中药版100粒"}}))
        self.assertFalse(scope_matches_purchase(
            scope, {"asin": "A1", "price": 170.0,
                    "options": {"规格": "中药版100粒"}}))
        self.assertFalse(scope_matches_purchase(
            scope, {"asin": "A1", "price": 168.0, "options": {}}))
        self.assertFalse(scope_matches_purchase({}, ok))


if __name__ == "__main__":
    unittest.main()
